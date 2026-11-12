package blockfield.workshop.ai;

import com.google.gson.JsonArray;
import com.google.gson.JsonElement;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.math.BigDecimal;
import java.math.RoundingMode;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.net.http.HttpTimeoutException;
import java.nio.ByteBuffer;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.List;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CompletionStage;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.Flow;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.TimeoutException;
import java.util.logging.Logger;

/**
 * Provider clients moved out of the game JVM (coord#20 U-AI1): the outbound AI HTTP choke point
 * plus the per-call money accounting. Ported from {@code WorkshopAiProvider}; the tool loop,
 * orchestration prompts, STT transcription flow and the strip stay for U-AI2, which also owns the
 * mod call-site cutover. Until then the game JVM keeps its in-JVM provider and this client serves
 * single-shot generations (plus the stub used by the round-trip test and unconfigured stands).
 *
 * <p>No secret values here: keys arrive as {@link Backend} records built from key files the
 * deployment mounts sidecar-only; upstream response bodies and exception text never reach logs.
 */
final class ProviderHttp {
    private static final Logger LOGGER = Logger.getLogger(ProviderHttp.class.getName());

    static final String OPENROUTER_BASE = "https://openrouter.ai";
    static final String GATEWAY_BASE = "https://ai-gateway.vercel.sh/v1";
    static final String APP_URL = "https://blockfield.pro";
    static final String APP_TITLE = "Blockfield Ai Workshop";

    static final String OPENROUTER = "openrouter";
    static final String VERCEL_GATEWAY = "vercel-gateway";
    static final String CUSTOM = "custom";
    private static final Set<String> PROVIDERS = Set.of(OPENROUTER, VERCEL_GATEWAY, CUSTOM);

    private static final int MAX_RESPONSE = 131072;
    // xhigh bodies carry the reasoning text at ~10 bytes per output token: the calibrated
    // highrise answered 187 kB inside a 64k token cap, so generation reads up to 1 MB.
    private static final int MAX_GENERATION_RESPONSE = 1048576;
    static final int MAX_TRANSCRIPT = 4096;
    private static final BigDecimal MAX_USAGE_TOKENS = BigDecimal.valueOf(2_097_152);
    // Right-sized input reservations: every generation-family call reserves input for its
    // serialized request, not the full 1M-token context. ASCII bytes count 1:1 (a BPE token
    // decodes to at least one byte) and other characters 4 each (at most 4 UTF-8 bytes per
    // character), so the count upper-bounds billed input tokens; the overhead covers chat
    // framing/role tokens plus tokenizer slack.
    static final int INPUT_TEMPLATE_OVERHEAD_TOKENS = 2048;
    static final String DEFAULT_GENERATION_MODEL = "meta/muse-spark-1.3-contributor";
    // Same caps as the generation routing max_price ($0.10 / $0.20 per million tokens) and
    // generationBound.
    private static final BigDecimal GENERATION_PROMPT_USD_PER_TOKEN = new BigDecimal("0.0000001");
    private static final BigDecimal GENERATION_COMPLETION_USD_PER_TOKEN =
            new BigDecimal("0.0000002");
    // Contract deadline hard max (mirrors the mod Speed.MAX_SECONDS).
    static final int MAX_DEADLINE_SECONDS = 1800;
    // Loop-turn token cap, carried over for usage-receipt validation (the loop itself is U-AI2).
    private static final int LOOP_TURN_TOKENS = 8000;
    // Single output tier: reasoning shares max_tokens, so every size gets the full limit.
    static final int SINGLE_TOKENS = 64000;

    /**
     * Upstream transport. OpenRouter serves OpenRouter paths with an endpoint catalog; the Vercel
     * AI Gateway and custom entries speak plain OpenAI paths without one, so the tariff probe and
     * the routing object are OpenRouter-only.
     */
    record Transport(String baseUrl, String chatPath, String audioPath, boolean priceApi) {
        static Transport openrouter(String baseUrl) {
            return new Transport(
                    baseUrl, "/api/v1/chat/completions", "/api/v1/audio/transcriptions", true);
        }

        static Transport gateway(String baseUrl) {
            return openAi(baseUrl);
        }

        static Transport custom(String baseUrl) {
            return openAi(baseUrl);
        }

        private static Transport openAi(String baseUrl) {
            return new Transport(baseUrl, "/chat/completions", "/audio/transcriptions", false);
        }

        static Transport forProvider(String id, String baseUrl) {
            if (OPENROUTER.equals(id)) return openrouter(baseUrl);
            return openAi(baseUrl);
        }
    }

    /** One provider account: its key plus the transport its API shape needs. */
    record Backend(String key, Transport transport) {}

    /** Per-model accounts chosen from the provider registry (cleanup rides transcription). */
    record Backends(Backend generation, Backend transcription, Backend cleanup) {
        static Backends shared(String key, Transport transport) {
            var backend = new Backend(key, transport);
            return new Backends(backend, backend, backend);
        }
    }

    /** Provider id + base URL the sidecar serves; null selects the stub (no outbound traffic). */
    record Selection(String provider, String baseUrl, String key) {}

    private final Backend backend;
    private final HttpClient http;

    ProviderHttp(Backend backend, HttpClient http) {
        this.backend = backend;
        this.http = http;
    }

    /** The account key, only for redacting rejected-plan debug files. */
    String key() {
        return backend.key();
    }

    static HttpClient defaultHttpClient() {
        return HttpClient.newBuilder()
                .connectTimeout(Duration.ofSeconds(5))
                .followRedirects(HttpClient.Redirect.NEVER)
                .build();
    }

