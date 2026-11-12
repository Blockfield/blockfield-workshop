package blockfield.workshop.ai;

import com.lunares.blockfield.workshop.WorkshopGeometry;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.math.BigDecimal;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.nio.file.Path;
import java.time.Clock;
import java.util.Base64;
import java.util.Map;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.Executors;
import java.util.logging.Logger;
import java.util.regex.Pattern;

/**
 * Workshop AI sidecar (server#321): health, auth, generate/jobs/transcribe and the idempotency
 * store per {@code docs/ai-sidecar-contract.md}. Owns the money ledger ({@link Budget}), the
 * provider clients ({@link ProviderHttp}), the tool loop/orchestration, prompts and STT ({@link
 * GenerateFlow} and friends); the stub backend completes jobs with an empty plan until provider
 * keys are configured. The mod calls over HTTP; the game JVM keeps jobs and apply.
 */
public final class AiSidecar {
    static final int MAX_BODY = 64 << 20;
    static final int DEFAULT_DEADLINE_SECONDS = 900;
    static final int MAX_DEADLINE_SECONDS = 1800;
    static final String STUB_WARNING = "stub backend: provider not wired (U-S3/U-W2)";
    static final String STUB_TEXT = "stub transcript (provider not wired)";
    static final Pattern PALETTE_ENTRY = Pattern.compile("[a-z0-9_.\\-]+:[a-z0-9_./\\-]+");

    private final String secret;
    private final Clock clock;
    private final IdempotencyStore idempotency;
    private final Map<String, Job> jobs = new ConcurrentHashMap<>();
    private static final Logger LOGGER = Logger.getLogger(AiSidecar.class.getName());

    /**
     * Null selects the stub backend (no outbound traffic): every job completes immediately with an
     * empty plan. Set when provider keys are configured; generation, transcription and cleanup each
     * have a peer, so the tool loop, STT and fill all bill to their own account.
     */
    private final ProviderHttp generation;

    private final ProviderHttp transcription;
    private final ProviderHttp cleanup;
    private final String generationProvider;
    private final String transcriptionProvider;
    private final String cleanupProvider;
    private final java.net.http.HttpClient http;
    private final Path rejectedPlans;

    /** Null disables the money ledger (dev default); set when a policy file is configured. */
    private final Budget budget;

    /** A completed plan with its creation time; jobs older than the idempotency window 404. */
    private record Job(JSONObject plan, long storedAt) {}

    AiSidecar(String secret, Clock clock) {
        this(secret, clock, null, null, Path.of("logs", "workshop-ai-rejected"));
    }

    AiSidecar(String secret, Clock clock, Selections selections, Budget budget) {
        this(secret, clock, selections, budget, Path.of("logs", "workshop-ai-rejected"));
    }

    AiSidecar(
            String secret, Clock clock, Selections selections, Budget budget, Path rejectedPlans) {
        this.secret = secret;
        this.clock = clock;
        this.idempotency = new IdempotencyStore(clock);
        this.http = ProviderHttp.defaultHttpClient();
        if (selections == null) {
            this.generation = null;
            this.transcription = null;
            this.cleanup = null;
            this.generationProvider = null;
            this.transcriptionProvider = null;
            this.cleanupProvider = null;
        } else {
            this.generation = peer(selections.generation());
            this.transcription = peer(selections.transcription());
            this.cleanup = peer(selections.cleanup());
            this.generationProvider = selections.generation().provider();
            this.transcriptionProvider = selections.transcription().provider();
            this.cleanupProvider = selections.cleanup().provider();
        }
        this.budget = budget;
        this.rejectedPlans = rejectedPlans;
    }

    private ProviderHttp peer(ProviderHttp.Selection selection) {
        return new ProviderHttp(
                new ProviderHttp.Backend(
                        selection.key(),
                        ProviderHttp.Transport.forProvider(
                                selection.provider(), selection.baseUrl())),
                http);
    }

    public static void main(String[] args) throws Exception {
        String bind = env("AI_SIDECAR_BIND", "127.0.0.1");
        int port = Integer.parseInt(env("AI_SIDECAR_PORT", "8101"));
        Path keyFile =
                Path.of(env("AI_SIDECAR_KEY_FILE", "/data/blockfield-workshop/ai-sidecar.key"));
        String secret = Auth.loadKey(keyFile);
        Selections selections = selectSelections(Map.copyOf(System.getenv()));
        Budget budget = openLedger();
        HttpServer server = start(bind, port, secret, Clock.systemUTC(), selections, budget);
        System.out.println("listening on " + bind + ":" + server.getAddress().getPort());
    }

    /**
     * Backend from env-referenced key files (no secret values in config): {@code
     * AI_SIDECAR_PROVIDER} is {@code stub} (default), {@code openrouter}, {@code vercel-gateway} or
     * {@code custom}; {@code AI_SIDECAR_PROVIDER_BASE_URL} overrides the default base URL; the key
     * comes from {@code AI_SIDECAR_PROVIDER_KEY_FILE}. Unknown provider or missing key file fails
     * startup, never the first request.
     */
    static ProviderHttp selectBackend() throws IOException {
        return selectBackend(Map.copyOf(System.getenv()));
    }

