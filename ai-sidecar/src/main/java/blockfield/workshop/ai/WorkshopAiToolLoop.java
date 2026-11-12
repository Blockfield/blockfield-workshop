package blockfield.workshop.ai;

import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;

import java.util.ArrayList;
import java.util.List;

/**
 * Bounded OpenRouter function-calling loop around plan generation. The system prompt and palette
 * prefix stay byte-stable so the provider input cache keeps applying; only the short tool suffix
 * and the tool/result messages are appended per turn.
 */
final class WorkshopAiToolLoop {
    /** Output cap of one tool turn; the final answer keeps the phase cap. */
    static final int TURN_TOKENS = 8000;

    private WorkshopAiToolLoop() {}

    enum Phase {
        SINGLE,
        PART,
        EXTEND,
        POLISH,
        REPAIR
    }

    /**
     * Strategy gate: smart-fill extend explicitly opts in whatever {@code toolLoopEnabled} says
     * (the model must read the exact boundary before continuing it); polish/repair always loop when
     * enabled; fresh generations and parts loop for large or non-trivial subjects, never for small
     * plain requests (each turn costs time and money).
     */
    static boolean useLoop(
            WorkshopAiPlanning.Settings settings,
            WorkshopAiPlanning.SubjectClass subject,
            long volume,
            Phase phase) {
        if (phase == Phase.EXTEND) return true;
        if (!settings.toolLoopEnabled()) return false;
        return switch (phase) {
            case EXTEND, POLISH, REPAIR -> true;
            case SINGLE, PART ->
                    volume >= settings.volumeThreshold()
                            || subject != WorkshopAiPlanning.SubjectClass.OTHER;
        };
    }

    /** Loop turns reserved up front per job, shared by every looped phase of that job. */
    static int loopTurns(
            WorkshopAiPlanning.Decision decision, WorkshopAiPlanning.Settings settings) {
        if (!settings.toolLoopEnabled()) return 0;
        if (!decision.parallel() && !decision.polishSingle()) return 0;
        return settings.toolLoopMaxTurns();
    }

    /** Short taught behaviour; appended AFTER the stable language/material/palette prefix. */
    static String suffix(int maxTurns) {
        return " TOOLS: you may call read_blocks, slice, heightmap and find to see the exact"
                + " blocks where it matters, then draft with check_plan and inspect_plan, fix what"
                + " they report, and submit the final plan as your answer. Read only where it"
                + " matters and never more than needed: every turn costs time. check_plan runs the"
                + " same tolerant validation as the final submit and shows the world diff;"
                + " inspect_plan shows components, seam gaps, anchors and silhouettes. At most "
                + maxTurns
                + " tool turns; the last answer must be the complete plan, not a tool call.";
    }

    static JsonArray definitions() {
        var tools = new JsonArray();
        tools.add(
                function(
                        "read_blocks",
                        "Exact block states in a box as per-column runs [y0,y1,block]; air is"
                                + " omitted.",
                        boxSchema().deepCopy()));
        tools.add(
                function(
                        "slice",
                        "One 2D letter grid of a world layer: axis x/y/z with an index inside the"
                                + " box.",
                        withAxis(boxSchema())));
        tools.add(
                function("heightmap", "Top solid local Y per column, -1 for empty.", boxSchema()));
        tools.add(
                function(
                        "find",
                        "Count and up to 256 positions of a block id or palette family in a box.",
                        withBlock(boxSchema())));
        tools.add(
                function(
                        "check_plan",
                        "Tolerant validation of a full plan draft: errors or normalizations, cell"
                                + " and budget totals, and the diff against the world (replaced and"
                                + " protected cells). Keeps the draft for plan_slice.",
                        planSchema()));
        tools.add(
                function(
                        "inspect_plan",
                        "Plan quality: components and boxes, seam gaps, anchors, warnings and"
                                + " front/side/top silhouettes. Keeps the draft for plan_slice.",
                        planSchema()));
        tools.add(
                function(
                        "plan_slice",
                        "The kept draft's own layer as a letter grid; pass plan to check another"
                                + " draft.",
                        planSliceSchema()));
        return tools;
    }

    private static JsonObject function(String name, String description, JsonObject parameters) {
        var tool = new JsonObject();
        tool.addProperty("type", "function");
        var function = new JsonObject();
        function.addProperty("name", name);
        function.addProperty("description", description);
        function.add("parameters", parameters);
        tool.add("function", function);
        return tool;
    }