    static Backend backendFor(Backends backends, String phase) {
        return switch (phase) {
            case "transcription" -> backends.transcription();
            case "cleanup" -> backends.cleanup();
            default -> backends.generation();
        };
    }

    // Reserve math (up-front envelopes and send-time slots; every reserve follows owner caps).

    static int generationTokens(int maxBlocks, int sideX, int sideY, int sideZ) {
        if (maxBlocks < 1
                || maxBlocks > 32768
                || sideX < 1
                || sideX > 128
                || sideY < 1
                || sideY > 128
                || sideZ < 1
                || sideZ > 128)
            throw new IllegalArgumentException("Generation bounds outside limits");
        return SINGLE_TOKENS;
    }

    static long generationBound(int tokens) {
        if (tokens != SINGLE_TOKENS) throw new IllegalArgumentException("Unknown token cap");
        // Reserve the full input context and selected output cap; reasoning shares max_tokens.
        return (1_048_576L * 10 + tokens * 20L + 99) / 100;
    }

    static long jobBound(int tokens) {
        return generationBound(tokens);
    }

    static long callBound(int tokens) {
        if (tokens < 1024 || tokens > SINGLE_TOKENS)
            throw new IllegalArgumentException("Call token cap");
        return (1_048_576L * 10 + tokens * 20L + 99) / 100;
    }

    static int requestBytes(JsonObject request) {
        return request.toString().getBytes(StandardCharsets.UTF_8).length;
    }

    static int inputTokensFor(String serialized) {
        int tokens = 0;
        for (int i = 0; i < serialized.length(); i++) tokens += serialized.charAt(i) < 128 ? 1 : 4;
        return tokens;
    }

    static long inputBoundFor(int requestBytes) {
        if (requestBytes < 0) throw new IllegalArgumentException("Request size outside limits");
        return (((long) requestBytes + INPUT_TEMPLATE_OVERHEAD_TOKENS) * 10 + 99) / 100;
    }

    static long inputBoundFor(String serialized) {
        return inputBoundFor(inputTokensFor(serialized));
    }

    /**
     * Context-capture reserve, carried over with the reserve math (the mod still captures the
     * context; the sidecar only sizes the envelope). Monetary ceilings round up: 2048 estimated
     * tokens cost 204.8 microUSD.
     */
    static long contextBound(int contextTokens) {
        if (contextTokens < 0 || contextTokens > 2048)
            throw new IllegalArgumentException("Context tokens outside limits");
        return (contextTokens * 10L + 99) / 100;
    }

    static int requestTokens(JsonObject request) {
        return inputTokensFor(request.toString());
    }

    static long callBoundFor(int inputTokens, int maxTokens) {
        return callBoundFor(inputTokens, maxTokens, 0.10, 0.20);
    }

    static long callBoundFor(
            int inputTokens, int maxTokens, double promptCapPerM, double completionCapPerM) {
        if (maxTokens < 1024 || maxTokens > SINGLE_TOKENS)
            throw new IllegalArgumentException("Call token cap");
        if (inputTokens < 0) throw new IllegalArgumentException("Request size outside limits");
        return BigDecimal.valueOf((long) inputTokens + INPUT_TEMPLATE_OVERHEAD_TOKENS)
                .multiply(BigDecimal.valueOf(promptCapPerM))
                .add(BigDecimal.valueOf(maxTokens).multiply(BigDecimal.valueOf(completionCapPerM)))
                .setScale(0, RoundingMode.CEILING)
                .longValueExact();
    }

    static boolean generationPhase(String phase) {
        return Set.of("generation", "layout", "parts", "polishing", "repair").contains(phase);
    }

    static int generationTimeoutSeconds(int budgetSeconds, int sx, int sy, int sz) {
        if (budgetSeconds < 60 || budgetSeconds > MAX_DEADLINE_SECONDS)
            throw new IllegalArgumentException("Generation budget outside limits");
        generationTokens(1, sx, sy, sz);
        double size = Math.max(1, Math.cbrt((long) sx * sy * sz / 9216.0));
        return Math.min(MAX_DEADLINE_SECONDS, (int) Math.ceil(budgetSeconds * size));
    }

    // Request building (single-shot only; loop prompts and orchestration stay for U-AI2).

    static JsonObject chatRequest(
            String model,
            String provider,
            double promptCapPerM,
            double completionCapPerM,
            String system,
            String user,
            String effort,
            int tokens) {
        var request = new JsonObject();
        request.addProperty("model", model);
        request.addProperty("max_tokens", tokens);
        request.addProperty("stream", false);
        var reasoning = new JsonObject();
        reasoning.addProperty("effort", effort);
        request.add("reasoning", reasoning);
        var messages = new JsonArray();
        for (var entry : List.of(new String[] {"system", system}, new String[] {"user", user})) {
            var m = new JsonObject();
            m.addProperty("role", entry[0]);
            m.addProperty("content", entry[1]);
            messages.add(m);
        }
        request.add("messages", messages);
        // The routing object is OpenRouter-specific; other providers speak plain OpenAI.
        if (OPENROUTER.equals(provider))
            request.add("provider", routing(promptCapPerM, completionCapPerM, model));
        return request;
    }

    static String messageContent(JsonObject response) {
        try {
            var m =
                    response.getAsJsonArray("choices")
                            .get(0)
                            .getAsJsonObject()
                            .getAsJsonObject("message");
            if (m.has("tool_calls")
                    || m.has("function_call")
                    || !m.get("content").isJsonPrimitive()
                    || !m.getAsJsonPrimitive("content").isString()) return null;
            return m.get("content").getAsString();
        } catch (RuntimeException missing) {
            return null;
        }
    }

