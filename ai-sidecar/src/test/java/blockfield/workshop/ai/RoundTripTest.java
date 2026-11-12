package blockfield.workshop.ai;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.sun.net.httpserver.HttpServer;

import org.json.JSONArray;
import org.json.JSONObject;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.io.IOException;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Clock;
import java.time.Instant;
import java.time.ZoneId;
import java.time.ZoneOffset;
import java.util.Base64;
import java.util.UUID;

/**
 * Round trip game → sidecar → provider → game over live loopback HTTP, speaking the contract
 * request the mod will send at cutover: the stub backend returns an empty plan with ledger
 * attribution, and a fake provider proves the moved HTTP client plus money accounting end to end.
 * Same requestId + same body replays the jobId without reserving twice.
 */
class RoundTripTest {
    private static final String SECRET = "round-trip-secret-0123456789-_AB";
    private static final UUID SESSION = UUID.fromString("c0000000-0000-0000-0000-000000000001");
    private static final UUID BUILDER = UUID.fromString("d0000000-0000-0000-0000-000000000001");

    private static final String PLAN =
            "{\"version\":2,\"operations\":[{\"block\":\"minecraft:stone\",\"match\":\"air\","
                    + "\"hollow\":false,\"from\":[1,2,3],\"to\":[1,2,3]}]}";

    @TempDir Path root;

    private HttpServer sidecar;
    private String base;
    private final HttpClient client = HttpClient.newHttpClient();
    private Budget budget;

    private static Clock fixedClock() {
        Instant now = Instant.parse("2026-11-10T00:00:00Z");
        return new Clock() {
            @Override
            public ZoneId getZone() {
                return ZoneOffset.UTC;
            }

            @Override
            public Clock withZone(ZoneId zone) {
                return this;
            }

            @Override
            public Instant instant() {
                return now;
            }
        };
    }

    private String policy(long grant) {
        return "{\"schema\":1,\"revision\":0,\"currency\":\"USD\",\"mode\":\"session\","
                + "\"sessions\":{\""
                + SESSION
                + "\":"
                + grant
                + "},"
                + "\"builders\":{\""
                + BUILDER
                + "\":"
                + grant
                + "}}";
    }

    private void startStub(long grant) throws Exception {
        Files.writeString(root.resolve("ai-limits.json"), policy(grant));
        budget = Budget.open(root.resolve("ai-limits.json"), root.resolve("ai-usage.json"));
        sidecar = AiSidecar.start("127.0.0.1", 0, SECRET, fixedClock(), null, budget);
        base = "http://127.0.0.1:" + sidecar.getAddress().getPort();
    }

    private void startReal(HttpServer fakeProvider) throws Exception {
        Files.writeString(root.resolve("ai-limits.json"), policy(10_000_000L));
        budget = Budget.open(root.resolve("ai-limits.json"), root.resolve("ai-usage.json"));
        String fakeBase = "http://127.0.0.1:" + fakeProvider.getAddress().getPort();
        // Custom transport skips the OpenRouter tariff probe, which the fake does not serve.
        ProviderHttp.Selection fake = new ProviderHttp.Selection("custom", fakeBase, "fake-key");
        sidecar =
                AiSidecar.start(
                        "127.0.0.1",
                        0,
                        SECRET,
                        fixedClock(),
                        new AiSidecar.Selections(fake, fake, fake),
                        budget);
        base = "http://127.0.0.1:" + sidecar.getAddress().getPort();
    }

    @AfterEach
    void stop() throws Exception {
        if (sidecar != null) {
            sidecar.stop(0);
        }
        if (budget != null) {
            budget.close();
        }
    }