    static ProviderHttp selectBackend(Map<String, String> env) throws IOException {
        ProviderHttp.Selection selection = selectGeneration(env);
        if (selection == null) {
            return null;
        }
        return new ProviderHttp(
                new ProviderHttp.Backend(
                        selection.key(),
                        ProviderHttp.Transport.forProvider(
                                selection.provider(), selection.baseUrl())),
                ProviderHttp.defaultHttpClient());
    }

    /** Null when the generation provider is stub: the whole backend is stubbed. */
    static Selections selectSelections(Map<String, String> env) throws IOException {
        ProviderHttp.Selection generation = selectGeneration(env);
        if (generation == null) {
            return null;
        }
        ProviderHttp.Selection transcription = selectOne(env, "TRANSCRIPTION", generation);
        ProviderHttp.Selection cleanup = selectOne(env, "CLEANUP", transcription);
        return new Selections(generation, transcription, cleanup);
    }

    /**
     * Per-phase provider topology: ids and URLs the mod config must agree with, keys sidecar-only.
     */
    record Selections(
            ProviderHttp.Selection generation,
            ProviderHttp.Selection transcription,
            ProviderHttp.Selection cleanup) {}

    private static ProviderHttp.Selection selectGeneration(Map<String, String> env)
            throws IOException {
        String provider = env.getOrDefault("AI_SIDECAR_PROVIDER", "stub");
        if (provider.equals("stub")) {
            return null;
        }
        if (!ProviderHttp.validProvider(provider)) {
            throw new IOException("unknown AI_SIDECAR_PROVIDER: " + provider);
        }
        String keyFile = env.get("AI_SIDECAR_PROVIDER_KEY_FILE");
        if (keyFile == null || keyFile.isEmpty()) {
            throw new IOException("AI_SIDECAR_PROVIDER_KEY_FILE is required for " + provider);
        }
        String key = Auth.loadKey(Path.of(keyFile));
        String baseUrl = env.get("AI_SIDECAR_PROVIDER_BASE_URL");
        if (provider.equals(ProviderHttp.CUSTOM) && (baseUrl == null || baseUrl.isEmpty())) {
            // A custom key sent to the gateway host would mis-bill and disclose the
            // credential, so custom refuses startup without its own explicit base URL
            // instead of defaulting to GATEWAY_BASE.
            throw new IOException("AI_SIDECAR_PROVIDER_BASE_URL is required for custom");
        }
        if (baseUrl == null || baseUrl.isEmpty()) {
            baseUrl =
                    provider.equals(ProviderHttp.OPENROUTER)
                            ? ProviderHttp.OPENROUTER_BASE
                            : ProviderHttp.GATEWAY_BASE;
        }
        return new ProviderHttp.Selection(provider, baseUrl, key);
    }

    private static ProviderHttp.Selection selectOne(
            Map<String, String> env, String prefix, ProviderHttp.Selection fallback)
            throws IOException {
        String provider =
                env.getOrDefault("AI_SIDECAR_" + prefix + "_PROVIDER", fallback.provider());
        if (!ProviderHttp.validProvider(provider)) {
            throw new IOException("unknown AI_SIDECAR_" + prefix + "_PROVIDER: " + provider);
        }
        String keyFile = env.get("AI_SIDECAR_" + prefix + "_KEY_FILE");
        String key =
                keyFile == null || keyFile.isEmpty()
                        ? fallback.key()
                        : Auth.loadKey(Path.of(keyFile));
        String explicit = env.get("AI_SIDECAR_" + prefix + "_BASE_URL");
        if (provider.equals(ProviderHttp.CUSTOM) && (explicit == null || explicit.isEmpty())) {
            // A custom key sent to the fallback host would mis-bill and disclose the
            // credential, so custom refuses startup without its own explicit base URL
            // instead of inheriting the fallback host like the generation path refuses
            // to default to GATEWAY_BASE.
            throw new IOException("AI_SIDECAR_" + prefix + "_BASE_URL is required for custom");
        }
        String baseUrl = explicit;
        if (baseUrl == null || baseUrl.isEmpty()) {
            baseUrl = fallback.baseUrl();
        }
        if (baseUrl == null || baseUrl.isEmpty()) {
            baseUrl =
                    provider.equals(ProviderHttp.OPENROUTER)
                            ? ProviderHttp.OPENROUTER_BASE
                            : ProviderHttp.GATEWAY_BASE;
        }
        return new ProviderHttp.Selection(provider, baseUrl, key);
    }

    /**
     * Money ledger on a sidecar volume: {@code AI_SIDECAR_POLICY_FILE} enables it (the admin policy
     * the controller deploys); {@code AI_SIDECAR_LEDGER_FILE} defaults to {@code ai-usage.json}
     * next to the policy. Absent policy file means no ledger (dev default).
     */
    static Budget openLedger() throws IOException {
        return openLedger(Map.copyOf(System.getenv()));
    }

