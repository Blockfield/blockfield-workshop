package blockfield.workshop.ai;

import com.google.gson.JsonArray;
import com.google.gson.JsonElement;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import com.lunares.blockfield.workshop.WorkshopGeometry;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashSet;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;

/** Pure planning contract and deterministic assembly, independent of HTTP and the live world. */
final class WorkshopAiPlanning {
    record Efforts(
            String periodic,
            String architecture,
            String organic,
            String other,
            String layout,
            String polish,
            String repair,
            String extend) {
        /** Extend continues a given structure, so it reasons less than a fresh design. */
        static Efforts defaults() {
            return new Efforts("xhigh", "xhigh", "xhigh", "xhigh", "low", "medium", "low", "low");
        }

        Efforts {
            for (String effort :
                    new String[] {
                        periodic, architecture, organic, other, layout, polish, repair, extend
                    })
                if (!Set.of("low", "medium", "high", "xhigh").contains(effort))
                    throw new IllegalArgumentException("Invalid reasoning effort");
        }

        static Efforts parse(JsonObject object) {
            var d = defaults();
            return new Efforts(
                    value(object, "periodicEffort", d.periodic),
                    value(object, "architectureEffort", d.architecture),
                    value(object, "organicEffort", d.organic),
                    value(object, "otherEffort", d.other),
                    value(object, "layoutEffort", d.layout),
                    value(object, "polishEffort", d.polish),
                    value(object, "repairEffort", d.repair),
                    value(object, "extendEffort", d.extend));
        }

        static String value(JsonObject object, String key, String fallback) {
            return object.has(key) ? text(object.get(key), 16) : fallback;
        }

        String single(SubjectClass subject) {
            return switch (subject) {
                case PERIODIC -> periodic;
                case SYMMETRIC_ARCHITECTURE -> architecture;
                case ORGANIC -> organic;
                case OTHER -> other;
            };
        }
    }

    /** Per-request choice; QUALITY is the configured behaviour, FAST trades depth for time. */
    enum Mode {
        QUALITY,
        FAST;

        String key() {
            return name().toLowerCase(Locale.ROOT);
        }
    }

    /**
     * Speed/quality budgets. Acceptance (2026-10) measured 168-368 s for one xhigh single call and
     * production timed out a small build at 390 s, so quality gets 15 min; fast keeps the layout,
     * repair and extend efforts and lowers only the single-class and polish efforts.
     */
    record Speed(int qualitySeconds, int fastSeconds, Efforts fast) {
        static final int MAX_SECONDS = 1800;

        Speed {
            if (qualitySeconds < 60
                    || qualitySeconds > MAX_SECONDS
                    || fastSeconds < 60
                    || fastSeconds > MAX_SECONDS) throw new IllegalArgumentException("Budget");
            java.util.Objects.requireNonNull(fast);
        }

        static Speed defaults() {
            return parse(new JsonObject(), Efforts.defaults());
        }

        static Speed parse(JsonObject o, Efforts quality) {
            return new Speed(
                    o.has("generationSeconds") ? integer(o.get("generationSeconds")) : 900,
                    o.has("fastGenerationSeconds") ? integer(o.get("fastGenerationSeconds")) : 420,
                    new Efforts(
                            Efforts.value(o, "fastPeriodicEffort", "medium"),
                            Efforts.value(o, "fastArchitectureEffort", "medium"),
                            Efforts.value(o, "fastOrganicEffort", "medium"),
                            Efforts.value(o, "fastOtherEffort", "medium"),
                            quality.layout,
                            Efforts.value(o, "fastPolishEffort", "low"),
                            quality.repair,
                            quality.extend));
        }
    }

