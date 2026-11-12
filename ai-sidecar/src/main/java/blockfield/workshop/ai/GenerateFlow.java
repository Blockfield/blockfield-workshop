package blockfield.workshop.ai;

import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;

import java.io.IOException;
import java.math.BigDecimal;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.Base64;
import java.util.Collections;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.TimeUnit;
import java.util.logging.Logger;

/**
 * Generation flow moved out of the game JVM (coord#20 U-AI2): tool-loop orchestration, prompts,
 * extend and the STT/transcribe flow. Method bodies are the moved mod code; only the seams changed
 * (config/budget/billing/progress types).
 */
final class GenerateFlow {
    private static final Logger LOGGER = Logger.getLogger(GenerateFlow.class.getName());

    /** Progress tick of a running flow, consumed by the jobs endpoint. */
    record Progress(String phase, int done, int total, long started, long deadline) {}

    private final ProviderHttp generation;
    private final ProviderHttp transcription;
    private final ProviderHttp cleanup;
    private final Path rejectedPlans;
    private final WorkshopAiPlanning.Strategy strategy;

    GenerateFlow(
            ProviderHttp generation,
            ProviderHttp transcription,
            ProviderHttp cleanup,
            Path rejectedPlans,
            WorkshopAiPlanning.Strategy strategy) {
        this.generation = generation;
        this.transcription = transcription;
        this.cleanup = cleanup;
        this.rejectedPlans = rejectedPlans;
        this.strategy = strategy;
    }

    GenerateFlow(ProviderHttp shared) {
        this(
                shared,
                shared,
                shared,
                Path.of("logs", "workshop-ai-rejected"),
                WorkshopAiPlanning::decide);
    }

    /** The account serving a phase: helpers bill to their own provider, never the default. */
    private ProviderHttp peer(String phase) {
        return switch (phase) {
            case "transcription" -> transcription;
            case "cleanup" -> cleanup;
            default -> generation;
        };
    }

    private static void info(String template, Object... args) {
        LOGGER.info(format(template, args));
    }

    private static void warn(String template, Object... args) {
        LOGGER.warning(format(template, args));
    }

    private static String format(String template, Object... args) {
        StringBuilder out = new StringBuilder();
        int arg = 0;
        for (int i = 0; i < template.length(); i++) {
            if (i + 1 < template.length()
                    && template.charAt(i) == '{'
                    && template.charAt(i + 1) == '}') {
                out.append(arg < args.length ? String.valueOf(args[arg++]) : "{}");
                i++;
            } else {
                out.append(template.charAt(i));
            }
        }
        return out.toString();
    }

    /** Game-shaped chat request: the moved provider client framing plus the tool schema. */
    static JsonObject chatRequest(
            SidecarConfig config,
            String system,
            String user,
            String effort,
            int tokens,
            JsonObject format) {
        var request = new JsonObject();
        request.addProperty("model", config.generationModel());
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
        request.add("response_format", format);
        // The routing object is OpenRouter-specific; other providers speak plain OpenAI.
        if (ProviderHttp.OPENROUTER.equals(config.generationProvider()))
            request.add(
                    "provider",
                    ProviderHttp.routing(
                            config.generationPromptCapUsdPerM(),
                            config.generationCompletionCapUsdPerM(),
                            config.generationModel()));
        return request;
    }

    public WorkshopAiPlan generate(
            String prompt,
            SidecarConfig config,
            Set<String> palette,
            int sideX,
            int sideY,
            int sideZ,
            Budget budget,
            UUID session,
            UUID builder)
            throws Exception {
        return generate(
                prompt, "", config, palette, sideX, sideY, sideZ, budget, session, builder, false);
    }

    public WorkshopAiPlan generate(
            String prompt,
            String contextText,
            SidecarConfig config,
            Set<String> palette,
            int sideX,
            int sideY,
            int sideZ,
            Budget budget,
            UUID session,
            UUID builder)
            throws Exception {
        return generate(
                prompt,
                contextText,
                config,
                palette,
                sideX,
                sideY,
                sideZ,
                budget,
                session,
                builder,
                true);
    }

    /**
     * Old clients never advertised v2 plans: v1 generation has no `match` fields and no context.
     * Capable clients get v2 with contextText; only they may accept either parsed version.
     */
    WorkshopAiPlan generate(
            String prompt,
            String contextText,
            SidecarConfig config,
            Set<String> palette,
            int sideX,
            int sideY,
            int sideZ,
            Budget budget,
            UUID session,
            UUID builder,
            boolean v2)
            throws Exception {
        return generate(
                prompt,
                contextText,
                config,
                palette,
                sideX,
                sideY,
                sideZ,
                budget,
                session,
                builder,
                v2 ? 2 : 1);
    }

    WorkshopAiPlan generate(
            String prompt,
            String contextText,
            SidecarConfig config,
            Set<String> palette,
            int sideX,
            int sideY,
            int sideZ,
            Budget budget,
            UUID session,
            UUID builder,
            int version)
            throws Exception {
        return generate(
                prompt,
                contextText,
                config,
                palette,
                sideX,
                sideY,
                sideZ,
                budget,
                session,
                builder,
                version,
                update -> {});
    }

    WorkshopAiPlan generate(
            String prompt,
            String contextText,
            SidecarConfig config,
            Set<String> palette,
            int sideX,
            int sideY,
            int sideZ,
            Budget budget,
            UUID session,
            UUID builder,
            int version,
            java.util.function.Consumer<Progress> progress)
            throws Exception {
        return generate(
                prompt,
                contextText,
                config,
                palette,
                sideX,
                sideY,
                sideZ,
                budget,
                session,
                builder,
                version,
                progress,
                Long.MAX_VALUE);
    }

    WorkshopAiPlan generate(
            String prompt,
            String contextText,
            SidecarConfig config,
            Set<String> palette,
            int sideX,
            int sideY,
            int sideZ,
            Budget budget,
            UUID session,
            UUID builder,
            int version,
            java.util.function.Consumer<Progress> progress,
            long deadline)
            throws Exception {
        boolean v2 = version >= 2;
        // The default model's tariff is already within the reserve; any other site-chosen model
        // must prove every OpenRouter endpoint stays within the owner-configured ceilings before
        // spending. Providers without a price API carry no endpoint catalog, so they skip the
        // probe.
        if (generation.priceApi()
                && !config.generationModel().equals(ProviderHttp.DEFAULT_GENERATION_MODEL))
            ProviderHttp.requireGenerationTariff(
                    generation.get(
                            "tariff", "/api/v1/models/" + config.generationModel() + "/endpoints"),
                    config.generationModel());
        if (version == 3)
            return orchestrate(
                    prompt,
                    contextText,
                    config,
                    palette,
                    sideX,
                    sideY,
                    sideZ,
                    budget,
                    session,
                    builder,
                    progress,
                    deadline,
                    WorkshopAiSandbox.Snapshot.EMPTY);
        // Validates the generation bounds; singleBound sizes the reserve from the request.
        int tokens = ProviderHttp.generationTokens(config.maxBlocks(), sideX, sideY, sideZ);
        long bound =
                singleBound(prompt, contextText, config, palette, sideX, sideY, sideZ, v2 ? 2 : 1);
        try (var billing =
                generation
                        .new Billing(
                                ProviderHttp.reserve(budget, session, builder, bound),
                                budget,
                                session,
                                builder)
                        .caps(
                                config.generationPromptCapUsdPerM(),
                                config.generationCompletionCapUsdPerM())) {
            return generate(
                    prompt,
                    contextText,
                    config,
                    palette,
                    sideX,
                    sideY,
                    sideZ,
                    tokens,
                    billing,
                    version);
        }
    }

    public WorkshopAiPlan generate(
            String prompt,
            String contextText,
            SidecarConfig config,
            Set<String> palette,
            int sideX,
            int sideY,
            int sideZ,
            Budget budget,
            UUID session,
            UUID builder,
            int version,
            java.util.function.Consumer<Progress> progress,
            long deadline,
            WorkshopAiSandbox.Snapshot snapshot)
            throws Exception {
        boolean v2 = version >= 2;
        if (generation.priceApi()
                && !config.generationModel().equals(ProviderHttp.DEFAULT_GENERATION_MODEL))
            ProviderHttp.requireGenerationTariff(
                    generation.get(
                            "tariff", "/api/v1/models/" + config.generationModel() + "/endpoints"),
                    config.generationModel());
        if (version == 3)
            return orchestrate(
                    prompt,
                    contextText,
                    config,
                    palette,
                    sideX,
                    sideY,
                    sideZ,
                    budget,
                    session,
                    builder,
                    progress,
                    deadline,
                    snapshot == null ? WorkshopAiSandbox.Snapshot.EMPTY : snapshot);
        return generate(
                prompt,
                contextText,
                config,
                palette,
                sideX,
                sideY,
                sideZ,
                budget,
                session,
                builder,
                version,
                progress,
                deadline);
    }

    private static long cappedBoundFor(SidecarConfig config, int inputTokens, int maxTokens) {
        return ProviderHttp.callBoundFor(
                inputTokens,
                maxTokens,
                config.generationPromptCapUsdPerM(),
                config.generationCompletionCapUsdPerM());
    }

    private final class CallGraph implements AutoCloseable {
        private final Budget budget;
        private final UUID session, builder;
        private final List<Budget.Reservation> reservations;
        private final boolean[] claimed;
        final java.util.concurrent.atomic.AtomicLong outputTokens =
                new java.util.concurrent.atomic.AtomicLong();
        final java.util.concurrent.atomic.AtomicInteger missingTokenReceipts =
                new java.util.concurrent.atomic.AtomicInteger();
        final UUID id = UUID.randomUUID();
        private final String mode;
        private final double promptCapPerM, completionCapPerM;

        CallGraph(Budget budget, UUID session, UUID builder, long[] bounds, SidecarConfig config)
                throws IOException {
            this.mode = config.planning().mode().key();
            this.promptCapPerM = config.generationPromptCapUsdPerM();
            this.completionCapPerM = config.generationCompletionCapUsdPerM();
            this.budget = budget;
            this.session = session;
            this.builder = builder;
            try {
                reservations = budget.reserveCalls(session, builder, bounds);
            } catch (Budget.PolicyUnavailable failure) {
                throw ProviderHttp.chained("budget", "budget_unavailable", failure);
            } catch (Budget.LedgerUnavailable | IllegalStateException failure) {
                throw ProviderHttp.chained("budget", "budget_ledger_unavailable", failure);
            }
            claimed = new boolean[bounds.length];
        }

        synchronized ProviderHttp.Billing claim(int slot) {
            if (claimed[slot]) throw new IllegalStateException("Call already started");
            claimed[slot] = true;
            var billing =
                    generation
                            .new Billing(
                                    reservations.get(slot),
                                    receipt -> {
                                        var tokens = receipt.get("outputTokens");
                                        if (tokens == null || tokens.isJsonNull())
                                            missingTokenReceipts.incrementAndGet();
                                        else outputTokens.addAndGet(tokens.getAsLong());
                                        receipt.addProperty("job", id.toString());
                                        receipt.addProperty("callSlot", slot);
                                    },
                                    budget,
                                    session,
                                    builder)
                            .caps(promptCapPerM, completionCapPerM);
            billing.mode = mode;
            return billing;
        }

        public synchronized void close() throws IOException {
            boolean interrupted = Thread.interrupted();
            try {
                for (int i = 0; i < claimed.length; i++)
                    if (!claimed[i]) {
                        reservations.get(i).confirm(BigDecimal.ZERO);
                        claimed[i] = true;
                    }
            } finally {
                if (interrupted) Thread.currentThread().interrupt();
            }
        }
    }

