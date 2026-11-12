package blockfield.workshop.ai;

import com.google.gson.JsonObject;
import com.google.gson.stream.JsonReader;
import com.google.gson.stream.JsonToken;

import java.io.IOException;
import java.io.StringReader;
import java.math.BigDecimal;
import java.math.RoundingMode;
import java.nio.ByteBuffer;
import java.nio.channels.FileChannel;
import java.nio.channels.FileLock;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.LinkOption;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.nio.file.StandardOpenOption;
import java.util.HashMap;
import java.util.HashSet;
import java.util.Map;
import java.util.Objects;
import java.util.Set;
import java.util.UUID;

/**
 * Durable, non-leasing reservations. Call disk operations on a worker before any paid HTTP call.
 */
final class Budget implements AutoCloseable {
    static final long MAX_MICRO_USD = 1_000_000_000_000L;
    private static final long MAX_REVISION = 9_007_199_254_740_991L;
    private static final int MAX_POLICY_BYTES = 65536;
    private static final int MAX_LEDGER_BYTES = 1_048_576;
    private static final int MAX_ACCOUNTS = 4096;

    static final class BudgetExhausted extends IOException {}

    static final class BudgetBlocked extends IOException {}

    /** The admin policy file is missing, unreadable or invalid. */
    static final class PolicyUnavailable extends IOException {
        PolicyUnavailable(String message, Throwable cause) {
            super(message, cause);
        }
    }

    /** The ledger is at capacity or a ledger write failed. */
    static final class LedgerUnavailable extends IOException {
        LedgerUnavailable(String message, Throwable cause) {
            super(message, cause);
        }
    }

    private record Ledger(Map<String, Long> accounts, boolean blocked) {}

    record Policy(long revision, String mode, Map<UUID, Long> sessions, Map<UUID, Long> builders) {
        Policy {
            if (revision < 0
                    || revision > MAX_REVISION
                    || !Set.of("session", "builder").contains(mode))
                throw new IllegalArgumentException("Invalid AI budget policy");
            sessions = validGrants(sessions);
            builders = validGrants(builders);
        }

        String account(UUID session, UUID builder) {
            return mode + ":" + (mode.equals("session") ? session : builder);
        }

        long limit(UUID session, UUID builder) {
            return (mode.equals("session") ? sessions : builders)
                    .getOrDefault(mode.equals("session") ? session : builder, 0L);
        }

        private static Map<UUID, Long> validGrants(Map<UUID, Long> grants) {
            if (grants == null || grants.size() > 256)
                throw new IllegalArgumentException("Invalid AI budget grants");
            for (var entry : grants.entrySet()) {
                Objects.requireNonNull(entry.getKey());
                Long amount = entry.getValue();
                if (amount == null || amount < 0 || amount > MAX_MICRO_USD)
                    throw new IllegalArgumentException("Invalid AI budget amount");
            }
            return Map.copyOf(grants);
        }
    }

    static final class Reservation {
        private final Budget budget;
        private final String account;
        private final long boundMicroUsd;
        private Long confirmed;
        private boolean overrun;

        private Reservation(Budget budget, String account, long boundMicroUsd) {
            this.budget = budget;
            this.account = account;
            this.boundMicroUsd = boundMicroUsd;
        }

        long boundMicroUsd() {
            return boundMicroUsd;
        }

        void checkPaidCall() throws IOException {
            budget.requirePaidCallsAllowed();
        }

        /** Keeps the reserve when the authenticated cost cannot fit in the ledger. */
        boolean recordOverrun(BigDecimal cumulativeCostUsd) throws IOException {
            return budget.recordOverrun(this, cumulativeCostUsd);
        }

        /** Only an authenticated, complete upstream usage response can release the difference. */
        void confirm(BigDecimal costUsd) throws IOException {
            if (costUsd == null || costUsd.signum() < 0)
                throw new IllegalArgumentException("Invalid AI usage cost");
            long actual =
                    costUsd.movePointRight(6).setScale(0, RoundingMode.CEILING).longValueExact();
            budget.confirm(this, actual);
        }
    }