    private static JsonObject boxSchema() {
        var schema = new JsonObject();
        schema.addProperty("type", "object");
        var properties = new JsonObject();
        properties.add("from", point());
        properties.add("to", point());
        schema.add("properties", properties);
        var required = new JsonArray();
        required.add("from");
        required.add("to");
        schema.add("required", required);
        schema.addProperty("additionalProperties", false);
        return schema;
    }

    private static JsonObject point() {
        var schema = new JsonObject();
        schema.addProperty("type", "array");
        var items = new JsonObject();
        items.addProperty("type", "integer");
        items.addProperty("minimum", -8);
        items.addProperty("maximum", 136);
        schema.add("items", items);
        schema.addProperty("minItems", 3);
        schema.addProperty("maxItems", 3);
        return schema;
    }

    private static JsonObject withAxis(JsonObject base) {
        var axis = new JsonObject();
        axis.addProperty("type", "string");
        var values = new JsonArray();
        values.add("x");
        values.add("y");
        values.add("z");
        axis.add("enum", values);
        base.getAsJsonObject("properties").add("axis", axis);
        var index = new JsonObject();
        index.addProperty("type", "integer");
        index.addProperty("minimum", -8);
        index.addProperty("maximum", 136);
        base.getAsJsonObject("properties").add("index", index);
        base.getAsJsonArray("required").add("axis");
        base.getAsJsonArray("required").add("index");
        return base;
    }

    private static JsonObject withBlock(JsonObject base) {
        var block = new JsonObject();
        block.addProperty("type", "string");
        block.addProperty("minLength", 1);
        block.addProperty("maxLength", 128);
        base.getAsJsonObject("properties").add("block", block);
        base.getAsJsonArray("required").add("block");
        return base;
    }

    private static JsonObject planSchema() {
        var schema = new JsonObject();
        schema.addProperty("type", "object");
        var properties = new JsonObject();
        var plan = new JsonObject();
        plan.addProperty("type", "string");
        plan.addProperty("minLength", 1);
        plan.addProperty("maxLength", 65536);
        properties.add("plan", plan);
        schema.add("properties", properties);
        var required = new JsonArray();
        required.add("plan");
        schema.add("required", required);
        schema.addProperty("additionalProperties", false);
        return schema;
    }

    private static JsonObject planSliceSchema() {
        var schema = planSchema();
        schema.getAsJsonArray("required").remove(0);
        var axis = new JsonObject();
        axis.addProperty("type", "string");
        var values = new JsonArray();
        values.add("x");
        values.add("y");
        values.add("z");
        axis.add("enum", values);
        schema.getAsJsonObject("properties").add("axis", axis);
        var index = new JsonObject();
        index.addProperty("type", "integer");
        index.addProperty("minimum", 0);
        index.addProperty("maximum", 127);
        schema.getAsJsonObject("properties").add("index", index);
        schema.getAsJsonArray("required").add("axis");
        schema.getAsJsonArray("required").add("index");
        return schema;
    }

    /** One model turn: either tool calls to execute or the final content answer. */
    record Turn(List<ToolCall> calls, String content) {}

    record ToolCall(String id, String name, JsonObject args) {}

    /** Transport implemented by the provider (paid, per-turn billing) and by fakes in tests. */
    interface Transport {
        JsonObject turn(JsonArray messages, boolean last) throws Exception;
    }

    static Turn parseTurn(JsonObject response) {
        var choices = response.getAsJsonArray("choices");
        if (choices == null || choices.isEmpty()) throw new IllegalArgumentException("bad turn");
        var message = choices.get(0).getAsJsonObject().getAsJsonObject("message");
        var calls = new ArrayList<ToolCall>();
        if (message.has("tool_calls") && message.get("tool_calls").isJsonArray())
            for (var element : message.getAsJsonArray("tool_calls")) {
                var call = element.getAsJsonObject();
                var function = call.getAsJsonObject("function");
                JsonObject args;
                try {
                    args =
                            JsonParser.parseString(function.get("arguments").getAsString())
                                    .getAsJsonObject();
                } catch (RuntimeException invalid) {
                    args = new JsonObject();
                    args.addProperty("unparseable", true);
                }
                calls.add(
                        new ToolCall(
                                call.has("id") ? call.get("id").getAsString() : "",
                                function.has("name") ? function.get("name").getAsString() : "",
                                args));
            }
        String content = null;
        if (message.has("content")
                && message.get("content").isJsonPrimitive()
                && message.getAsJsonPrimitive("content").isString())
            content = message.get("content").getAsString();
        return new Turn(List.copyOf(calls), content);
    }

