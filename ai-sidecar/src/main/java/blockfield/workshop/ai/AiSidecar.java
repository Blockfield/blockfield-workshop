package blockfield.workshop.ai;

import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.nio.file.Path;
import java.time.Clock;
import java.util.Base64;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.Executors;
import java.util.regex.Pattern;

/**
 * Workshop AI sidecar skeleton (server#321 U-W1): health, auth, stub generate/jobs/transcribe and
 * the idempotency store per {@code docs/ai-sidecar-contract.md}. No provider code moves in this
 * unit; the stub backend completes every job immediately with an empty plan.
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

    /** A completed stub plan with its creation time; jobs older than the idempotency window 404. */
    private record Job(JSONObject plan, long storedAt) {}

    AiSidecar(String secret, Clock clock) {
        this.secret = secret;
        this.clock = clock;
        this.idempotency = new IdempotencyStore(clock);
    }

    public static void main(String[] args) throws Exception {
        String bind = env("AI_SIDECAR_BIND", "127.0.0.1");
        int port = Integer.parseInt(env("AI_SIDECAR_PORT", "8101"));
        Path keyFile =
                Path.of(env("AI_SIDECAR_KEY_FILE", "/data/blockfield-workshop/ai-sidecar.key"));
        String secret = Auth.loadKey(keyFile);
        HttpServer server = start(bind, port, secret, Clock.systemUTC());
        System.out.println("ai-sidecar listening on " + bind + ":" + server.getAddress().getPort());
    }

    static String env(String name, String fallback) {
        String value = System.getenv(name);
        return value == null || value.isEmpty() ? fallback : value;
    }

    static HttpServer start(String bind, int port, String secret, Clock clock) throws IOException {
        AiSidecar app = new AiSidecar(secret, clock);
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
            uuid(request, "session", false);
            uuid(request, "builder", false);
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
                                jobs.put(jobId, new Job(stubPlan(), clock.millis()));
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
        } catch (HttpError e) {
            send(exchange, e.status, e.envelope());
        } catch (Exception e) {
            if (key != null) {
                idempotency.forget(key); // internal is not final: a same-key retry re-executes.
            }
            send(exchange, 500, envelope("unknown", "internal", 500, "internal error"));
        }
    }

    private void onTranscribe(HttpExchange exchange) throws IOException {
        String key = null;
        try {
            requireMethod(exchange, "POST");
            requireAuth(exchange);
            byte[] body = readBody(exchange);
            JSONObject request = parse(body);
            key = uuid(request, "requestId", true);
            uuid(request, "session", false);
            uuid(request, "builder", false);
            String wav = string(request, "wav");
            if (wav.isEmpty()) {
                throw new HttpError(
                        400, "request", "invalid_request", "wav must be non-empty base64");
            }
            try {
                Base64.getDecoder().decode(wav);
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
                            () -> json(200, new JSONObject().put("text", STUB_TEXT)));
            send(
                    exchange,
                    stored.status(),
                    new JSONObject(new String(stored.body(), StandardCharsets.UTF_8)));
        } catch (IdempotencyStore.ConflictException e) {
            send(
                    exchange,
                    409,
                    envelope("request", "conflict", 409, "requestId reused with a different body"));
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

    private static JSONObject stubPlan() {
        return new JSONObject()
                .put("cells", new JSONArray())
                .put("canonical", "stub")
                .put("demolishVolume", 0)
                .put("warnings", new JSONArray().put(STUB_WARNING));
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