    static Budget openLedger(Map<String, String> env) throws IOException {
        String policy = env.get("AI_SIDECAR_POLICY_FILE");
        if (policy == null || policy.isEmpty()) {
            return null;
        }
        Path policyFile = Path.of(policy);
        String ledger = env.get("AI_SIDECAR_LEDGER_FILE");
        Path ledgerFile =
                ledger != null && !ledger.isEmpty()
                        ? Path.of(ledger)
                        : policyFile.resolveSibling("ai-usage.json");
        return Budget.open(policyFile, ledgerFile);
    }

    static String env(String name, String fallback) {
        String value = System.getenv(name);
        return value == null || value.isEmpty() ? fallback : value;
    }

    static HttpServer start(String bind, int port, String secret, Clock clock) throws IOException {
        return start(bind, port, secret, clock, null, null);
    }

    static HttpServer start(
            String bind, int port, String secret, Clock clock, Selections selections, Budget budget)
            throws IOException {
        AiSidecar app = new AiSidecar(secret, clock, selections, budget);
        if (selections == null) {
            LOGGER.info("ai-sidecar: stub backend (no outbound AI traffic)");
        } else if (budget == null) {
            LOGGER.warning(
                    "ai-sidecar: provider configured without a ledger; serving stub plans until"
                            + " AI_SIDECAR_POLICY_FILE is set");
        } else {
            LOGGER.info("ai-sidecar: provider backend with ledger");
        }
        HttpServer server = HttpServer.create(new InetSocketAddress(bind, port), 128);
        server.createContext("/health", app::onHealth);
        server.createContext("/v1/generate", app::onGenerate);
        server.createContext("/v1/transcribe", app::onTranscribe);
        server.createContext("/v1/jobs", app::onJob);
        var threads =
                Executors.newFixedThreadPool(
                        8,
                        runnable -> {
                            Thread thread = new Thread(runnable, "ai-sidecar");
                            thread.setDaemon(true);
                            return thread;
                        });
        server.setExecutor(threads);
        server.start();
        return server;
    }

    // Handlers.

    private void onHealth(HttpExchange exchange) throws IOException {
        try {
            requireMethod(exchange, "GET");
            send(exchange, 200, new JSONObject().put("ok", true));
        } catch (HttpError e) {
            send(exchange, e.status, e.envelope());
        }
    }

    private void onGenerate(HttpExchange exchange) throws IOException {
        String key = null;
        try {
            requireMethod(exchange, "POST");
            requireAuth(exchange);
            byte[] body = readBody(exchange);
            JSONObject request = parse(body);
            key = uuid(request, "requestId", true);
            UUID sessionId = UUID.fromString(uuid(request, "session", false));
            UUID builderId = UUID.fromString(uuid(request, "builder", false));
            String prompt = string(request, "prompt");
            if (prompt.isEmpty()) {
                throw new HttpError(400, "request", "invalid_request", "prompt must be non-empty");
            }
            String mode = string(request, "mode");
            if (!mode.equals("generate") && !mode.equals("fill")) {
                throw new HttpError(
                        400, "request", "invalid_request", "mode must be generate|fill");
            }
            if (mode.equals("fill") != request.has("fill")) {
                throw new HttpError(
                        400, "request", "invalid_request", "fill present iff mode is fill");
            }
            if (request.has("fill") && !(request.opt("fill") instanceof JSONObject)) {
                throw new HttpError(400, "request", "invalid_request", "fill must be an object");
            }
            dims(request);
            palette(request);
            if (!(request.opt("config") instanceof JSONObject)
                    || !(request.opt("snapshot") instanceof JSONObject)) {
                throw new HttpError(
                        400, "request", "invalid_request", "config/snapshot must be objects");
            }
            deadline(request);
            byte[] hash = IdempotencyStore.sha256(body);
            String idempotencyKey = "generate:" + key;
            purgeExpiredJobs(clock.millis());
            IdempotencyStore.Stored stored =
                    idempotency.use(
                            idempotencyKey,
                            hash,
                            () -> {
                                String jobId = UUID.randomUUID().toString();
                                JSONObject plan;
                                try {
                                    plan = runGenerate(request, sessionId, builderId);
                                } catch (RuntimeException e) {
                                    throw e;
                                } catch (Exception e) {
                                    throw new GenerateFailed(e);
                                }
                                jobs.put(jobId, new Job(plan, clock.millis()));
                                return json(200, new JSONObject().put("jobId", jobId));
                            });
            send(
                    exchange,
                    stored.status(),
                    new JSONObject(new String(stored.body(), StandardCharsets.UTF_8)));
        } catch (IdempotencyStore.ConflictException e) {
            send(
                    exchange,
                    409,
                    envelope("request", "conflict", 409, "requestId reused with a different body"));
        } catch (GenerateFailed e) {
            sendGenerateFailed(exchange, key, e.getCause());
        } catch (ProviderHttp.Failure e) {
            int status = failureStatus(e);
            send(exchange, status, envelope(e.phase, e.category, status, e.getMessage()));
        } catch (HttpError e) {
            send(exchange, e.status, e.envelope());
        } catch (Exception e) {
            if (key != null) {
                idempotency.forget(key); // internal is not final: a same-key retry re-executes.
            }
            send(exchange, 500, envelope("unknown", "internal", 500, "internal error"));
        }
    }

