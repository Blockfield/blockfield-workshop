package blockfield.workshop.ai;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.io.IOException;
import java.math.BigDecimal;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import java.util.UUID;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;

class BudgetTest {
    private static final UUID SESSION = UUID.fromString("a0000000-0000-0000-0000-000000000001");
    private static final UUID SECOND_SESSION =
            UUID.fromString("a0000000-0000-0000-0000-000000000002");
    private static final UUID BUILDER = UUID.fromString("b0000000-0000-0000-0000-000000000001");
    private static final UUID SECOND_BUILDER =
            UUID.fromString("b0000000-0000-0000-0000-000000000002");

    @TempDir Path root;

    private Path policy() {
        return root.resolve("ai-limits.json");
    }

    private Path ledger() {
        return root.resolve("ai-usage.json");
    }

    private String policy(String mode, long revision, long amount) {
        return "{\"schema\":1,\"revision\":"
                + revision
                + ",\"currency\":\"USD\",\"mode\":\""
                + mode
                + "\",\"sessions\":{\""
                + SESSION
                + "\":"
                + amount
                + "},\"builders\":{\""
                + BUILDER
                + "\":"
                + amount
                + "}}";
    }

    private void setPolicy(String mode, long revision, long amount) throws IOException {
        Files.writeString(policy(), policy(mode, revision, amount));
    }

    @Test
    void onlyMissingZeroOrFullyUsedGrantsUseTheTypedBudgetFailure() throws Exception {
        setPolicy("session", 0, 0);
        try (var budget = Budget.open(policy(), ledger())) {
            // A zero grant leaves no remainder, so the first job is refused.
            assertThrows(Budget.BudgetExhausted.class, () -> budget.reserve(SESSION, BUILDER, 1));
            setPolicy("session", 1, 1000);
            assertThrows(
                    Budget.BudgetExhausted.class, () -> budget.reserve(SECOND_SESSION, BUILDER, 1));
            budget.reserve(SESSION, BUILDER, 600);
            // Soft limit (owner, 2026-10-14): the 400 remainder admits a 600 job even
            // though its bound exceeds what is left; usage overshoots the grant.
            budget.reserve(SESSION, BUILDER, 600);
            assertEquals(1200, budget.usedMicroUsd("session", SESSION));
            // Usage now covers the grant, so the next job waits until the grant is raised.
            assertThrows(Budget.BudgetExhausted.class, () -> budget.reserve(SESSION, BUILDER, 1));
            setPolicy("session", 2, 500);
            assertThrows(Budget.BudgetExhausted.class, () -> budget.reserve(SESSION, BUILDER, 1));
            assertEquals(1200, budget.usedMicroUsd("session", SESSION));
            Files.writeString(policy(), "{");
            assertFalse(
                    assertThrows(IOException.class, () -> budget.reserve(SESSION, BUILDER, 1))
                            instanceof Budget.BudgetExhausted);
        }
    }

    @Test
    void contextInclusiveJobReserveSettlesLikeAnyOtherBound() throws Exception {
        // Ledger-level pin: whatever exact bound the provider reserves, the ledger
        // itself is unchanged, so any exact bound must settle.
        long bound = ProviderHttp.jobBound(64000) + ProviderHttp.contextBound(512);
        assertEquals(117658 + 52, bound);
        setPolicy("session", 0, bound);
        try (var budget = Budget.open(policy(), ledger())) {
            var reservation = budget.reserve(SESSION, BUILDER, bound);
            assertEquals(bound, reservation.boundMicroUsd());
            reservation.confirm(new BigDecimal("0.000220"));
            assertEquals(220, budget.usedMicroUsd("session", SESSION));
            // The settled 220 leaves a remainder, so the soft limit admits the next job
            // even though its bound dwarfs what is left.
            var next = budget.reserve(SESSION, BUILDER, bound - 219);
            assertEquals(bound - 219, next.boundMicroUsd());
            next.confirm(BigDecimal.ZERO);
            assertEquals(220, budget.usedMicroUsd("session", SESSION));
            // A grant cut to the settled usage leaves no remainder: new jobs wait.
            setPolicy("session", 1, 220);
            assertThrows(Budget.BudgetExhausted.class, () -> budget.reserve(SESSION, BUILDER, 1));
            assertEquals(220, budget.usedMicroUsd("session", SESSION));
        }
    }