    private WorkshopAiPlan orchestrate(
            String prompt,
            String contextText,
            SidecarConfig config,
            Set<String> palette,
            int sx,
            int sy,
            int sz,
            Budget budget,
            UUID session,
            UUID builder,
            java.util.function.Consumer<Progress> progress,
            long outerDeadline,
            WorkshopAiSandbox.Snapshot snapshot)
            throws Exception {
        int budgetSeconds = config.planning().budgetSeconds();
        int seconds = ProviderHttp.generationTimeoutSeconds(budgetSeconds, sx, sy, sz);
        long deadline =
                Math.min(
                        outerDeadline,
                        System.currentTimeMillis()
                                + planningTimeoutMillis(
                                        budgetSeconds, sx, sy, sz, !contextText.isEmpty()));
        timeout(deadline);
        var decision =
                WorkshopAiPlanning.choose(
                        strategy, prompt, sx, sy, sz, config.maxBlocks(), config.planning());
        info(
                "AI strategy class={} mode={} budgetSeconds={} session={} builder={} volume={}"
                    + " cells={} promptChars={} parallel={} parts={} symmetry={} singlePolish={}"
                    + " confidence={} reason={}",
                WorkshopAiPlanning.subjectClass(prompt),
                config.planning().mode().key(),
                seconds,
                session,
                builder,
                (long) sx * sy * sz,
                config.maxBlocks(),
                prompt.length(),
                decision.parallel(),
                decision.parts(),
                decision.symmetry(),
                decision.polishSingle(),
                decision.confidence(),
                decision.reason());
        int n = decision.parts(), polishStart = decision.parallel() ? 2 + n * 2 : 2;
        long[] bounds =
                callBounds(
                        decision,
                        config.planning(),
                        new CallInputs(prompt, contextText, config, palette, sx, sy, sz));
        int loopTurns = WorkshopAiToolLoop.loopTurns(decision, config.planning());
        int loopBase = bounds.length - loopTurns;
        int repairSlot = loopBase - (decision.parallel() ? 2 : 1);
        try (var graph = new CallGraph(budget, session, builder, bounds, config)) {
            LoopPool pool = loopTurns == 0 ? null : new LoopPool(graph, loopBase, loopTurns);
            WorkshopAiSandbox.Scope scope =
                    new WorkshopAiSandbox.Scope(
                            snapshot,
                            palette,
                            config.maxBlocks(),
                            config.maxBlocks(),
                            sx,
                            sy,
                            sz,
                            prompt);
            phase(
                    progress,
                    "validating",
                    0,
                    0,
                    Math.min(deadline, System.currentTimeMillis() + 30_000));
            // AI plans may replace and remove inside the selection; the builder confirms any
            // demolition before apply (WorkshopAi.needsDemolishConfirm).
            double destructive = 1;
            long generationDeadline =
                    Math.min(deadline - 5_000, System.currentTimeMillis() + seconds * 1000L);
            long windowStart = System.currentTimeMillis();
            int retrySlot = retrySlot(decision, config.planning());
            WorkshopAiPlanning.Layout layout = null;
            boolean layoutRepaired = false;
            WorkshopAiPlan plan;
            if (decision.parallel()) {
                long layoutDeadline =
                        Math.min(generationDeadline, System.currentTimeMillis() + 50_000);
                phase(progress, "layout", 0, n, layoutDeadline);
                try (var billing = graph.claim(1)) {
                    String content =
                            chat(
                                    "layout",
                                    layoutSystem(config, palette, sx, sy, sz, decision),
                                    prompt
                                            + (contextText.isEmpty()
                                                    ? ""
                                                    : "\nGLOBAL CONTEXT " + contextText),
                                    config,
                                    config.planning().efforts().layout(),
                                    4096,
                                    timeout(layoutDeadline),
                                    billing,
                                    WorkshopAiSchema.layout(n, config.planning().mirrorEnabled()));
                    try {
                        layout =
                                WorkshopAiPlanning.layout(
                                        content,
                                        sx,
                                        sy,
                                        sz,
                                        config.maxBlocks(),
                                        config.planning().maxParts(),
                                        n);
                    } catch (RuntimeException invalid) {
                        try (var repair = graph.claim(repairSlot + 1)) {
                            String corrected =
                                    chat(
                                            "repair",
                                            layoutSystem(config, palette, sx, sy, sz, decision)
                                                    + " Repair this rejected layout; return the"
                                                    + " corrected complete closed layout only.",
                                            "VALIDATOR "
                                                    + layoutReason(invalid)
                                                    + "\nREJECTED LAYOUT "
                                                    + content,
                                            config,
                                            config.planning().efforts().repair(),
                                            4096,
                                            Math.min(40, timeout(generationDeadline)),
                                            repair,
                                            WorkshopAiSchema.layout(
                                                    n, config.planning().mirrorEnabled()));
                            layout =
                                    WorkshopAiPlanning.layout(
                                            corrected,
                                            sx,
                                            sy,
                                            sz,
                                            config.maxBlocks(),
                                            config.planning().maxParts(),
                                            n);
                            layoutRepaired = true;
                        }
                    }
                    if (!config.planning().mirrorEnabled() && !layout.symmetry().equals("none")) {
                        layout = null;
                        throw new ProviderHttp.Failure("layout", "invalid_symmetry", 0);
                    }
                } catch (RuntimeException invalid) {
                    if (fatal(invalid)) throw invalid;
                    warn(
                            "AI layout fallback: {}",
                            invalid instanceof ProviderHttp.Failure
                                    ? ProviderHttp.diagnostic(invalid)
                                    : "invalid_layout");
                }
                if (layout != null) {
                    long partsDeadline =
                            generationDeadline - Math.min(90_000, Math.max(30_000, seconds * 200L));
                    var partPlans =
                            parts(
                                    prompt,
                                    contextText,
                                    config,
                                    palette,
                                    layout,
                                    graph,
                                    partsDeadline,
                                    destructive,
                                    progress,
                                    snapshot,
                                    pool,
                                    prompt);
                    phase(progress, "merging", n, n, generationDeadline);
                    try {
                        plan =
                                WorkshopAiPlanning.merge(
                                        layout, partPlans, palette, config.maxBlocks(), sx, sy, sz);
                    } catch (RuntimeException invalid) {
                        warn(
                                "AI merge validation failed: {}",
                                WorkshopAiRejectedPlans.reason(invalid));
                        throw new ProviderHttp.Failure("merging", "invalid_merge", 0);
                    }
                    plan = WorkshopAiPlan.withDestructiveScore(plan, destructive);
                } else {
                    phase(progress, "generation", 0, 0, generationDeadline);
                    try (var billing = graph.claim(2)) {
                        plan =
                                recoveredSingle(
                                        prompt + "\nSymmetry hint: " + decision.symmetry(),
                                        contextText,
                                        config,
                                        palette,
                                        sx,
                                        sy,
                                        sz,
                                        64000,
                                        billing,
                                        3,
                                        destructive,
                                        "generation",
                                        config.planning()
                                                .efforts()
                                                .single(WorkshopAiPlanning.subjectClass(prompt)),
                                        timeout(generationDeadline),
                                        graph,
                                        repairSlot,
                                        retrySlot,
                                        windowStart,
                                        generationDeadline,
                                        scope,
                                        WorkshopAiToolLoop.Phase.SINGLE,
                                        pool);
                    }
                }
            } else {
                phase(progress, "generation", 0, 0, generationDeadline);
                try (var billing = graph.claim(1)) {
                    plan =
                            recoveredSingle(
                                    prompt + "\nSymmetry hint: " + decision.symmetry(),
                                    contextText,
                                    config,
                                    palette,
                                    sx,
                                    sy,
                                    sz,
                                    64000,
                                    billing,
                                    3,
                                    destructive,
                                    "generation",
                                    config.planning()
                                            .efforts()
                                            .single(WorkshopAiPlanning.subjectClass(prompt)),
                                    timeout(generationDeadline),
                                    graph,
                                    repairSlot,
                                    retrySlot,
                                    windowStart,
                                    generationDeadline,
                                    scope,
                                    WorkshopAiToolLoop.Phase.SINGLE,
                                    pool);
                }
            }
            if (WorkshopAiPlanning.subjectClass(prompt) == WorkshopAiPlanning.SubjectClass.PERIODIC)
                logPeriodicModules(
                        JsonParser.parseString(plan.canonical())
                                .getAsJsonObject()
                                .getAsJsonArray("operations"),
                        palette,
                        config.maxBlocks(),
                        sx,
                        sy,
                        sz);
            var before =
                    WorkshopAiPlanQuality.report(
                            plan, prompt, layout, sx, sy, sz, config.planning().detail());
            boolean polish =
                    layout != null
                            || WorkshopAiPlanning.polishSingle(
                                    decision,
                                    WorkshopAiPlanQuality.inspect(
                                            plan, prompt, sx, sy, sz, config.planning().detail()));
            if (polish)
                plan =
                        polish(
                                plan,
                                prompt,
                                layout,
                                before,
                                config,
                                palette,
                                sx,
                                sy,
                                sz,
                                graph,
                                polishStart,
                                generationDeadline,
                                seconds,
                                progress,
                                snapshot,
                                pool);
            phase(progress, "validating", n, n, deadline);
            // Re-parse the final graph under unchanged cells/work/instances/side limits.
            var checked =
                    WorkshopAiPlan.parseMerged(
                            plan.canonical(), palette, config.maxBlocks(), sx, sy, sz);
            var after =
                    WorkshopAiPlanQuality.report(
                            plan, prompt, layout, sx, sy, sz, config.planning().detail());
            info(
                    "AI quality job={} before={} after={} polish={} mirrorAxes={} sourceCells={}"
                            + " expandedCells={} outputTokens={} missingTokenReceipts={}",
                    graph.id,
                    metrics(before),
                    metrics(after),
                    polish,
                    mirrorAxes(plan),
                    sourceCells(plan, palette, config.maxBlocks(), sx, sy, sz),
                    checked.cells().size(),
                    graph.outputTokens.get(),
                    graph.missingTokenReceipts.get());
            var warnings = new java.util.ArrayList<>(plan.warnings());
            if (layoutRepaired) warnings.add("plan_repaired");
            if (!after.getAsJsonArray("unconnectedAnchors").isEmpty()) warnings.add("anchors");
            return new WorkshopAiPlan(
                    checked.cells(),
                    checked.canonical(),
                    checked.demolishVolume(),
                    destructive,
                    warnings);
        }
    }

    /** Everything known up front that sizes requests: prompt, contextText, palette, dims. */
    record CallInputs(
            String prompt,
            String contextText,
            SidecarConfig config,
            Set<String> palette,
            int sx,
            int sy,
            int sz) {}

    /**
     * Index of the spare generation slot in the call-bounds array: right after the polish slots,
     * before the repair slots at the tail.
     */
    static int retrySlot(
            WorkshopAiPlanning.Decision decision, WorkshopAiPlanning.Settings settings) {
        int polishStart = decision.parallel() ? 2 + decision.parts() * 2 : 2;
        return polishStart
                + (decision.parallel() || decision.polishSingle() || settings.detail().enabled()
                        ? 2
                        : 0);
    }

    /**
     * Up-front call-graph envelope with right-sized input: fully known requests (single, layout)
     * reserve their exact bytes; not-yet-built ones (parts, polish, the generation retry, repair,
     * loop turns) use a conservative per-phase maximum from the same caps. Each call re-reserves to
     * its exact bound at send time (see ProviderHttp.Billing) and settles the difference on a
     * confirmed receipt, so an unknown-cost timeout keeps only its slot bound here.
     */
    static long[] callBounds(
            WorkshopAiPlanning.Decision decision,
            WorkshopAiPlanning.Settings settings,
            CallInputs in) {
        int retrySlot = retrySlot(decision, settings);
        long[] bounds = new long[retrySlot + 1 + (decision.parallel() ? 2 : 1)];
        // shortcut: slot 0 belonged to the retired guard (ws#53) and stays a zero bound so slot
        // indexes keep their meaning; drop it when the call graph is renumbered.
        bounds[0] = 0;
        bounds[1] =
                decision.parallel()
                        ? cappedBoundFor(in.config(), layoutRequestBytes(in, decision), 4096)
                        : singleSlotBound(in, hintedPrompt(in.prompt, decision), 64000);
        int polishStart = decision.parallel() ? 2 + decision.parts() * 2 : 2;
        for (int i = 2; i < polishStart; i++)
            bounds[i] =
                    cappedBoundFor(in.config(), partRequestBytes(in, decision, settings), 32000);
        for (int i = polishStart; i < retrySlot; i++)
            bounds[i] =
                    cappedBoundFor(in.config(), polishRequestBytes(in, decision, settings), 16000);
        // One spare generation-shaped slot: a free HTTP 200 empty reply (cost 0) retries the
        // main generation call once while the window allows it. Unused it settles to zero.
        bounds[retrySlot] = singleSlotBound(in, hintedPrompt(in.prompt, decision), 64000);
        int repairSlot = bounds.length - (decision.parallel() ? 2 : 1);
        bounds[repairSlot] = cappedBoundFor(in.config(), repairRequestBytes(in), 8000);
        if (decision.parallel())
            bounds[repairSlot + 1] =
                    cappedBoundFor(in.config(), layoutRepairRequestBytes(in, decision), 4096);
        int turns = WorkshopAiToolLoop.loopTurns(decision, settings);
        if (turns == 0) return bounds;
        // The whole tool loop is reserved up front: one 8k turn slot per turn, shared by every
        // looped phase of the job; unused turns settle to zero like any unsent slot.
        var withLoop = new long[bounds.length + turns];
        System.arraycopy(bounds, 0, withLoop, 0, bounds.length);
        long turn = cappedBoundFor(in.config(), loopTurnRequestBytes(in, decision, settings), 8000);
        for (int i = bounds.length; i < withLoop.length; i++) withLoop[i] = turn;
        return withLoop;
    }

    /**
     * Output cap of the final extend answer: the single generation tier, which usage receipts
     * accept for the generation phase; the lower default effort is what saves reasoning.
     */
    static final int EXTEND_TOKENS = 64000;