    /**
     * Runs inside the idempotency supplier, so a retried request never reserves twice: the first
     * execution stores the jobId and the replay returns it. Stub plans cost nothing (bound 0); real
     * generations run the moved tool loop/orchestration and settle the exact cost.
     */
    private JSONObject runGenerate(JSONObject request, UUID sessionId, UUID builderId)
            throws Exception {
        if (generation == null || budget == null) {
            // Stub backend (or unconfigured ledger): free empty plan, no reservation.
            Budget.Reservation reservation = null;
            if (budget != null) {
                reservation = ProviderHttp.reserve(budget, sessionId, builderId, 0);
            }
            try {
                return stubPlan();
            } finally {
                if (reservation != null) {
                    reservation.confirm(BigDecimal.ZERO);
                }
            }
        }
        SidecarConfig config = parseConfig(request);
        checkProvider("generation", config.generationProvider(), generationProvider);
        JSONObject dims = request.getJSONObject("dims");
        int sx = dims.getInt("x"), sy = dims.getInt("y"), sz = dims.getInt("z");
        Set<String> palette = paletteSet(request);
        int version = version(request);
        String contextText = contextText(request);
        WorkshopAiSandbox.Snapshot snapshot = snapshot(request, sx, sy, sz);
        int deadlineSeconds = request.optInt("deadlineSeconds", DEFAULT_DEADLINE_SECONDS);
        long outerDeadline = System.currentTimeMillis() + deadlineSeconds * 1000L;
        GenerateFlow flow =
                new GenerateFlow(
                        generation,
                        transcription,
                        cleanup,
                        rejectedPlans,
                        WorkshopAiPlanning::decide);
        String mode = request.getString("mode");
        WorkshopAiPlan plan;
        if (mode.equals("fill")) {
            checkProvider("transcription", config.transcriptionProvider(), transcriptionProvider);
            checkProvider("cleanup", config.cleanupProvider(), cleanupProvider);
            plan =
                    runExtend(
                            request,
                            flow,
                            config,
                            palette,
                            sx,
                            sy,
                            sz,
                            sessionId,
                            builderId,
                            outerDeadline);
        } else {
            plan =
                    flow.generate(
                            request.getString("prompt"),
                            contextText,
                            config,
                            palette,
                            sx,
                            sy,
                            sz,
                            budget,
                            sessionId,
                            builderId,
                            version,
                            progress -> {},
                            outerDeadline,
                            snapshot);
        }
        return planJson(plan);
    }

    private WorkshopAiPlan runExtend(
            JSONObject request,
            GenerateFlow flow,
            SidecarConfig config,
            Set<String> palette,
            int sx,
            int sy,
            int sz,
            UUID sessionId,
            UUID builderId,
            long outerDeadline)
            throws Exception {
        JSONObject fill = request.getJSONObject("fill");
        String extendRequest = fillString(fill, "request", WorkshopAiFill.MAX_REQUEST_CHARS);
        double destructive = fillNumber(fill, "destructive");
        if (destructive != 0 && destructive != 1) {
            throw new HttpError(
                    400, "request", "invalid_request", "fill.destructive must be 0 or 1");
        }
        if (!fill.has("option") || !(fill.opt("option") instanceof JSONObject)) {
            throw new HttpError(400, "request", "invalid_request", "fill.option must be an object");
        }
        WorkshopAiFill.Option option =
                WorkshopAiFill.parseOption(
                        com.google.gson.JsonParser.parseString(
                                        fill.getJSONObject("option").toString())
                                .getAsJsonObject(),
                        sy);
        if (!fill.has("capture") || !(fill.opt("capture") instanceof JSONObject)) {
            throw new HttpError(
                    400, "request", "invalid_request", "fill.capture must be an object");
        }
        WorkshopAiFill.Capture capture;
        try {
            capture =
                    WorkshopAiFill.parseCapture(
                            com.google.gson.JsonParser.parseString(
                                            fill.getJSONObject("capture").toString())
                                    .getAsJsonObject());
        } catch (IllegalArgumentException invalid) {
            throw new HttpError(
                    400, "request", "invalid_request", "fill.capture: " + invalid.getMessage());
        }
        if (capture.sx != sx || capture.sy != sy || capture.sz != sz) {
            throw new HttpError(
                    400, "request", "invalid_request", "fill.capture dims must match dims");
        }
        Set<String> allowed = new java.util.HashSet<>(palette);
        allowed.addAll(WorkshopAiFill.sourceIds(capture, option));
        String hint = request.getString("prompt");
        WorkshopAiSandbox.Snapshot snapshot =
                WorkshopAiSandbox.snapshot(sx, sy, sz, capture.columns(), Map.of(), Set.of());
        return flow.extend(
                extendRequest,
                hint,
                config,
                allowed,
                sx,
                sy,
                sz,
                destructive,
                budget,
                sessionId,
                builderId,
                progress -> {},
                outerDeadline,
                snapshot,
                plan -> WorkshopAiFill.seamReport(capture, option, plan));
    }

