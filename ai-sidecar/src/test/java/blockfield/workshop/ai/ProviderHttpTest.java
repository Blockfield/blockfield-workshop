package blockfield.workshop.ai;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import com.sun.net.httpserver.HttpServer;

import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.io.OutputStream;
import java.math.BigDecimal;
import java.net.InetSocketAddress;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.concurrent.atomic.AtomicReference;

/** Provider HTTP choke point against a stub upstream: headers, taxonomy, tariff, bounds. */
class ProviderHttpTest {
    private static final String KEY = "test-provider-key-0123456789-_AB";

    private HttpServer upstream;
    private String upstreamBase;
    private final AtomicReference<String> lastAuth = new AtomicReference<>();
    private final AtomicReference<String> lastReferer = new AtomicReference<>();
    private final AtomicReference<String> lastTitle = new AtomicReference<>();
    private volatile int status = 200;
    private volatile String payload = "{}";
    private volatile long delayMillis;

    @BeforeEach
    void startUpstream() throws IOException {
        upstream = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 8);
        upstream.createContext(
                "/",
                exchange -> {
                    lastAuth.set(exchange.getRequestHeaders().getFirst("Authorization"));
                    lastReferer.set(exchange.getRequestHeaders().getFirst("HTTP-Referer"));
                    lastTitle.set(exchange.getRequestHeaders().getFirst("X-Title"));
                    if (delayMillis > 0) {
                        try {
                            Thread.sleep(delayMillis);
                        } catch (InterruptedException interrupted) {
                            Thread.currentThread().interrupt();
                        }
                    }
                    byte[] body = payload.getBytes(StandardCharsets.UTF_8);
                    exchange.getResponseHeaders().set("Content-Type", "application/json");
                    exchange.sendResponseHeaders(status, body.length);
                    try (OutputStream out = exchange.getResponseBody()) {
                        out.write(body);
                    }
                });
        upstream.start();
        upstreamBase = "http://127.0.0.1:" + upstream.getAddress().getPort();
    }

    @AfterEach
    void stopUpstream() {
        upstream.stop(0);
    }

    private ProviderHttp client() {
        return new ProviderHttp(
                new ProviderHttp.Backend(KEY, ProviderHttp.Transport.openrouter(upstreamBase)),
                ProviderHttp.defaultHttpClient());
    }

    private static JsonObject chat(String effort, int tokens) {
        JsonObject reasoning = new JsonObject();
        reasoning.addProperty("effort", effort);
        JsonObject request = new JsonObject();
        request.addProperty("model", "test/model");
        request.addProperty("max_tokens", tokens);
        request.add("reasoning", reasoning);
        return request;
    }

    @Test
    void postSendsBilledHeadersAndParsesJson() throws Exception {
        payload = "{\"ok\":true}";
        JsonObject response =
                client().post("generation", "/api/v1/chat/completions", chat("high", 64000), 5);
        assertTrue(response.get("ok").getAsBoolean());
        assertEquals("Bearer " + KEY, lastAuth.get());
        assertEquals(ProviderHttp.APP_URL, lastReferer.get());
        assertEquals(ProviderHttp.APP_TITLE, lastTitle.get());
    }

    @Test
    void refusedKeyWithoutReceiptIsUnbilled() {
        status = 401;
        payload = "{\"error\":\"no credit\"}";
        ProviderHttp.Failure failure =
                assertThrows(
                        ProviderHttp.Failure.class,
                        () ->
                                client().post(
                                                "generation",
                                                "/api/v1/chat/completions",
                                                chat("high", 64000),
                                                5));
        assertEquals("http", failure.category);
        assertEquals(401, failure.status);
        assertTrue(failure.unbilled());
    }

    @Test
    void refusedKeyWithUsageCarriesTheReceipt() {
        status = 402;
        payload = "{\"usage\":{\"cost\":0.0001}}";
        ProviderHttp.Failure failure =
                assertThrows(
                        ProviderHttp.Failure.class,
                        () ->
                                client().post(
                                                "generation",
                                                "/api/v1/chat/completions",
                                                chat("high", 64000),
                                                5));
        assertEquals(402, failure.status);
        assertTrue(failure.unbilled());
    }

    @Test
    void serverErrorWithoutReceiptIsBilledFailure() {
        status = 500;
        payload = "{\"error\":\"boom\"}";
        ProviderHttp.Failure failure =
                assertThrows(
                        ProviderHttp.Failure.class,
                        () ->
                                client().post(
                                                "generation",
                                                "/api/v1/chat/completions",
                                                chat("high", 64000),
                                                5));
        assertEquals("http", failure.category);
        assertEquals(500, failure.status);
        assertFalse(failure.unbilled());
    }

    @Test
    void invalidJsonIsClosedAsInvalidJson() {
        payload = "not json";
        ProviderHttp.Failure failure =
                assertThrows(
                        ProviderHttp.Failure.class,
                        () ->
                                client().post(
                                                "generation",
                                                "/api/v1/chat/completions",
                                                chat("high", 64000),
                                                5));
        assertEquals("invalid_json", failure.category);
        assertEquals(200, failure.status);
    }

    @Test
    void slowUpstreamIsATimeout() {
        delayMillis = 3000;
        ProviderHttp.Failure failure =
                assertThrows(
                        ProviderHttp.Failure.class,
                        () ->
                                client().post(
                                                "generation",
                                                "/api/v1/chat/completions",
                                                chat("high", 64000),
                                                1));
        assertEquals("timeout", failure.category);
    }

    @Test
    void oversizedBodyIsATransportFailure() {
        payload = "{\"pad\":\"" + "x".repeat(200_000) + "\"}";
        ProviderHttp.Failure failure =
                assertThrows(
                        ProviderHttp.Failure.class,
                        () ->
                                client().post(
                                                "transcription",
                                                "/audio/transcriptions",
                                                chat("high", 1024),
                                                5));
        assertEquals("transport", failure.category);
    }

    @Test
    void transportsShapePaths() {
        assertEquals(
                "/api/v1/chat/completions",
                ProviderHttp.Transport.forProvider("openrouter", "https://x").chatPath());
        assertTrue(ProviderHttp.Transport.forProvider("openrouter", "https://x").priceApi());
        assertEquals(
                "/chat/completions",
                ProviderHttp.Transport.forProvider("vercel-gateway", "https://x").chatPath());
        assertEquals(
                "/chat/completions",
                ProviderHttp.Transport.forProvider("custom", "https://x").chatPath());
        assertFalse(ProviderHttp.Transport.forProvider("custom", "https://x").priceApi());
    }

    @Test
    void tariffProbeAcceptsEligibleEndpoint() {
        JsonObject pricing = new JsonObject();
        pricing.addProperty("prompt", "0.0000001");
        pricing.addProperty("completion", "0.0000002");
        pricing.addProperty("request", "0");
        pricing.addProperty("image", "0");
        JsonObject endpoint = new JsonObject();
        endpoint.add("pricing", pricing);
        com.google.gson.JsonArray endpoints = new com.google.gson.JsonArray();
        endpoints.add(endpoint);
        JsonObject data = new JsonObject();
        data.addProperty("id", "test/model");
        data.add("endpoints", endpoints);
        JsonObject response = new JsonObject();
        response.add("data", data);
        ProviderHttp.requireGenerationTariff(response, "test/model");
    }

    @Test
    void tariffProbeRejectsOverCapEndpoint() {
        JsonObject pricing = new JsonObject();
        pricing.addProperty("prompt", "1.0");
        pricing.addProperty("completion", "1.0");
        JsonObject endpoint = new JsonObject();
        endpoint.add("pricing", pricing);
        com.google.gson.JsonArray endpoints = new com.google.gson.JsonArray();
        endpoints.add(endpoint);
        JsonObject data = new JsonObject();
        data.addProperty("id", "test/model");
        data.add("endpoints", endpoints);
        JsonObject response = new JsonObject();
        response.add("data", data);
        ProviderHttp.Failure failure =
                assertThrows(
                        ProviderHttp.Failure.class,
                        () -> ProviderHttp.requireGenerationTariff(response, "test/model"));
        assertEquals("tariff", failure.phase);
        assertEquals("unsupported_generation_tariff", failure.category);
    }

    @Test
    void routingPinsOnlyTheDefaultModel() {
        JsonObject pinned = ProviderHttp.routing(ProviderHttp.DEFAULT_GENERATION_MODEL);
        assertEquals("meta", pinned.getAsJsonArray("only").get(0).getAsString());
        assertEquals("0.10", pinned.getAsJsonObject("max_price").get("prompt").getAsString());
        assertFalse(ProviderHttp.routing("other/model").has("only"));
    }

    @Test
    void multipartBodyCarriesFileModelAndLanguage() {
        byte[] body =
                ProviderHttp.multipartBody("boundary", new byte[] {1, 2, 3}, "test/model", "en");
        String text = new String(body, StandardCharsets.UTF_8);
        assertTrue(text.contains("filename=\"audio.wav\""));
        assertTrue(text.contains("name=\"model\""));
        assertTrue(text.contains("test/model"));
        assertTrue(text.contains("name=\"language\""));
        assertTrue(text.endsWith("--boundary--\r\n"));
    }

    @Test
    void backendSelectionFollowsPhase() {
        ProviderHttp.Backend generation =
                new ProviderHttp.Backend("gen", ProviderHttp.Transport.openrouter("https://gen"));
        ProviderHttp.Backend transcription =
                new ProviderHttp.Backend("stt", ProviderHttp.Transport.gateway("https://stt"));
        ProviderHttp.Backends backends =
                new ProviderHttp.Backends(generation, transcription, transcription);
        assertEquals(generation, ProviderHttp.backendFor(backends, "generation"));
        assertEquals(transcription, ProviderHttp.backendFor(backends, "transcription"));
        assertEquals(transcription, ProviderHttp.backendFor(backends, "cleanup"));
    }

    @Test
    void boundMathMatchesTheGameLedgerPins() {
        assertEquals(64000, ProviderHttp.generationTokens(1, 9, 9, 9));
        assertEquals(117658, ProviderHttp.jobBound(64000));
        assertEquals(52, ProviderHttp.contextBound(512));
        assertThrows(
                IllegalArgumentException.class, () -> ProviderHttp.generationTokens(1, 0, 9, 9));
        assertThrows(IllegalArgumentException.class, () -> ProviderHttp.callBound(64));
        assertTrue(ProviderHttp.callBoundFor(600, 64000, 0.10, 0.20) > 0);
    }

    @Test
    void messageContentReadsTheFirstChoice() {
        JsonObject message = new JsonObject();
        message.addProperty("content", "hello");
        JsonObject choice = new JsonObject();
        choice.add("message", message);
        com.google.gson.JsonArray choices = new com.google.gson.JsonArray();
        choices.add(choice);
        JsonObject response = new JsonObject();
        response.add("choices", choices);
        assertEquals("hello", ProviderHttp.messageContent(response));
        assertNull(ProviderHttp.messageContent(new JsonObject()));
    }

    @Test
    void diagnosticClosesEveryCategory() {
        assertEquals(
                "phase=budget category=exhausted",
                ProviderHttp.diagnostic(new Budget.BudgetExhausted()));
        assertEquals(
                "phase=budget category=blocked",
                ProviderHttp.diagnostic(new Budget.BudgetBlocked()));
        ProviderHttp.Failure failure = new ProviderHttp.Failure("generation", "timeout", 0);
        assertEquals("phase=generation category=timeout", ProviderHttp.diagnostic(failure));
        assertEquals(
                "phase=unknown category=internal",
                ProviderHttp.diagnostic(new IOException("upstream text")));
    }

    @Test
    void chatRequestShapesAndRoutes() {
        JsonObject openrouter =
                ProviderHttp.chatRequest(
                        "test/model", "openrouter", 0.10, 0.20, "sys", "user", "high", 64000);
        assertEquals("test/model", openrouter.get("model").getAsString());
        assertEquals(64000, openrouter.get("max_tokens").getAsInt());
        assertEquals("high", openrouter.getAsJsonObject("reasoning").get("effort").getAsString());
        assertTrue(openrouter.has("provider"));
        JsonObject custom =
                ProviderHttp.chatRequest(
                        "test/model", "custom", 0.10, 0.20, "sys", "user", "high", 64000);
        assertFalse(custom.has("provider"));
        assertNotNull(JsonParser.parseString(custom.toString()));
    }

    @Test
    void usageReceiptValidatesTheTokenCap() {
        JsonObject receipt =
                ProviderHttp.usageReceipt(
                        "generation", new JsonObject(), BigDecimal.valueOf(100, 6), 64000);
        assertEquals(64000, receipt.get("requestedMaxTokens").getAsInt());
        assertThrows(
                IllegalArgumentException.class,
                () ->
                        ProviderHttp.usageReceipt(
                                "generation", new JsonObject(), BigDecimal.valueOf(1, 6), 7));
        assertThrows(
                IllegalArgumentException.class,
                () ->
                        ProviderHttp.usageReceipt(
                                "nope", new JsonObject(), BigDecimal.valueOf(1, 6), null));
    }

    @Test
    void httpClientDefaultsToNoRedirects() {
        HttpClient http = ProviderHttp.defaultHttpClient();
        assertEquals(Duration.ofSeconds(5), http.connectTimeout().orElseThrow());
        assertEquals(HttpClient.Redirect.NEVER, http.followRedirects());
        HttpRequest request =
                HttpRequest.newBuilder(URI.create(upstreamBase + "/x"))
                        .timeout(Duration.ofSeconds(5))
                        .GET()
                        .build();
        assertEquals(Duration.ofSeconds(5), request.timeout().orElseThrow());
    }
}