    /**
     * Smart-fill «extend»: one strict-schema plan call over the exact boundary voxels in the
     * request, unit 9's repair slot for an invalid answer and one seam patch when the free
     * continuity check reports an open boundary. Extend explicitly opts in to the sandbox tool loop
     * (see {@link WorkshopAiToolLoop#useLoop}) whatever {@code toolLoopEnabled} says for fresh
     * generations; its turns are the usual {@code toolLoopMaxTurns} pool. The destructive score
     * comes from the hint alone: without an explicit replace request the snapshot keeps every
     * existing block.
     */
    public WorkshopAiPlan extend(
            String request,
            String hint,
            SidecarConfig config,
            Set<String> palette,
            int sx,
            int sy,
            int sz,
            double destructive,
            Budget budget,
            UUID session,
            UUID builder,
            java.util.function.Consumer<Progress> progress,
            long outerDeadline,
            WorkshopAiSandbox.Snapshot snapshot,
            java.util.function.Function<WorkshopAiPlan, String> seam)
            throws Exception {
        if (generation.priceApi()
                && !config.generationModel().equals(ProviderHttp.DEFAULT_GENERATION_MODEL))
            ProviderHttp.requireGenerationTariff(
                    generation.get(
                            "tariff", "/api/v1/models/" + config.generationModel() + "/endpoints"),
                    config.generationModel());
        long deadline =
                Math.min(
                        outerDeadline,
                        System.currentTimeMillis()
                                + planningTimeoutMillis(settingsBudget(config), sx, sy, sz, true));
        timeout(deadline);
        var settings = config.planning();
        long[] bounds = extendBounds(new CallInputs(request, "", config, palette, sx, sy, sz));
        try (var graph = new CallGraph(budget, session, builder, bounds, config)) {
            var pool = new LoopPool(graph, 4, settings.toolLoopMaxTurns());
            var scope =
                    new WorkshopAiSandbox.Scope(
                            snapshot,
                            palette,
                            config.maxBlocks(),
                            config.maxBlocks(),
                            sx,
                            sy,
                            sz,
                            request);
            long generationDeadline =
                    Math.min(
                            deadline - 5_000,
                            System.currentTimeMillis()
                                    + ProviderHttp.generationTimeoutSeconds(
                                                    settingsBudget(config), sx, sy, sz)
                                            * 1000L);
            phase(progress, "extending_ai", 0, 0, generationDeadline);
            WorkshopAiPlan plan;
            try (var billing = graph.claim(1)) {
                plan =
                        recoveredSingle(
                                request,
                                "",
                                config,
                                palette,
                                sx,
                                sy,
                                sz,
                                EXTEND_TOKENS,
                                billing,
                                3,
                                destructive,
                                "generation",
                                settings.efforts().extend(),
                                timeout(generationDeadline),
                                graph,
                                2,
                                -1,
                                generationDeadline,
                                generationDeadline,
                                scope,
                                WorkshopAiToolLoop.Phase.EXTEND,
                                pool);
            }
            String open = seam.apply(plan);
            String outcome = open == null ? "continuous" : "seam_open";
            if (open != null) {
                phase(progress, "polishing", 0, 0, generationDeadline);
                try (var billing = graph.claim(3)) {
                    String patch =
                            chatLoop(
                                    "polishing",
                                    generationSystemPrompt(config, palette, sx, sy, sz, 3, false)
                                            + seamRules(settings.polishOpCap()),
                                    request
                                            + "\nCANONICAL PLAN "
                                            + plan.canonical()
                                            + "\nSEAM "
                                            + open,
                                    config,
                                    settings.efforts().polish(),
                                    16000,
                                    Math.min(90, timeout(generationDeadline)),
                                    scope,
                                    WorkshopAiToolLoop.Phase.POLISH,
                                    (long) sx * sy * sz,
                                    graph,
                                    pool,
                                    billing,
                                    WorkshopAiSchema.plan(
                                            3, palette, settings.polishOpCap(), true, false));
                    plan =
                            WorkshopAiPlanning.patch(
                                    plan,
                                    patch,
                                    palette,
                                    settings.polishOpCap(),
                                    config.maxBlocks(),
                                    sx,
                                    sy,
                                    sz);
                    outcome = seam.apply(plan) == null ? "seam_repaired" : "seam_open";
                } catch (Exception invalid) {
                    if (fatal(invalid)) throw invalid;
                    warn(
                            "AI extend seam patch rejected: {}",
                            invalid instanceof ProviderHttp.Failure
                                    ? ProviderHttp.diagnostic(invalid)
                                    : "invalid_patch");
                }
            }
            var checked =
                    WorkshopAiPlan.parseMerged(
                            plan.canonical(), palette, config.maxBlocks(), sx, sy, sz);
            info(
                    "AI extend job={} cells={} seam={} destructive={} outputTokens={}"
                            + " missingTokenReceipts={}",
                    graph.id,
                    checked.cells().size(),
                    outcome,
                    destructive,
                    graph.outputTokens.get(),
                    graph.missingTokenReceipts.get());
            var warnings = new java.util.ArrayList<>(plan.warnings());
            if (!outcome.equals("continuous")) warnings.add(outcome);
            return new WorkshopAiPlan(
                    checked.cells(),
                    checked.canonical(),
                    checked.demolishVolume(),
                    destructive,
                    warnings);
        }
    }

    /**
     * Extend call graph sized like a single job (unit 14): the answer's request is fully known;
     * repair, the seam patch and loop turns use their per-phase maxima and top up at send time.
     */
    static long[] extendBounds(CallInputs in) {
        var settings = in.config().planning();
        var bounds = new long[4 + settings.toolLoopMaxTurns()];
        // shortcut: slot 0 belonged to the retired guard (ws#53) and stays a zero bound so slot
        // indexes keep their meaning; drop it when the call graph is renumbered.
        bounds[0] = 0;
        bounds[1] = singleSlotBound(in, in.prompt(), EXTEND_TOKENS);
        bounds[2] = cappedBoundFor(in.config(), repairRequestBytes(in), 8000);
        int opCap = settings.polishOpCap();
        bounds[3] =
                cappedBoundFor(
                        in.config(),
                        ProviderHttp.requestTokens(
                                chatRequest(
                                        in.config(),
                                        singleSystem(in, in.prompt()) + seamRules(opCap),
                                        "x".repeat(MAX_SINGLE_PLAN_BYTES + MAX_REPORT_BYTES),
                                        settings.efforts().polish(),
                                        16000,
                                        WorkshopAiSchema.plan(
                                                3,
                                                in.palette(),
                                                opCap,
                                                true,
                                                settings.mirrorEnabled()))),
                        16000);
        int turnBase =
                ProviderHttp.requestTokens(
                                loopRequest(
                                        in.config(),
                                        loopBaseMessages(
                                                singleSystem(in, in.prompt())
                                                        + WorkshopAiToolLoop.suffix(
                                                                settings.toolLoopMaxTurns()),
                                                singleInput(in.prompt(), in.contextText, 3)),
                                        settings.efforts().extend(),
                                        WorkshopAiToolLoop.TURN_TOKENS,
                                        null,
                                        WorkshopAiToolLoop.definitions(),
                                        false))
                        + settings.toolLoopMaxTurns() * MAX_TURN_HISTORY_BYTES;
        for (int i = 4; i < bounds.length; i++)
            bounds[i] = cappedBoundFor(in.config(), turnBase, WorkshopAiToolLoop.TURN_TOKENS);
        return bounds;
    }

    private static String seamRules(int opCap) {
        return " You are closing the seam of a smart-fill extension. Return ONLY a v3 PATCH"
                + " {version:3,operations:[...]} with <="
                + opCap
                + " template objects that applies AFTER the plan. The SEAM report lists source"
                + " boundary cells whose neighbour inside the target box stays empty: continue the"
                + " source cross-section across the boundary with matching blocks, then keep the"
                + " rhythm. Write only into empty cells; never touch existing blocks.";
    }

    private static String hintedPrompt(String prompt, WorkshopAiPlanning.Decision decision) {
        return prompt + "\nSymmetry hint: " + decision.symmetry();
    }

    /** Exact single-shaped request bytes: system, user and schema are all known up front. */
    private static long singleSlotBound(CallInputs in, String prompt, int tokens) {
        String effort =
                in.config.planning().efforts().single(WorkshopAiPlanning.subjectClass(prompt));
        return cappedBoundFor(
                in.config,
                ProviderHttp.requestTokens(
                        chatRequest(
                                in.config,
                                singleSystem(in, prompt),
                                singleInput(prompt, in.contextText, 3),
                                effort,
                                tokens,
                                WorkshopAiSchema.plan(
                                        3,
                                        in.palette,
                                        128,
                                        false,
                                        in.config.planning().mirrorEnabled()))),
                tokens);
    }

    private static String singleSystem(CallInputs in, String prompt) {
        return generationSystemPrompt(
                in.config, in.palette, in.sx, in.sy, in.sz, 3, !in.contextText.isEmpty());
    }

    private static int layoutRequestBytes(CallInputs in, WorkshopAiPlanning.Decision decision) {
        String user =
                in.prompt + (in.contextText.isEmpty() ? "" : "\nGLOBAL CONTEXT " + in.contextText);
        return ProviderHttp.requestTokens(
                chatRequest(
                        in.config,
                        layoutSystem(in.config, in.palette, in.sx, in.sy, in.sz, decision),
                        user,
                        in.config.planning().efforts().layout(),
                        4096,
                        WorkshopAiSchema.layout(
                                decision.parts(), in.config.planning().mirrorEnabled())));
    }

    /**
     * Part-shaped request maximum. The part system only shrinks numbers (part dims and budget), so
     * the full-volume system covers it; the layout canonical is parse-capped; context is already
     * built. Always carries the periodic suffix single() may add.
     */
    private static int partRequestBytes(
            CallInputs in,
            WorkshopAiPlanning.Decision decision,
            WorkshopAiPlanning.Settings settings) {
        String user =
                partUser(
                        in.prompt,
                        "x".repeat(MAX_LAYOUT_BYTES),
                        "x".repeat(24),
                        "x".repeat(160),
                        "Point[x=127, y=127, z=127]",
                        "128,128,128",
                        in.contextText.isEmpty() ? "" : "\nGLOBAL CONTEXT " + in.contextText);
        return ProviderHttp.requestTokens(
                chatRequest(
                        in.config,
                        generationSystemPrompt(
                                in.config, in.palette, in.sx, in.sy, in.sz, 3, false),
                        singleInput(user, "", 3) + PERIODIC_SUFFIX,
                        settings.partEffort(),
                        32000,
                        // Part phases never emit mirrors, so the schema always takes the
                        // mirror-free variant, exactly like single() builds it.
                        WorkshopAiSchema.plan(3, in.palette, 128, false, false)));
    }

    /**
     * Polish-shaped request maximum. Single-path canonicals are parse-capped at 64K, merged
     * (parallel) ones at 512K; the detail variant is the longer system. Both schema variants are
     * measured and the larger wins.
     */
    private static int polishRequestBytes(
            CallInputs in,
            WorkshopAiPlanning.Decision decision,
            WorkshopAiPlanning.Settings settings) {
        int opCap = Math.min(settings.polishOpCap(), settings.detail().opCap());
        String system =
                generationSystemPrompt(in.config, in.palette, in.sx, in.sy, in.sz, 3, false)
                        + polishSuffix(opCap, true);
        String user =
                in.prompt
                        + "\nLAYOUT "
                        + (decision.parallel() ? "x".repeat(MAX_LAYOUT_BYTES) : "single")
                        + "\nCANONICAL PLAN "
                        + "x".repeat(decision.parallel() ? MAX_PLAN_BYTES : MAX_SINGLE_PLAN_BYTES)
                        + "\nFREE QUALITY AND SILHOUETTES "
                        + "x".repeat(MAX_REPORT_BYTES);
        int schema =
                Math.max(
                        ProviderHttp.requestTokens(
                                WorkshopAiSchema.plan(
                                        3, in.palette, opCap, true, settings.mirrorEnabled())),
                        ProviderHttp.requestTokens(
                                WorkshopAiSchema.plan(
                                        3,
                                        in.palette,
                                        settings.polishOpCap(),
                                        true,
                                        settings.mirrorEnabled())));
        // Measure the full request shape with a placeholder schema of the larger size.
        return ProviderHttp.requestTokens(
                        chatRequest(
                                in.config,
                                system,
                                user,
                                settings.efforts().polish(),
                                16000,
                                WorkshopAiSchema.plan(
                                        3, in.palette, opCap, true, settings.mirrorEnabled())))
                - ProviderHttp.requestTokens(
                        WorkshopAiSchema.plan(3, in.palette, opCap, true, settings.mirrorEnabled()))
                + schema;
    }

    /** Repair-shaped request maximum: validator reason plus one rejected model response. */
    private static int repairRequestBytes(CallInputs in) {
        String system =
                generationSystemPrompt(in.config, in.palette, in.sx, in.sy, in.sz, 3, false)
                        + repairSuffix(in.config.planning().polishOpCap(), true);
        String user =
                "VALIDATOR "
                        + "x".repeat(MAX_REASON_BYTES)
                        + "\nREJECTED PLAN "
                        + "x".repeat(MAX_REJECT_BYTES);
        int schema =
                Math.max(
                        ProviderHttp.requestTokens(
                                WorkshopAiSchema.plan(
                                        3,
                                        in.palette,
                                        in.config.planning().polishOpCap(),
                                        false,
                                        in.config.planning().mirrorEnabled())),
                        ProviderHttp.requestTokens(
                                WorkshopAiSchema.plan(
                                        3,
                                        in.palette,
                                        in.config.planning().polishOpCap(),
                                        false,
                                        false)));
        return ProviderHttp.requestTokens(
                        chatRequest(
                                in.config,
                                system,
                                user,
                                in.config.planning().efforts().repair(),
                                8000,
                                WorkshopAiSchema.plan(
                                        3,
                                        in.palette,
                                        in.config.planning().polishOpCap(),
                                        false,
                                        in.config.planning().mirrorEnabled())))
                - ProviderHttp.requestTokens(
                        WorkshopAiSchema.plan(
                                3,
                                in.palette,
                                in.config.planning().polishOpCap(),
                                false,
                                in.config.planning().mirrorEnabled()))
                + schema;
    }

    /** Layout-repair maximum: the rejected layout fits the 128K non-generation body cap. */
    private static int layoutRepairRequestBytes(
            CallInputs in, WorkshopAiPlanning.Decision decision) {
        String system =
                layoutSystem(in.config, in.palette, in.sx, in.sy, in.sz, decision)
                        + " Repair this rejected layout; return the corrected complete closed"
                        + " layout only.";
        String user =
                "VALIDATOR "
                        + "x".repeat(MAX_REASON_BYTES)
                        + "\nREJECTED LAYOUT "
                        + "x".repeat(MAX_REJECT_BYTES);
        return ProviderHttp.requestTokens(
                chatRequest(
                        in.config,
                        system,
                        user,
                        in.config.planning().efforts().repair(),
                        4096,
                        WorkshopAiSchema.layout(
                                decision.parts(), in.config.planning().mirrorEnabled())));
    }