    private final Path policyFile;
    private final Path ledgerFile;
    private final FileChannel lockChannel;
    private final FileLock lock;
    private Map<String, Long> accounts;
    private boolean blocked;
    private boolean initializedLedger;
    private boolean closed;
    private boolean failed;

    static Budget open(Path policyFile, Path ledgerFile) throws IOException {
        return new Budget(policyFile, ledgerFile);
    }

    private Budget(Path policyFile, Path ledgerFile) throws IOException {
        this.policyFile = Objects.requireNonNull(policyFile).toAbsolutePath().normalize();
        this.ledgerFile = Objects.requireNonNull(ledgerFile).toAbsolutePath().normalize();
        Path directory = this.ledgerFile.getParent();
        Files.createDirectories(directory);
        if (!Files.isDirectory(directory, LinkOption.NOFOLLOW_LINKS))
            throw new IOException("AI usage directory must not be a symlink");
        Path marker = this.ledgerFile.resolveSibling(this.ledgerFile.getFileName() + ".lock");
        boolean initialized = Files.exists(marker, LinkOption.NOFOLLOW_LINKS);
        lockChannel =
                FileChannel.open(
                        marker,
                        StandardOpenOption.CREATE,
                        StandardOpenOption.WRITE,
                        LinkOption.NOFOLLOW_LINKS);
        FileLock acquired;
        try {
            acquired = lockChannel.tryLock();
            if (acquired == null) throw new IOException("AI usage ledger is already owned");
        } catch (IOException | RuntimeException failure) {
            lockChannel.close();
            throw failure;
        }
        lock = acquired;
        try {
            lockChannel.force(true);
            forceDirectory(directory);
            if (Files.exists(this.ledgerFile, LinkOption.NOFOLLOW_LINKS)) {
                Ledger ledger = parseLedger(read(this.ledgerFile, MAX_LEDGER_BYTES));
                accounts = ledger.accounts();
                blocked = ledger.blocked();
                initializedLedger = true;
            } else {
                // The persistent lock marker distinguishes first boot from a lost usage ledger.
                if (initialized) throw new IOException("Initialized AI usage ledger is missing");
                accounts = Map.of();
                write(accounts);
            }
        } catch (IOException | RuntimeException failure) {
            try {
                lock.release();
            } finally {
                lockChannel.close();
            }
            throw failure;
        }
    }

    /** Admits a new paid job while any grant remains (soft limit). */
    synchronized Reservation reserve(UUID session, UUID builder, long boundMicroUsd)
            throws IOException {
        return reserve(session, builder, boundMicroUsd, true);
    }

    /**
     * Send-time top-up for a job that was already admitted: never refused for the grant, so a paid
     * job is not cut off between its calls.
     */
    synchronized Reservation reserveTopUp(UUID session, UUID builder, long boundMicroUsd)
            throws IOException {
        return reserve(session, builder, boundMicroUsd, false);
    }

    private Reservation reserve(UUID session, UUID builder, long boundMicroUsd, boolean admission)
            throws IOException {
        requirePaidCallsAllowed();
        Objects.requireNonNull(session);
        Objects.requireNonNull(builder);
        if (boundMicroUsd < 0 || boundMicroUsd > MAX_MICRO_USD)
            throw new IllegalArgumentException("Invalid AI reservation bound");
        Policy policy;
        try {
            policy = parsePolicy(read(policyFile, MAX_POLICY_BYTES));
        } catch (IOException unavailable) {
            throw new PolicyUnavailable("AI budget policy unavailable", unavailable);
        }
        String account = policy.account(session, builder);
        long limit = policy.limit(session, builder), used = accounts.getOrDefault(account, 0L);
        // Soft limit (owner, 2026-10-14): any remaining grant admits a job even when its
        // conservative bound is larger; settlement brings usage back to the real cost and later
        // jobs wait until the grant is raised. The OpenRouter key limit stays the hard ceiling.
        if (admission && (limit == 0 || used >= limit)) throw new BudgetExhausted();
        if (!accounts.containsKey(account) && accounts.size() >= MAX_ACCOUNTS)
            throw new LedgerUnavailable("AI usage ledger account limit reached", null);
        Map<String, Long> updated = new HashMap<>(accounts);
        updated.put(account, Math.addExact(used, boundMicroUsd));
        try {
            write(updated);
        } catch (IOException unwritable) {
            throw new LedgerUnavailable("AI usage ledger write failed", unwritable);
        }
        return new Reservation(this, account, boundMicroUsd);
    }