    record Settings(
            boolean parallelEnabled,
            int volumeThreshold,
            int maxParts,
            int concurrency,
            String partEffort,
            int polishOpCap,
            boolean strategyEnabled,
            boolean mirrorEnabled,
            boolean toolLoopEnabled,
            int toolLoopMaxTurns,
            Efforts efforts,
            WorkshopAiFlatness.Settings detail,
            Speed speed,
            Mode mode) {
        Settings(
                boolean parallelEnabled,
                int volumeThreshold,
                int maxParts,
                int concurrency,
                String partEffort,
                int polishOpCap,
                boolean strategyEnabled,
                boolean mirrorEnabled,
                boolean toolLoopEnabled,
                int toolLoopMaxTurns,
                Efforts efforts,
                WorkshopAiFlatness.Settings detail) {
            this(
                    parallelEnabled,
                    volumeThreshold,
                    maxParts,
                    concurrency,
                    partEffort,
                    polishOpCap,
                    strategyEnabled,
                    mirrorEnabled,
                    toolLoopEnabled,
                    toolLoopMaxTurns,
                    efforts,
                    detail,
                    Speed.defaults(),
                    Mode.QUALITY);
        }

        /** Resolves a request's mode once; downstream calls just read efforts and budget. */
        Settings forMode(Mode chosen) {
            if (mode != Mode.QUALITY) throw new IllegalStateException("Mode already resolved");
            if (chosen == Mode.QUALITY) return this;
            return new Settings(
                    parallelEnabled,
                    volumeThreshold,
                    maxParts,
                    concurrency,
                    partEffort,
                    polishOpCap,
                    strategyEnabled,
                    mirrorEnabled,
                    toolLoopEnabled,
                    toolLoopMaxTurns,
                    speed.fast,
                    detail,
                    speed,
                    chosen);
        }

        int budgetSeconds() {
            return mode == Mode.FAST ? speed.fastSeconds : speed.qualitySeconds;
        }

        Settings(
                boolean parallelEnabled,
                int volumeThreshold,
                int maxParts,
                int concurrency,
                String partEffort,
                int polishOpCap,
                boolean strategyEnabled,
                boolean mirrorEnabled,
                boolean toolLoopEnabled,
                int toolLoopMaxTurns,
                Efforts efforts) {
            this(
                    parallelEnabled,
                    volumeThreshold,
                    maxParts,
                    concurrency,
                    partEffort,
                    polishOpCap,
                    strategyEnabled,
                    mirrorEnabled,
                    toolLoopEnabled,
                    toolLoopMaxTurns,
                    efforts,
                    WorkshopAiFlatness.Settings.defaults());
        }

        Settings(
                boolean parallelEnabled,
                int volumeThreshold,
                int maxParts,
                int concurrency,
                String partEffort,
                int polishOpCap,
                boolean strategyEnabled,
                boolean mirrorEnabled,
                Efforts efforts) {
            this(
                    parallelEnabled,
                    volumeThreshold,
                    maxParts,
                    concurrency,
                    partEffort,
                    polishOpCap,
                    strategyEnabled,
                    mirrorEnabled,
                    false,
                    10,
                    efforts,
                    WorkshopAiFlatness.Settings.defaults());
        }

        Settings(
                boolean parallelEnabled,
                int volumeThreshold,
                int maxParts,
                int concurrency,
                String partEffort,
                int polishOpCap,
                boolean strategyEnabled,
                boolean mirrorEnabled) {
            this(
                    parallelEnabled,
                    volumeThreshold,
                    maxParts,
                    concurrency,
                    partEffort,
                    polishOpCap,
                    strategyEnabled,
                    mirrorEnabled,
                    false,
                    10,
                    Efforts.defaults());
        }

        Settings(
                boolean parallelEnabled,
                int volumeThreshold,
                int maxParts,
                int concurrency,
                String partEffort,
                int polishOpCap,
                boolean strategyEnabled) {
            this(
                    parallelEnabled,
                    volumeThreshold,
                    maxParts,
                    concurrency,
                    partEffort,
                    polishOpCap,
                    strategyEnabled,
                    true);
        }

        static Settings defaults() {
            return new Settings(true, 32768, 6, 4, "low", 48, true);
        }

        Settings {
            if (volumeThreshold < 4096
                    || volumeThreshold > 2097152
                    || maxParts < 2
                    || maxParts > 6
                    || concurrency < 1
                    || concurrency > 4
                    || polishOpCap < 1
                    || polishOpCap > 128
                    || toolLoopMaxTurns < 1
                    || toolLoopMaxTurns > 16
                    || !Set.of("low", "medium", "high", "xhigh").contains(partEffort))
                throw new IllegalArgumentException("Invalid planning settings");
            java.util.Objects.requireNonNull(efforts);
            java.util.Objects.requireNonNull(detail);
            java.util.Objects.requireNonNull(speed);
            java.util.Objects.requireNonNull(mode);
        }

        static Settings parse(JsonObject o) {
            Settings d = defaults();
            Efforts efforts = Efforts.parse(o);
            return new Settings(
                    flag(o, "parallelEnabled", d.parallelEnabled),
                    num(o, "parallelVolumeThreshold", d.volumeThreshold),
                    num(o, "parallelMaxParts", d.maxParts),
                    num(o, "parallelConcurrency", d.concurrency),
                    o.has("parallelPartEffort")
                            ? text(o.get("parallelPartEffort"), 16)
                            : d.partEffort,
                    num(o, "polishOpCap", d.polishOpCap),
                    flag(o, "strategyEnabled", d.strategyEnabled),
                    flag(o, "mirrorEnabled", d.mirrorEnabled),
                    flag(o, "toolLoopEnabled", d.toolLoopEnabled),
                    num(o, "toolLoopMaxTurns", d.toolLoopMaxTurns),
                    efforts,
                    WorkshopAiFlatness.Settings.parse(o),
                    Speed.parse(o, efforts),
                    Mode.QUALITY);
        }

        private static boolean flag(JsonObject o, String name, boolean d) {
            if (!o.has(name)) return d;
            var v = o.get(name);
            if (!v.isJsonPrimitive() || !v.getAsJsonPrimitive().isBoolean())
                throw new IllegalArgumentException("Expected boolean");
            return v.getAsBoolean();
        }

        private static int num(JsonObject o, String name, int d) {
            return o.has(name) ? integer(o.get(name)) : d;
        }
    }