    /**
     * Loop-turn maximum: the largest single/layout/part base any pooled turn can start from, plus
     * per-turn history growth. Looped polish/repair turns with bigger bases top up the difference
     * at send time.
     */
    private static int loopTurnRequestBytes(
            CallInputs in,
            WorkshopAiPlanning.Decision decision,
            WorkshopAiPlanning.Settings settings) {
        String suffix = WorkshopAiToolLoop.suffix(settings.toolLoopMaxTurns());
        int base =
                ProviderHttp.requestTokens(
                        loopRequest(
                                in.config,
                                loopBaseMessages(
                                        singleSystem(in, hintedPrompt(in.prompt, decision))
                                                + suffix,
                                        singleInput(
                                                        hintedPrompt(in.prompt, decision),
                                                        in.contextText,
                                                        3)
                                                + PERIODIC_SUFFIX),
                                "medium",
                                8000,
                                null,
                                WorkshopAiToolLoop.definitions(),
                                false));
        if (decision.parallel()) {
            String layoutBase =
                    layoutSystem(in.config, in.palette, in.sx, in.sy, in.sz, decision) + suffix;
            String layoutUser =
                    in.prompt
                            + (in.contextText.isEmpty()
                                    ? ""
                                    : "\nGLOBAL CONTEXT " + in.contextText);
            base =
                    Math.max(
                            base,
                            ProviderHttp.requestTokens(
                                    loopRequest(
                                            in.config,
                                            loopBaseMessages(layoutBase, layoutUser),
                                            "medium",
                                            8000,
                                            null,
                                            WorkshopAiToolLoop.definitions(),
                                            false)));
            base =
                    Math.max(
                            base,
                            ProviderHttp.requestTokens(
                                    loopRequest(
                                            in.config,
                                            loopBaseMessages(
                                                    generationSystemPrompt(
                                                                    in.config,
                                                                    in.palette,
                                                                    in.sx,
                                                                    in.sy,
                                                                    in.sz,
                                                                    3,
                                                                    false)
                                                            + suffix,
                                                    partUser(
                                                                    in.prompt,
                                                                    "x".repeat(MAX_LAYOUT_BYTES),
                                                                    "x".repeat(24),
                                                                    "x".repeat(160),
                                                                    "Point[x=127, y=127, z=127]",
                                                                    "128,128,128",
                                                                    in.contextText.isEmpty()
                                                                            ? ""
                                                                            : "\nGLOBAL CONTEXT "
                                                                                    + in
                                                                                            .contextText())
                                                            + PERIODIC_SUFFIX),
                                            "medium",
                                            8000,
                                            null,
                                            WorkshopAiToolLoop.definitions(),
                                            false)));
        }
        return base + settings.toolLoopMaxTurns() * MAX_TURN_HISTORY_BYTES;
    }

    private static com.google.gson.JsonArray loopBaseMessages(String system, String user) {
        var messages = new com.google.gson.JsonArray();
        var first = new com.google.gson.JsonObject();
        first.addProperty("role", "system");
        first.addProperty("content", system);
        messages.add(first);
        var second = new com.google.gson.JsonObject();
        second.addProperty("role", "user");
        second.addProperty("content", user);
        messages.add(second);
        return messages;
    }

    static String singleInput(String prompt, String contextText, int version) {
        return (WorkshopAiPlanning.subjectClass(prompt) == WorkshopAiPlanning.SubjectClass.PERIODIC
                                && version == 3
                        ? prompt + PERIODIC_SUFFIX
                        : prompt)
                + (contextText.isEmpty() ? "" : "\nCONTEXT " + contextText);
    }

    static String partUser(
            String prompt,
            String layoutCanonical,
            String id,
            String role,
            String origin,
            String sizes,
            String contextSuffix) {
        return prompt
                + "\nGLOBAL LAYOUT "
                + layoutCanonical
                + "\nYOUR PART "
                + id
                + " role "
                + role
                + ". Output coordinates LOCAL to your box, origin "
                + origin
                + "; sizes "
                + sizes
                + ". Convert your global anchors to local coordinates, fill them and attach the"
                + " geometry. Do NOT mirror; merge mirrors the fundamental domain."
                + contextSuffix;
    }

    static String polishSuffix(int opCap, boolean detail) {
        return " You are polishing an existing plan. Return ONLY a v3 PATCH"
                + " {version:3,operations:[...]} with <="
                + opCap
                + " total template objects (empty operations is allowed if no change is needed). It"
                + " applies AFTER the existing merged operations, with the same sequential"
                + " match/air carving semantics. Fix joins, all unconnected anchors and detached"
                + " fragments, connect mirror seams, then add proportionate details."
                + (detail
                        ? " FLATNESS detail pass: add 1-2 cell facade depth, proud"
                              + " pilasters/columns, recessed windows with sills/lintels, plinth"
                              + " and cornice/eaves overhang. Add corner/opening trims and"
                              + " pitched/hipped/stepped stair/slab roofs; texture large walls with"
                              + " mixes. Preserve the requested silhouette and walkable entrances."
                              + " Use the front/side/top silhouettes and flatness metrics to target"
                              + " the flattest surfaces. "
                        : "")
                + " Never replace the whole plan or shrink its footprint. Respect the TOTAL unique"
                + " cells/work/instance limits after patching. Asymmetric details may be appended."
                + " Silhouettes are orthographic downsampled colour-letter occupancy, '.' empty."
                + " Missing small features in silhouettes may still exist in canonical geometry. Do"
                + " not pay for a mirrored half again.";
    }

    static String repairSuffix(int opCap, boolean noMirror) {
        return " You are repairing a rejected plan. Return ONLY the corrected v3 operations using"
                + " the polish patch schema {version:3,operations:[...]}, at most "
                + opCap
                + " template objects. This replaces the invalid plan, which cannot be safely"
                + " applied. Preserve its design and fix the exact validator errors."
                + (noMirror ? " Do NOT mirror this part." : "");
    }

    /**
     * Up-front reserve for the legacy single-call path: the exact request. Every input is already
     * known here, so this is exact, not a maximum.
     */
    static long singleBound(
            String prompt,
            String contextText,
            SidecarConfig config,
            Set<String> palette,
            int sx,
            int sy,
            int sz,
            int version) {
        String effort = config.planning().efforts().single(WorkshopAiPlanning.subjectClass(prompt));
        int tokens = ProviderHttp.generationTokens(config.maxBlocks(), sx, sy, sz);
        return cappedBoundFor(
                config,
                ProviderHttp.requestTokens(
                        chatRequest(
                                config,
                                generationSystemPrompt(
                                        config,
                                        palette,
                                        sx,
                                        sy,
                                        sz,
                                        version,
                                        !contextText.isEmpty()),
                                singleInput(prompt, contextText, version),
                                effort,
                                tokens,
                                WorkshopAiSchema.plan(
                                        version,
                                        palette,
                                        128,
                                        false,
                                        config.planning().mirrorEnabled()))),
                tokens);
    }

    static String layoutSystem(
            SidecarConfig config,
            Set<String> palette,
            int sx,
            int sy,
            int sz,
            WorkshopAiPlanning.Decision d) {
        return languageSystemPrompt(palette, 3)
                + " Return ONLY a closed JSON layout:"
                + " {version:1,symmetry:none|x|z|xz,style:string,parts:[{id:lowercase_id,role:string,from:[x,y,z],to:[x,y,z],priority:integer,budget:integer}],anchors:[{parts:[id,id],cells:[[x,y,z],[x,y,z]]}]}."
                + " Exactly "
                + d.parts()
                + " parts, priority permutation 0.."
                + (d.parts() - 1)
                + " (later wins overlaps). Boxes inclusive inside volume "
                + sx
                + ","
                + sy
                + ","
                + sz
                + ". Symmetry hint "
                + d.symmetry()
                + "; if symmetric, ALL source boxes must be in the fundamental domain x<ceil(sx/2)"
                + " and/or z<ceil(sz/2)."
                + mirrorExtents(sx, sz)
                + " Sum of part cell budgets times mirror copies (1/2/4) <= "
                + config.maxBlocks()
                + "; include air cuts. Parts may overlap to join. Each anchor specifies two"
                + " same/face-adjacent cells, first in the first part, second in the second part."
                + " Use anchors for neck/body, limbs, wing/body and tower/wall connections. Give"
                + " shared materials, scale and style in style."
                + (config.planning().mirrorEnabled()
                        ? ""
                        : " Mirror is disabled: symmetry MUST be none.")
                + " Each part must have enough volume and cells for its role; avoid detached"
                + " decoration. Each part uses <=128 v3 templates. Layout <=16384 chars; style"
                + " <=1024, roles <=160, <=24 anchor pairs. Palette "
                + palette.stream().sorted().toList();
    }

    private record PartResult(String id, WorkshopAiPlan plan) {}

    private java.util.Map<String, WorkshopAiPlan> parts(
            String prompt,
            String contextText,
            SidecarConfig config,
            Set<String> palette,
            WorkshopAiPlanning.Layout layout,
            CallGraph graph,
            long deadline,
            double destructive,
            java.util.function.Consumer<Progress> progress,
            WorkshopAiSandbox.Snapshot snapshot,
            LoopPool pool,
            String jobPrompt)
            throws Exception {
        int concurrency = Math.min(config.planning().concurrency(), layout.parts().size());
        int waves = (layout.parts().size() + concurrency - 1) / concurrency;
        int perAttempt = Math.max(1, (timeout(deadline) - 5 * waves * 2) / (waves * 2));
        long started = System.currentTimeMillis();
        progress.accept(new Progress("parts", 0, layout.parts().size(), started, deadline));
        var executor =
                java.util.concurrent.Executors.newFixedThreadPool(
                        concurrency,
                        r -> {
                            var t = new Thread(r, "workshop-ai-part");
                            t.setDaemon(true);
                            return t;
                        });
        var completion = new java.util.concurrent.ExecutorCompletionService<PartResult>(executor);
        var cancelled = new java.util.concurrent.atomic.AtomicBoolean();
        List<java.util.concurrent.Future<PartResult>> pending = new java.util.ArrayList<>();
        try {
            for (int index = 0; index < layout.parts().size(); index++) {
                final int slot = 2 + index * 2;
                var part = layout.parts().get(index);
                pending.add(
                        completion.submit(
                                () -> {
                                    Exception last = null;
                                    for (int attempt = 0; attempt < 2; attempt++) {
                                        if (cancelled.get()
                                                || Thread.currentThread().isInterrupted())
                                            throw new InterruptedException();
                                        int callTimeout = Math.min(perAttempt, timeout(deadline));
                                        try (var billing = graph.claim(slot + attempt)) {
                                            var local = config.withMaxBlocks(part.budget());
                                            String request =
                                                    partUser(
                                                            prompt,
                                                            layout.canonical(),
                                                            part.id(),
                                                            part.role(),
                                                            part.from().toString(),
                                                            part.sx() + "," + part.sy() + ","
                                                                    + part.sz(),
                                                            contextText.isEmpty()
                                                                    ? ""
                                                                    : "\nGLOBAL CONTEXT "
                                                                            + contextText);
                                            WorkshopAiPlan plan;
                                            try {
                                                var partScope =
                                                        snapshot == null
                                                                ? null
                                                                : new WorkshopAiSandbox.Scope(
                                                                        snapshot,
                                                                        palette,
                                                                        part.budget(),
                                                                        config.maxBlocks(),
                                                                        part.sx(),
                                                                        part.sy(),
                                                                        part.sz(),
                                                                        jobPrompt,
                                                                        part.from().x(),
                                                                        part.from().y(),
                                                                        part.from().z(),
                                                                        part.sx(),
                                                                        part.sy(),
                                                                        part.sz());
                                                plan =
                                                        single(
                                                                request,
                                                                "",
                                                                local,
                                                                palette,
                                                                part.sx(),
                                                                part.sy(),
                                                                part.sz(),
                                                                32000,
                                                                billing,
                                                                3,
                                                                destructive,
                                                                "parts",
                                                                config.planning().partEffort(),
                                                                callTimeout,
                                                                config.maxBlocks(),
                                                                partScope,
                                                                WorkshopAiToolLoop.Phase.PART,
                                                                (long) part.sx()
                                                                        * part.sy()
                                                                        * part.sz(),
                                                                graph,
                                                                pool);
                                            } catch (InvalidPlan invalid) {
                                                if (attempt != 0) throw invalid;
                                                try (var repair = graph.claim(slot + 1)) {
                                                    plan =
                                                            repairPlan(
                                                                    invalid,
                                                                    local,
                                                                    palette,
                                                                    part.sx(),
                                                                    part.sy(),
                                                                    part.sz(),
                                                                    repair,
                                                                    Math.min(60, timeout(deadline)),
                                                                    destructive,
                                                                    snapshot == null
                                                                            ? null
                                                                            : new WorkshopAiSandbox
                                                                                    .Scope(
                                                                                    snapshot,
                                                                                    palette,
                                                                                    part.budget(),
                                                                                    config
                                                                                            .maxBlocks(),
                                                                                    part.sx(),
                                                                                    part.sy(),
                                                                                    part.sz(),
                                                                                    jobPrompt,
                                                                                    part.from().x(),
                                                                                    part.from().y(),
                                                                                    part.from().z(),
                                                                                    part.sx(),
                                                                                    part.sy(),
                                                                                    part.sz()),
                                                                    graph,
                                                                    pool);
                                                }
                                            }
                                            forbidPartMirrors(
                                                    JsonParser.parseString(plan.canonical())
                                                            .getAsJsonObject()
                                                            .getAsJsonArray("operations"));
                                            if (JsonParser.parseString(plan.canonical())
                                                            .getAsJsonObject()
                                                            .get("version")
                                                            .getAsInt()
                                                    != 3)
                                                throw new ProviderHttp.Failure(
                                                        "parts", "invalid_plan", 0);
                                            return new PartResult(part.id(), plan);
                                        } catch (Exception failure) {
                                            if (fatal(failure)) throw failure;
                                            last = failure;
                                            if (failure instanceof InvalidPlan) break;
                                        }
                                    }
                                    throw new ProviderHttp.Failure(
                                            "parts",
                                            last instanceof ProviderHttp.Failure f
                                                            && f.category.equals("timeout")
                                                    ? "part_timeout"
                                                    : "part_failed",
                                            0);
                                }));
            }
            var plans = new java.util.LinkedHashMap<String, WorkshopAiPlan>();
            for (int done = 0; done < layout.parts().size(); done++) {
                long left = deadline - System.currentTimeMillis();
                var finished = left > 0 ? completion.poll(left, TimeUnit.MILLISECONDS) : null;
                if (finished == null) throw new ProviderHttp.Failure("parts", "part_timeout", 0);
                PartResult result;
                try {
                    result = finished.get();
                } catch (ExecutionException failure) {
                    if (failure.getCause() instanceof Exception cause) throw cause;
                    throw new ProviderHttp.Failure("parts", "part_failed", 0);
                }
                plans.put(result.id(), result.plan());
                progress.accept(
                        new Progress("parts", done + 1, layout.parts().size(), started, deadline));
            }
            return plans;
        } finally {
            // Interrupting a sibling during fsync poisons the shared ledger. Stop unsent work;
            // sent HTTP calls retain their own deadline and settle independently.
            cancelled.set(true);
            pending.forEach(f -> f.cancel(false));
            executor.shutdown();
            if (!executor.awaitTermination(10, TimeUnit.SECONDS))
                warn(
                        "AI part cancellation deadline reached; unsettled calls retain their"
                                + " reservations");
        }
    }