    static boolean unbilled(JsonObject response) {
        try {
            var usage = response.getAsJsonObject("usage");
            var cost = usage.get("cost");
            return cost != null
                    && cost.isJsonPrimitive()
                    && cost.getAsJsonPrimitive().isNumber()
                    && new BigDecimal(cost.getAsString()).signum() == 0;
        } catch (RuntimeException missing) {
            return false;
        }
    }

    // Tariff probe and routing (OpenRouter-only; the max_price object is the enforcement).

    static void requireGenerationTariff(JsonObject response, String model) {
        requireGenerationTariff(
                response,
                model,
                GENERATION_PROMPT_USD_PER_TOKEN,
                GENERATION_COMPLETION_USD_PER_TOKEN);
    }

    static void requireGenerationTariff(
            JsonObject response,
            String model,
            BigDecimal promptCeiling,
            BigDecimal completionCeiling) {
        try {
            JsonObject data = response.getAsJsonObject("data");
            JsonArray endpoints = data.getAsJsonArray("endpoints");
            if (!model.equals(data.get("id").getAsString()) || endpoints.size() > 64)
                throw new IllegalArgumentException("Unexpected generation endpoints");
            int eligible = 0;
            for (var element : endpoints) {
                JsonObject pricing = element.getAsJsonObject().getAsJsonObject("pricing");
                BigDecimal prompt = price(pricing, "prompt"),
                        completion = price(pricing, "completion");
                if (prompt == null
                        || completion == null
                        || prompt.compareTo(promptCeiling) > 0
                        || completion.compareTo(completionCeiling) > 0
                        || nonZero(pricing, "request")
                        || nonZero(pricing, "image")) continue;
                for (var entry : pricing.entrySet()) {
                    BigDecimal ceiling =
                            switch (entry.getKey()) {
                                case "discount",
                                        "web_search",
                                        "prompt",
                                        "completion",
                                        "request",
                                        "image" ->
                                        null;
                                case "input_cache_read", "input_cache_write" -> promptCeiling;
                                case "internal_reasoning" -> completionCeiling;
                                default -> BigDecimal.ZERO;
                            };
                    if (ceiling != null && price(pricing, entry.getKey()).compareTo(ceiling) > 0)
                        throw new IllegalArgumentException("Unfiltered price above the reserve");
                }
                eligible++;
            }
            if (eligible == 0) throw new IllegalArgumentException("No endpoint within the reserve");
        } catch (RuntimeException invalid) {
            throw new Failure("tariff", "unsupported_generation_tariff", 0);
        }
    }

    private static BigDecimal price(JsonObject pricing, String key) {
        if (!pricing.has(key)) return null;
        BigDecimal value = new BigDecimal(pricing.getAsJsonPrimitive(key).getAsString());
        if (value.signum() < 0) throw new IllegalArgumentException("Negative tariff");
        return value;
    }

    private static boolean nonZero(JsonObject pricing, String key) {
        BigDecimal value = price(pricing, key);
        return value != null && value.signum() != 0;
    }

    /** Helper routing: no provider pin and no price caps. */
    static JsonObject routing() {
        JsonObject provider = new JsonObject();
        provider.addProperty("data_collection", "deny");
        provider.addProperty("require_parameters", true);
        provider.addProperty("allow_fallbacks", false);
        return provider;
    }

    static JsonObject routing(String generationModel) {
        return routing(
                BigDecimal.valueOf(0.10),
                BigDecimal.valueOf(0.20),
                DEFAULT_GENERATION_MODEL.equals(generationModel) ? "meta" : null);
    }

    static JsonObject routing(
            double promptCapPerM, double completionCapPerM, String generationModel) {
        return routing(
                BigDecimal.valueOf(promptCapPerM),
                BigDecimal.valueOf(completionCapPerM),
                DEFAULT_GENERATION_MODEL.equals(generationModel) ? "meta" : null);
    }

    private static JsonObject routing(
            BigDecimal promptCapUsdPerM, BigDecimal completionCapUsdPerM, String pinned) {
        JsonObject provider = new JsonObject();
        provider.addProperty("data_collection", "allow");
        provider.addProperty("require_parameters", true);
        provider.addProperty("allow_fallbacks", false);
        if (pinned != null) {
            JsonArray only = new JsonArray();
            only.add(pinned);
            provider.add("only", only);
        }
        JsonObject price = new JsonObject();
        // Two decimals, like the historic 0.10/0.20: the serialized request sizes the reserve.
        price.addProperty("prompt", twoDecimals(promptCapUsdPerM));
        price.addProperty("completion", twoDecimals(completionCapUsdPerM));
        price.addProperty("request", 0);
        price.addProperty("image", 0);
        price.addProperty("audio", 0);
        provider.add("max_price", price);
        return provider;
    }

    private static BigDecimal twoDecimals(BigDecimal value) {
        // Round down: max_price must never exceed the owner's ceiling (0.125 -> 0.12, not 0.13).
        return value.setScale(2, java.math.RoundingMode.DOWN);
    }

    // Usage receipts (called only with the cost already validated by Billing).

    static JsonObject usageReceipt(
            String phase, JsonObject response, BigDecimal cost, Integer requestedMaxTokens) {
        return usageReceipt(phase, response, cost, requestedMaxTokens, false);
    }