    enum SubjectClass {
        PERIODIC,
        SYMMETRIC_ARCHITECTURE,
        ORGANIC,
        OTHER
    }

    static SubjectClass subjectClass(String prompt) {
        String p = prompt.toLowerCase(Locale.ROOT);
        if (p.matches(
                "(?s).*(colonnad|arcade|aqueduct|storeys|stories|floors|multi.?storey|high.?rise|skyscraper|колоннад|аркад|акведук|многоэтаж|этаж[аи]|multi.?arch.*bridge|bridge.*multi.?arch|многоарочн.*мост|мост.*(нескольк|мног).*арк|rows?"
                    + " of (identical )?houses|ряд.*(одинаков.*)?дом).*"))
            return SubjectClass.PERIODIC;
        boolean buildingWithWalls =
                p.matches(
                        "(?s).*(\\b(house|castle|palace)\\b|дом|замок|дворец).*(\\bwalls?\\b|стен[ауы]).*");
        if (!buildingWithWalls
                && p.matches(
                        "(?s).*(\\b(walls?|fences?|colonnades?|arcades?|aqueducts?|storeys|stories|floors|multi.?storey|high.?rise|skyscrapers?)\\b|стен[ауы]|забор|ограда|колоннад|аркад|акведук|многоэтаж|этаж[аи]|multi.?arch.*bridge|bridge.*multi.?arch|многоарочн.*мост|мост.*(нескольк|мног).*арк|rows?"
                            + " of (identical )?houses|ряд.*(одинаков.*)?дом).*"))
            return SubjectClass.PERIODIC;
        if (p.matches(
                "(?s).*(dragon|дракон|wyvern|виверн|rayquaza|райкваз|pikachu|пикачу|creature|существ|pokemon|покемон|serpent|зме[яию]).*"))
            return SubjectClass.ORGANIC;
        if (!p.matches("(?s).*(asymmetr|асимметр|несимметр).*")
                && p.matches(
                        "(?s).*(house|дом|castle|замок|palace|дворец|bridge|мост|gate|ворот|temple|храм).*"))
            return SubjectClass.SYMMETRIC_ARCHITECTURE;
        return SubjectClass.OTHER;
    }

    record Decision(
            boolean parallel,
            boolean polishSingle,
            String symmetry,
            int parts,
            double confidence,
            String reason) {}

    @FunctionalInterface
    interface Strategy {
        Decision decide(String prompt, int sx, int sy, int sz, int cap, Settings settings)
                throws Exception;
    }

    static Decision choose(
            Strategy strategy, String prompt, int sx, int sy, int sz, int cap, Settings settings) {
        Decision fallback = decide(prompt, sx, sy, sz, cap, settings);
        if (!settings.strategyEnabled || subjectClass(prompt) == SubjectClass.PERIODIC)
            return fallback;
        try {
            Decision result =
                    recommendation(
                            strategy.decide(prompt, sx, sy, sz, cap, settings), fallback, null);
            return result.parallel
                                    && (!settings.parallelEnabled
                                            || result.parts > settings.maxParts)
                            || !settings.mirrorEnabled && !result.symmetry.equals("none")
                    ? fallback
                    : result;
        } catch (InterruptedException cancelled) {
            Thread.currentThread().interrupt();
            return fallback;
        } catch (Exception failure) {
            return fallback;
        }
    }