    /** One durable write reserves the complete graph before any upstream call can start. */
    synchronized java.util.List<Reservation> reserveCalls(UUID session, UUID builder, long[] bounds)
            throws IOException {
        long total = 0;
        for (long bound : bounds) {
            if (bound < 0) throw new IllegalArgumentException("Invalid call bound");
            total = Math.addExact(total, bound);
        }
        Reservation envelope = reserve(session, builder, total);
        var calls = new java.util.ArrayList<Reservation>();
        for (long bound : bounds) calls.add(new Reservation(this, envelope.account, bound));
        return java.util.List.copyOf(calls);
    }

    private synchronized void confirm(Reservation reservation, long actual) throws IOException {
        requireOpen();
        if (reservation.overrun || actual < 0 || actual > reservation.boundMicroUsd)
            throw new IllegalArgumentException("AI cost exceeded its reservation");
        if (reservation.confirmed != null) {
            if (reservation.confirmed != actual)
                throw new IllegalArgumentException("Conflicting AI usage confirmation");
            return;
        }
        long used = accounts.getOrDefault(reservation.account, 0L);
        long difference = reservation.boundMicroUsd - actual;
        if (difference > used) throw new IOException("AI reservation ledger disagrees");
        Map<String, Long> updated = new HashMap<>(accounts);
        updated.put(reservation.account, used - difference);
        write(updated);
        reservation.confirmed = actual;
    }

    private synchronized boolean recordOverrun(Reservation reservation, BigDecimal costUsd)
            throws IOException {
        requireOpen();
        if (costUsd == null || costUsd.signum() <= 0)
            throw new IllegalArgumentException("Invalid AI overrun cost");
        if (reservation.confirmed != null || reservation.overrun)
            throw new IllegalArgumentException("AI reservation is already settled");
        long used = accounts.getOrDefault(reservation.account, 0L);
        if (used < reservation.boundMicroUsd)
            throw new IOException("AI reservation ledger disagrees");
        // Compare before rescaling: an upstream exponent must not allocate an enormous integer.
        boolean representable = costUsd.compareTo(BigDecimal.valueOf(Long.MAX_VALUE, 6)) <= 0;
        long excess = 0;
        if (representable) {
            long actual =
                    costUsd.movePointRight(6).setScale(0, RoundingMode.CEILING).longValueExact();
            excess = Math.max(0, actual - reservation.boundMicroUsd);
            representable = excess <= Long.MAX_VALUE - used;
        }
        Map<String, Long> updated = new HashMap<>(accounts);
        if (representable) updated.put(reservation.account, used + excess);
        write(updated, true);
        reservation.overrun = true;
        return representable;
    }

    synchronized long usedMicroUsd(String mode, UUID subject) {
        requireOpen();
        if (!Set.of("session", "builder").contains(mode))
            throw new IllegalArgumentException("Invalid AI budget scope");
        return accounts.getOrDefault(mode + ":" + Objects.requireNonNull(subject), 0L);
    }