    static JsonObject usageReceipt(
            String phase,
            JsonObject response,
            BigDecimal cost,
            Integer requestedMaxTokens,
            boolean estimated) {
        if (phase == null
                || !Set.of(
                                "generation",
                                "transcription",
                                "cleanup",
                                "layout",
                                "parts",
                                "polishing",
                                "repair")
                        .contains(phase)) throw new IllegalArgumentException("Unknown usage phase");
        Integer cap = null;
        if (generationPhase(phase)) {
            if (requestedMaxTokens == null)
                throw new IllegalArgumentException("Missing generation token cap");
            if (phase.equals("generation") && requestedMaxTokens == SINGLE_TOKENS)
                generationBound(requestedMaxTokens);
            else if (requestedMaxTokens == LOOP_TURN_TOKENS || !phase.equals("generation"))
                callBound(requestedMaxTokens);
            else throw new IllegalArgumentException("Unknown generation token cap");
            cap = requestedMaxTokens;
        }
        JsonElement rawUsage = response == null ? null : response.get("usage");
        JsonObject usage =
                rawUsage != null && rawUsage.isJsonObject() ? rawUsage.getAsJsonObject() : null;
        JsonObject receipt = new JsonObject();
        receipt.addProperty("phase", phase);
        receipt.addProperty("inputTokens", usageCounter(usage, "input_tokens", "prompt_tokens"));
        receipt.addProperty(
                "outputTokens", usageCounter(usage, "output_tokens", "completion_tokens"));
        JsonElement details = usageField(usage, "input_tokens_details", "prompt_tokens_details");
        receipt.addProperty(
                "cachedTokens",
                details != null && details.isJsonObject()
                        ? tokenCounter(details.getAsJsonObject().get("cached_tokens"))
                        : null);
        JsonElement outputDetails =
                usageField(usage, "output_tokens_details", "completion_tokens_details");
        receipt.addProperty(
                "reasoningTokens",
                outputDetails != null && outputDetails.isJsonObject()
                        ? tokenCounter(outputDetails.getAsJsonObject().get("reasoning_tokens"))
                        : null);
        receipt.addProperty("requestedMaxTokens", cap);
        receipt.addProperty("costUsd", cost.stripTrailingZeros());
        if (estimated) receipt.addProperty("estimated", true);
        receipt.addProperty("finishReason", generationPhase(phase) ? finishReason(response) : null);
        return receipt;
    }

    private static String finishReason(JsonObject response) {
        JsonElement choices = response == null ? null : response.get("choices");
        if (choices == null || !choices.isJsonArray() || choices.getAsJsonArray().isEmpty())
            return null;
        JsonElement choice = choices.getAsJsonArray().get(0);
        if (!choice.isJsonObject()) return null;
        JsonElement reason = choice.getAsJsonObject().get("finish_reason");
        if (reason == null || !reason.isJsonPrimitive() || !reason.getAsJsonPrimitive().isString())
            return null;
        return switch (reason.getAsString()) {
            case "stop", "length", "content_filter", "tool_calls" -> reason.getAsString();
            default -> null;
        };
    }

    private static Integer usageCounter(JsonObject usage, String primary, String fallback) {
        return tokenCounter(usageField(usage, primary, fallback));
    }

    private static JsonElement usageField(JsonObject usage, String primary, String fallback) {
        return usage == null ? null : usage.get(usage.has(primary) ? primary : fallback);
    }

    private static Integer tokenCounter(JsonElement value) {
        if (value == null || !value.isJsonPrimitive() || !value.getAsJsonPrimitive().isNumber())
            return null;
        try {
            BigDecimal count = value.getAsBigDecimal();
            return count.signum() < 0 || count.compareTo(MAX_USAGE_TOKENS) > 0
                    ? null
                    : count.intValueExact();
        } catch (NumberFormatException | ArithmeticException invalid) {
            return null;
        }
    }

    // Budget seam (mirrors the mod reserve/topup mapping; fail closed, never fail open).

    static Budget.Reservation reserve(Budget budget, UUID session, UUID builder, long bound)
            throws IOException {
        try {
            return budget.reserve(session, builder, bound);
        } catch (Budget.BudgetExhausted | Budget.BudgetBlocked known) {
            throw known;
        } catch (Budget.PolicyUnavailable unavailable) {
            LOGGER.warning("AI budget policy unavailable (" + causeName(unavailable) + ")");
            throw chained("budget", "budget_unavailable", unavailable);
        } catch (Budget.LedgerUnavailable unwritable) {
            LOGGER.warning("AI budget ledger unavailable (" + causeName(unwritable) + ")");
            throw chained("budget", "budget_ledger_unavailable", unwritable);
        } catch (IllegalStateException poisoned) {
            // The ledger stays poisoned after a write failure until restart; every later
            // reserve() throws here instead of failing open.
            LOGGER.warning("AI budget ledger unavailable (" + causeName(poisoned) + ")");
            throw chained("budget", "budget_ledger_unavailable", poisoned);
        }
    }

    static Budget.Reservation topup(Budget budget, UUID session, UUID builder, long extra)
            throws IOException {
        try {
            return budget.reserveTopUp(session, builder, extra);
        } catch (Budget.BudgetExhausted | Budget.BudgetBlocked known) {
            throw known;
        } catch (Budget.PolicyUnavailable unavailable) {
            LOGGER.warning("AI budget policy unavailable (" + causeName(unavailable) + ")");
            throw chained("budget", "budget_unavailable", unavailable);
        } catch (Budget.LedgerUnavailable unwritable) {
            LOGGER.warning("AI budget ledger unavailable (" + causeName(unwritable) + ")");
            throw chained("budget", "budget_ledger_unavailable", unwritable);
        } catch (IllegalStateException poisoned) {
            LOGGER.warning("AI budget ledger unavailable (" + causeName(poisoned) + ")");
            throw chained("budget", "budget_ledger_unavailable", poisoned);
        }
    }