    private SidecarConfig parseConfig(JSONObject request) {
        if (!(request.opt("config") instanceof JSONObject)) {
            throw new HttpError(400, "request", "invalid_request", "config must be an object");
        }
        try {
            return SidecarConfig.parse(
                    com.google.gson.JsonParser.parseString(
                                    request.getJSONObject("config").toString())
                            .getAsJsonObject());
        } catch (IllegalArgumentException invalid) {
            throw new HttpError(
                    400, "request", "invalid_request", "config: " + invalid.getMessage());
        }
    }

    /** The deployment owns provider topology; the mod config must agree, never override it. */
    private static void checkProvider(String phase, String requestId, String selected) {
        if (!requestId.equals(selected)) {
            throw new HttpError(
                    400,
                    "request",
                    "invalid_request",
                    phase + " provider " + requestId + " is not configured on this sidecar");
        }
    }

    private static int version(JSONObject request) {
        int version = request.optInt("version", -1);
        if (version < 1 || version > 3) {
            throw new HttpError(400, "request", "invalid_request", "version must be 1..3");
        }
        return version;
    }

    private static String contextText(JSONObject request) {
        if (!request.has("contextText")) {
            return "";
        }
        if (!(request.opt("contextText") instanceof String)) {
            throw new HttpError(400, "request", "invalid_request", "contextText must be a string");
        }
        String text = request.getString("contextText");
        if (text.length() > 8192) {
            throw new HttpError(
                    400, "request", "invalid_request", "contextText must hold at most 8192 chars");
        }
        return text;
    }

    private static Set<String> paletteSet(JSONObject request) {
        JSONArray palette = request.getJSONArray("palette");
        Set<String> ids = new java.util.HashSet<>();
        for (int i = 0; i < palette.length(); i++) {
            ids.add(palette.getString(i));
        }
        return ids;
    }

    private static WorkshopAiSandbox.Snapshot snapshot(JSONObject request, int sx, int sy, int sz) {
        if (!request.has("snapshot")) {
            return WorkshopAiSandbox.Snapshot.EMPTY;
        }
        if (!(request.opt("snapshot") instanceof JSONObject)) {
            throw new HttpError(400, "request", "invalid_request", "snapshot must be an object");
        }
        JSONObject snap = request.getJSONObject("snapshot");
        if (snap.length() == 0) {
            return WorkshopAiSandbox.Snapshot.EMPTY;
        }
        try {
            return WorkshopAiSandbox.Snapshot.fromWire(
                    wireDims(snap, "sx", sx),
                    wireDims(snap, "sy", sy),
                    wireDims(snap, "sz", sz),
                    wireCells(snap, "blocks"),
                    wireCells(snap, "surface"),
                    wireGuarded(snap));
        } catch (IllegalArgumentException invalid) {
            throw new HttpError(
                    400, "request", "invalid_request", "snapshot: " + invalid.getMessage());
        }
    }

    private static int wireDims(JSONObject snap, String field, int expected) {
        int value = snap.optInt(field, -1);
        if (value != expected) {
            throw new IllegalArgumentException(field + " must match dims");
        }
        return value;
    }

    private static Map<Integer, String> wireCells(JSONObject snap, String field) {
        if (!snap.has(field)) {
            return Map.of();
        }
        if (!(snap.opt(field) instanceof JSONObject)) {
            throw new IllegalArgumentException(field + " must be an object");
        }
        JSONObject cells = snap.getJSONObject(field);
        Map<Integer, String> out = new java.util.HashMap<>();
        for (String key : cells.keySet()) {
            final int index;
            try {
                index = Integer.parseUnsignedInt(key);
            } catch (NumberFormatException invalid) {
                throw new IllegalArgumentException(field + " keys must be cell indexes");
            }
            if (!(cells.opt(key) instanceof String) || cells.getString(key).length() > 256) {
                throw new IllegalArgumentException(field + " values must be block texts");
            }
            out.put(index, cells.getString(key));
        }
        return out;
    }

    private static Set<Integer> wireGuarded(JSONObject snap) {
        if (!snap.has("guarded")) {
            return Set.of();
        }
        if (!(snap.opt("guarded") instanceof JSONArray)) {
            throw new IllegalArgumentException("guarded must be an array");
        }
        JSONArray guarded = snap.getJSONArray("guarded");
        Set<Integer> out = new java.util.HashSet<>();
        for (int i = 0; i < guarded.length(); i++) {
            if (!(guarded.opt(i) instanceof Integer)) {
                throw new IllegalArgumentException("guarded entries must be cell indexes");
            }
            out.add(guarded.getInt(i));
        }
        return out;
    }