    private static void forbidPartMirrors(JsonArray operations) {
        for (var element : operations) {
            var o = element.getAsJsonObject();
            if (o.get("type").getAsString().equals("mirror"))
                throw new ProviderHttp.Failure("parts", "invalid_plan", 0);
            if (o.has("operations")) forbidPartMirrors(o.getAsJsonArray("operations"));
        }
    }

    private WorkshopAiPlan polish(
            WorkshopAiPlan base,
            String prompt,
            WorkshopAiPlanning.Layout layout,
            JsonObject report,
            SidecarConfig config,
            Set<String> palette,
            int sx,
            int sy,
            int sz,
            CallGraph graph,
            int slot,
            long deadline,
            int generationSeconds,
            java.util.function.Consumer<Progress> progress,
            WorkshopAiSandbox.Snapshot snapshot,
            LoopPool pool)
            throws Exception {
        phase(progress, "polishing", 0, 0, deadline);
        boolean detail =
                report.getAsJsonArray("warnings").asList().stream()
                        .anyMatch(v -> v.getAsString().equals("flatness"));
        int opCap =
                detail
                        ? Math.min(
                                config.planning().polishOpCap(), config.planning().detail().opCap())
                        : config.planning().polishOpCap();
        int attempts = detail ? 1 : 2;
        info(
                "AI detailPass={} opCap={} attempts={} flatness={}",
                detail,
                opCap,
                attempts,
                report.get("flatness"));
        String system =
                generationSystemPrompt(config, palette, sx, sy, sz, 3, false)
                        + polishSuffix(opCap, detail);
        String input =
                prompt
                        + "\nLAYOUT "
                        + (layout == null ? "single" : layout.canonical())
                        + "\nCANONICAL PLAN "
                        + base.canonical()
                        + "\nFREE QUALITY AND SILHOUETTES "
                        + report;
        for (int attempt = 0; attempt < attempts; attempt++) {
            try {
                // The detail pass has one attempt and a sub-cap of the generation window: a
                // hung call with a plan already in hand falls back instead of waiting out
                // the whole budget. Regular polish keeps splitting what is left.
                int left = timeout(deadline);
                int seconds =
                        detail
                                ? detailPolishSeconds(generationSeconds, left)
                                : Math.max(1, left / (attempts - attempt));
                try (var billing = graph.claim(slot + attempt)) {
                    String patch =
                            chatLoop(
                                    "polishing",
                                    system,
                                    input,
                                    config,
                                    config.planning().efforts().polish(),
                                    16000,
                                    seconds,
                                    snapshot == null
                                            ? null
                                            : new WorkshopAiSandbox.Scope(
                                                    snapshot,
                                                    palette,
                                                    config.maxBlocks(),
                                                    config.maxBlocks(),
                                                    sx,
                                                    sy,
                                                    sz,
                                                    prompt),
                                    WorkshopAiToolLoop.Phase.POLISH,
                                    (long) sx * sy * sz,
                                    graph,
                                    pool,
                                    billing,
                                    WorkshopAiSchema.plan(
                                            3,
                                            palette,
                                            opCap,
                                            true,
                                            config.planning().mirrorEnabled()));
                    if (!config.planning().mirrorEnabled())
                        forbidPartMirrors(
                                com.lunares.blockfield.workshop.WorkshopGeometry.object(
                                                patch, 65536)
                                        .getAsJsonArray("operations"));
                    return WorkshopAiPlanning.patch(
                            base, patch, palette, opCap, config.maxBlocks(), sx, sy, sz);
                }
            } catch (Exception invalid) {
                if (fatal(invalid)) throw invalid;
                warn(
                        "AI polish attempt={} fallback={} category={}",
                        attempt + 1,
                        attempt == attempts - 1,
                        invalid instanceof ProviderHttp.Failure
                                ? ProviderHttp.diagnostic(invalid)
                                : "invalid_patch");
            }
        }
        var warnings = new java.util.ArrayList<>(base.warnings());
        warnings.add("polish_unavailable");
        return new WorkshopAiPlan(
                base.cells(),
                base.canonical(),
                base.demolishVolume(),
                base.destructiveScore(),
                warnings);
    }

    static JsonObject loopRequest(
            SidecarConfig config,
            JsonArray messages,
            String effort,
            int tokens,
            JsonObject format,
            JsonArray tools,
            boolean last) {
        var request = new JsonObject();
        request.addProperty("model", config.generationModel());
        request.addProperty("max_tokens", tokens);
        request.addProperty("stream", false);
        var reasoning = new JsonObject();
        reasoning.addProperty("effort", effort);
        request.add("reasoning", reasoning);
        request.add("messages", messages);
        if (tools != null) {
            request.add("tools", tools);
            request.addProperty("tool_choice", last ? "none" : "auto");
        }
        if (format != null) request.add("response_format", format);
        if (ProviderHttp.OPENROUTER.equals(config.generationProvider()))
            request.add(
                    "provider",
                    ProviderHttp.routing(
                            config.generationPromptCapUsdPerM(),
                            config.generationCompletionCapUsdPerM(),
                            config.generationModel()));
        return request;
    }

    private String chat(
            String phase,
            String system,
            String user,
            SidecarConfig config,
            String effort,
            int tokens,
            int timeout,
            ProviderHttp.Billing billing,
            JsonObject format)
            throws Exception {
        JsonObject request = chatRequest(config, system, user, effort, tokens, format);
        JsonObject response = billing.post(phase, peer(phase).chatPath(), request, timeout);
        String content = ProviderHttp.messageContent(response);
        if (content == null) {
            if (ProviderHttp.generationPhase(phase) && unbilled(response))
                throw new ProviderHttp.EmptyResponse(phase);
            throw new ProviderHttp.Failure(phase, "invalid_response", 0);
        }
        // A blank answer the provider did not bill is the same dropped long response.
        if (content.isBlank() && ProviderHttp.generationPhase(phase) && unbilled(response))
            throw new ProviderHttp.EmptyResponse(phase);
        return content;
    }

    private static boolean unbilled(JsonObject response) {
        try {
            return response.getAsJsonObject("usage").get("cost").getAsBigDecimal().signum() == 0;
        } catch (RuntimeException missing) {
            return false;
        }
    }

    /**
     * One retry of a free empty generation reply while most of the job window is left. The window
     * runs from the generation start to its deadline; at least 60 % must remain.
     */
    static boolean emptyRetryAllowed(long deadlineMs, long windowStartMs, long nowMs) {
        long window = deadlineMs - windowStartMs;
        if (window <= 0 || nowMs < windowStartMs) return false;
        long remaining = deadlineMs - nowMs;
        return remaining >= 0 && 5 * remaining >= 3 * window;
    }

    /**
     * One detail-polish attempt never waits out the whole generation window with a plan already in
     * hand: at most a quarter of the generation budget, at least two minutes for a 16k low-effort
     * patch, never past what is left.
     */
    static int detailPolishSeconds(int generationSeconds, int remainingSeconds) {
        if (generationSeconds < 60 || generationSeconds > WorkshopAiPlanning.Speed.MAX_SECONDS)
            throw new IllegalArgumentException("Generation budget outside limits");
        if (remainingSeconds < 1) throw new IllegalArgumentException("No time left to polish");
        return Math.min(remainingSeconds, Math.max(120, (generationSeconds + 3) / 4));
    }

    /** Shared tool-turn pool: one reserved slot per turn, claimed first-come by looped phases. */
    private static final class LoopPool {
        final CallGraph graph;
        final int base, size;
        final java.util.concurrent.atomic.AtomicInteger cursor =
                new java.util.concurrent.atomic.AtomicInteger();

        LoopPool(CallGraph graph, int base, int size) {
            this.graph = graph;
            this.base = base;
            this.size = size;
        }

        int next() {
            int slot = cursor.getAndIncrement();
            return slot < size ? base + slot : -1;
        }
    }

    /**
     * One-shot chat today, bounded tool loop when the strategy gate opens it. The system prefix
     * stays byte-identical in both cases except the appended tool suffix; tool calls run
     * server-side per turn, each settled under the existing ledger rules.
     */
    private String chatLoop(
            String phase,
            String systemBase,
            String user,
            SidecarConfig config,
            String effort,
            int finalTokens,
            int timeoutSeconds,
            WorkshopAiSandbox.Scope scope,
            WorkshopAiToolLoop.Phase loopPhase,
            long volume,
            CallGraph graph,
            LoopPool pool,
            ProviderHttp.Billing finalBilling,
            JsonObject format)
            throws Exception {
        boolean loop =
                scope != null
                        && pool != null
                        && WorkshopAiToolLoop.useLoop(
                                config.planning(),
                                WorkshopAiPlanning.subjectClass(scope.prompt),
                                volume,
                                loopPhase);
        String system =
                loop
                        ? systemBase
                                + WorkshopAiToolLoop.suffix(config.planning().toolLoopMaxTurns())
                        : systemBase;
        if (!loop)
            return chat(
                    phase,
                    system,
                    user,
                    config,
                    effort,
                    finalTokens,
                    timeoutSeconds,
                    finalBilling,
                    format);
        long started = System.currentTimeMillis();
        long deadline = started + timeoutSeconds * 1000L;
        WorkshopAiToolLoop.Outcome outcome =
                WorkshopAiToolLoop.run(
                        system,
                        user,
                        scope,
                        config.planning().toolLoopMaxTurns(),
                        (messages, last) -> {
                            int left = Math.max(1, timeout(deadline));
                            int perTurn = loopTurnSeconds(left);
                            if (!last && pool != null) {
                                int slot = pool.next();
                                if (slot >= 0)
                                    try (var turn = graph.claim(slot)) {
                                        return turn.post(
                                                phase,
                                                peer(phase).chatPath(),
                                                loopRequest(
                                                        config,
                                                        messages,
                                                        effort,
                                                        WorkshopAiToolLoop.TURN_TOKENS,
                                                        null,
                                                        WorkshopAiToolLoop.definitions(),
                                                        false),
                                                perTurn);
                                    }
                            }
                            return finalBilling.post(
                                    phase,
                                    peer(phase).chatPath(),
                                    loopRequest(
                                            config,
                                            messages,
                                            effort,
                                            finalTokens,
                                            format,
                                            null,
                                            true),
                                    (int)
                                            Math.max(
                                                    1,
                                                    Math.min(
                                                            WorkshopAiPlanning.Speed.MAX_SECONDS,
                                                            left)));
                        });
        info(
                "AI loop phase={} turns={} inputTokens={} cachedTokens={} latencyMs={}",
                phase,
                outcome.turns(),
                outcome.inputTokens(),
                outcome.cachedTokens(),
                System.currentTimeMillis() - started);
        return outcome.content();
    }

    private static void phase(
            java.util.function.Consumer<Progress> listener,
            String phase,
            int done,
            int total,
            long deadline) {
        long started = System.currentTimeMillis();
        listener.accept(new Progress(phase, done, total, started, Math.max(started, deadline)));
    }

    private static int timeout(long deadline) {
        long left = deadline - System.currentTimeMillis() - 5_000;
        if (left < 1000) throw new ProviderHttp.Failure("generation", "timeout", 0);
        return (int) Math.min(WorkshopAiPlanning.Speed.MAX_SECONDS, left / 1000);
    }

    private static boolean fatal(Throwable failure) {
        if (failure instanceof ExecutionException) return fatal(failure.getCause());
        return failure instanceof InterruptedException
                || failure instanceof Budget.BudgetBlocked
                || failure instanceof Budget.BudgetExhausted
                || failure instanceof IOException
                        && !(failure instanceof java.net.http.HttpTimeoutException)
                || failure instanceof ProviderHttp.Failure f
                        && (f.unbilled()
                                || f.category.startsWith("billing")
                                || f.phase.equals("budget"));
    }

    private static JsonObject metrics(JsonObject report) {
        var result = new JsonObject();
        for (String key : List.of("cells", "components", "warnings", "extent", "flatness"))
            result.add(key, report.get(key));
        return result;
    }