    private static String causeName(Throwable failure) {
        Throwable cause = failure.getCause();
        return cause == null ? "none" : cause.getClass().getSimpleName();
    }

    static Failure chained(String phase, String category, Throwable cause) {
        Failure failure = new Failure(phase, category, 0);
        failure.initCause(cause);
        return failure;
    }

    /** Only closed categories reach logs; upstream text never does. */
    static String diagnostic(Throwable failure) {
        Throwable cause = failure instanceof ExecutionException ? failure.getCause() : failure;
        if (cause instanceof Budget.BudgetExhausted) return "phase=budget category=exhausted";
        if (cause instanceof Budget.BudgetBlocked) return "phase=budget category=blocked";
        return cause instanceof Failure ? cause.getMessage() : "phase=unknown category=internal";
    }

    static boolean validProvider(String provider) {
        return PROVIDERS.contains(provider);
    }

    /** Chat path of the generation backend (the only one serving generate). */
    String chatPath() {
        return backend.transport().chatPath();
    }

    /** Audio path of the transcription backend. */
    String audioPath() {
        return backend.transport().audioPath();
    }

    /** Whether this backend serves an endpoint catalog (tariff probe applies). */
    boolean priceApi() {
        return backend.transport().priceApi();
    }

    /** Authenticated GET for public metadata (tariff probe); never sends credentials elsewhere. */
    JsonObject get(String phase, String path) throws Exception {
        return exchange(
                phase,
                HttpRequest.newBuilder(URI.create(backend.transport().baseUrl() + path))
                        .timeout(Duration.ofSeconds(10))
                        .header("Authorization", "Bearer " + backend.key())
                        .header("Content-Type", "application/json")
                        .header("HTTP-Referer", APP_URL)
                        .header("X-Title", APP_TITLE)
                        .GET()
                        .build());
    }

    // HTTP choke point: the only code that performs outbound AI HTTP.

    JsonObject post(String phase, String path, JsonObject body, int timeout) throws Exception {
        return exchange(
                phase,
                HttpRequest.newBuilder(URI.create(backend.transport().baseUrl() + path))
                        .timeout(Duration.ofSeconds(timeout))
                        .header("Authorization", "Bearer " + backend.key())
                        .header("Content-Type", "application/json")
                        // OpenRouter and the Vercel AI Gateway attribute usage to the app by these.
                        .header("HTTP-Referer", APP_URL)
                        .header("X-Title", APP_TITLE)
                        .POST(HttpRequest.BodyPublishers.ofString(body.toString()))
                        .build());
    }

    /**
     * OpenAI audio upload for non-OpenRouter transports: multipart/form-data with the wav bytes as
     * {@code file} plus the {@code model} (and {@code language} when known). The boundary is
     * per-request random so binary audio can never forge a part edge. (Kept for the U-AI2 STT move;
     * the sidecar answers {@code /v1/transcribe} with the stub until then.)
     */
    JsonObject postMultipart(
            String phase, String path, byte[] wav, String model, String language, int timeout)
            throws Exception {
        String boundary = "----BlockfieldAi" + UUID.randomUUID().toString().replace("-", "");
        return exchange(
                phase,
                HttpRequest.newBuilder(URI.create(backend.transport().baseUrl() + path))
                        .timeout(Duration.ofSeconds(timeout))
                        .header("Authorization", "Bearer " + backend.key())
                        .header("Content-Type", "multipart/form-data; boundary=" + boundary)
                        .POST(
                                HttpRequest.BodyPublishers.ofByteArray(
                                        multipartBody(boundary, wav, model, language)))
                        .build());
    }

    static byte[] multipartBody(String boundary, byte[] wav, String model, String language) {
        var out = new ByteArrayOutputStream();
        try {
            out.write(("--" + boundary + "\r\n").getBytes(StandardCharsets.US_ASCII));
            out.write(
                    ("Content-Disposition: form-data; name=\"file\"; filename=\"audio.wav\"\r\n")
                            .getBytes(StandardCharsets.US_ASCII));
            out.write("Content-Type: audio/wav\r\n\r\n".getBytes(StandardCharsets.US_ASCII));
            out.write(wav);
            part(out, boundary, "model", model);
            if (language != null) part(out, boundary, "language", language);
            out.write(("\r\n--" + boundary + "--\r\n").getBytes(StandardCharsets.US_ASCII));
        } catch (IOException impossible) {
            throw new AssertionError(impossible);
        }
        return out.toByteArray();
    }

    private static void part(ByteArrayOutputStream out, String boundary, String name, String value)
            throws IOException {
        out.write(("\r\n--" + boundary + "\r\n").getBytes(StandardCharsets.US_ASCII));
        out.write(
                ("Content-Disposition: form-data; name=\"" + name + "\"\r\n\r\n")
                        .getBytes(StandardCharsets.US_ASCII));
        out.write(value.getBytes(StandardCharsets.UTF_8));
    }

    /** Free public metadata; sent without credentials. */
    JsonObject tariffProbe() throws Exception {
        return exchange(
                "tariff",
                HttpRequest.newBuilder(URI.create(backend.transport().baseUrl()))
                        .timeout(Duration.ofSeconds(10))
                        .GET()
                        .build());
    }