    static Policy parsePolicy(String json) throws IOException {
        if (json == null || json.getBytes(StandardCharsets.UTF_8).length > MAX_POLICY_BYTES)
            throw new IOException("AI policy outside size bounds");
        try (JsonReader reader = new JsonReader(new StringReader(json))) {
            Set<String> seen = new HashSet<>();
            Long schema = null, revision = null;
            String currency = null, mode = null;
            Map<UUID, Long> sessions = null, builders = null;
            reader.beginObject();
            while (reader.hasNext()) {
                String name = reader.nextName();
                if (!seen.add(name)) throw new IOException("Duplicate AI policy field");
                switch (name) {
                    case "schema" -> schema = number(reader, 1);
                    case "revision" -> revision = number(reader, MAX_REVISION);
                    case "currency" -> currency = string(reader);
                    case "mode" -> mode = string(reader);
                    case "sessions" -> sessions = grants(reader);
                    case "builders" -> builders = grants(reader);
                    default -> throw new IOException("Unknown AI policy field");
                }
            }
            reader.endObject();
            if (reader.peek() != JsonToken.END_DOCUMENT
                    || schema == null
                    || schema != 1
                    || revision == null
                    || !"USD".equals(currency)
                    || mode == null) throw new IOException("Invalid AI budget policy");
            return new Policy(revision, mode, sessions, builders);
        } catch (RuntimeException invalid) {
            throw new IOException("Invalid AI budget policy", invalid);
        }
    }

    private static Map<UUID, Long> grants(JsonReader reader) throws IOException {
        Map<UUID, Long> grants = new HashMap<>();
        reader.beginObject();
        while (reader.hasNext()) {
            UUID id = canonicalUuid(reader.nextName());
            if (grants.size() >= 256
                    || grants.putIfAbsent(id, number(reader, MAX_MICRO_USD)) != null)
                throw new IOException("Invalid AI budget grants");
        }
        reader.endObject();
        return grants;
    }

    private static Ledger parseLedger(String json) throws IOException {
        try (JsonReader reader = new JsonReader(new StringReader(json))) {
            Set<String> seen = new HashSet<>();
            Long schema = null;
            String currency = null;
            boolean blocked = false;
            Map<String, Long> result = null;
            reader.beginObject();
            while (reader.hasNext()) {
                String name = reader.nextName();
                if (!seen.add(name)) throw new IOException("Duplicate AI usage field");
                switch (name) {
                    case "schema" -> schema = number(reader, 1);
                    case "currency" -> currency = string(reader);
                    case "blocked" -> {
                        if (reader.peek() != JsonToken.BOOLEAN)
                            throw new IOException("AI usage block must be boolean");
                        blocked = reader.nextBoolean();
                    }
                    case "accounts" -> {
                        result = new HashMap<>();
                        reader.beginObject();
                        while (reader.hasNext()) {
                            String account = reader.nextName();
                            if (!account.matches("(session|builder):[0-9a-f-]{36}"))
                                throw new IOException("Invalid AI usage account");
                            canonicalUuid(account.substring(account.indexOf(':') + 1));
                            if (result.size() >= MAX_ACCOUNTS
                                    || result.putIfAbsent(account, number(reader, Long.MAX_VALUE))
                                            != null)
                                throw new IOException("Invalid AI usage accounts");
                        }
                        reader.endObject();
                    }
                    default -> throw new IOException("Unknown AI usage field");
                }
            }
            reader.endObject();
            if (reader.peek() != JsonToken.END_DOCUMENT
                    || schema == null
                    || schema != 1
                    || !"USD".equals(currency)
                    || result == null) throw new IOException("Invalid AI usage ledger");
            return new Ledger(Map.copyOf(result), blocked);
        } catch (RuntimeException invalid) {
            throw new IOException("Invalid AI usage ledger", invalid);
        }
    }

    private static UUID canonicalUuid(String value) throws IOException {
        UUID id = UUID.fromString(value);
        if (!id.toString().equals(value))
            throw new IOException("Canonical AI budget UUID required");
        return id;
    }