    @Test
    void topUpBeyondTheGrantSucceedsForAnAdmittedJob() throws Exception {
        setPolicy("session", 0, 1000);
        try (var budget = Budget.open(policy(), ledger())) {
            var job = budget.reserve(SESSION, BUILDER, 600);
            assertEquals(600, budget.usedMicroUsd("session", SESSION));
            // Send-time top-ups never refuse for the grant: an admitted job is not cut
            // between its calls, even past the grant or once usage already covers it.
            var extra = budget.reserveTopUp(SESSION, BUILDER, 600);
            assertEquals(1200, budget.usedMicroUsd("session", SESSION));
            var late = budget.reserveTopUp(SESSION, BUILDER, 100);
            assertEquals(1300, budget.usedMicroUsd("session", SESSION));
            late.confirm(BigDecimal.ZERO);
            extra.confirm(BigDecimal.ZERO);
            job.confirm(new BigDecimal("0.000400"));
            assertEquals(400, budget.usedMicroUsd("session", SESSION));
            // Settlement brought usage back under the grant, so new jobs are admitted again.
            budget.reserve(SESSION, BUILDER, 100);
            assertEquals(500, budget.usedMicroUsd("session", SESSION));
        }
    }

    @Test
    void sharedSessionIncludesEveryBuilderAndRestartKeepsUnconfirmedSpend() throws Exception {
        setPolicy("session", 0, 1000);
        try (var budget = Budget.open(policy(), ledger())) {
            budget.reserve(SESSION, BUILDER, 600);
            budget.reserve(SESSION, SECOND_BUILDER, 400);
            assertEquals(1000, budget.usedMicroUsd("session", SESSION));
            assertThrows(IOException.class, () -> budget.reserve(SESSION, BUILDER, 1));
            assertThrows(IOException.class, () -> budget.reserve(SECOND_SESSION, BUILDER, 1));
        }
        try (var resumed = Budget.open(policy(), ledger())) {
            assertEquals(1000, resumed.usedMicroUsd("session", SESSION));
            assertThrows(IOException.class, () -> resumed.reserve(SESSION, BUILDER, 1));
        }
    }

    @Test
    void builderBudgetSurvivesSessionSwitchAndCannotUseAnotherBuildersGrant() throws Exception {
        setPolicy("builder", 1, 1000);
        try (var budget = Budget.open(policy(), ledger())) {
            budget.reserve(SESSION, BUILDER, 600);
            budget.reserve(SECOND_SESSION, BUILDER, 400);
            assertThrows(IOException.class, () -> budget.reserve(SESSION, BUILDER, 1));
            assertThrows(IOException.class, () -> budget.reserve(SESSION, SECOND_BUILDER, 1));
        }
    }

    @Test
    void confirmationRoundsUpAndReleasesOnlyOnce() throws Exception {
        setPolicy("session", 0, 1000);
        try (var budget = Budget.open(policy(), ledger())) {
            var reservation = budget.reserve(SESSION, BUILDER, 1000);
            assertEquals(1000, reservation.boundMicroUsd());
            reservation.confirm(new BigDecimal("0.00000001"));
            assertEquals(1, budget.usedMicroUsd("session", SESSION));
            reservation.confirm(new BigDecimal("0.000001"));
            assertEquals(1, budget.usedMicroUsd("session", SESSION));
            assertThrows(
                    IllegalArgumentException.class,
                    () -> reservation.confirm(new BigDecimal("0.000002")));
            budget.reserve(SESSION, SECOND_BUILDER, 999).confirm(BigDecimal.ZERO);
            assertEquals(1, budget.usedMicroUsd("session", SESSION));
        }
        try (var resumed = Budget.open(policy(), ledger())) {
            assertEquals(1, resumed.usedMicroUsd("session", SESSION));
        }
    }

    @Test
    void invalidOrExcessiveCostsRetainTheWholeReservation() throws Exception {
        setPolicy("session", 0, 1000);
        try (var budget = Budget.open(policy(), ledger())) {
            var reservation = budget.reserve(SESSION, BUILDER, 1000);
            for (BigDecimal invalid : List.of(new BigDecimal("-1"), new BigDecimal("0.0010001")))
                assertThrows(IllegalArgumentException.class, () -> reservation.confirm(invalid));
            assertThrows(IllegalArgumentException.class, () -> reservation.confirm(null));
            assertThrows(
                    ArithmeticException.class, () -> reservation.confirm(new BigDecimal("1e100")));
            assertEquals(1000, budget.usedMicroUsd("session", SESSION));
        }
    }