    private HttpResponse<String> post(String path, String body) throws Exception {
        return client.send(
                HttpRequest.newBuilder(URI.create(base + path))
                        .header("Content-Type", "application/json")
                        .header("Authorization", "Bearer " + SECRET)
                        .POST(HttpRequest.BodyPublishers.ofString(body, StandardCharsets.UTF_8))
                        .build(),
                HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
    }

    private HttpResponse<String> poll(String jobId) throws Exception {
        return client.send(
                HttpRequest.newBuilder(URI.create(base + "/v1/jobs/" + jobId))
                        .header("Authorization", "Bearer " + SECRET)
                        .GET()
                        .build(),
                HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
    }

    private static String generateBody(String requestId, JSONObject config) {
        return new JSONObject()
                .put("requestId", requestId)
                .put("session", SESSION.toString())
                .put("builder", BUILDER.toString())
                .put("prompt", "a small hut")
                .put("mode", "generate")
                .put("version", 2)
                .put("dims", new JSONObject().put("x", 9).put("y", 9).put("z", 9))
                .put("palette", new JSONArray().put("minecraft:stone"))
                .put("config", config)
                .put("snapshot", new JSONObject())
                .put("deadlineSeconds", 120)
                .toString();
    }

    private static com.google.gson.JsonObject gson(JSONObject config) {
        return com.google.gson.JsonParser.parseString(config.toString()).getAsJsonObject();
    }

    private static JSONObject realConfig() {
        return new JSONObject()
                .put("generationModel", "test/model")
                .put("generationProvider", "custom")
                .put("transcriptionModel", "test/model")
                .put("transcriptionProvider", "custom")
                .put("cleanupProvider", "custom")
                .put("maxBlocks", 512)
                .put("planning", new JSONObject())
                .put("generationPromptCapUsdPerM", 100.0)
                .put("generationCompletionCapUsdPerM", 100.0)
                .put("effort", "high");
    }

    @Test
    void stubRoundTripAttributesTheLedgerWithoutDoubleReserve() throws Exception {
        startStub(10_000_000L);
        String payload = generateBody(UUID.randomUUID().toString(), new JSONObject());

        HttpResponse<String> created = post("/v1/generate", payload);
        assertEquals(200, created.statusCode());
        String jobId = new JSONObject(created.body()).getString("jobId");

        HttpResponse<String> polled = poll(jobId);
        assertEquals(200, polled.statusCode());
        JSONObject plan = new JSONObject(polled.body()).getJSONObject("plan");
        assertTrue(plan.has("cells"));
        assertTrue(plan.has("canonical"));
        assertTrue(plan.has("demolishVolume"));
        assertTrue(plan.has("warnings"));
        assertEquals(0, budget.usedMicroUsd("session", SESSION));

        // Same requestId + same body replays the jobId; the supplier (and its
        // reservation) runs once, so the ledger does not move.
        HttpResponse<String> replay = post("/v1/generate", payload);
        assertEquals(jobId, new JSONObject(replay.body()).getString("jobId"));
        assertEquals(0, budget.usedMicroUsd("session", SESSION));
    }

    @Test
    void stubTranscribeRoundTripWithLedger() throws Exception {
        startStub(10_000_000L);
        String requestId = UUID.randomUUID().toString();
        String payload =
                new JSONObject()
                        .put("requestId", requestId)
                        .put("session", SESSION.toString())
                        .put("builder", BUILDER.toString())
                        .put("wav", Base64.getEncoder().encodeToString(new byte[] {1, 2, 3}))
                        .put("model", "stub-model")
                        .toString();
        HttpResponse<String> first = post("/v1/transcribe", payload);
        assertEquals(200, first.statusCode());
        String text = new JSONObject(first.body()).getString("text");
        assertTrue(!text.isEmpty());
        assertEquals(
                text, new JSONObject(post("/v1/transcribe", payload).body()).getString("text"));
        assertEquals(0, budget.usedMicroUsd("session", SESSION));
    }

    @Test
    void exhaustedGrantRefusesBeforeAnyUpstreamCall() throws Exception {
        startStub(0);
        HttpResponse<String> refused =
                post("/v1/generate", generateBody(UUID.randomUUID().toString(), new JSONObject()));
        assertEquals(402, refused.statusCode());
        JSONObject body = new JSONObject(refused.body());
        assertEquals("budget", body.getString("phase"));
        assertEquals("exhausted", body.getString("category"));
    }

    @Test
    void realPathAgainstAFakeProviderSettlesTheAuthenticatedCost() throws Exception {
        String answer =
                new JSONObject()
                        .put(
                                "choices",
                                new JSONArray()
                                        .put(
                                                new JSONObject()
                                                        .put(
                                                                "message",
                                                                new JSONObject()
                                                                        .put("content", PLAN))
                                                        .put("finish_reason", "stop")))
                        .put(
                                "usage",
                                new JSONObject()
                                        .put("cost", 0.0001)
                                        .put("prompt_tokens", 10)
                                        .put("completion_tokens", 5))
                        .toString();
        HttpServer fake = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 8);
        fake.createContext(
                "/chat/completions",
                exchange -> {
                    assertEquals(
                            "Bearer fake-key",
                            exchange.getRequestHeaders().getFirst("Authorization"));
                    byte[] body = answer.getBytes(StandardCharsets.UTF_8);
                    exchange.getResponseHeaders().set("Content-Type", "application/json");
                    exchange.sendResponseHeaders(200, body.length);
                    try (OutputStream out = exchange.getResponseBody()) {
                        out.write(body);
                    }
                });
        fake.start();
        try {
            startReal(fake);
            String requestId = UUID.randomUUID().toString();
            String payload = generateBody(requestId, realConfig());

            HttpResponse<String> created = post("/v1/generate", payload);
            assertEquals(200, created.statusCode());
            String jobId = new JSONObject(created.body()).getString("jobId");

            HttpResponse<String> polled = poll(jobId);
            assertEquals(200, polled.statusCode());
            JSONObject plan = new JSONObject(polled.body()).getJSONObject("plan");
            assertEquals(1, plan.getJSONArray("cells").length());
            assertEquals(
                    "minecraft:stone",
                    plan.getJSONArray("cells").getJSONObject(0).getString("block"));
            assertTrue(plan.getString("canonical").contains("\"version\":2"));
            // Authenticated cost 0.0001 USD settles to exactly 100 microUSD.
            assertEquals(100, budget.usedMicroUsd("session", SESSION));

            HttpResponse<String> replay = post("/v1/generate", payload);
            assertEquals(jobId, new JSONObject(replay.body()).getString("jobId"));
            assertEquals(100, budget.usedMicroUsd("session", SESSION));
        } finally {
            fake.stop(0);
        }
    }

    @Test
    void realPathRejectsAnIncompleteConfig() throws Exception {
        HttpServer fake = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 8);
        fake.start();
        try {
            startReal(fake);
            HttpResponse<String> rejected =
                    post(
                            "/v1/generate",
                            generateBody(UUID.randomUUID().toString(), new JSONObject()));
            assertEquals(400, rejected.statusCode());
            assertEquals("invalid_request", new JSONObject(rejected.body()).getString("category"));
        } finally {
            fake.stop(0);
        }
    }