    private static String fillString(JSONObject fill, String field, int max) {
        if (!(fill.opt(field) instanceof String)
                || fill.getString(field).isEmpty()
                || fill.getString(field).length() > max) {
            throw new HttpError(
                    400, "request", "invalid_request", "fill." + field + " outside bounds");
        }
        return fill.getString(field);
    }

    private static double fillNumber(JSONObject fill, String field) {
        if (!(fill.opt(field) instanceof Number)) {
            throw new HttpError(
                    400, "request", "invalid_request", "fill." + field + " must be a number");
        }
        return fill.getDouble(field);
    }

    /** Moved-plan to the contract plan shape; the mod re-validates it before apply. */
    private static JSONObject planJson(WorkshopAiPlan plan) {
        JSONArray cells = new JSONArray();
        for (WorkshopAiPlan.Cell cell : plan.cells()) {
            JSONArray writes = new JSONArray();
            for (WorkshopGeometry.Write write : cell.writes()) {
                writes.put(
                        new JSONObject().put("block", write.block()).put("match", write.match()));
            }
            cells.put(
                    new JSONObject()
                            .put("x", cell.x())
                            .put("y", cell.y())
                            .put("z", cell.z())
                            .put("block", cell.block())
                            .put("match", cell.match())
                            .put("writes", writes));
        }
        JSONArray warnings = new JSONArray();
        for (String warning : plan.warnings()) {
            warnings.put(warning);
        }
        return new JSONObject()
                .put("cells", cells)
                .put("canonical", plan.canonical())
                .put("demolishVolume", plan.demolishVolume())
                .put("warnings", warnings)
                .put("destructiveScore", plan.destructiveScore());
    }

    private void onTranscribe(HttpExchange exchange) throws IOException {
        String key = null;
        try {
            requireMethod(exchange, "POST");
            requireAuth(exchange);
            byte[] body = readBody(exchange);
            JSONObject request = parse(body);
            key = uuid(request, "requestId", true);
            UUID sessionId = UUID.fromString(uuid(request, "session", false));
            UUID builderId = UUID.fromString(uuid(request, "builder", false));
            String wavBase64 = string(request, "wav");
            if (wavBase64.isEmpty()) {
                throw new HttpError(
                        400, "request", "invalid_request", "wav must be non-empty base64");
            }
            try {
                Base64.getDecoder().decode(wavBase64);
            } catch (IllegalArgumentException e) {
                throw new HttpError(400, "request", "invalid_request", "wav must be base64");
            }
            if (string(request, "model").isEmpty()) {
                throw new HttpError(400, "request", "invalid_request", "model must be non-empty");
            }
            byte[] hash = IdempotencyStore.sha256(body);
            String idempotencyKey = "transcribe:" + key;
            IdempotencyStore.Stored stored =
                    idempotency.use(
                            idempotencyKey,
                            hash,
                            () -> {
                                // STT runs the moved transcription flow; the stub transcript
                                // costs nothing but still flows through the ledger when enabled,
                                // so replays stay free.
                                if (generation == null || budget == null) {
                                    Budget.Reservation reservation = null;
                                    try {
                                        if (budget != null) {
                                            reservation =
                                                    ProviderHttp.reserve(
                                                            budget, sessionId, builderId, 0);
                                        }
                                        return json(200, new JSONObject().put("text", STUB_TEXT));
                                    } catch (RuntimeException e) {
                                        throw e;
                                    } catch (Exception e) {
                                        throw new GenerateFailed(e);
                                    } finally {
                                        if (reservation != null) {
                                            try {
                                                reservation.confirm(BigDecimal.ZERO);
                                            } catch (IOException confirmed) {
                                                throw new GenerateFailed(confirmed);
                                            }
                                        }
                                    }
                                }
                                SidecarConfig config = parseConfig(request);
                                checkProvider(
                                        "generation",
                                        config.generationProvider(),
                                        generationProvider);
                                checkProvider(
                                        "transcription",
                                        config.transcriptionProvider(),
                                        transcriptionProvider);
                                checkProvider("cleanup", config.cleanupProvider(), cleanupProvider);
                                String model = string(request, "model");
                                String cleanupModel = optionalString(request, "cleanupModel", 128);
                                String language = optionalString(request, "language", 16);
                                byte[] wav;
                                try {
                                    wav = Base64.getDecoder().decode(wavBase64);
                                } catch (IllegalArgumentException e) {
                                    throw new HttpError(
                                            400,
                                            "request",
                                            "invalid_request",
                                            "wav must be base64");
                                }
                                // Validation failures are the caller's fault (400), never an
                                // internal error: reject them before the idempotent section
                                // so they reserve nothing and stay retryable.
                                if (!GenerateFlow.voiceModel(model)) {
                                    throw new HttpError(
                                            400,
                                            "request",
                                            "invalid_request",
                                            "model outside bounds");
                                }
                                try {
                                    WorkshopAiAudio.validate(wav);
                                } catch (IllegalArgumentException invalid) {
                                    throw new HttpError(
                                            400,
                                            "request",
                                            "invalid_request",
                                            "wav must be 16kHz mono PCM16");
                                }
                                GenerateFlow flow =
                                        new GenerateFlow(
                                                generation,
                                                transcription,
                                                cleanup,
                                                rejectedPlans,
                                                WorkshopAiPlanning::decide);
                                try {
                                    String text =
                                            flow.transcribe(
                                                    wav,
                                                    model,
                                                    cleanupModel,
                                                    language,
                                                    config,
                                                    budget,
                                                    sessionId,
                                                    builderId);
                                    return json(200, new JSONObject().put("text", text));
                                } catch (RuntimeException e) {
                                    throw e;
                                } catch (Exception e) {
                                    throw new GenerateFailed(e);
                                }
                            });
            send(
                    exchange,
                    stored.status(),
                    new JSONObject(new String(stored.body(), StandardCharsets.UTF_8)));
        } catch (IdempotencyStore.ConflictException e) {
            send(
                    exchange,
                    409,
                    envelope("request", "conflict", 409, "requestId reused with a different body"));
        } catch (GenerateFailed e) {
            sendGenerateFailed(exchange, key, e.getCause());
        } catch (HttpError e) {
            send(exchange, e.status, e.envelope());
        } catch (Exception e) {
            if (key != null) {
                idempotency.forget(key);
            }
            send(exchange, 500, envelope("unknown", "internal", 500, "internal error"));
        }
    }