    /** Bounded accumulation per endpoint: only generation carries reasoning text. */
    static int responseCap(String phase) {
        return generationPhase(phase) ? MAX_GENERATION_RESPONSE : MAX_RESPONSE;
    }

    JsonObject exchange(String phase, HttpRequest request) throws Exception {
        CompletableFuture<HttpResponse<byte[]>> pending = null;
        try {
            pending = http.sendAsync(request, info -> new LimitedBody(responseCap(phase)));
            HttpResponse<byte[]> response =
                    pending.get(
                            request.timeout().orElseThrow().plusSeconds(5).toMillis(),
                            TimeUnit.MILLISECONDS);
            // Never expose upstream response bodies or exception messages: either may echo prompt
            // or credentials.
            if (response.statusCode() != 200) {
                var refused = new Failure(phase, "http", response.statusCode());
                try {
                    var body =
                            JsonParser.parseString(
                                            new String(response.body(), StandardCharsets.UTF_8))
                                    .getAsJsonObject();
                    if (body.has("usage")) refused.receipt = body;
                } catch (RuntimeException ignored) {
                    /* An unreadable error has no usable receipt. */
                }
                throw refused;
            }
            try {
                return JsonParser.parseString(new String(response.body(), StandardCharsets.UTF_8))
                        .getAsJsonObject();
            } catch (RuntimeException invalid) {
                throw new Failure(phase, "invalid_json", 200);
            }
        } catch (Failure safe) {
            throw safe;
        } catch (InterruptedException cancelled) {
            throw cancelled;
        } catch (Exception failure) {
            Throwable cause = failure instanceof ExecutionException ? failure.getCause() : failure;
            throw new Failure(
                    phase,
                    cause instanceof TimeoutException || cause instanceof HttpTimeoutException
                            ? "timeout"
                            : "transport",
                    0);
        } finally {
            if (pending != null) pending.cancel(true);
        }
    }

    /**
     * HTTP 200 with an empty choice the provider did not bill (cost 0): a dropped long response,
     * not a paid answer. Same {@code invalid_response} category, so diagnostics are unchanged; the
     * generation path retries it once while the window allows it. Anything billed keeps failing
     * outright: it was paid for, so it is final.
     */
    static final class EmptyResponse extends Failure {
        EmptyResponse(String phase) {
            super(phase, "invalid_response", 0);
        }
    }

    static class Failure extends IllegalArgumentException {
        final String phase;
        final String category;
        final int status;
        private JsonObject receipt;

        boolean unbilled() {
            return category.equals("http") && (status == 401 || status == 402);
        }

        Failure(String phase, String category, int status) {
            super(
                    "phase="
                            + phase
                            + " category="
                            + category
                            + (status == 0 ? "" : " http=" + status));
            this.phase = phase;
            this.category = category;
            this.status = status;
        }
    }

    static final class LimitedBody implements HttpResponse.BodySubscriber<byte[]> {
        private final int limit;
        private final ByteArrayOutputStream bytes = new ByteArrayOutputStream();
        private final CompletableFuture<byte[]> result = new CompletableFuture<>();
        private Flow.Subscription subscription;

        LimitedBody(int limit) {
            this.limit = limit;
        }

        public CompletionStage<byte[]> getBody() {
            return result;
        }

        public void onSubscribe(Flow.Subscription value) {
            subscription = value;
            value.request(1);
        }

        public void onNext(List<ByteBuffer> chunks) {
            for (ByteBuffer chunk : chunks) {
                if (chunk.remaining() > limit - bytes.size()) {
                    subscription.cancel();
                    result.completeExceptionally(
                            new IllegalArgumentException("AI response too large"));
                    return;
                }
                byte[] value = new byte[chunk.remaining()];
                chunk.get(value);
                bytes.writeBytes(value);
            }
            subscription.request(1);
        }

        public void onError(Throwable error) {
            result.completeExceptionally(error);
        }

        public void onComplete() {
            result.complete(bytes.toByteArray());
        }
    }

    /**
     * Per-call money accounting shared by the JSON and multipart posts. A generation call reserves
     * its exact serialized size at send time (topping the envelope up when a bigger-than-maximum
     * request needs it); helpers top a higher authenticated cost up to the exact amount instead of
     * overrunning, so only generation phases can exceed their bound and block later paid calls.
     */
    final class Billing implements AutoCloseable {
        private final Budget.Reservation reservation;
        private final Budget budget;
        private final UUID session, builder;
        private Budget.Reservation topup;
        private BigDecimal confirmed = BigDecimal.ZERO;
        private boolean certain = true;
        private final java.util.function.Consumer<JsonObject> receiptSink;
        private final boolean singleCall;
        private boolean sent;
        // Request speed choice, logged with each paid generation call.
        String mode = "quality";
        // Owner generation ceilings (USD per million tokens) sizing reserves and every
        // cost-less estimate. Set via caps() at each construction site: there is no
        // reference-rate fallback.
        double promptCapPerM, completionCapPerM;

        /** Generation reserves follow the owner's caps (see callBoundFor). */
        Billing caps(double promptCapPerM, double completionCapPerM) {
            this.promptCapPerM = promptCapPerM;
            this.completionCapPerM = completionCapPerM;
            return this;
        }

        Billing(Budget.Reservation reservation, Budget budget, UUID session, UUID builder) {
            this(reservation, receipt -> {}, false, budget, session, builder);
        }

        Billing(
                Budget.Reservation reservation,
                java.util.function.Consumer<JsonObject> receiptSink,
                Budget budget,
                UUID session,
                UUID builder) {
            this(reservation, receiptSink, true, budget, session, builder);
        }