    private static String transcribeBody(String requestId, String model, byte[] wav) {
        return new JSONObject()
                .put("requestId", requestId)
                .put("session", SESSION.toString())
                .put("builder", BUILDER.toString())
                .put("wav", Base64.getEncoder().encodeToString(wav))
                .put("model", model)
                .put("config", realConfig())
                .toString();
    }

    private static byte[] validWav() {
        var wav = ByteBuffer.allocate(46).order(ByteOrder.LITTLE_ENDIAN);
        wav.putInt(0x46464952)
                .putInt(38)
                .putInt(0x45564157)
                .putInt(0x20746d66)
                .putInt(16)
                .putShort((short) 1)
                .putShort((short) 1)
                .putInt(16000)
                .putInt(32000)
                .putShort((short) 2)
                .putShort((short) 16)
                .putInt(0x61746164)
                .putInt(2);
        return wav.array();
    }

    @Test
    void realPathMapsInvalidTranscribeAudioAndModelTo400() throws Exception {
        // Review finding: well-formed base64 that is not 16kHz mono PCM16 (or a
        // malformed model id) escaped as a 500 internal error; validation failures
        // are the caller's fault and must be 400 invalid_request.
        HttpServer fake = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 8);
        fake.start();
        try {
            startReal(fake);
            HttpResponse<String> badWav =
                    post(
                            "/v1/transcribe",
                            transcribeBody(
                                    UUID.randomUUID().toString(),
                                    "openai/whisper-1",
                                    new byte[] {1, 2, 3, 4}));
            assertEquals(400, badWav.statusCode());
            assertEquals("invalid_request", new JSONObject(badWav.body()).getString("category"));

            HttpResponse<String> badModel =
                    post(
                            "/v1/transcribe",
                            transcribeBody(UUID.randomUUID().toString(), "!!!", validWav()));
            assertEquals(400, badModel.statusCode());
            assertEquals("invalid_request", new JSONObject(badModel.body()).getString("category"));
        } finally {
            fake.stop(0);
        }
    }

    @Test
    void realPathMapsUpstreamRefusalToItsStatus() throws Exception {
        HttpServer fake = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 8);
        fake.createContext(
                "/chat/completions",
                exchange -> {
                    byte[] body = "{\"error\":\"bad key\"}".getBytes(StandardCharsets.UTF_8);
                    exchange.sendResponseHeaders(401, body.length);
                    try (OutputStream out = exchange.getResponseBody()) {
                        out.write(body);
                    }
                });
        fake.start();
        try {
            startReal(fake);
            HttpResponse<String> refused =
                    post("/v1/generate", generateBody(UUID.randomUUID().toString(), realConfig()));
            assertEquals(401, refused.statusCode());
            JSONObject body = new JSONObject(refused.body());
            assertEquals("http", body.getString("category"));
            // An unbilled refusal settles nothing: the envelope is released.
            assertEquals(0, budget.usedMicroUsd("session", SESSION));
        } finally {
            fake.stop(0);
        }
    }

    @Test
    void configParsingBounds() {
        SidecarConfig config = SidecarConfig.parse(gson(realConfig()));
        assertEquals("test/model", config.generationModel());
        assertTrue(
                org.junit.jupiter.api.Assertions.assertThrows(
                                IllegalArgumentException.class,
                                () ->
                                        SidecarConfig.parse(
                                                gson(
                                                        new JSONObject()
                                                                .put(
                                                                        "generationModel",
                                                                        "test/model"))))
                        .getMessage()
                        .contains("generationProvider"));
        assertTrue(
                org.junit.jupiter.api.Assertions.assertThrows(
                                IllegalArgumentException.class,
                                () ->
                                        SidecarConfig.parse(
                                                gson(
                                                        realConfig()
                                                                .put(
                                                                        "generationPromptCapUsdPerM",
                                                                        -1.0))))
                        .getMessage()
                        .contains("generationPromptCapUsdPerM"));
    }

    @Test
    void customProviderWithoutBaseUrlRefusesStartup() throws Exception {
        // Review finding: custom without AI_SIDECAR_PROVIDER_BASE_URL silently used
        // GATEWAY_BASE, sending the custom bearer key to the wrong host.
        Path keyFile = root.resolve("provider.key");
        Files.writeString(keyFile, "custom-provider-key-0123456789-_AB");
        java.util.Map<String, String> env =
                java.util.Map.of(
                        "AI_SIDECAR_PROVIDER",
                        "custom",
                        "AI_SIDECAR_PROVIDER_KEY_FILE",
                        keyFile.toString());
        IOException failure =
                org.junit.jupiter.api.Assertions.assertThrows(
                        IOException.class, () -> AiSidecar.selectBackend(env));
        assertTrue(failure.getMessage().contains("AI_SIDECAR_PROVIDER_BASE_URL"));
        // Explicit base URLs still select the custom transport; the OpenRouter
        // default is unchanged.
        assertTrue(
                AiSidecar.selectBackend(
                                java.util.Map.of(
                                        "AI_SIDECAR_PROVIDER",
                                        "custom",
                                        "AI_SIDECAR_PROVIDER_KEY_FILE",
                                        keyFile.toString(),
                                        "AI_SIDECAR_PROVIDER_BASE_URL",
                                        "https://custom.example"))
                        != null);
        assertTrue(
                AiSidecar.selectBackend(
                                java.util.Map.of(
                                        "AI_SIDECAR_PROVIDER",
                                        "openrouter",
                                        "AI_SIDECAR_PROVIDER_KEY_FILE",
                                        keyFile.toString()))
                        != null);
    }

    @Test
    void backendSelectionAndLedgerEnv() throws Exception {
        assertTrue(AiSidecar.selectBackend(java.util.Map.of()) == null);
        assertTrue(
                org.junit.jupiter.api.Assertions.assertThrows(
                                IOException.class,
                                () ->
                                        AiSidecar.selectBackend(
                                                java.util.Map.of("AI_SIDECAR_PROVIDER", "nope")))
                        .getMessage()
                        .contains("unknown"));
        assertTrue(AiSidecar.openLedger(java.util.Map.of()) == null);
    }
}