    static Decision decide(String prompt, int sx, int sy, int sz, int cap, Settings settings) {
        String p = prompt.toLowerCase(Locale.ROOT);
        boolean periodic = subjectClass(prompt) == SubjectClass.PERIODIC;
        boolean asymmetric =
                p.matches(
                        "(?s).*(asymmetr|asymmetric|асимметр|несимметр|river|река|рек[уеи]|rayquaza|райкваза).*");
        String symmetry = "none";
        if (settings.strategyEnabled && settings.mirrorEnabled && !asymmetric && !periodic) {
            if (p.matches(
                            "(?s).*(white house|белый"
                                + " дом|house|дом|high.?rise|многоэтаж|skyscraper|небоскр[её]б).*")
                    && sx > 1
                    && sz > 1) symmetry = "xz";
            else if (p.matches(
                            "(?s).*(bridge|мост|gate|ворот|wall|стен[ауы]|pikachu|пикачу|dragon|дракон|wyvern|виверн).*")
                    && sx > 1) symmetry = "x";
        }
        boolean complex =
                settings.strategyEnabled
                        && p.matches(
                                "(?s).*(dragon|дракон|wyvern|виверн|rayquaza|райкваза|castle|замок|white"
                                    + " house|белый дом).*");
        long volume = (long) sx * sy * sz;
        boolean parallel =
                !periodic
                        && settings.parallelEnabled
                        && cap >= 512
                        && (volume >= settings.volumeThreshold || complex && volume >= 8192);
        int copies = copies(symmetry);
        int parts =
                parallel
                        ? Math.min(
                                settings.maxParts,
                                Math.max(2, (int) Math.min(6, (volume / copies + 16383) / 16384)))
                        : 0;
        return new Decision(
                parallel,
                settings.strategyEnabled,
                symmetry,
                parts,
                1.0,
                periodic
                        ? "periodic_module_repeat"
                        : settings.strategyEnabled
                                ? "deterministic_size_subject_quality"
                                : "size_threshold_only");
    }

    /**
     * Optional decision implementations cannot turn a failed recommendation into a failed build.
     */
    static Decision recommendation(Decision recommended, Decision fallback, Throwable failure) {
        return failure != null
                        || recommended == null
                        || !Double.isFinite(recommended.confidence)
                        || recommended.confidence < .7
                        || recommended.reason == null
                        || !recommended.reason.matches("[a-z0-9_]{1,64}")
                        || !Set.of("none", "x", "z", "xz").contains(recommended.symmetry)
                        || recommended.parallel && (recommended.parts < 2 || recommended.parts > 6)
                        || !recommended.parallel && recommended.parts != 0
                ? fallback
                : recommended;
    }

    static boolean polishSingle(Decision d, WorkshopAiPlanQuality quality) {
        if (quality.issues().contains("flatness")) return true;
        if (d.reason.equals("periodic_module_repeat"))
            return d.polishSingle && quality.components() > 1;
        return d.polishSingle
                && (quality.components() > 1
                        || quality.issues().contains("anchors")
                        || quality.issues().contains("mirror_seam"));
    }

    record Point(int x, int y, int z) {
        int[] values() {
            return new int[] {x, y, z};
        }

        int key() {
            return WorkshopGeometry.index(x, y, z);
        }
    }

    record Part(String id, String role, Point from, Point to, int priority, int budget) {
        int sx() {
            return to.x - from.x + 1;
        }

        int sy() {
            return to.y - from.y + 1;
        }

        int sz() {
            return to.z - from.z + 1;
        }

        boolean contains(Point p) {
            return p.x >= from.x
                    && p.x <= to.x
                    && p.y >= from.y
                    && p.y <= to.y
                    && p.z >= from.z
                    && p.z <= to.z;
        }
    }

    record Anchor(String a, String b, Point first, Point second) {}

    record Layout(
            String canonical,
            String symmetry,
            String style,
            List<Part> parts,
            List<Anchor> anchors) {}