    private static int sourceCells(
            WorkshopAiPlan plan, Set<String> palette, int cap, int sx, int sy, int sz) {
        var root = JsonParser.parseString(plan.canonical()).getAsJsonObject();
        if (root.get("version").getAsInt() < 3) return plan.cells().size();
        var source = new JsonArray();
        for (var element : root.getAsJsonArray("operations")) {
            var op = element.getAsJsonObject();
            if (op.get("type").getAsString().equals("mirror"))
                for (var child : op.getAsJsonArray("operations")) source.add(child);
            else source.add(op);
        }
        return WorkshopAiPlan.parseMerged(
                        WorkshopAiPlanning.plan(source).toString(), palette, cap, sx, sy, sz)
                .cells()
                .size();
    }

    private static void logPeriodicModules(
            JsonArray operations, Set<String> palette, int cap, int sx, int sy, int sz) {
        for (var element : operations) {
            var op = element.getAsJsonObject();
            if (op.has("type") && op.get("type").getAsString().equals("repeat")) {
                var module =
                        WorkshopAiPlan.parseMerged(
                                WorkshopAiPlanning.plan(op.getAsJsonArray("operations")).toString(),
                                palette,
                                cap,
                                sx,
                                sy,
                                sz);
                int[] lo = {128, 128, 128}, hi = {-1, -1, -1};
                for (var c : module.cells()) {
                    int[] at = {c.x(), c.y(), c.z()};
                    for (int a = 0; a < 3; a++) {
                        lo[a] = Math.min(lo[a], at[a]);
                        hi[a] = Math.max(hi[a], at[a]);
                    }
                }
                info(
                        "AI periodic class=PERIODIC moduleSize={}x{}x{} copyCount={} offset={}",
                        hi[0] - lo[0] + 1,
                        hi[1] - lo[1] + 1,
                        hi[2] - lo[2] + 1,
                        op.get("count").getAsInt(),
                        op.get("offset"));
            } else if (op.has("operations"))
                logPeriodicModules(op.getAsJsonArray("operations"), palette, cap, sx, sy, sz);
        }
    }

    private static java.util.Set<String> mirrorAxes(WorkshopAiPlan plan) {
        var axes = new java.util.TreeSet<String>();
        var root = JsonParser.parseString(plan.canonical()).getAsJsonObject();
        for (var element : root.getAsJsonArray("operations")) {
            var op = element.getAsJsonObject();
            if (op.has("type") && op.get("type").getAsString().equals("mirror"))
                axes.add(op.get("axes").getAsString());
        }
        return axes;
    }

    /** Input-tariff ($0.10/M) reserve for the attached contextText; empty context costs nothing. */
    static long contextBound(int contextTokens) {
        if (contextTokens < 0 || contextTokens > MAX_CONTEXT_TOKENS)
            throw new IllegalArgumentException("Context tokens outside limits");
        // Monetary ceilings round up: 2048 estimated tokens cost 204.8 microUSD.
        return (contextTokens * 10L + 99) / 100;
    }

    /**
     * A tool turn may use half of what is left, at most two minutes. An even split over every
     * allowed turn gave a 10-turn loop 39 s per turn, below one medium-effort turn's latency, so AI
     * extend timed out on its first call; turns are sequential and few, the rest stays for later
     * turns and the final answer.
     */
    static int loopTurnSeconds(int leftSeconds) {
        return Math.max(5, Math.min(120, leftSeconds / 2));
    }

    private static int settingsBudget(SidecarConfig config) {
        return config.planning().budgetSeconds();
    }

    /**
     * The mode's configured budget at 24x16x24, scaled by linear size: R3's wall took 308 s at that
     * size and R4's 64x24x40 exhausted 390 s. One cap bounds both the job and every call.
     */
    static long planningTimeoutMillis(
            int budgetSeconds, int sx, int sy, int sz, boolean withContext) {
        return (25 + ProviderHttp.generationTimeoutSeconds(budgetSeconds, sx, sy, sz) + 10) * 1000L
                + (withContext ? 15_000 : 0);
    }

    /**
     * A missing, unreadable or invalid budget policy is an admin configuration state, not an
     * internal error: surface it as a closed budget category. Ledger capacity, write failures and
     * the poisoned ledger state after them keep their own category so a full disk is never
     * misreported as a money limit. BudgetExhausted/Blocked keep their mapping. The original cause
     * is chained; the log carries only our closed message plus the cause class name, never cause
     * message text (it can contain server paths).
     */
    private WorkshopAiPlan generate(
            String prompt,
            String contextText,
            SidecarConfig config,
            Set<String> palette,
            int sideX,
            int sideY,
            int sideZ,
            int tokens,
            ProviderHttp.Billing billing,
            int version)
            throws Exception {
        boolean v2 = version >= 2;
        double destructive = 1;
        return single(
                prompt,
                contextText,
                config,
                palette,
                sideX,
                sideY,
                sideZ,
                tokens,
                billing,
                version,
                destructive,
                "generation",
                config.planning().efforts().single(WorkshopAiPlanning.subjectClass(prompt)),
                ProviderHttp.generationTimeoutSeconds(settingsBudget(config), sideX, sideY, sideZ),
                config.maxBlocks(),
                null,
                WorkshopAiToolLoop.Phase.SINGLE,
                (long) sideX * sideY * sideZ,
                null,
                null);
    }

    private static String layoutReason(RuntimeException invalid) {
        String reason = invalid.getMessage();
        return reason != null
                        && reason.matches(
                                "Layout [a-z ]+|Expected [a-z ]+|Unknown or missing field")
                ? reason
                : "Malformed layout JSON or structure";
    }

    private static final class InvalidPlan extends ProviderHttp.Failure {
        final String reason, content;

        InvalidPlan(String phase, String reason, String content) {
            super(phase, "invalid_plan", 0);
            this.reason = reason;
            this.content = content;
        }
    }

    private WorkshopAiPlan recoveredSingle(
            String prompt,
            String contextText,
            SidecarConfig config,
            Set<String> palette,
            int sx,
            int sy,
            int sz,
            int tokens,
            ProviderHttp.Billing billing,
            int version,
            double destructive,
            String phase,
            String effort,
            int seconds,
            CallGraph graph,
            int repairSlot,
            int retrySlot,
            long windowStartMs,
            long deadline,
            WorkshopAiSandbox.Scope scope,
            WorkshopAiToolLoop.Phase loopPhase,
            LoopPool pool)
            throws Exception {
        try {
            return single(
                    prompt,
                    contextText,
                    config,
                    palette,
                    sx,
                    sy,
                    sz,
                    tokens,
                    billing,
                    version,
                    destructive,
                    phase,
                    effort,
                    seconds,
                    config.maxBlocks(),
                    scope,
                    loopPhase,
                    (long) sx * sy * sz,
                    graph,
                    pool);
        } catch (ProviderHttp.EmptyResponse empty) {
            if (retrySlot < 0
                    || !emptyRetryAllowed(deadline, windowStartMs, System.currentTimeMillis()))
                throw empty;
            // The empty reply cost nothing, so the first slot settled to zero; the retry
            // bills its own spare slot and the player keeps seeing the normal progress.
            info("AI empty generation response, retrying once job={} phase={}", graph.id, phase);
            try (var again = graph.claim(retrySlot)) {
                try {
                    return single(
                            prompt,
                            contextText,
                            config,
                            palette,
                            sx,
                            sy,
                            sz,
                            tokens,
                            again,
                            version,
                            destructive,
                            phase,
                            effort,
                            Math.min(seconds, timeout(deadline)),
                            config.maxBlocks(),
                            scope,
                            loopPhase,
                            (long) sx * sy * sz,
                            graph,
                            pool);
                } catch (InvalidPlan invalid) {
                    try (var repair = graph.claim(repairSlot)) {
                        return repairPlan(
                                invalid,
                                config,
                                palette,
                                sx,
                                sy,
                                sz,
                                repair,
                                Math.min(60, timeout(deadline)),
                                destructive,
                                scope,
                                graph,
                                pool);
                    }
                }
            }
        } catch (InvalidPlan invalid) {
            try (var repair = graph.claim(repairSlot)) {
                return repairPlan(
                        invalid,
                        config,
                        palette,
                        sx,
                        sy,
                        sz,
                        repair,
                        Math.min(60, timeout(deadline)),
                        destructive,
                        scope,
                        graph,
                        pool);
            }
        }
    }

    private WorkshopAiPlan repairPlan(
            InvalidPlan original,
            SidecarConfig config,
            Set<String> palette,
            int sx,
            int sy,
            int sz,
            ProviderHttp.Billing billing,
            int seconds,
            double destructive,
            WorkshopAiSandbox.Scope scope,
            CallGraph graph,
            LoopPool pool)
            throws Exception {
        try {
            String corrected =
                    chatLoop(
                            "repair",
                            generationSystemPrompt(config, palette, sx, sy, sz, 3, false)
                                    + repairSuffix(
                                            config.planning().polishOpCap(),
                                            original.phase.equals("parts")),
                            "VALIDATOR " + original.reason + "\nREJECTED PLAN " + original.content,
                            config,
                            config.planning().efforts().repair(),
                            8000,
                            seconds,
                            scope,
                            WorkshopAiToolLoop.Phase.REPAIR,
                            (long) sx * sy * sz,
                            graph,
                            pool,
                            billing,
                            WorkshopAiSchema.plan(
                                    3,
                                    palette,
                                    config.planning().polishOpCap(),
                                    false,
                                    !original.phase.equals("parts")
                                            && config.planning().mirrorEnabled()));
            var empty =
                    new WorkshopAiPlan(
                            List.of(), "{\"version\":3,\"operations\":[]}", 0, destructive);
            var plan =
                    WorkshopAiPlanning.patch(
                            empty,
                            corrected,
                            palette,
                            config.planning().polishOpCap(),
                            config.maxBlocks(),
                            sx,
                            sy,
                            sz);
            if (plan.cells().isEmpty()) throw new IllegalArgumentException("Empty operations");
            if (original.phase.equals("parts") || !config.planning().mirrorEnabled())
                forbidPartMirrors(
                        JsonParser.parseString(plan.canonical())
                                .getAsJsonObject()
                                .getAsJsonArray("operations"));
            info("AI repair phase={} result=accepted", original.phase);
            return new WorkshopAiPlan(
                    plan.cells(),
                    plan.canonical(),
                    plan.demolishVolume(),
                    destructive,
                    List.of("plan_repaired"));
        } catch (Exception failure) {
            if (fatal(failure)) throw failure;
            warn(
                    "AI repair phase={} result=rejected category={} reason={}",
                    original.phase,
                    ProviderHttp.diagnostic(failure),
                    failure instanceof RuntimeException invalid
                            ? WorkshopAiRejectedPlans.reason(invalid)
                            : "unavailable");
            throw original;
        }
    }

    private WorkshopAiPlan single(
            String prompt,
            String contextText,
            SidecarConfig config,
            Set<String> palette,
            int sideX,
            int sideY,
            int sideZ,
            int tokens,
            ProviderHttp.Billing billing,
            int version,
            double destructive,
            String phase,
            String effort,
            int timeout,
            int hardCap,
            WorkshopAiSandbox.Scope scope,
            WorkshopAiToolLoop.Phase loopPhase,
            long volume,
            CallGraph graph,
            LoopPool pool)
            throws Exception {
        String input = singleInput(prompt, contextText, version);
        String system =
                generationSystemPrompt(
                        config, palette, sideX, sideY, sideZ, version, !contextText.isEmpty());
        JsonObject format =
                WorkshopAiSchema.plan(
                        version,
                        palette,
                        128,
                        false,
                        !phase.equals("parts") && config.planning().mirrorEnabled());
        String content =
                chatLoop(
                        phase, system, input, config, effort, tokens, timeout, scope, loopPhase,
                        volume, graph, pool, billing, format);
        WorkshopAiPlan plan;
        try {
            plan =
                    WorkshopAiNormalization.parse(
                            content, palette, config.maxBlocks(), hardCap, sideX, sideY, sideZ);
            if (!plan.warnings().isEmpty())
                info("AI normalization phase={} notes={}", phase, plan.warnings());
            if (version == 3
                    && (phase.equals("parts") || !config.planning().mirrorEnabled())
                    && JsonParser.parseString(plan.canonical())
                                    .getAsJsonObject()
                                    .get("version")
                                    .getAsInt()
                            == 3)
                forbidPartMirrors(
                        JsonParser.parseString(plan.canonical())
                                .getAsJsonObject()
                                .getAsJsonArray("operations"));
            if (JsonParser.parseString(plan.canonical()).getAsJsonObject().get("version").getAsInt()
                    > version)
                throw new IllegalArgumentException("Client requires older plan language");
        } catch (RuntimeException invalid) {
            String reason = WorkshopAiRejectedPlans.reason(invalid);
            warn("AI plan validation failed: {}", reason);
            try {
                WorkshopAiRejectedPlans.save(rejectedPlans, reason, content, generation.key());
            } catch (IOException | RuntimeException unwritable) {
                warn(
                        "AI rejected plan debug file unavailable ({})",
                        unwritable.getClass().getSimpleName());
            }
            throw new InvalidPlan(phase, reason, content);
        }
        // No model judges the plan: validation above, the builder's demolish confirm and the
        // per-write checks after it are the whole guard.
        return new WorkshopAiPlan(
                plan.cells(),
                plan.canonical(),
                plan.demolishVolume(),
                destructive,
                plan.warnings());
    }