    /** Result of the loop: the final answer plus loop telemetry for the money log. */
    record Outcome(String content, int turns, long inputTokens, long cachedTokens) {}

    /**
     * Runs the bounded loop. Tool messages are appended to the same message list, so the stable
     * system prefix is sent byte-identical on every turn.
     */
    static Outcome run(
            String system,
            String user,
            WorkshopAiSandbox.Scope scope,
            int maxTurns,
            Transport transport)
            throws Exception {
        var messages = new JsonArray();
        messages.add(message("system", system));
        messages.add(message("user", user));
        long inputTokens = 0, cachedTokens = 0;
        int turns = 0;
        boolean forceFinal = false;
        while (true) {
            if (Thread.currentThread().isInterrupted()) throw new InterruptedException();
            boolean last = forceFinal || turns >= maxTurns;
            JsonObject response = transport.turn(messages, last);
            long[] counters = counters(response, inputTokens, cachedTokens);
            inputTokens = counters[0];
            cachedTokens = counters[1];
            var turn = parseTurn(response);
            if (!turn.calls().isEmpty() && !last) {
                turns++;
                messages.add(assistantCall(turn));
                for (var call : turn.calls()) {
                    JsonObject result;
                    try {
                        if (call.args().has("unparseable"))
                            throw new WorkshopAiSandbox.ToolRefused("bad_arguments");
                        result = WorkshopAiSandbox.execute(scope, call.name(), call.args());
                    } catch (WorkshopAiSandbox.ToolRefused refused) {
                        result = new JsonObject();
                        result.addProperty("ok", false);
                        result.addProperty("error", refused.getMessage());
                    }
                    messages.add(toolResult(call.id(), result.toString()));
                }
                continue;
            }
            if (turn.content() == null || turn.content().isBlank()) {
                if (last) throw new ProviderHttp.Failure("generation", "empty_answer", 0);
                // Reasoning shares max_tokens: a medium-effort tool turn can spend all of
                // TURN_TOKENS thinking and return neither calls nor text. The schema-bound final
                // answer keeps the phase cap, so go straight to it instead of failing the job.
                forceFinal = true;
                continue;
            }
            return new Outcome(turn.content(), turns, inputTokens, cachedTokens);
        }
    }

    private static long[] counters(JsonObject response, long input, long cached) {
        try {
            var usage = response.getAsJsonObject("usage");
            long next = usage.has("prompt_tokens") ? usage.get("prompt_tokens").getAsLong() : input;
            long cache = cached;
            for (String key : List.of("prompt_tokens_details", "input_tokens_details")) {
                if (usage.has(key) && usage.getAsJsonObject(key).has("cached_tokens"))
                    cache = usage.getAsJsonObject(key).get("cached_tokens").getAsLong();
            }
            return new long[] {next, cache};
        } catch (RuntimeException ignored) {
            return new long[] {input, cached};
        }
    }

    private static JsonObject message(String role, String content) {
        var message = new JsonObject();
        message.addProperty("role", role);
        if (content == null) message.add("content", null);
        else message.addProperty("content", content);
        return message;
    }

    private static JsonObject assistantCall(Turn turn) {
        var message = new JsonObject();
        message.addProperty("role", "assistant");
        message.add("content", null);
        var calls = new JsonArray();
        for (var call : turn.calls()) {
            var entry = new JsonObject();
            entry.addProperty("id", call.id());
            entry.addProperty("type", "function");
            var function = new JsonObject();
            function.addProperty("name", call.name());
            function.addProperty("arguments", call.args().toString());
            entry.add("function", function);
            calls.add(entry);
        }
        message.add("tool_calls", calls);
        return message;
    }

    private static JsonObject toolResult(String id, String content) {
        var message = new JsonObject();
        message.addProperty("role", "tool");
        message.addProperty("tool_call_id", id);
        message.addProperty(
                "content", content.length() > 8192 ? content.substring(0, 8192) : content);
        // ponytail: 8k chars cap per tool result keeps context small; revisit if a legit
        // read_blocks column set is ever truncated (MAX_CALL_CELLS bounds it first).
        return message;
    }

    /** Assistant content echoed for cache measurement: the system prefix must be untouched. */
    static boolean stablePrefix(List<JsonObject> requests, String system) {
        for (var request : requests) {
            var messages = request.getAsJsonArray("messages");
            if (messages == null
                    || messages.isEmpty()
                    || !system.equals(
                            messages.get(0).getAsJsonObject().get("content").getAsString()))
                return false;
        }
        return true;
    }
}