    static Layout layout(
            String json, int sx, int sy, int sz, int cap, int maxParts, int expectedParts) {
        if (json == null || json.length() > 16384)
            throw new IllegalArgumentException("Layout size");
        JsonObject root = WorkshopGeometry.object(json, 16384);
        keys(root, Set.of("version", "symmetry", "style", "parts", "anchors"));
        if (integer(root.get("version")) != 1) throw new IllegalArgumentException("Layout version");
        String symmetry = text(root.get("symmetry"), 4), style = text(root.get("style"), 1024);
        if (!Set.of("none", "x", "z", "xz").contains(symmetry))
            throw new IllegalArgumentException("Layout symmetry");
        int dx = symmetry.contains("x") ? (sx + 1) / 2 : sx,
                dz = symmetry.contains("z") ? (sz + 1) / 2 : sz;
        JsonArray raw = root.getAsJsonArray("parts");
        if (raw == null || raw.size() < 2 || raw.size() > maxParts || raw.size() != expectedParts)
            throw new IllegalArgumentException("Layout part count");
        List<Part> parts = new ArrayList<>();
        Set<String> ids = new HashSet<>();
        Set<Integer> priorities = new HashSet<>();
        long budgets = 0;
        for (var element : raw) {
            var o = element.getAsJsonObject();
            keys(o, Set.of("id", "role", "from", "to", "priority", "budget"));
            String id = text(o.get("id"), 24), role = text(o.get("role"), 160);
            Point from = point(o.getAsJsonArray("from")), to = point(o.getAsJsonArray("to"));
            int priority = integer(o.get("priority")), budget = integer(o.get("budget"));
            Part part = new Part(id, role, from, to, priority, budget);
            if (!id.matches("[a-z][a-z0-9_]{0,23}")
                    || !ids.add(id)
                    || !priorities.add(priority)
                    || priority < 0
                    || priority >= raw.size()
                    || from.x < 0
                    || from.y < 0
                    || from.z < 0
                    || to.x >= dx
                    || to.y >= sy
                    || to.z >= dz
                    || part.sx() < 1
                    || part.sy() < 1
                    || part.sz() < 1
                    || budget < 1
                    || budget > cap
                    || budget > (long) part.sx() * part.sy() * part.sz())
                throw new IllegalArgumentException("Layout part bounds");
            budgets += budget;
            parts.add(part);
        }
        if (budgets * copies(symmetry) > cap)
            throw new IllegalArgumentException("Layout cell budget");
        var rawAnchors = root.getAsJsonArray("anchors");
        if (rawAnchors == null || rawAnchors.size() > 24)
            throw new IllegalArgumentException("Layout anchors");
        var anchors = new ArrayList<Anchor>();
        for (var element : rawAnchors) {
            var o = element.getAsJsonObject();
            keys(o, Set.of("parts", "cells"));
            var owners = o.getAsJsonArray("parts");
            var cells = o.getAsJsonArray("cells");
            if (owners.size() != 2 || cells.size() != 2)
                throw new IllegalArgumentException("Layout anchor pair");
            String a = text(owners.get(0), 24), b = text(owners.get(1), 24);
            Point first = point(cells.get(0).getAsJsonArray()),
                    second = point(cells.get(1).getAsJsonArray());
            Part pa = parts.stream().filter(p -> p.id.equals(a)).findFirst().orElseThrow();
            Part pb = parts.stream().filter(p -> p.id.equals(b)).findFirst().orElseThrow();
            if (a.equals(b)
                    || !pa.contains(first)
                    || !pb.contains(second)
                    || Math.abs(first.x - second.x)
                                    + Math.abs(first.y - second.y)
                                    + Math.abs(first.z - second.z)
                            > 1) throw new IllegalArgumentException("Layout disconnected anchors");
            anchors.add(new Anchor(a, b, first, second));
        }
        parts.sort(Comparator.comparingInt(Part::priority));
        return new Layout(
                root.toString(), symmetry, style, List.copyOf(parts), List.copyOf(anchors));
    }

    static WorkshopAiPlan merge(
            Layout layout,
            Map<String, WorkshopAiPlan> plans,
            Set<String> palette,
            int cap,
            int sx,
            int sy,
            int sz) {
        if (plans.size() != layout.parts.size()) throw new IllegalArgumentException("Missing part");
        JsonArray operations = new JsonArray();
        var warnings = new ArrayList<String>();
        for (Part part : layout.parts) {
            WorkshopAiPlan p = plans.get(part.id);
            if (p == null) throw new IllegalArgumentException("Missing part");
            warnings.addAll(p.warnings());
            WorkshopAiPlan.parse(
                    p.canonical(), palette, part.budget, part.sx(), part.sy(), part.sz());
            var root = JsonParser.parseString(p.canonical()).getAsJsonObject();
            if (integer(root.get("version")) != 3)
                throw new IllegalArgumentException("Part version");
            var ops = root.getAsJsonArray("operations");
            shift(ops, part.from);
            for (var op : ops) operations.add(op);
        }
        if (!layout.symmetry.equals("none")) {
            JsonObject mirror = new JsonObject();
            mirror.addProperty("type", "mirror");
            mirror.addProperty("axes", layout.symmetry);
            mirror.add("operations", operations);
            operations = new JsonArray();
            operations.add(mirror);
        }
        var merged =
                WorkshopAiPlan.parseMerged(plan(operations).toString(), palette, cap, sx, sy, sz);
        return new WorkshopAiPlan(
                merged.cells(),
                merged.canonical(),
                merged.demolishVolume(),
                0,
                warnings.stream().distinct().toList());
    }