    private void onJob(HttpExchange exchange) throws IOException {
        try {
            requireMethod(exchange, "GET");
            requireAuth(exchange);
            String id = exchange.getRequestURI().getPath().substring("/v1/jobs".length());
            if (id.startsWith("/")) {
                id = id.substring(1);
            }
            purgeExpiredJobs(clock.millis());
            Job job = id.isEmpty() || id.contains("/") ? null : jobs.get(id);
            if (job == null) {
                throw new HttpError(404, "request", "http", "unknown job");
            }
            send(exchange, 200, new JSONObject().put("phase", "done").put("plan", job.plan()));
        } catch (HttpError e) {
            send(exchange, e.status, e.envelope());
        }
    }

    /** Jobs share the idempotency retention window; expired entries never accumulate. */
    private void purgeExpiredJobs(long now) {
        jobs.entrySet()
                .removeIf(
                        entry ->
                                now - entry.getValue().storedAt() > IdempotencyStore.RETAIN_MILLIS);
    }

    // Validation.

    private void requireMethod(HttpExchange exchange, String method) {
        if (!exchange.getRequestMethod().equalsIgnoreCase(method)) {
            throw new HttpError(405, "request", "http", "method not allowed");
        }
    }

    private void requireAuth(HttpExchange exchange) {
        if (!Auth.authorized(exchange.getRequestHeaders().getFirst("Authorization"), secret)) {
            throw new HttpError(401, "auth", "http", "missing or invalid bearer secret");
        }
    }

    private static JSONObject parse(byte[] body) {
        try {
            Object parsed = new JSONObject(new String(body, StandardCharsets.UTF_8));
            if (!(parsed instanceof JSONObject)) {
                throw new HttpError(
                        400, "request", "invalid_request", "body must be a JSON object");
            }
            return (JSONObject) parsed;
        } catch (JSONException e) {
            throw new HttpError(400, "request", "invalid_request", "body must be JSON");
        }
    }

    private static String string(JSONObject request, String field) {
        if (!(request.opt(field) instanceof String)) {
            throw new HttpError(400, "request", "invalid_request", field + " must be a string");
        }
        return request.getString(field);
    }

    private static String optionalString(JSONObject request, String field, int max) {
        if (!request.has(field)) {
            return "";
        }
        if (!(request.opt(field) instanceof String) || request.getString(field).length() > max) {
            throw new HttpError(
                    400, "request", "invalid_request", field + " must be a short string");
        }
        return request.getString(field);
    }

    private static String uuid(JSONObject request, String field, boolean v4) {
        String value = string(request, field);
        try {
            UUID parsed = UUID.fromString(value);
            if (v4 && parsed.version() != 4) {
                throw new HttpError(
                        400, "request", "invalid_request", field + " must be a UUID v4");
            }
            return value;
        } catch (IllegalArgumentException e) {
            throw new HttpError(400, "request", "invalid_request", field + " must be a UUID");
        }
    }

    private static void dims(JSONObject request) {
        if (!(request.opt("dims") instanceof JSONObject)) {
            throw new HttpError(400, "request", "invalid_request", "dims must be an object");
        }
        JSONObject dims = request.getJSONObject("dims");
        for (String axis : new String[] {"x", "y", "z"}) {
            int value = dims.optInt(axis, -1);
            if (value < 1 || value > 127) {
                throw new HttpError(
                        400, "request", "invalid_request", "dims." + axis + " must be 1..127");
            }
        }
    }