    @Test
    void overrunPersistsActualCostAndOtherInflightSettlementsKeepTheBlock() throws Exception {
        setPolicy("session", 0, 1000);
        try (var budget = Budget.open(policy(), ledger())) {
            var inflight = budget.reserve(SESSION, BUILDER, 400);
            var exceeded = budget.reserve(SESSION, SECOND_BUILDER, 100);
            assertTrue(exceeded.recordOverrun(new BigDecimal("0.0002")));
            assertEquals(600, budget.usedMicroUsd("session", SESSION));
            assertThrows(Budget.BudgetBlocked.class, inflight::checkPaidCall);
            inflight.confirm(new BigDecimal("0.0001"));
            assertEquals(300, budget.usedMicroUsd("session", SESSION));
            assertThrows(IllegalArgumentException.class, () -> exceeded.confirm(BigDecimal.ZERO));
            setPolicy("builder", 1, 10000);
            assertThrows(
                    Budget.BudgetBlocked.class, () -> budget.reserve(SECOND_SESSION, BUILDER, 1));
        }
        try (var resumed = Budget.open(policy(), ledger())) {
            assertEquals(300, resumed.usedMicroUsd("session", SESSION));
            assertThrows(
                    Budget.BudgetBlocked.class, () -> resumed.reserve(SECOND_SESSION, BUILDER, 1));
        }
    }

    @Test
    void actualSpendAboveTheGrantMaximumStillFitsTheLedger() throws Exception {
        setPolicy("session", 0, 1000);
        try (var budget = Budget.open(policy(), ledger())) {
            assertTrue(
                    budget.reserve(SESSION, BUILDER, 100)
                            .recordOverrun(new BigDecimal("1000000.000001")));
            assertEquals(Budget.MAX_MICRO_USD + 1, budget.usedMicroUsd("session", SESSION));
        }
        try (var resumed = Budget.open(policy(), ledger())) {
            assertEquals(Budget.MAX_MICRO_USD + 1, resumed.usedMicroUsd("session", SESSION));
            assertThrows(Budget.BudgetBlocked.class, () -> resumed.reserve(SESSION, BUILDER, 1));
        }
    }

    @Test
    void unrepresentableCostOrAccountTotalKeepsReservesAndPersistsTheBlock() throws Exception {
        setPolicy("session", 0, 1000);
        for (BigDecimal cost :
                List.of(new BigDecimal("1e2147483647"), BigDecimal.valueOf(Long.MAX_VALUE, 6))) {
            Path usage = root.resolve("usage-" + cost.scale() + ".json");
            try (var budget = Budget.open(policy(), usage)) {
                var exceeded = budget.reserve(SESSION, BUILDER, 100);
                budget.reserve(SESSION, SECOND_BUILDER, 200);
                assertFalse(exceeded.recordOverrun(cost));
                assertEquals(300, budget.usedMicroUsd("session", SESSION));
                assertThrows(Budget.BudgetBlocked.class, () -> budget.reserve(SESSION, BUILDER, 1));
            }
            try (var resumed = Budget.open(policy(), usage)) {
                assertEquals(300, resumed.usedMicroUsd("session", SESSION));
                assertThrows(
                        Budget.BudgetBlocked.class, () -> resumed.reserve(SESSION, BUILDER, 1));
            }
        }
    }

    @Test
    void ledgerBlockIsOptionalButStrictlyTypedAndCannotBeDuplicated() throws Exception {
        setPolicy("session", 0, 1000);
        String legacy = "{\"schema\":1,\"currency\":\"USD\",\"accounts\":{}}";
        Files.writeString(ledger(), legacy);
        try (var budget = Budget.open(policy(), ledger())) {
            budget.reserve(SESSION, BUILDER, 100);
            assertEquals(100, budget.usedMicroUsd("session", SESSION));
        }
        for (String invalid :
                List.of(
                        legacy.replace("\"accounts\"", "\"blocked\":\"true\",\"accounts\""),
                        legacy.replace("\"accounts\"", "\"blocked\":0,\"accounts\""),
                        legacy.replace("\"accounts\"", "\"blocked\":null,\"accounts\""),
                        legacy.replace(
                                "\"accounts\"", "\"blocked\":true,\"blocked\":false,\"accounts\""),
                        legacy.replace("\"accounts\"", "\"block\":true,\"accounts\""))) {
            Files.writeString(ledger(), invalid);
            assertThrows(IOException.class, () -> Budget.open(policy(), ledger()));
        }
        Files.writeString(
                ledger(), legacy.replace("\"accounts\"", "\"blocked\":true,\"accounts\""));
        try (var budget = Budget.open(policy(), ledger())) {
            assertThrows(Budget.BudgetBlocked.class, () -> budget.reserve(SESSION, BUILDER, 1));
        }
    }