    static String mirrorExtents(int sx, int sz) {
        return " Mirror source half: x=0.."
                + ((sx - 1) / 2)
                + ", z=0.."
                + ((sz - 1) / 2)
                + "; centre x="
                + ((sx - 1) / 2.0)
                + ", z="
                + ((sz - 1) / 2.0)
                + ". For an even side, centred features MUST touch the last source-half cell; for"
                + " an odd side the centre column belongs to the source and is written once. Every"
                + " centred pediment, roof ridge, door frame, ornament and tower must reach that"
                + " source-half end; never leave 1-2 air cells before the mirror plane.";
    }

    /**
     * Curated colour families for modded palette blocks (vanilla ids carry their dye colour in the
     * name). Evidence: blockstate + full-cube model in the given server-mod JAR, runtime {@code
     * paletteSafe} on the workshop server; waxed copper only (unwaxed oxidises).
     */
    static final Map<String, String> MODDED_FAMILY =
            Map.ofEntries(
                    Map.entry("create:asurine", "blues"),
                    Map.entry("create:veridium", "greens"),
                    Map.entry("create:crimsite", "reds"),
                    Map.entry("create:limestone", "whites"),
                    Map.entry("create:scoria", "browns"),
                    Map.entry("create:brass_block", "yellows"),
                    Map.entry("create:zinc_block", "grays"),
                    Map.entry("create:waxed_weathered_copper_tiles", "greens"),
                    Map.entry("create:rose_quartz_tiles", "pinks"),
                    Map.entry("minecraft:resin_block", "oranges"),
                    Map.entry("minecraft:resin_bricks", "oranges"),
                    Map.entry("minecraft:sulfur_bricks", "yellows"),
                    Map.entry("minecraft:pale_oak_planks", "whites"),
                    Map.entry("minecraft:pale_oak_leaves", "greens"),
                    Map.entry("minecraft:pale_moss_block", "greens"),
                    Map.entry("minecraft:waxed_weathered_copper_bars", "greens"),
                    Map.entry("blockfield:cyan_plated_locometal", "cyans"),
                    Map.entry("blockfield:flat_riveted_locometal", "blacks"),
                    Map.entry("blockfield:white_slashed_locometal", "whites"),
                    Map.entry("rechiseled:end_stone_scales", "yellows"),
                    Map.entry("rechiseled:gold_block_scales", "yellows"),
                    Map.entry("rechiseled:lapis_block_scales", "blues"),
                    Map.entry("minecraft:terracotta", "oranges"));

    static String stateInstructions() {
        return "block is a palette id or a vanilla state id[prop=value,...]. Only stairs, slabs,"
                   + " walls, fences, panes/iron bars, closed trapdoors, axis pillars/logs/chains"
                   + " and standing/hanging lanterns accept states. block may also be"
                   + " {\"mix\":[[\"minecraft:stone_bricks\",6],[\"minecraft:cracked_stone_bricks\",2],[\"minecraft:mossy_stone_bricks\",1]]}."
                   + " Use 2..6 [palette id/state,positive integer weight] entries for cheap wall"
                   + " texture. A mix counts once toward the template cap; deterministic cell/seed"
                   + " sampling is identical in preview, placement and Undo and moves with"
                   + " rotation/mirror. Omitted properties keep defaults; use exact vanilla"
                   + " property names/values, waterlogged=false, trapdoor open=false,powered=false."
                   + " No other interactive blocks. Facing and axis rotate/mirror with the"
                   + " geometry; neighbour connections and stair corners are derived automatically."
                   + " State examples:"
                   + " minecraft:quartz_stairs[facing=north,half=bottom,shape=straight] for eaves"
                   + " and pitched/hipped roofs; minecraft:quartz_slab[type=top] for slab ledges"
                   + " and cornices; minecraft:stone_brick_wall and minecraft:oak_fence for"
                   + " railings; minecraft:oak_trapdoor[facing=east,half=top,open=false] for"
                   + " shutters; minecraft:quartz_pillar[axis=y] for columns. ";
    }

    static final List<String> DYE_FAMILY =
            List.of(
                    "white",
                    "light_gray",
                    "gray",
                    "black",
                    "brown",
                    "red",
                    "orange",
                    "yellow",
                    "lime",
                    "green",
                    "cyan",
                    "light_blue",
                    "blue",
                    "purple",
                    "magenta",
                    "pink");

    /** Colour family of one palette id for the grouped prompt section below; pure, no registry. */
    static String paletteFamily(String id) {
        if ("minecraft:air".equals(id)) return "air";
        if (MODDED_FAMILY.containsKey(id)) return MODDED_FAMILY.get(id);
        String path = id.substring(id.indexOf(':') + 1);
        for (String colour : DYE_FAMILY) if (path.startsWith(colour + "_")) return colour + "s";
        if (path.contains("fire")
                || path.contains("soul")
                || path.contains("netherrack")
                || path.contains("magma")) return "fire";
        if (path.contains("water")) return "water";
        if (path.contains("log")
                || path.contains("planks")
                || path.contains("leaves")
                || path.contains("wood")) return "wood";
        if (path.contains("glass") || path.contains("pane")) return "glass";
        if (path.contains("glowstone") || path.contains("lantern") || path.contains("shroomlight"))
            return "light";
        if (path.contains("dirt")
                || path.contains("mud")
                || path.contains("moss")
                || path.contains("sand")
                || path.contains("snow")
                || path.contains("ice")
                || path.contains("hay")) return "ground";
        if (path.contains("stone")
                || path.contains("brick")
                || path.contains("deepslate")
                || path.contains("granite")
                || path.contains("andesite")
                || path.contains("diorite")
                || path.contains("tuff")
                || path.contains("calcite")
                || path.contains("quartz")
                || path.contains("prismarine")) return "stone";
        return "detail";
    }

    /**
     * The palette grouped into labelled colour-family runs. The ids render in global sorted order,
     * only split by family labels, so CONTEXT RLE indexes (positions in the sorted palette,
     * counting every id left to right) stay valid.
     */
    static String paletteSection(Set<String> palette) {
        var sorted = new ArrayList<>(palette);
        Collections.sort(sorted);
        var section = new StringBuilder("Palette: ");
        String family = null;
        for (String id : sorted) {
            String next = paletteFamily(id);
            if (!next.equals(family)) {
                if (family != null) section.append("; ");
                section.append(next).append(": ");
                family = next;
            } else {
                section.append(", ");
            }
            section.append(id);
        }
        return section.toString();
    }

    /**
     * The exact generation system prompt. Extracted so calibration and tests build the same string
     * the paid call sends; the grouped palette stays last because context RLE indexes into it.
     */
    static String generationSystemPrompt(
            SidecarConfig config,
            Set<String> palette,
            int sideX,
            int sideY,
            int sideZ,
            boolean v2,
            boolean withContext) {
        return generationSystemPrompt(
                config, palette, sideX, sideY, sideZ, v2 ? 2 : 1, withContext);
    }

    static String generationSystemPrompt(
            SidecarConfig config,
            Set<String> palette,
            int sideX,
            int sideY,
            int sideZ,
            int version,
            boolean withContext) {
        return languageSystemPrompt(palette, version)
                + requestLimits(config, sideX, sideY, sideZ, version);
    }

    static String requestLimits(SidecarConfig config, int sx, int sy, int sz, int version) {
        return " REQUEST LIMITS: Maximum sides: "
                + sx
                + ","
                + sy
                + ","
                + sz
                + (version == 3
                        ? ". Unique visited grid cells (including air cuts) <= "
                        : ". Sum of cuboid volumes including overlap <= ")
                + config.maxBlocks()
                + ". These request limits override all example dimensions."
                + (version == 3 ? mirrorExtents(sx, sz) : "")
                + (version == 3 && !config.planning().mirrorEnabled()
                        ? " Mirror is disabled for this request: generate the full volume with no"
                                + " mirror wrappers."
                        : "");
    }

    static String languageSystemPrompt(Set<String> palette, int version) {
        boolean v2 = version >= 2;
        StringBuilder constraints =
                new StringBuilder(
                        "You design Minecraft structures. Follow the phase output schema. The plan"
                                + " language uses version:"
                                + version
                                + " and"
                                + " operations. Each operation has exactly"
                                + (v2
                                        ? " from:[x,y,z],to:[x,y,z],block:string,hollow:boolean,match:string."
                                        : " from:[x,y,z],to:[x,y,z],block:string,hollow:boolean.")
                                + " Coordinates are inclusive integers >=0 and strictly less than"
                                + " the corresponding side, relative to the selection's minimum. No"
                                + " commands, scripts, URLs, entities, NBT, extra fields or prose."
                                + " Use the request-specific sizes and cell budget supplied after"
                                + " this language reference"
                                + (version == 3
                                        ? ". Overlap counts once; hollow interiors cost no cells."
                                                + " For tall"
                                        : ". Hollow cuboids still count their full volume. For"
                                                + " tall")
                                + " buildings use thin wall cuboids and floor slabs. Prefer a few"
                                + " large wall/slab cuboids instead of per-block operations. Choose"
                                + " blocks by part: grass tops are moss_block over dirt over stone;"
                                + " tree trunks are *_log with *_leaves crowns; water is real"
                                + " minecraft:water and flows; ponds, pools and rivers need solid"
                                + " basins, a continuous floor and solid rims at water level; a"
                                + " waterfall is a water source in an enclosed upper basin falling"
                                + " through a deliberate spillway to a solid enclosed basin below."
                                + " Every waterfall must land in its basin inside the plan; no open"
                                + " rim gaps at water level. For flowing rivers slope the river bed"
                                + " down by 1 block every 7-8 cells, use upstream sources only and"
                                + " leave downstream air for vanilla flowing water; never fill the"
                                + " entire river with source blocks. Keep water inside the"
                                + " requested size; never place water where it would run off the"
                                + " build. Other fluids are unavailable; stained glass and blue_ice"
                                + " remain styling options; snow is snow_block; floating islands"
                                + " taper stone and dirt under a grassy top; buildings need"
                                + " foundation, walls, roof, windows, trim and lighting. Unless a"
                                + " door block is in the palette, doors, doorways and entrances"
                                + " must be open air gaps at least 2 blocks high, connected to the"
                                + " interior; never paint a solid facade as a door. Windows may be"
                                + " glass or open air gaps. Vary materials by part, typically 4-10"
                                + " contrasting blocks. Creatures read by silhouette AND colour:"
                                + " pick one body family (greens for Rayquaza and dragons, yellows"
                                + " for Pikachu, blues for wyverns, browns or grays for beasts)"
                                + " with a lighter belly and a darker back from the same family;"
                                + " eyes, teeth and claws must contrast the head (black, white,"
                                + " yellow, glass or metal); horns, spikes, fins and wing membranes"
                                + " use metallic or scale blocks (brass, copper tiles, locometal,"
                                + " gold, lapis and end-stone scales) or coloured glass. Glowing"
                                + " blocks read as eyes, runes and fireflies. Visible fire is"
                                + " allowed only on its own base from this plan: soul fire on soul"
                                + " soil or soul sand (always safe), normal fire only on netherrack"
                                + " or magma and only where the server allows it; unsupported fire"
                                + " is skipped, never an error. Use soul fire for dragon breath,"
                                + " braziers and torches, with the base block in the plan directly"
                                + " under every fire cell. Use minified JSON <=65536 characters. "
                                + (version == 3
                                        ? ""
                                        : "Never add block-state properties to block ids; water is"
                                                + " minecraft:water. "
                                                + (palette.stream()
                                                                .anyMatch(
                                                                        id ->
                                                                                !com.lunares
                                                                                        .blockfield
                                                                                        .workshop
                                                                                        .WorkshopMaterials
                                                                                        .legacy(id))
                                                        ? "Stairs and trapdoors are unavailable in"
                                                                + " this ID-only version. "
                                                        : ""))
                                + "At most 128 operations. "
                                + paletteSection(palette)
                                + (v2
                                        ? ". Operations apply in array order. match decides per"
                                              + " cell whether the write happens: air checks the"
                                              + " ORIGINAL world snapshot is air (even after"
                                              + " earlier planned writes), any overwrites anything"
                                              + " including demolition, a block id checks the"
                                              + " latest successful planned write at that cell, or"
                                              + " the world snapshot if no earlier operation wrote"
                                              + " it. A failed match leaves the previous planned"
                                              + " value unchanged. Without an explicit demolition"
                                              + " or replacement request, occupied world cells are"
                                              + " always preserved, regardless of match. Use air to"
                                              + " extend without touching builds, a block id for"
                                              + " surgical replacement, any with an air block to"
                                              + " demolish."
                                        : ""));
        if (version == 3) {
            int start = constraints.indexOf(" Each operation has exactly");
            int end = constraints.indexOf(" Coordinates are inclusive", start);
            constraints.replace(
                    start,
                    end,
                    " Each leaf operation has type, block, hollow and optional match (default air)."
                        + " The ONLY leaf types are cuboid, ellipsoid and tube. cuboid and"
                        + " ellipsoid use inclusive from/to bounds; for a sphere use type:ellipsoid"
                        + " with equal sides. tube uses path (2..16 integer control points),"
                        + " curve:polyline or curve:bezier (exactly 4 points), radiusStart and"
                        + " radiusEnd (integers 0..64, at least one positive); capped round tube"
                        + " follows the path and tapers. repeat has ONLY"
                        + " type:repeat,count:1..128,offset:[dx,dy,dz],operations:[leaf"
                        + " operations]; copy i translates all children by i*offset, in copy order."
                        + " No nested repeat. mirror has ONLY"
                        + " type:mirror,axes:x|z|xz,operations:[leaves or repeat]; emit identity"
                        + " then X then Z then XZ as applicable. Reflection is x'=sx-1-x and"
                        + " z'=sz-1-z about the requested volume centre; no Y mirror. Children must"
                        + " already be inside the volume. repeat may be inside mirror; mirror"
                        + " inside repeat or mirror is forbidden. For bilateral houses, high-rises,"
                        + " gates, bridges, Pikachu and dragon bodies generate one half or quarter,"
                        + " wrap it in mirror, then append asymmetric doors, signs and details"
                        + " outside the wrapper. A mirror wrapper and its children count once"
                        + " toward the template cap; cells, instances and work count expanded"
                        + " copies. At most 128 total operation/template objects and 4096 expanded"
                        + " leaf instances; expansion work (candidate tests plus emitted writes)"
                        + " <=8388608."
                            + stateInstructions()
                            + " Examples:"
                            + " {\"type\":\"repeat\",\"count\":8,\"offset\":[0,4,0],\"operations\":[{\"type\":\"cuboid\",\"from\":[0,0,0],\"to\":[15,0,15],\"block\":\"minecraft:stone\",\"hollow\":false}]}"
                            + " {\"type\":\"ellipsoid\",\"from\":[2,0,2],\"to\":[12,16,12],\"block\":\"minecraft:stone\",\"hollow\":false}"
                            + " {\"type\":\"tube\",\"path\":[[8,4,8],[12,12,12],[8,20,16]],\"curve\":\"polyline\",\"radiusStart\":3,\"radiusEnd\":1,\"block\":\"minecraft:stone\",\"hollow\":false}.");
            constraints.append(
                    " Requested dimensions are the target occupied width, height and depth, not"
                        + " just upper limits. Respect the requested size and silhouette; allocate"
                        + " the cell budget to recognizable proportions and relief. Preserve"
                        + " recognizable proportions. Creatures need a thick volumetric torso and"
                        + " ellipsoid head, a curved spine/neck/tail made with tapered tubes,"
                        + " connected limbs and wings with substantial membranes. Colour them by"
                        + " part from the palette families: one body colour with a lighter belly"
                        + " and darker back, contrasting eyes, teeth and claws, metallic horns,"
                        + " spikes and scales. Use shaped slabs/stairs for claws and teeth, tapered"
                        + " stair horns, and stepped slab/stair snouts; preserve the body colour"
                        + " families above. Avoid thin straight box chains or flat figures; use"
                        + " hollow ellipsoids/tubes when a large body needs to fit the cell budget."
                        + " Architecture: every facade varies in depth by 1-2 cells:"
                        + " pilasters/columns stand proud of walls, windows are recessed with sills"
                        + " and lintels, a plinth anchors the base and a cornice/eaves overhang"
                        + " crowns it. Make pitched/hipped/stepped roofs from stairs and slabs"
                        + " instead of broad flat slabs. Trim corners and openings; use material"
                        + " mixes on large walls. Keep entrances walkable. Repeat storeys, window"
                        + " rows, columns and battlements while respecting the requested size and"
                        + " silhouette. Connect ALL solid parts by faces; no floating junk."
                        + " Requested entrances need a two-high walkable air path from outside into"
                        + " the interior over a solid floor. Check size, connectivity and entrance"
                        + " before returning JSON.");
        }
        if (version == 3) {
            constraints.append(
                    " PERIODIC subjects (walls, fences, colonnades, arcades, aqueducts, multi-arch"
                        + " bridges, identical house rows and storeys): design ONE tileable module"
                        + " whose two ends match and replicate it with repeat. Use a few repeats"
                        + " with lateral offsets for a winding wall. Reuse the SAME module"
                        + " geometry; do not hand-write every tower, arch or storey. Never mirror"
                        + " along the repetition axis: that doubles the tower at the centre seam. A"
                        + " mirror may wrap the repeat on perpendicular axes for symmetric"
                        + " cross-sections; mirror inside repeat is forbidden. Keep module joins"
                        + " face-connected, including continuous parapets, walkways and rooflines."
                        + " Put unique end caps after the repeated module.");
        }
        if (v2)
            constraints.append(
                    " Existing blocks are given as CONTEXT: hist id:count, rle"
                        + " x,z:y0-y1=paletteIndexOrId (indices into the grouped palette above,"
                        + " counting every id left to right and ignoring family labels; x,z and y"
                        + " are local, 0-based from the selection minimum), hm top solid ground per"
                        + " column in row-major order (z rows, x inner), stride 1 as hm1 or stride"
                        + " 2 as hm2; each value is world Y plus 32768, 0 means no ground in that"
                        + " column; subtract the selection minimum Y from the sel header for local"
                        + " plan Y. shell is the surrounding histogram. Continue their style and"
                        + " materials.");
        return constraints.toString();
    }