    private static void palette(JSONObject request) {
        if (!(request.opt("palette") instanceof JSONArray)) {
            throw new HttpError(400, "request", "invalid_request", "palette must be an array");
        }
        JSONArray palette = request.getJSONArray("palette");
        if (palette.length() < 1 || palette.length() > 192) {
            throw new HttpError(
                    400, "request", "invalid_request", "palette must hold 1..192 entries");
        }
        for (int i = 0; i < palette.length(); i++) {
            if (!(palette.opt(i) instanceof String)
                    || !PALETTE_ENTRY.matcher(palette.getString(i)).matches()) {
                throw new HttpError(
                        400, "request", "invalid_request", "palette entry #" + i + " is invalid");
            }
        }
    }

    private static void deadline(JSONObject request) {
        int deadline = request.optInt("deadlineSeconds", DEFAULT_DEADLINE_SECONDS);
        if (deadline < 1 || deadline > MAX_DEADLINE_SECONDS) {
            throw new HttpError(
                    400, "request", "invalid_request", "deadlineSeconds must be 1..1800");
        }
    }

    /**
     * Unchecked carrier for checked failures escaping the idempotency suppliers (the suppliers
     * cannot throw checked exceptions, and the ledger/billing mapping must survive the trip).
     */
    static final class GenerateFailed extends RuntimeException {
        GenerateFailed(Throwable cause) {
            super(cause);
        }
    }

    /** Maps an unwrapped supplier failure; ledgers stay closed, internals stay opaque. */
    private void sendGenerateFailed(HttpExchange exchange, String key, Throwable cause)
            throws IOException {
        if (cause instanceof Budget.BudgetExhausted) {
            send(exchange, 402, envelope("budget", "exhausted", 402, "AI budget exhausted"));
        } else if (cause instanceof Budget.BudgetBlocked) {
            send(exchange, 403, envelope("budget", "blocked", 403, "AI budget blocked"));
        } else {
            if (cause instanceof InterruptedException) {
                Thread.currentThread().interrupt();
            }
            if (key != null) {
                idempotency.forget(key); // internal is not final: a same-key retry re-executes.
            }
            send(exchange, 500, envelope("unknown", "internal", 500, "internal error"));
        }
    }

    private static JSONObject stubPlan() {
        return new JSONObject()
                .put("cells", new JSONArray())
                .put("canonical", "stub")
                .put("demolishVolume", 0)
                .put("destructiveScore", 0)
                .put("warnings", new JSONArray().put(STUB_WARNING));
    }

    /** Contract error mapping: upstream statuses pass through, sidecar faults stay 5xx. */
    private static int failureStatus(ProviderHttp.Failure failure) {
        return switch (failure.category) {
            case "http" -> failure.status;
            case "timeout" -> 504;
            case "budget_unavailable", "budget_ledger_unavailable" -> 503;
            case "transport",
                    "invalid_response",
                    "invalid_json",
                    "invalid_plan",
                    "invalid_merge",
                    "invalid_symmetry",
                    "invalid_billing_schema",
                    "billing_cost_outside_bounds",
                    "billing_bound_exceeded",
                    "unsupported_generation_tariff" ->
                    502;
            default -> 500;
        };
    }

    // HTTP plumbing.

    private static final class HttpError extends RuntimeException {
        final int status;
        final String phase;
        final String category;

        HttpError(int status, String phase, String category, String message) {
            super(message);
            this.status = status;
            this.phase = phase;
            this.category = category;
        }

        JSONObject envelope() {
            return AiSidecar.envelope(phase, category, status, getMessage());
        }
    }

    static JSONObject envelope(String phase, String category, int http, String message) {
        JSONObject body = new JSONObject().put("phase", phase).put("category", category);
        if (http >= 0) {
            body.put("http", http);
        }
        if (message != null) {
            body.put("message", message);
        }
        return body;
    }

    private static IdempotencyStore.Stored json(int status, JSONObject body) {
        return new IdempotencyStore.Stored(
                status, body.toString().getBytes(StandardCharsets.UTF_8));
    }

    static byte[] readBody(HttpExchange exchange) {
        try (InputStream in = exchange.getRequestBody();
                ByteArrayOutputStream out = new ByteArrayOutputStream()) {
            byte[] buffer = new byte[65536];
            int total = 0;
            int read;
            while ((read = in.read(buffer)) != -1) {
                total += read;
                if (total > MAX_BODY) {
                    throw new HttpError(413, "request", "http", "body exceeds 64 MiB");
                }
                out.write(buffer, 0, read);
            }
            return out.toByteArray();
        } catch (IOException e) {
            throw new HttpError(400, "request", "invalid_request", "cannot read body");
        }
    }

    static void send(HttpExchange exchange, int status, JSONObject body) throws IOException {
        byte[] bytes = body.toString().getBytes(StandardCharsets.UTF_8);
        exchange.getResponseHeaders().set("Content-Type", "application/json");
        exchange.sendResponseHeaders(status, bytes.length);
        try (OutputStream out = exchange.getResponseBody()) {
            out.write(bytes);
        }
    }
}