    @Test
    void policyChangesApplyToNextReservationWithoutResettingUsage() throws Exception {
        setPolicy("session", 0, 1000);
        try (var budget = Budget.open(policy(), ledger())) {
            var pending = budget.reserve(SESSION, BUILDER, 600);
            setPolicy("session", 1, 500);
            assertThrows(IOException.class, () -> budget.reserve(SESSION, BUILDER, 1));
            pending.confirm(new BigDecimal("0.000400"));
            budget.reserve(SESSION, BUILDER, 100);
            Files.writeString(
                    policy(),
                    "{\"schema\":1,\"revision\":2,\"currency\":\"USD\",\"mode\":\"session\","
                            + "\"sessions\":{},\"builders\":{}}");
            assertThrows(IOException.class, () -> budget.reserve(SESSION, BUILDER, 1));
            setPolicy("session", 3, 1000);
            assertEquals(500, budget.usedMicroUsd("session", SESSION));
            budget.reserve(SESSION, BUILDER, 500);
            setPolicy("builder", 4, 1000);
            budget.reserve(SESSION, BUILDER, 1000);
            setPolicy("session", 5, 1000);
            assertThrows(IOException.class, () -> budget.reserve(SESSION, BUILDER, 1));
        }
    }

    @Test
    void concurrentAdmissionsOvershootByAtMostOneEnvelope() throws Exception {
        setPolicy("session", 0, 1000);
        try (var budget = Budget.open(policy(), ledger());
                var executor = Executors.newFixedThreadPool(2)) {
            CountDownLatch ready = new CountDownLatch(2), start = new CountDownLatch(1);
            var first = executor.submit(() -> reserveTogether(budget, BUILDER, ready, start));
            var second =
                    executor.submit(() -> reserveTogether(budget, SECOND_BUILDER, ready, start));
            assertTrue(ready.await(5, TimeUnit.SECONDS));
            start.countDown();
            // Admission is serialized and each thread still sees a remainder (0, then 600),
            // so both 600 jobs are admitted. The last admission saw usage below the grant,
            // so the overshoot past 1000 stays below its single 600 envelope.
            assertEquals(2, first.get(5, TimeUnit.SECONDS) + second.get(5, TimeUnit.SECONDS));
            assertEquals(1200, budget.usedMicroUsd("session", SESSION));
            assertTrue(budget.usedMicroUsd("session", SESSION) - 1000 < 600);
            // Usage now covers the grant: the next job waits until the grant is raised.
            assertThrows(Budget.BudgetExhausted.class, () -> budget.reserve(SESSION, BUILDER, 1));
        }
    }

    private static int reserveTogether(
            Budget budget, UUID builder, CountDownLatch ready, CountDownLatch start)
            throws Exception {
        ready.countDown();
        if (!start.await(5, TimeUnit.SECONDS))
            throw new IllegalStateException("Reservation timeout");
        try {
            budget.reserve(SESSION, builder, 600);
            return 1;
        } catch (IOException exhausted) {
            return 0;
        }
    }

    @Test
    void onlyOneOpenLedgerOwnerIsAllowedAndClosedReservationsCannotRefund() throws Exception {
        setPolicy("session", 0, 1000);
        Budget.Reservation pending;
        try (var budget = Budget.open(policy(), ledger())) {
            pending = budget.reserve(SESSION, BUILDER, 600);
            assertThrows(RuntimeException.class, () -> Budget.open(policy(), ledger()));
        }
        assertThrows(IllegalStateException.class, () -> pending.confirm(BigDecimal.ZERO));
        try (var resumed = Budget.open(policy(), ledger())) {
            assertEquals(600, resumed.usedMicroUsd("session", SESSION));
        }
    }

    @Test
    void missingInitializedLedgerFailsClosedOnRestartAndWhileOpen() throws Exception {
        setPolicy("session", 0, 1000);
        try (var budget = Budget.open(policy(), ledger())) {
            budget.reserve(SESSION, BUILDER, 600);
            Files.delete(ledger());
            assertThrows(IOException.class, () -> budget.reserve(SESSION, BUILDER, 1));
            assertThrows(IllegalStateException.class, () -> budget.reserve(SESSION, BUILDER, 1));
        }
        assertThrows(IOException.class, () -> Budget.open(policy(), ledger()));
    }