        Billing(
                Budget.Reservation reservation,
                java.util.function.Consumer<JsonObject> receiptSink,
                boolean singleCall,
                Budget budget,
                UUID session,
                UUID builder) {
            this.reservation = reservation;
            this.receiptSink = receiptSink;
            this.singleCall = singleCall;
            this.budget = budget;
            this.session = session;
            this.builder = builder;
        }

        JsonObject post(String phase, String path, JsonObject request) throws Exception {
            return post(phase, path, request, 25);
        }

        JsonObject post(String phase, String path, JsonObject request, int timeout)
                throws Exception {
            return post(phase, path, request, timeout, Long.MAX_VALUE);
        }

        JsonObject post(String phase, String path, JsonObject request, int timeout, long ceiling)
                throws Exception {
            if (Thread.currentThread().isInterrupted()) throw new InterruptedException();
            if (singleCall && sent) throw new IllegalStateException("Call already sent");
            reservation.checkPaidCall();
            sent = true;
            // Right-sized send-time bound: the slot holds the up-front per-phase maximum, but
            // the overrun check and any top-up use this call's exact serialized request.
            long bound = Math.min(reservation.boundMicroUsd(), ceiling);
            if (generationPhase(phase)) {
                bound =
                        callBoundFor(
                                requestTokens(request),
                                request.get("max_tokens").getAsInt(),
                                promptCapPerM,
                                completionCapPerM);
                if (bound > reservation.boundMicroUsd())
                    topup = topup(budget, session, builder, bound - reservation.boundMicroUsd());
                LOGGER.info(
                        "AI call phase="
                                + phase
                                + " mode="
                                + mode
                                + " effort="
                                + request.getAsJsonObject("reasoning").get("effort").getAsString()
                                + " maxTokens="
                                + request.get("max_tokens"));
            }
            certain = false;
            JsonObject response;
            Failure upstreamFailure = null;
            try {
                response = ProviderHttp.this.post(phase, path, request, timeout);
            } catch (Failure refused) {
                // OpenRouter rejects a bad key (401) or missing credit (402) before routing to a
                // model, so nothing was charged; other statuses and timeouts may still be billed.
                if (refused.receipt == null) {
                    if (refused.unbilled()) {
                        certain = true;
                    } else {
                        // Unknown billing outcome: the provider may still have billed this
                        // exact request, so keep the send-time bound (the most it could cost
                        // under the owner ceilings) and release only the provably unspent
                        // remainder on close. Like WorkshopAiBudget.confirm, only a settled
                        // amount releases funds; the accumulation never under-charges a retry.
                        confirmed = confirmed.add(BigDecimal.valueOf(bound, 6));
                        certain = true;
                    }
                    throw refused;
                }
                response = refused.receipt;
                upstreamFailure = refused;
            }
            return account(phase, request, response, upstreamFailure, bound);
        }

        private JsonObject account(
                String phase,
                JsonObject request,
                JsonObject response,
                Failure upstreamFailure,
                long bound)
                throws Exception {
            BigDecimal cost;
            boolean estimated = false;
            try {
                JsonObject usage = response.getAsJsonObject("usage");
                JsonElement element = usage.get("cost");
                if (element == null || element.isJsonNull()) {
                    cost = estimatedCost(phase, request, response, bound);
                    estimated = true;
                } else {
                    // A present but malformed cost from a cost-API provider is a broken
                    // receipt, not a missing one: fail closed instead of estimating.
                    if (!element.isJsonPrimitive() || !element.getAsJsonPrimitive().isNumber())
                        throw new Failure(phase, "invalid_billing_schema", 0);
                    try {
                        cost = element.getAsBigDecimal();
                    } catch (RuntimeException unrepresentable) {
                        throw new Failure(phase, "invalid_billing_schema", 0);
                    }
                }
            } catch (Failure invalid) {
                throw invalid;
            } catch (RuntimeException missing) {
                // Replies without a cost API (gateway, custom endpoints) bill by token
                // counts instead: a conservative tokens × ceiling estimate, never zero
                // unless the call's own bound is a free model.
                cost = estimatedCost(phase, request, response, bound);
                estimated = true;
            }
            // Numeric exponents can otherwise make exact addition allocate beyond the body cap.
            if (cost.signum() < 0 || cost.scale() > MAX_RESPONSE)
                throw new Failure(phase, "invalid_billing_schema", 0);
            if (cost.compareTo(BigDecimal.valueOf(bound, 6)) > 0) {
                if (!generationPhase(phase)) {
                    long extra;
                    try {
                        extra =
                                cost.movePointRight(6)
                                                .setScale(0, RoundingMode.CEILING)
                                                .longValueExact()
                                        - reservation.boundMicroUsd();
                    } catch (ArithmeticException unrepresentable) {
                        throw new Failure(phase, "billing_cost_outside_bounds", 0);
                    }
                    if (extra > 0) topup = topup(budget, session, builder, extra);
                } else {
                    BigDecimal total =
                            cost.compareTo(BigDecimal.valueOf(Long.MAX_VALUE, 6)) > 0
                                    ? cost
                                    : confirmed.add(cost);
                    boolean accounted = reservation.recordOverrun(total);
                    throw new Failure(
                            phase,
                            accounted ? "billing_bound_exceeded" : "billing_cost_outside_bounds",
                            0);
                }
            }
            confirmed = confirmed.add(cost);
            certain = true;
            if (singleCall) settle();
            JsonObject receipt =
                    usageReceipt(
                            phase,
                            response,
                            cost,
                            generationPhase(phase) ? request.get("max_tokens").getAsInt() : null,
                            estimated);
            receiptSink.accept(receipt);
            LOGGER.info("AI usage " + receipt);
            if (upstreamFailure != null) throw upstreamFailure;
            return response;
        }

