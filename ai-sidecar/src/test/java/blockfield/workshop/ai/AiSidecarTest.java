package blockfield.workshop.ai;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.sun.net.httpserver.HttpServer;

import org.json.JSONObject;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.io.IOException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.time.ZoneId;
import java.time.ZoneOffset;
import java.util.Base64;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.atomic.AtomicReference;

/** Contract shapes over a live loopback server: auth, stub endpoints, idempotency. */
class AiSidecarTest {
    private static final String SECRET = "test-secret-0123456789-_ABCDEF";

    private HttpServer server;
    private HttpClient client;
    private String base;
    private AtomicReference<Instant> now;

    @TempDir Path root;

    @BeforeEach
    void start() throws Exception {
        now = new AtomicReference<>(Instant.parse("2026-11-09T00:00:00Z"));
        Clock clock =
                new Clock() {
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
                        return now.get();
                    }
                };
        server = AiSidecar.start("127.0.0.1", 0, SECRET, clock);
        base = "http://127.0.0.1:" + server.getAddress().getPort();
        client = HttpClient.newHttpClient();
    }

    @AfterEach
    void stop() {
        server.stop(0);
    }

    private HttpResponse<String> post(String path, String body, String auth) throws Exception {
        HttpRequest.Builder request =
                HttpRequest.newBuilder(URI.create(base + path))
                        .header("Content-Type", "application/json")
                        .POST(HttpRequest.BodyPublishers.ofString(body, StandardCharsets.UTF_8));
        if (auth != null) {
            request.header("Authorization", auth);
        }
        return client.send(
                request.build(), HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
    }

    private HttpResponse<String> get(String path, String auth) throws Exception {
        HttpRequest.Builder request = HttpRequest.newBuilder(URI.create(base + path)).GET();
        if (auth != null) {
            request.header("Authorization", auth);
        }
        return client.send(
                request.build(), HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
    }

    private static String generateBody(String requestId, String prompt) {
        return new JSONObject()
                .put("requestId", requestId)
                .put("session", UUID.randomUUID().toString())
                .put("builder", UUID.randomUUID().toString())
                .put("prompt", prompt)
                .put("mode", "generate")
                .put("dims", new JSONObject().put("x", 9).put("y", 9).put("z", 9))
                .put("palette", new org.json.JSONArray().put("minecraft:stone"))
                .put("config", new JSONObject())
                .put("snapshot", new JSONObject())
                .toString();
    }

    @Test
    void healthNeedsNoAuth() throws Exception {
        HttpResponse<String> response = get("/health", null);
        assertEquals(200, response.statusCode());
        assertEquals(true, new JSONObject(response.body()).getBoolean("ok"));
    }

    @Test
    void v1RequiresBearer() throws Exception {
        HttpResponse<String> response =
                post("/v1/generate", generateBody(UUID.randomUUID().toString(), "hut"), null);
        assertEquals(401, response.statusCode());
        JSONObject body = new JSONObject(response.body());
        assertEquals("auth", body.getString("phase"));
        assertEquals("http", body.getString("category"));
        assertEquals(401, body.getInt("http"));
    }

    @Test
    void wrongSecretIs401() throws Exception {
        HttpResponse<String> response =
                post(
                        "/v1/generate",
                        generateBody(UUID.randomUUID().toString(), "hut"),
                        "Bearer wrong-secret-value-0123456789-_AB");
        assertEquals(401, response.statusCode());
    }

    @Test
    void generateThenPollReturnsStubPlan() throws Exception {
        String requestId = UUID.randomUUID().toString();
        HttpResponse<String> created =
                post("/v1/generate", generateBody(requestId, "hut"), "Bearer " + SECRET);
        assertEquals(200, created.statusCode());
        String jobId = new JSONObject(created.body()).getString("jobId");
        assertTrue(!jobId.isEmpty());

        HttpResponse<String> polled = get("/v1/jobs/" + jobId, "Bearer " + SECRET);
        assertEquals(200, polled.statusCode());
        JSONObject body = new JSONObject(polled.body());
        assertEquals("done", body.getString("phase"));
        assertTrue(body.getJSONObject("plan").has("cells"));
    }

    @Test
    void sameBodyReplaysSameJobId() throws Exception {
        String requestId = UUID.randomUUID().toString();
        String payload = generateBody(requestId, "hut");
        String first =
                new JSONObject(post("/v1/generate", payload, "Bearer " + SECRET).body())
                        .getString("jobId");
        String second =
                new JSONObject(post("/v1/generate", payload, "Bearer " + SECRET).body())
                        .getString("jobId");
        assertEquals(first, second);
    }

    @Test
    void sameRequestIdDifferentBodyConflicts() throws Exception {
        String requestId = UUID.randomUUID().toString();
        HttpResponse<String> first =
                post("/v1/generate", generateBody(requestId, "hut"), "Bearer " + SECRET);
        assertEquals(200, first.statusCode());
        HttpResponse<String> second =
                post("/v1/generate", generateBody(requestId, "tower"), "Bearer " + SECRET);
        assertEquals(409, second.statusCode());
        JSONObject body = new JSONObject(second.body());
        assertEquals("request", body.getString("phase"));
        assertEquals("conflict", body.getString("category"));
        assertEquals(409, body.getInt("http"));
    }

    @Test
    void invalidRequestIdIs400() throws Exception {
        HttpResponse<String> response =
                post("/v1/generate", generateBody("not-a-uuid", "hut"), "Bearer " + SECRET);
        assertEquals(400, response.statusCode());
        assertEquals("invalid_request", new JSONObject(response.body()).getString("category"));
    }

    @Test
    void unknownJobIs404() throws Exception {
        HttpResponse<String> response = get("/v1/jobs/" + UUID.randomUUID(), "Bearer " + SECRET);
        assertEquals(404, response.statusCode());
    }

    @Test
    void expiredJobIs404() throws Exception {
        String requestId = UUID.randomUUID().toString();
        HttpResponse<String> created =
                post("/v1/generate", generateBody(requestId, "hut"), "Bearer " + SECRET);
        assertEquals(200, created.statusCode());
        String jobId = new JSONObject(created.body()).getString("jobId");
        assertEquals(200, get("/v1/jobs/" + jobId, "Bearer " + SECRET).statusCode());
        now.set(now.get().plus(Duration.ofHours(25)));
        assertEquals(404, get("/v1/jobs/" + jobId, "Bearer " + SECRET).statusCode());
    }

    @Test
    void transcribeFlowWithReplay() throws Exception {
        String requestId = UUID.randomUUID().toString();
        String payload =
                new JSONObject()
                        .put("requestId", requestId)
                        .put("session", UUID.randomUUID().toString())
                        .put("builder", UUID.randomUUID().toString())
                        .put("wav", Base64.getEncoder().encodeToString(new byte[] {1, 2, 3, 4}))
                        .put("model", "stub-model")
                        .toString();
        HttpResponse<String> first = post("/v1/transcribe", payload, "Bearer " + SECRET);
        assertEquals(200, first.statusCode());
        String text = new JSONObject(first.body()).getString("text");
        assertTrue(!text.isEmpty());
        HttpResponse<String> replay = post("/v1/transcribe", payload, "Bearer " + SECRET);
        assertEquals(text, new JSONObject(replay.body()).getString("text"));
    }

    @Test
    void transcribeConflictOnDifferentBody() throws Exception {
        String requestId = UUID.randomUUID().toString();
        JSONObject base =
                new JSONObject()
                        .put("requestId", requestId)
                        .put("session", UUID.randomUUID().toString())
                        .put("builder", UUID.randomUUID().toString())
                        .put("wav", Base64.getEncoder().encodeToString(new byte[] {1}))
                        .put("model", "stub-model");
        assertEquals(200, post("/v1/transcribe", base.toString(), "Bearer " + SECRET).statusCode());
        base.put("wav", Base64.getEncoder().encodeToString(new byte[] {2}));
        HttpResponse<String> conflict = post("/v1/transcribe", base.toString(), "Bearer " + SECRET);
        assertEquals(409, conflict.statusCode());
    }

    @Test
    void customTranscriptionPhaseNeedsItsOwnBaseUrl() throws Exception {
        // Review finding: the per-phase selector inherited the fallback host for a
        // custom phase, so the guard below never fired and the custom bearer went
        // to the fallback host.
        Path keyFile = root.resolve("sidecar-key.txt");
        Files.writeString(keyFile, "test-transcription-key-0123456789-_AB");
        Map<String, String> env =
                Map.of(
                        "AI_SIDECAR_PROVIDER",
                        "openrouter",
                        "AI_SIDECAR_PROVIDER_KEY_FILE",
                        keyFile.toString(),
                        "AI_SIDECAR_TRANSCRIPTION_PROVIDER",
                        "custom");
        assertThrows(IOException.class, () -> AiSidecar.selectSelections(env));
    }

    @Test
    void customPhaseWithExplicitBaseUrlIsSelected() throws Exception {
        Path keyFile = root.resolve("sidecar-key.txt");
        Files.writeString(keyFile, "test-transcription-key-0123456789-_AB");
        Map<String, String> env =
                Map.of(
                        "AI_SIDECAR_PROVIDER",
                        "openrouter",
                        "AI_SIDECAR_PROVIDER_KEY_FILE",
                        keyFile.toString(),
                        "AI_SIDECAR_TRANSCRIPTION_PROVIDER",
                        "custom",
                        "AI_SIDECAR_TRANSCRIPTION_BASE_URL",
                        "https://stt.example.invalid/v1",
                        "AI_SIDECAR_CLEANUP_PROVIDER",
                        "openrouter");
        AiSidecar.Selections selections = AiSidecar.selectSelections(env);
        assertEquals("custom", selections.transcription().provider());
        assertEquals("https://stt.example.invalid/v1", selections.transcription().baseUrl());
    }

    @Test
    void nonCustomPhaseStillInheritsTheFallbackHost() throws Exception {
        Path keyFile = root.resolve("sidecar-key.txt");
        Files.writeString(keyFile, "test-transcription-key-0123456789-_AB");
        Map<String, String> env =
                Map.of(
                        "AI_SIDECAR_PROVIDER",
                        "openrouter",
                        "AI_SIDECAR_PROVIDER_KEY_FILE",
                        keyFile.toString());
        AiSidecar.Selections selections = AiSidecar.selectSelections(env);
        assertEquals("openrouter", selections.transcription().provider());
        assertEquals(ProviderHttp.OPENROUTER_BASE, selections.transcription().baseUrl());
    }
}