    @Test
    void missingMalformedOrSymlinkPolicyNeverReservesMoney() throws Exception {
        try (var budget = Budget.open(policy(), ledger())) {
            assertThrows(IOException.class, () -> budget.reserve(SESSION, BUILDER, 1));
            Files.writeString(policy(), "{}");
            assertThrows(IOException.class, () -> budget.reserve(SESSION, BUILDER, 1));
            assertEquals(0, budget.usedMicroUsd("session", SESSION));
            Files.delete(policy());
            Path target = root.resolve("target.json");
            Files.writeString(target, policy("session", 0, 1000));
            try {
                Files.createSymbolicLink(policy(), target);
            } catch (UnsupportedOperationException
                    | java.nio.file.FileSystemException unavailable) {
                org.junit.jupiter.api.Assumptions.assumeTrue(
                        false, "Symlinks require OS permission");
            }
            assertThrows(IOException.class, () -> budget.reserve(SESSION, BUILDER, 1));
        }
    }

    @Test
    void corruptExistingLedgerCannotBeReinitialized() throws Exception {
        Files.writeString(
                ledger(), "{\"schema\":1,\"currency\":\"USD\",\"accounts\":{\"owner:any\":1}}");
        assertThrows(IOException.class, () -> Budget.open(policy(), ledger()));
        Files.delete(ledger());
        assertThrows(IOException.class, () -> Budget.open(policy(), ledger()));
    }

    @Test
    void liveLedgerCorruptionOrResetCannotOverwriteReservedSpend() throws Exception {
        setPolicy("session", 0, 1000);
        try (var budget = Budget.open(policy(), ledger())) {
            var pending = budget.reserve(SESSION, BUILDER, 600);
            Files.writeString(ledger(), "{\"schema\":1,\"currency\":\"USD\",\"accounts\":{}}");
            assertThrows(IOException.class, () -> pending.confirm(BigDecimal.ZERO));
            assertThrows(IllegalStateException.class, () -> budget.reserve(SESSION, BUILDER, 1));
        }
    }

    @Test
    void policySchemaIsClosedTypedCanonicalAndBounded() throws Exception {
        String valid = policy("session", 0, 1000);
        assertEquals(1000, Budget.parsePolicy(valid).limit(SESSION, BUILDER));
        for (String invalid :
                List.of(
                        "{}",
                        valid + "{}",
                        valid.replace("\"schema\":1", "\"schema\":\"1\""),
                        valid.replace("\"schema\":1", "\"schema\":true"),
                        valid.replace("\"schema\":1", "\"schema\":2"),
                        valid.replace("\"revision\":0", "\"revision\":-1"),
                        valid.replace("\"revision\":0", "\"revision\":1.5"),
                        valid.replace("\"revision\":0", "\"revision\":9007199254740992"),
                        valid.replace("\"USD\"", "\"EUR\""),
                        valid.replace("\"mode\":\"session\"", "\"mode\":\"owner\""),
                        valid.replace(SESSION.toString(), SESSION.toString().toUpperCase()),
                        valid.replace(SESSION.toString(), "../../account"),
                        valid.replace(":1000", ":-1"),
                        valid.replace(":1000", ":1000000000001"),
                        valid.replace(":1000", ":1.5"),
                        valid.replace(":1000", ":\"1000\""),
                        valid.replace("\"revision\":0", "\"revision\":0,\"revision\":1"),
                        valid.replace("\"schema\":1", "\"schema\":1,\"ownerBypass\":true"),
                        valid.replace(":1000}", ":1000,\"" + SESSION + "\":1000}")))
            assertThrows(IOException.class, () -> Budget.parsePolicy(invalid), invalid);
        assertThrows(IOException.class, () -> Budget.parsePolicy(" ".repeat(65537)));
    }

    @Test
    void excessiveGrantsAndReservationBoundsAreRejected() throws Exception {
        StringBuilder grants = new StringBuilder();
        for (int i = 0; i < 257; i++) {
            if (i != 0) grants.append(',');
            grants.append('"').append(new UUID(0, i)).append("\":1");
        }
        String document =
                "{\"schema\":1,\"revision\":0,\"currency\":\"USD\",\"mode\":\"session\","
                        + "\"sessions\":{"
                        + grants
                        + "},\"builders\":{}}";
        assertThrows(IOException.class, () -> Budget.parsePolicy(document));
        setPolicy("session", 0, 1000);
        try (var budget = Budget.open(policy(), ledger())) {
            assertThrows(
                    IllegalArgumentException.class, () -> budget.reserve(SESSION, BUILDER, -1));
            assertThrows(
                    IllegalArgumentException.class,
                    () -> budget.reserve(SESSION, BUILDER, Budget.MAX_MICRO_USD + 1));
            assertEquals(0, budget.usedMicroUsd("session", SESSION));
        }
    }
}