    static void shift(JsonArray ops, Point offset) {
        for (var element : ops) {
            var op = element.getAsJsonObject();
            String type = text(op.get("type"), 16);
            if (type.equals("mirror")) throw new IllegalArgumentException("Part mirror forbidden");
            if (type.equals("repeat")) shift(op.getAsJsonArray("operations"), offset);
            else if (op.has("path")) {
                var path = op.getAsJsonArray("path");
                for (int i = 0; i < path.size(); i++)
                    path.set(i, translated(point(path.get(i).getAsJsonArray()), offset));
            } else {
                op.add("from", translated(point(op.getAsJsonArray("from")), offset));
                op.add("to", translated(point(op.getAsJsonArray("to")), offset));
            }
        }
    }

    static WorkshopAiPlan patch(
            WorkshopAiPlan base,
            String patch,
            Set<String> palette,
            int opCap,
            int cap,
            int sx,
            int sy,
            int sz) {
        if (patch == null || patch.length() > WorkshopGeometry.MAX_JSON)
            throw new IllegalArgumentException("Patch size");
        JsonObject extra = WorkshopGeometry.object(patch, WorkshopGeometry.MAX_JSON);
        keys(extra, Set.of("version", "operations"));
        if (integer(extra.get("version")) != 3
                || objects(extra.getAsJsonArray("operations")) > opCap)
            throw new IllegalArgumentException("Polish patch cap");
        if (extra.getAsJsonArray("operations").isEmpty()) return base;
        WorkshopAiPlan.parse(patch, palette, cap, sx, sy, sz);
        JsonObject root = JsonParser.parseString(base.canonical()).getAsJsonObject();
        for (var op : extra.getAsJsonArray("operations")) root.getAsJsonArray("operations").add(op);
        var checked = WorkshopAiPlan.parseMerged(root.toString(), palette, cap, sx, sy, sz);
        return new WorkshopAiPlan(
                checked.cells(),
                checked.canonical(),
                checked.demolishVolume(),
                base.destructiveScore(),
                base.warnings());
    }

    private static int objects(JsonArray ops) {
        int count = 0;
        for (var element : ops) {
            count++;
            var op = element.getAsJsonObject();
            if (op.has("operations")) count += objects(op.getAsJsonArray("operations"));
        }
        return count;
    }

    static JsonObject plan(JsonArray operations) {
        var root = new JsonObject();
        root.addProperty("version", 3);
        root.add("operations", operations);
        return root;
    }

    static int copies(String symmetry) {
        return symmetry.equals("xz") ? 4 : symmetry.equals("none") ? 1 : 2;
    }

    static JsonArray translated(Point p, Point offset) {
        return array(p.x + offset.x, p.y + offset.y, p.z + offset.z);
    }

    static JsonArray array(int x, int y, int z) {
        var a = new JsonArray();
        a.add(x);
        a.add(y);
        a.add(z);
        return a;
    }

    static Point point(JsonArray a) {
        if (a == null || a.size() != 3) throw new IllegalArgumentException("Expected point");
        return new Point(integer(a.get(0)), integer(a.get(1)), integer(a.get(2)));
    }

    static int integer(JsonElement v) {
        if (v == null || !v.isJsonPrimitive() || !v.getAsJsonPrimitive().isNumber())
            throw new IllegalArgumentException("Expected integer");
        return v.getAsBigDecimal().intValueExact();
    }

    static String text(JsonElement v, int cap) {
        if (v == null
                || !v.isJsonPrimitive()
                || !v.getAsJsonPrimitive().isString()
                || v.getAsString().isBlank()
                || v.getAsString().length() > cap
                || v.getAsString().matches("(?s).*\\p{Cntrl}.*"))
            throw new IllegalArgumentException("Expected bounded text");
        return v.getAsString();
    }

    static void keys(JsonObject o, Set<String> fields) {
        if (!o.keySet().equals(fields))
            throw new IllegalArgumentException("Unknown or missing field");
    }
}
