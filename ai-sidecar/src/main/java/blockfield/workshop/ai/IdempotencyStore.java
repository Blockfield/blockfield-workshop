package blockfield.workshop.ai;

import java.security.MessageDigest;
import java.time.Clock;
import java.util.Arrays;
import java.util.HashMap;
import java.util.Map;
import java.util.function.Supplier;

/**
 * Idempotency store per contract: requestId dedupes on the SHA-256 of the request body bytes. Same
 * key + same hash replays the stored result; same key + different hash is a 409 conflict. Completed
 * results are retained for 24 h; {@link #forget} drops non-final (internal) outcomes so a same-key
 * retry re-executes instead of replaying them.
 */
final class IdempotencyStore {
    static final long RETAIN_MILLIS = 24L * 3600 * 1000;

    /** Stored HTTP outcome: status code plus the exact body bytes to replay. */
    record Stored(int status, byte[] body) {
        Stored {
            body = body.clone();
        }
    }

    static final class ConflictException extends RuntimeException {
        ConflictException() {
            super("requestId reused with a different body");
        }
    }

    private record Entry(byte[] hash, Stored stored, long storedAt) {}

    private final Map<String, Entry> entries = new HashMap<>();
    private final Clock clock;

    IdempotencyStore(Clock clock) {
        this.clock = clock;
    }

    static byte[] sha256(byte[] body) {
        try {
            return MessageDigest.getInstance("SHA-256").digest(body);
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
    }

    /**
     * Returns the stored outcome for key+hash, computes and stores it when absent, or throws {@link
     * ConflictException} when the key exists with a different hash.
     */
    synchronized Stored use(String key, byte[] hash, Supplier<Stored> compute) {
        long now = clock.millis();
        Entry existing = entries.get(key);
        if (existing != null) {
            if (now - existing.storedAt > RETAIN_MILLIS) {
                entries.remove(key);
            } else if (Arrays.equals(existing.hash, hash)) {
                return existing.stored;
            } else {
                throw new ConflictException();
            }
        }
        Stored stored = compute.get();
        entries.put(key, new Entry(hash.clone(), stored, now));
        return stored;
    }

    synchronized void forget(String key) {
        entries.remove(key);
    }
}