    private static String string(JsonReader reader) throws IOException {
        if (reader.peek() != JsonToken.STRING) throw new IOException("AI policy string required");
        return reader.nextString();
    }

    private static long number(JsonReader reader, long maximum) throws IOException {
        if (reader.peek() != JsonToken.NUMBER) throw new IOException("AI budget number required");
        long value = new BigDecimal(reader.nextString()).longValueExact();
        if (value < 0 || value > maximum) throw new IOException("AI budget number outside bounds");
        return value;
    }

    private static String read(Path file, int maximum) throws IOException {
        if (!Files.isRegularFile(file, LinkOption.NOFOLLOW_LINKS))
            throw new IOException("AI budget file must be a regular file");
        try (var input = Files.newInputStream(file, LinkOption.NOFOLLOW_LINKS)) {
            byte[] bytes = input.readNBytes(maximum + 1);
            if (bytes.length == 0 || bytes.length > maximum)
                throw new IOException("AI budget file outside size bounds");
            return new String(bytes, StandardCharsets.UTF_8);
        }
    }

    private void write(Map<String, Long> updated) throws IOException {
        write(updated, blocked);
    }

    private void write(Map<String, Long> updated, boolean updatedBlocked) throws IOException {
        Path temporary = ledgerFile.resolveSibling(".ai-usage-" + UUID.randomUUID() + ".tmp");
        try {
            if (initializedLedger
                    && !parseLedger(read(ledgerFile, MAX_LEDGER_BYTES))
                            .equals(new Ledger(accounts, blocked)))
                throw new IOException("Initialized AI usage ledger disagrees with its owner");
            JsonObject document = new JsonObject(), values = new JsonObject();
            document.addProperty("schema", 1);
            document.addProperty("currency", "USD");
            document.addProperty("blocked", updatedBlocked);
            updated.entrySet().stream()
                    .sorted(Map.Entry.comparingByKey())
                    .forEach(entry -> values.addProperty(entry.getKey(), entry.getValue()));
            document.add("accounts", values);
            byte[] json = document.toString().getBytes(StandardCharsets.UTF_8);
            if (json.length > MAX_LEDGER_BYTES) throw new IOException("AI usage ledger too large");
            try (FileChannel channel =
                    FileChannel.open(
                            temporary,
                            StandardOpenOption.CREATE_NEW,
                            StandardOpenOption.WRITE,
                            LinkOption.NOFOLLOW_LINKS)) {
                ByteBuffer bytes = ByteBuffer.wrap(json);
                while (bytes.hasRemaining()) channel.write(bytes);
                channel.force(true);
            }
            Files.move(
                    temporary,
                    ledgerFile,
                    StandardCopyOption.ATOMIC_MOVE,
                    StandardCopyOption.REPLACE_EXISTING);
            forceDirectory(ledgerFile.getParent());
            accounts = Map.copyOf(updated);
            blocked = updatedBlocked;
            initializedLedger = true;
        } catch (IOException | RuntimeException failure) {
            // A rename/fsync failure may have changed disk state: never reuse the old RAM balance.
            failed = true;
            throw failure;
        } finally {
            try {
                Files.deleteIfExists(temporary);
            } catch (IOException failure) {
                failed = true;
                throw failure;
            }
        }
    }

    private static void forceDirectory(Path path) throws IOException {
        try (FileChannel directory = FileChannel.open(path, StandardOpenOption.READ)) {
            directory.force(true);
        }
    }

    private void requireOpen() {
        if (closed || failed) throw new IllegalStateException("AI usage ledger is unavailable");
    }

    private synchronized void requirePaidCallsAllowed() throws BudgetBlocked {
        requireOpen();
        if (blocked) throw new BudgetBlocked();
    }

    @Override
    public synchronized void close() throws IOException {
        if (closed) return;
        closed = true;
        try {
            lock.release();
        } finally {
            lockChannel.close();
        }
    }
}