        /**
         * Multipart audio post for OpenAI-compatible transports (file, model, optional language).
         * Accounting mirrors the JSON post: the transcription settles the duration-sized reserve,
         * topped up to an authenticated cost.
         */
        JsonObject postAudio(String path, byte[] wav, String model, String language)
                throws Exception {
            return postAudio(path, wav, model, language, 25);
        }

        JsonObject postAudio(String path, byte[] wav, String model, String language, int timeout)
                throws Exception {
            if (Thread.currentThread().isInterrupted()) throw new InterruptedException();
            if (singleCall && sent) throw new IllegalStateException("Call already sent");
            reservation.checkPaidCall();
            sent = true;
            long bound = reservation.boundMicroUsd();
            certain = false;
            JsonObject response;
            Failure upstreamFailure = null;
            try {
                response =
                        ProviderHttp.this.postMultipart(
                                "transcription", path, wav, model, language, timeout);
            } catch (Failure refused) {
                // Same receipt rule as the JSON post: only a carried receipt may be billed.
                if (refused.receipt == null) {
                    if (refused.unbilled()) certain = true;
                    else {
                        // Unknown billing outcome: the provider may still have billed this
                        // exact request, so keep the send-time bound (the most it could
                        // cost under the owner ceilings) and release only the provably
                        // unspent remainder on close. The accumulation never under-charges
                        // a retry.
                        confirmed = confirmed.add(BigDecimal.valueOf(bound, 6));
                        certain = true;
                    }
                    throw refused;
                }
                response = refused.receipt;
                upstreamFailure = refused;
            }
            return account("transcription", new JsonObject(), response, upstreamFailure, bound);
        }

        private BigDecimal estimatedCost(
                String phase, JsonObject request, JsonObject response, long bound) {
            JsonElement rawUsage = response == null ? null : response.get("usage");
            JsonObject usage =
                    rawUsage != null && rawUsage.isJsonObject() ? rawUsage.getAsJsonObject() : null;
            if (phase.equals("transcription"))
                return BigDecimal.valueOf(Math.max(1, reservation.boundMicroUsd()), 6);
            Integer inTokens = usageCounter(usage, "input_tokens", "prompt_tokens");
            Integer outTokens = usageCounter(usage, "output_tokens", "completion_tokens");
            // A zero counter for a non-empty request is a broken receipt, not free
            // usage: fall back to the request-sized conservative values below.
            if (inTokens != null && inTokens <= 0) inTokens = null;
            if (outTokens != null && outTokens <= 0) outTokens = null;
            // Counters above what was sent are a broken receipt: a BPE token decodes to
            // at least one byte, so the serialized request upper-bounds the true input, and the
            // provider cannot return more output than the requested cap.
            int requestIn = requestTokens(request);
            int cap = requestMaxTokens(request);
            int in = Math.min(inTokens != null ? inTokens : requestIn, requestIn);
            int out;
            // Every role estimates at the owner's ceilings: no reference rates.
            double promptRate = promptCapPerM, completionRate = completionCapPerM;
            if (generationPhase(phase)) {
                int limit = cap > 0 ? cap : MAX_USAGE_TOKENS.intValue();
                out = Math.min(outTokens != null ? outTokens : cap, limit);
            } else if (cap > 0) {
                out = Math.min(outTokens != null ? outTokens : cap, cap);
            } else {
                out = outTokens != null ? Math.min(outTokens, MAX_USAGE_TOKENS.intValue()) : 0;
            }
            out = Math.min(out, MAX_USAGE_TOKENS.intValue());
            // Same input sizing as every reserve (see callBoundFor): estimated and
            // reserved costs run on identical arithmetic. A helper estimate is never
            // clamped down to its slot; account() tops the reservation up instead.
            long micro =
                    BigDecimal.valueOf(in + INPUT_TEMPLATE_OVERHEAD_TOKENS)
                            .multiply(BigDecimal.valueOf(promptRate))
                            .add(
                                    BigDecimal.valueOf(out)
                                            .multiply(BigDecimal.valueOf(completionRate)))
                            .setScale(0, RoundingMode.CEILING)
                            .longValueExact();
            if (bound == 0) return BigDecimal.valueOf(0, 6);
            return BigDecimal.valueOf(Math.max(1, micro), 6);
        }

        /** Requested output cap, or 0 for cap-less helper calls such as transcription. */
        private static int requestMaxTokens(JsonObject request) {
            try {
                return request.get("max_tokens").getAsInt();
            } catch (RuntimeException missing) {
                return 0;
            }
        }

        @Override
        public void close() throws IOException {
            settle();
        }

        private void settle() throws IOException {
            if (certain) {
                boolean interrupted = Thread.interrupted();
                try {
                    if (topup == null) {
                        reservation.confirm(confirmed);
                    } else {
                        // Split the confirmed cost across both reservations; each share fits
                        // its own bound because the total never exceeds slot + top-up.
                        BigDecimal slot =
                                confirmed.min(BigDecimal.valueOf(reservation.boundMicroUsd(), 6));
                        topup.confirm(confirmed.subtract(slot));
                        reservation.confirm(slot);
                    }
                } finally {
                    if (interrupted) Thread.currentThread().interrupt();
                }
            }
        }
    }
}
