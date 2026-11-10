package blockfield.workshop.ai;

import static org.junit.jupiter.api.Assertions.assertArrayEquals;
import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;

import org.junit.jupiter.api.Test;

import java.nio.charset.StandardCharsets;
import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.time.ZoneId;
import java.time.ZoneOffset;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicReference;

class IdempotencyStoreTest {
    private static final Instant NOW = Instant.parse("2026-11-09T00:00:00Z");

    private static IdempotencyStore.Stored stored(int status, String body) {
        return new IdempotencyStore.Stored(status, body.getBytes(StandardCharsets.UTF_8));
    }

    @Test
    void sameKeySameHashReplaysWithoutRecomputing() {
        IdempotencyStore store = new IdempotencyStore(Clock.fixed(NOW, ZoneOffset.UTC));
        AtomicInteger calls = new AtomicInteger();
        byte[] hash = IdempotencyStore.sha256(new byte[] {1, 2, 3});
        IdempotencyStore.Stored first =
                store.use(
                        "generate:req",
                        hash,
                        () -> {
                            calls.incrementAndGet();
                            return stored(200, "{\"jobId\":\"a\"}");
                        });
        IdempotencyStore.Stored second =
                store.use(
                        "generate:req",
                        hash,
                        () -> {
                            calls.incrementAndGet();
                            return stored(200, "{\"jobId\":\"b\"}");
                        });
        assertEquals(1, calls.get());
        assertArrayEquals(first.body(), second.body());
    }

    @Test
    void sameKeyDifferentHashConflicts() {
        IdempotencyStore store = new IdempotencyStore(Clock.fixed(NOW, ZoneOffset.UTC));
        store.use("generate:req", IdempotencyStore.sha256(new byte[] {1}), () -> stored(200, "{}"));
        assertThrows(
                IdempotencyStore.ConflictException.class,
                () ->
                        store.use(
                                "generate:req",
                                IdempotencyStore.sha256(new byte[] {2}),
                                () -> stored(200, "{}")));
    }

    @Test
    void entriesExpireAfter24h() {
        AtomicReference<Instant> now = new AtomicReference<>(NOW);
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
        IdempotencyStore store = new IdempotencyStore(clock);
        store.use("generate:req", IdempotencyStore.sha256(new byte[] {1}), () -> stored(200, "{}"));
        now.set(NOW.plus(Duration.ofHours(25)));
        // Expired: no conflict, recomputes under the same key.
        AtomicInteger calls = new AtomicInteger();
        IdempotencyStore.Stored fresh =
                store.use(
                        "generate:req",
                        IdempotencyStore.sha256(new byte[] {2}),
                        () -> {
                            calls.incrementAndGet();
                            return stored(200, "{\"jobId\":\"new\"}");
                        });
        assertEquals(1, calls.get());
        assertEquals("{\"jobId\":\"new\"}", new String(fresh.body(), StandardCharsets.UTF_8));
    }

    @Test
    void forgetDropsNonFinalOutcome() {
        IdempotencyStore store = new IdempotencyStore(Clock.fixed(NOW, ZoneOffset.UTC));
        store.use("generate:req", IdempotencyStore.sha256(new byte[] {1}), () -> stored(500, "{}"));
        store.forget("generate:req");
        AtomicInteger calls = new AtomicInteger();
        store.use(
                "generate:req",
                IdempotencyStore.sha256(new byte[] {2}),
                () -> {
                    calls.incrementAndGet();
                    return stored(200, "{}");
                });
        assertEquals(1, calls.get());
    }

    @Test
    void expiredEntriesArePurged() {
        AtomicReference<Instant> now = new AtomicReference<>(NOW);
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
        IdempotencyStore store = new IdempotencyStore(clock);
        store.use("generate:old", IdempotencyStore.sha256(new byte[] {1}), () -> stored(200, "{}"));
        assertEquals(1, store.size());
        now.set(NOW.plus(Duration.ofHours(25)));
        store.use("generate:new", IdempotencyStore.sha256(new byte[] {2}), () -> stored(200, "{}"));
        assertEquals(1, store.size());
    }
}