    /**
     * Helper calls carry no limits: no tariff probe, no price ceiling, no grant refusal. The
     * duration-sized reserve is topped up to the authenticated cost, so accounting stays exact
     * without ever refusing or blocking on money.
     */
    String transcribe(
            byte[] wav,
            String model,
            String clientLanguage,
            SidecarConfig config,
            Budget budget,
            UUID session,
            UUID builder)
            throws Exception {
        return transcribe(wav, model, "", clientLanguage, config, budget, session, builder);
    }

    /**
     * Helper calls carry no limits: no tariff probe, no price ceiling, no grant refusal. The
     * duration-sized reserve is topped up to the authenticated cost, so accounting stays exact
     * without ever refusing or blocking on money. An optional cleanup model rewrites the transcript
     * afterwards; any error there falls back to the raw transcript. Cost-less replies are estimated
     * at the owner's generation ceilings; the transcription itself keeps the duration-sized reserve
     * as its estimate. OpenRouter keeps its JSON audio shape; OpenAI-compatible transports take
     * multipart/form-data.
     */
    public String transcribe(
            byte[] wav,
            String model,
            String cleanupModel,
            String clientLanguage,
            SidecarConfig config,
            Budget budget,
            UUID session,
            UUID builder)
            throws Exception {
        long bound = transcriptionBound(wav, model);
        String raw;
        try (var billing =
                generation
                        .new Billing(
                                ProviderHttp.topup(budget, session, builder, bound),
                                budget,
                                session,
                                builder)
                        .caps(
                                config.generationPromptCapUsdPerM(),
                                config.generationCompletionCapUsdPerM())) {
            if (Thread.currentThread().isInterrupted()) throw new InterruptedException();
            String language = transcriptionLanguage(clientLanguage);
            JsonObject response;
            if (transcription.priceApi()) {
                JsonObject request = new JsonObject();
                request.addProperty("model", model);
                JsonObject audio = new JsonObject();
                audio.addProperty("data", Base64.getEncoder().encodeToString(wav));
                audio.addProperty("format", "wav");
                request.add("input_audio", audio);
                request.addProperty("response_format", "json");
                if (language != null) request.addProperty("language", language);
                JsonObject provider = new JsonObject();
                provider.addProperty("data_collection", "deny");
                if (model.equals("elevenlabs/scribe-v2")) {
                    JsonObject settings = new JsonObject();
                    settings.addProperty("diarize", false);
                    settings.addProperty("tag_audio_events", false);
                    settings.addProperty("no_verbatim", false);
                    JsonObject options = new JsonObject();
                    options.add("elevenlabs", settings);
                    provider.add("options", options);
                }
                request.add("provider", provider);
                response = billing.post("transcription", transcription.audioPath(), request);
            } else {
                response = billing.postAudio(transcription.audioPath(), wav, model, language);
            }
            try {
                raw = transcript(response);
            } catch (RuntimeException invalid) {
                throw new ProviderHttp.Failure("transcription", "invalid_response", 0);
            }
        }
        if (cleanupModel == null || cleanupModel.isEmpty()) return raw;
        // Its own call scope: sharing the transcription billing would confirm two top-ups
        // against one reservation.
        JsonObject cleanupCall = cleanupRequest(raw, cleanupModel, cleanup.priceApi());
        try (var billing =
                cleanup
                        .new Billing(
                                ProviderHttp.topup(
                                        budget,
                                        session,
                                        builder,
                                        cappedBoundFor(
                                                config,
                                                ProviderHttp.requestTokens(cleanupCall),
                                                CLEANUP_MAX_TOKENS)),
                                budget,
                                session,
                                builder)
                        .caps(
                                config.generationPromptCapUsdPerM(),
                                config.generationCompletionCapUsdPerM())) {
            return cleanup(raw, cleanupCall, billing);
        } catch (Exception fallback) {
            if (fallback instanceof InterruptedException) Thread.currentThread().interrupt();
            return raw;
        }
    }

    static final int CLEANUP_MAX_TOKENS = 4096;

    static JsonObject cleanupRequest(String raw, String model, boolean routing) {
        var request = new JsonObject();
        request.addProperty("model", model);
        request.addProperty("max_tokens", CLEANUP_MAX_TOKENS);
        request.addProperty("stream", false);
        var messages = new JsonArray();
        var system = new JsonObject();
        system.addProperty("role", "system");
        system.addProperty("content", PROMPT_TRANSCRIPT_CLEANUP);
        messages.add(system);
        var user = new JsonObject();
        user.addProperty("role", "user");
        user.addProperty("content", raw);
        messages.add(user);
        request.add("messages", messages);
        if (routing) request.add("provider", ProviderHttp.routing());
        return request;
    }

    private String cleanup(String raw, JsonObject request, ProviderHttp.Billing billing)
            throws Exception {
        JsonObject response = billing.post("cleanup", cleanup.chatPath(), request);
        String content = ProviderHttp.messageContent(response);
        if (content == null) throw new ProviderHttp.Failure("cleanup", "invalid_response", 0);
        String text = content.replaceAll("\\p{Cntrl}", " ").strip();
        if (text.length() >= 2
                && ((text.startsWith("\"") && text.endsWith("\""))
                        || (text.startsWith("'") && text.endsWith("'"))))
            text = text.substring(1, text.length() - 1).strip();
        if (text.isEmpty() || text.length() > MAX_TRANSCRIPT)
            throw new ProviderHttp.Failure("cleanup", "invalid_response", 0);
        return text;
    }

    static String transcriptionLanguage(String clientLanguage) {
        if (clientLanguage == null
                || clientLanguage.length() > 16
                || !clientLanguage.matches("[a-zA-Z]{2}(?:[_-][a-zA-Z]{2})?")) return null;
        String language = clientLanguage.substring(0, 2).toLowerCase(Locale.ROOT);
        return Set.of("ru", "uk", "en").contains(language) ? language : null;
    }

    /**
     * Any well-shaped model ID transcribes; the owner configures which one. The config gate keeps
     * the vendor/name shape for presets; natively named IDs ride custom providers.
     */
    static boolean voiceModel(String model) {
        return model != null
                && (model.matches(SidecarConfig.MODEL)
                        || model.matches(SidecarConfig.NATIVE_MODEL));
    }

    /**
     * OpenRouter routes only to endpoints within max_price (prompt, completion, request, image). At
     * least one must qualify, and on every qualifying endpoint the prices max_price does not filter
     * (cache, reasoning, anything unknown) must stay within the same reserve; web search is never
     * requested. Limits apply to the generation model only.
     */
    static long transcriptionBound(byte[] wav, String model) {
        if (!voiceModel(model))
            throw new IllegalArgumentException("Transcription model ID outside bounds");
        WorkshopAiAudio.validate(wav);
        return ((wav.length - 44L + 31999) / 32000) * 100;
    }

    static String transcript(JsonObject response) {
        var value = response.get("text");
        if (value == null || !value.isJsonPrimitive() || !value.getAsJsonPrimitive().isString())
            throw new IllegalArgumentException("Missing transcription");
        String text = value.getAsString().replaceAll("\\p{Cntrl}", " ").strip();
        if (text.isEmpty() || text.length() > MAX_TRANSCRIPT)
            throw new IllegalArgumentException("Transcription outside bounds");
        return text;
    }

    /** Helper routing: no provider pin and no price caps. */
    static final double DESTRUCTION_AUTHORIZATION_THRESHOLD = 0.75;

    static boolean destructionAuthorized(double destructive) {
        if (!Double.isFinite(destructive) || destructive < 0 || destructive > 1)
            throw new IllegalArgumentException("Invalid destructive score");
        return destructive >= DESTRUCTION_AUTHORIZATION_THRESHOLD;
    }

    static final int MAX_LAYOUT_BYTES = 16384; // WorkshopAiPlanning.layout rejects more
    static final int MAX_PLAN_BYTES = 524288; // WorkshopGeometry.MAX_MERGED_JSON
    static final int MAX_SINGLE_PLAN_BYTES = 65536; // WorkshopGeometry.MAX_JSON, non-merged
    static final int MAX_REJECT_BYTES = 131072; // non-generation response body cap
    static final int MAX_REPORT_BYTES = 65536; // 3 64x64 silhouettes + 64 component boxes
    static final int MAX_REASON_BYTES = 256; // validator reason literals
    static final int MAX_TURN_HISTORY_BYTES = 12288; // typical tool-turn growth
    // The periodic suffix single() appends; the envelope always carries it so layout
    // keywords in a part request can never make the real call bigger than its slot.
    static final String PERIODIC_SUFFIX =
            "\n"
                + "PERIODIC strategy: ONE tileable module + repeat, matching ends; no longitudinal"
                + " mirror.";
    static final int MAX_TRANSCRIPT = 4096;
    static final int MAX_CONTEXT_TOKENS = 2048; // WorkshopAiContext cap in the game

    private static final String PROMPT_TRANSCRIPT_CLEANUP =
            "Russian fillers: э, эм, ну, вот, типа, значит; stutter п-п-привет becomes привет. "
                    + "Rewrite the following speech transcript: remove filler words and"
                    + " hesitations (including the Russian fillers above), join stuttered"
                    + " repetitions and drop restarted false starts, fix misrecognised words,"
                    + " keep the original meaning and language, add nothing. Reply with only"
                    + " the rewritten transcript.";
}
