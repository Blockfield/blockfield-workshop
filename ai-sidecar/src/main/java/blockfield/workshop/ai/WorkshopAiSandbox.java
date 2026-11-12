package blockfield.workshop.ai;

import com.google.gson.JsonArray;
import com.google.gson.JsonElement;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import com.lunares.blockfield.workshop.WorkshopGeometry;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.TreeMap;

/**
 * In-memory sandbox behind the model tool loop: exact world reads from the server-thread capture
 * and plan checks against unit 9's tolerant normalization. Never touches the live world; every
 * method here is pure data. Tool failures are returned as results, never thrown, except thread
 * interruption.
 */
final class WorkshopAiSandbox {
    /** Margin around the selection the tools may address; always clamped to map rights. */
    static final int MARGIN = 4;

    /** Max cells served by one world-tool call. */
    static final int MAX_CALL_CELLS = 4096;

    /** Max world cells served per job across all tool calls. */
    static final int MAX_JOB_READS = 32768;

    /** Max positions returned by one find call; the count is always exact. */
    static final int MAX_FIND_POSITIONS = 256;

    /** Max sampled positions in a check_plan world diff. */
    static final int MAX_DIFF_SAMPLES = 64;

    private WorkshopAiSandbox() {}

    /** Immutable capture of the builder's selection plus margin, taken on the server thread. */
    static final class Snapshot {
        static final Snapshot EMPTY = new Snapshot(0, 0, 0, Map.of(), Map.of(), Set.of());
        private final int sx, sy, sz;

        /** Sandbox coordinates with origin at selection min minus MARGIN. */
        private final Map<Integer, String> blocks;

        private final Map<Integer, String> marginSurface;
        private final Set<Integer> protectedCells;

        Snapshot(
                int sx,
                int sy,
                int sz,
                Map<Integer, String> blocks,
                Map<Integer, String> marginSurface,
                Set<Integer> protectedCells) {
            this.sx = sx;
            this.sy = sy;
            this.sz = sz;
            this.blocks = Map.copyOf(blocks);
            this.marginSurface = Map.copyOf(marginSurface);
            this.protectedCells = Set.copyOf(protectedCells);
        }

        static int key(int x, int y, int z) {
            return WorkshopGeometry.index(x + MARGIN, y, z + MARGIN);
        }

        /**
         * Rebuilds a mod-uploaded snapshot (server thread capture) for the sandbox tools. Keys are
         * geometry indexes, values state texts; the mod serializes them and the counts are capped
         * so a corrupt upload cannot exhaust the heap.
         */
        static Snapshot fromWire(
                int sx,
                int sy,
                int sz,
                Map<Integer, String> blocks,
                Map<Integer, String> surface,
                Set<Integer> guarded) {
            if (sx < 1 || sx > 128 || sy < 1 || sy > 128 || sz < 1 || sz > 128)
                throw new IllegalArgumentException("Snapshot sides outside limits");
            if (blocks.size() > 65536 || surface.size() > 65536 || guarded.size() > 65536)
                throw new IllegalArgumentException("Snapshot too many cells");
            for (var entry : java.util.Map.of("blocks", blocks, "surface", surface).entrySet()) {
                for (var cell : entry.getValue().entrySet()) {
                    if (cell.getKey() == null
                            || cell.getKey() < 0
                            || cell.getKey() >= 128 * 128 * 128
                            || cell.getValue() == null
                            || cell.getValue().isEmpty()
                            || cell.getValue().length() > 256)
                        throw new IllegalArgumentException(
                                "Snapshot " + entry.getKey() + " cell outside bounds");
                }
            }
            for (Integer guard : guarded) {
                if (guard == null || guard < 0 || guard >= 128 * 128 * 128)
                    throw new IllegalArgumentException("Snapshot guard outside bounds");
            }
            return new Snapshot(sx, sy, sz, blocks, surface, guarded);
        }

        boolean inside(int x, int y, int z) {
            return x >= 0 && x < sx && y >= 0 && y < sy && z >= 0 && z < sz;
        }

        boolean reachable(int x, int y, int z) {
            return x >= -MARGIN
                    && x < sx + MARGIN
                    && y >= 0
                    && y < sy
                    && z >= -MARGIN
                    && z < sz + MARGIN;
        }

        /** Exact id, or null outside the captured rights (refuse, never guess). */
        String exact(int x, int y, int z) {
            if (inside(x, y, z)) return blocks.get(key(x, y, z));
            // Margin columns carry only their captured surface block (see partial flags).
            return marginSurface.get(key(x, 0, z));
        }

        boolean marginApproximate(int x, int y, int z) {
            return !inside(x, y, z);
        }

        boolean isProtected(int x, int y, int z) {
            return protectedCells.contains(key(x, y, z));
        }

        int sx() {
            return sx;
        }

        int sy() {
            return sy;
        }

        int sz() {
            return sz;
        }
    }

    /** Per-job read accounting shared by every tool call of one generation job. */
    static final class JobReads {
        private int reads;

        synchronized void charge(int cells) {
            if (cells < 0 || reads + cells > MAX_JOB_READS)
                throw new ToolRefused("job_read_budget_exceeded");
            reads += cells;
        }

        synchronized int reads() {
            return reads;
        }
    }

    /** A refused or malformed tool call; the loop turns it into a tool result. */
    static final class ToolRefused extends RuntimeException {
        ToolRefused(String message) {
            super(message);
        }
    }

    /** One stateful plan under construction; large builds assemble it chunk by chunk. */
    static final class WorkingPlan {
        private String plan;

        synchronized void set(String plan) {
            this.plan = plan;
        }

        synchronized String get() {
            return plan;
        }
    }

    /** Execution scope for one job: snapshot, palette, caps and shared budgets. */
    static final class Scope {
        final Snapshot snapshot;
        final Set<String> palette;
        final int maxBlocks;
        final int hardCap;
        final int sx, sy, sz;
        final String prompt;

        /** Part origin inside the snapshot; global scopes use 0,0,0. */
        final int ox, oy, oz;

        /** Tool box limits in scope-local coordinates (part box or full selection). */
        final int limX, limY, limZ;

        final JobReads reads = new JobReads();
        final WorkingPlan working = new WorkingPlan();

        Scope(
                Snapshot snapshot,
                Set<String> palette,
                int maxBlocks,
                int hardCap,
                int sx,
                int sy,
                int sz,
                String prompt) {
            this(snapshot, palette, maxBlocks, hardCap, sx, sy, sz, prompt, 0, 0, 0, sx, sy, sz);
        }

        Scope(
                Snapshot snapshot,
                Set<String> palette,
                int maxBlocks,
                int hardCap,
                int sx,
                int sy,
                int sz,
                String prompt,
                int ox,
                int oy,
                int oz,
                int limX,
                int limY,
                int limZ) {
            this.snapshot = snapshot;
            this.palette = Set.copyOf(palette);
            this.maxBlocks = maxBlocks;
            this.hardCap = hardCap;
            this.sx = sx;
            this.sy = sy;
            this.sz = sz;
            this.prompt = prompt;
            this.ox = ox;
            this.oy = oy;
            this.oz = oz;
            this.limX = limX;
            this.limY = limY;
            this.limZ = limZ;
        }

        /** Exact snapshot id for scope-local coordinates, or null outside captured rights. */
        String idAt(int x, int y, int z) {
            return snapshot.exact(x + ox, y + oy, z + oz);
        }

        boolean approxAt(int x, int y, int z) {
            return snapshot.marginApproximate(x + ox, y + oy, z + oz);
        }

        boolean protAt(int x, int y, int z) {
            return snapshot.isProtected(x + ox, y + oy, z + oz);
        }
    }

    /** Dispatches one function call by name; errors become {ok:false} results. */
    static JsonObject execute(Scope scope, String name, JsonObject args) {
        try {
            return switch (name) {
                case "read_blocks" -> readBlocks(scope, args);
                case "slice" -> slice(scope, args);
                case "heightmap" -> heightmap(scope, args);
                case "find" -> find(scope, args);
                case "check_plan" -> checkPlan(scope, args);
                case "inspect_plan" -> inspectPlan(scope, args);
                case "plan_slice" -> planSlice(scope, args);
                default -> error("unknown_tool");
            };
        } catch (ToolRefused refused) {
            return error(refused.getMessage());
        }
    }

    private static JsonObject error(String code) {
        var result = new JsonObject();
        result.addProperty("ok", false);
        result.addProperty("error", code);
        return result;
    }

    private record Box(int x0, int y0, int z0, int x1, int y1, int z1) {
        long cells() {
            return (long) (x1 - x0 + 1) * (y1 - y0 + 1) * (z1 - z0 + 1);
        }
    }

    private static Box box(Scope scope, JsonObject args, boolean world) {
        int[] from = point(args.get("from"));
        int[] to = point(args.get("to"));
        int loX = world ? -MARGIN : 0, loY = 0, loZ = world ? -MARGIN : 0;
        int hiX = world ? scope.limX + MARGIN - 1 : scope.sx - 1;
        int hiY = (world ? scope.limY : scope.sy) - 1;
        int hiZ = world ? scope.limZ + MARGIN - 1 : scope.sz - 1;
        if (from == null
                || to == null
                || from[0] > to[0]
                || from[1] > to[1]
                || from[2] > to[2]
                || from[0] < loX
                || from[1] < loY
                || from[2] < loZ
                || to[0] > hiX
                || to[1] > hiY
                || to[2] > hiZ) throw new ToolRefused("box_outside_rights");
        var box = new Box(from[0], from[1], from[2], to[0], to[1], to[2]);
        if (box.cells() > MAX_CALL_CELLS) throw new ToolRefused("box_too_large");
        return box;
    }

    private static int[] point(JsonElement value) {
        if (value == null || !value.isJsonArray() || value.getAsJsonArray().size() != 3)
            return null;
        int[] out = new int[3];
        int i = 0;
        for (var element : value.getAsJsonArray()) {
            if (!element.isJsonPrimitive() || !element.getAsJsonPrimitive().isNumber()) return null;
            try {
                out[i] = element.getAsBigDecimal().intValueExact();
            } catch (ArithmeticException notInteger) {
                return null;
            }
            if (out[i] < -MARGIN || out[i] > 128 + MARGIN) return null;
            i++;
        }
        return out;
    }

    private static JsonObject readBlocks(Scope scope, JsonObject args) {
        var box = box(scope, args, true);
        scope.reads.charge((int) box.cells());
        var columns = new JsonArray();
        boolean partial = false;
        for (int x = box.x0(); x <= box.x1(); x++)
            for (int z = box.z0(); z <= box.z1(); z++) {
                var runs = new JsonArray();
                String open = null;
                int start = 0;
                boolean columnPartial = scope.approxAt(x, box.y0(), z);
                for (int y = box.y0(); y <= box.y1() + 1; y++) {
                    // The past-the-end sentinel is null by design to flush the last run.
                    String id = y > box.y1() ? null : scope.idAt(x, y, z);
                    if (id == null && y <= box.y1()) throw new ToolRefused("box_outside_rights");
                    if (columnPartial && y > box.y0()) id = null;
                    if (id != null && id.equals(open)) continue;
                    if (open != null && !isAir(open)) {
                        var run = new JsonArray();
                        run.add(start);
                        run.add(y - 1);
                        run.add(open);
                        runs.add(run);
                    }
                    open = id;
                    start = y;
                    if (id == null) break;
                }
                if (columnPartial) partial = true;
                if (runs.size() > 0) {
                    var column = new JsonObject();
                    column.addProperty("x", x);
                    column.addProperty("z", z);
                    column.add("runs", runs);
                    columns.add(column);
                }
            }
        var result = new JsonObject();
        result.addProperty("ok", true);
        result.add("columns", columns);
        result.addProperty("cells", box.cells());
        if (partial) result.addProperty("partial", "margin_columns_carry_surface_only");
        return result;
    }

    private static JsonObject slice(Scope scope, JsonObject args) {
        var box = box(scope, args, true);
        String axis = text(args.get("axis"), Set.of("x", "y", "z"));
        if (axis == null) throw new ToolRefused("bad_axis");
        int index =
                switch (axis) {
                    case "x" -> bounded(args, "index", box.x0(), box.x1());
                    case "y" -> bounded(args, "index", box.y0(), box.y1());
                    default -> bounded(args, "index", box.z0(), box.z1());
                };
        if (index == Integer.MIN_VALUE) throw new ToolRefused("bad_index");
        scope.reads.charge((int) box.cells());
        var grid = new ArrayList<String>();
        var legend = new TreeMap<String, String>();
        boolean partial = false;
        int rows =
                switch (axis) {
                    case "x" -> box.z1() - box.z0() + 1;
                    case "y" -> box.z1() - box.z0() + 1;
                    default -> box.y1() - box.y0() + 1;
                };
        int cols =
                switch (axis) {
                    case "x" -> box.y1() - box.y0() + 1;
                    case "y" -> box.x1() - box.x0() + 1;
                    default -> box.x1() - box.x0() + 1;
                };
        for (int r = 0; r < rows; r++) {
            var line = new StringBuilder();
            for (int c = 0; c < cols; c++) {
                int x, y, z;
                if (axis.equals("x")) {
                    x = index;
                    y = box.y1() - c;
                    z = box.z0() + r;
                } else if (axis.equals("y")) {
                    x = box.x0() + c;
                    y = index;
                    z = box.z0() + r;
                } else {
                    x = box.x0() + c;
                    y = box.y1() - r;
                    z = index;
                }
                String id = scope.idAt(x, y, z);
                if (id == null) throw new ToolRefused("box_outside_rights");
                if (scope.approxAt(x, y, z)) partial = true;
                char letter = blockLetter(id);
                line.append(letter);
                if (letter != '.' && letter != '~') legend.putIfAbsent(String.valueOf(letter), id);
            }
            grid.add(line.toString());
        }
        var result = new JsonObject();
        result.addProperty("ok", true);
        var lines = new JsonArray();
        grid.forEach(lines::add);
        result.add("grid", lines);
        var legendJson = new JsonObject();
        legend.forEach(legendJson::addProperty);
        result.add("legend", legendJson);
        if (partial) result.addProperty("partial", "margin_cells_carry_surface_only");
        return result;
    }

    private static JsonObject heightmap(Scope scope, JsonObject args) {
        var box = box(scope, args, true);
        long columns = (long) (box.x1() - box.x0() + 1) * (box.z1() - box.z0() + 1);
        scope.reads.charge((int) Math.min(columns * 2, Integer.MAX_VALUE));
        var values = new JsonArray();
        for (int z = box.z0(); z <= box.z1(); z++)
            for (int x = box.x0(); x <= box.x1(); x++) {
                int top = -1;
                for (int y = box.y1(); y >= box.y0(); y--) {
                    String id = scope.idAt(x, y, z);
                    if (id == null) throw new ToolRefused("box_outside_rights");
                    if (!isAir(id)) {
                        top = y;
                        break;
                    }
                }
                values.add(top);
            }
        var result = new JsonObject();
        result.addProperty("ok", true);
        result.addProperty("stride", 1);
        result.add("values", values);
        return result;
    }

    private static JsonObject find(Scope scope, JsonObject args) {
        var box = box(scope, args, true);
        String want = text(args.get("block"), null);
        if (want == null || want.length() > 128) throw new ToolRefused("bad_block");
        boolean family = !want.contains(":");
        scope.reads.charge((int) box.cells());
        var positions = new JsonArray();
        int count = 0;
        boolean capped = false;
        for (int y = box.y0(); y <= box.y1(); y++)
            for (int z = box.z0(); z <= box.z1(); z++)
                for (int x = box.x0(); x <= box.x1(); x++) {
                    String id = scope.idAt(x, y, z);
                    if (id == null) throw new ToolRefused("box_outside_rights");
                    boolean hit =
                            family
                                    ? GenerateFlow.paletteFamily(id).equals(want)
                                            || id.equals(want)
                                            || id.endsWith(":" + want)
                                    : id.equals(want);
                    if (!hit) continue;
                    count++;
                    if (positions.size() < MAX_FIND_POSITIONS) {
                        var pos = new JsonArray();
                        pos.add(x);
                        pos.add(y);
                        pos.add(z);
                        positions.add(pos);
                    } else capped = true;
                }
        var result = new JsonObject();
        result.addProperty("ok", true);
        result.addProperty("count", count);
        result.add("positions", positions);
        if (capped) result.addProperty("capped", true);
        return result;
    }

    private static JsonObject checkPlan(Scope scope, JsonObject args) {
        if (args.get("plan") == null
                || !args.get("plan").isJsonPrimitive()
                || !args.get("plan").getAsJsonPrimitive().isString()
                || args.get("plan").getAsString().length() > WorkshopGeometry.MAX_JSON)
            throw new ToolRefused("bad_plan");
        String raw = args.get("plan").getAsString();
        WorkshopAiPlan plan;
        try {
            plan =
                    WorkshopAiNormalization.parse(
                            raw,
                            scope.palette,
                            scope.maxBlocks,
                            scope.hardCap,
                            scope.sx,
                            scope.sy,
                            scope.sz);
        } catch (RuntimeException invalid) {
            var result = new JsonObject();
            result.addProperty("ok", false);
            result.addProperty("error", WorkshopAiRejectedPlans.reason(invalid));
            return result;
        }
        int replace = 0;
        var protectedHits = new JsonArray();
        for (var cell : plan.cells()) {
            String before = scope.idAt(cell.x(), cell.y(), cell.z());
            if (before == null) {
                var result = new JsonObject();
                result.addProperty("ok", false);
                result.addProperty("error", "plan_outside_rights");
                return result;
            }
            if (!isAir(before)) {
                String after = cell.resolve(before, false);
                if (after != null && !after.equals(before)) replace++;
            }
            if (scope.protAt(cell.x(), cell.y(), cell.z())
                    && protectedHits.size() < MAX_DIFF_SAMPLES) {
                var pos = new JsonArray();
                pos.add(cell.x());
                pos.add(cell.y());
                pos.add(cell.z());
                protectedHits.add(pos);
            }
        }
        var result = new JsonObject();
        result.addProperty("ok", true);
        var notes = new JsonArray();
        plan.warnings().forEach(notes::add);
        result.add("normalizations", notes);
        result.addProperty("cells", plan.cells().size());
        result.addProperty(
                "operations",
                plan.canonical().isBlank()
                        ? 0
                        : JsonParser.parseString(plan.canonical())
                                .getAsJsonObject()
                                .getAsJsonArray("operations")
                                .size());
        var budget = new JsonObject();
        budget.addProperty("cells", plan.cells().size());
        budget.addProperty("maxBlocks", scope.maxBlocks);
        budget.addProperty("demolishVolume", plan.demolishVolume());
        result.add("budget", budget);
        var diff = new JsonObject();
        diff.addProperty("replaceExisting", replace);
        diff.add("protectedCells", protectedHits);
        result.add("diff", diff);
        scope.working.set(plan.canonical());
        return result;
    }

    private static JsonObject inspectPlan(Scope scope, JsonObject args) {
        if (args.get("plan") == null
                || !args.get("plan").isJsonPrimitive()
                || !args.get("plan").getAsJsonPrimitive().isString()
                || args.get("plan").getAsString().length() > WorkshopGeometry.MAX_MERGED_JSON)
            throw new ToolRefused("bad_plan");
        String raw = args.get("plan").getAsString();
        WorkshopAiPlan plan;
        try {
            plan =
                    WorkshopAiPlan.parseMerged(
                            raw, scope.palette, scope.maxBlocks, scope.sx, scope.sy, scope.sz);
        } catch (RuntimeException invalid) {
            var result = new JsonObject();
            result.addProperty("ok", false);
            result.addProperty("error", WorkshopAiRejectedPlans.reason(invalid));
            return result;
        }
        var report =
                WorkshopAiPlanQuality.report(
                        plan, scope.prompt, null, scope.sx, scope.sy, scope.sz);
        report.addProperty("ok", true);
        scope.working.set(plan.canonical());
        return report;
    }

    private static JsonObject planSlice(Scope scope, JsonObject args) {
        String raw = scope.working.get();
        if (args.has("plan")) {
            if (!args.get("plan").isJsonPrimitive()
                    || !args.get("plan").getAsJsonPrimitive().isString())
                throw new ToolRefused("bad_plan");
            raw = args.get("plan").getAsString();
        }
        if (raw == null || raw.length() > WorkshopGeometry.MAX_MERGED_JSON)
            throw new ToolRefused("bad_plan");
        WorkshopAiPlan plan;
        try {
            plan =
                    WorkshopAiPlan.parseMerged(
                            raw, scope.palette, scope.maxBlocks, scope.sx, scope.sy, scope.sz);
        } catch (RuntimeException invalid) {
            var result = new JsonObject();
            result.addProperty("ok", false);
            result.addProperty("error", WorkshopAiRejectedPlans.reason(invalid));
            return result;
        }
        String axis = text(args.get("axis"), Set.of("x", "y", "z"));
        if (axis == null) throw new ToolRefused("bad_axis");
        int limit =
                axis.equals("x") ? scope.sx - 1 : axis.equals("y") ? scope.sy - 1 : scope.sz - 1;
        int index = bounded(args, "index", 0, limit);
        if (index == Integer.MIN_VALUE) throw new ToolRefused("bad_index");
        var filled = new HashMap<Integer, String>();
        for (var cell : plan.cells()) {
            int at = axis.equals("x") ? cell.x() : axis.equals("y") ? cell.y() : cell.z();
            if (at != index) continue;
            String resolved = cell.resolve("minecraft:air", true);
            if (resolved != null && !isAir(resolved))
                filled.put(WorkshopGeometry.index(cell.x(), cell.y(), cell.z()), resolved);
        }
        int rows = axis.equals("z") ? scope.sy : scope.sz;
        int cols = axis.equals("x") ? scope.sy : scope.sx;
        var lines = new JsonArray();
        var legend = new TreeMap<String, String>();
        for (int r = 0; r < rows; r++) {
            var line = new StringBuilder();
            for (int c = 0; c < cols; c++) {
                int x, y, z;
                if (axis.equals("x")) {
                    x = index;
                    y = scope.sy - 1 - c;
                    z = r;
                } else if (axis.equals("y")) {
                    x = c;
                    y = index;
                    z = r;
                } else {
                    x = c;
                    y = scope.sy - 1 - r;
                    z = index;
                }
                String id = filled.get(WorkshopGeometry.index(x, y, z));
                char letter = id == null ? '.' : blockLetter(id);
                line.append(letter);
                if (letter != '.') legend.putIfAbsent(String.valueOf(letter), id);
            }
            lines.add(line.toString());
        }
        var result = new JsonObject();
        result.addProperty("ok", true);
        result.add("grid", lines);
        var legendJson = new JsonObject();
        legend.forEach(legendJson::addProperty);
        result.add("legend", legendJson);
        return result;
    }

    static boolean isAir(String id) {
        return "minecraft:air".equals(id)
                || "minecraft:cave_air".equals(id)
                || "minecraft:void_air".equals(id);
    }

    /** Same colour letters as the unit 7 silhouettes; water reads as waves, air as dots. */
    static char blockLetter(String id) {
        if (isAir(id)) return '.';
        if (id.contains("water")) return '~';
        if (id.contains("white") || id.contains("quartz") || id.contains("snow")) return 'W';
        if (id.contains("red") || id.contains("brick")) return 'R';
        if (id.contains("yellow") || id.contains("glow") || id.contains("lantern")) return 'Y';
        if (id.contains("moss") || id.contains("leaves")) return 'G';
        if (id.contains("blue") || id.contains("ice") || id.contains("prismarine")) return 'B';
        if (id.contains("black") || id.contains("deepslate")) return 'K';
        if (id.contains("wood")
                || id.contains("log")
                || id.contains("planks")
                || id.contains("dirt")) return 'D';
        return 'S';
    }

    private static String text(JsonElement value, Set<String> allowed) {
        if (value == null || !value.isJsonPrimitive() || !value.getAsJsonPrimitive().isString())
            return null;
        String text = value.getAsString();
        return allowed == null || allowed.contains(text) ? text : null;
    }

    private static int bounded(JsonObject args, String name, int min, int max) {
        var value = args.get(name);
        if (value == null || !value.isJsonPrimitive() || !value.getAsJsonPrimitive().isNumber())
            return Integer.MIN_VALUE;
        try {
            int parsed = value.getAsBigDecimal().intValueExact();
            return parsed >= min && parsed <= max ? parsed : Integer.MIN_VALUE;
        } catch (ArithmeticException notInteger) {
            return Integer.MIN_VALUE;
        }
    }

    /** Records one captured inside column into an immutable snapshot; zero extra world reads. */
    static Snapshot snapshot(
            int sx,
            int sy,
            int sz,
            Map<List<Integer>, List<String>> columns,
            Map<List<Integer>, String> shellSurface,
            Set<List<Integer>> protectedCells) {
        var blocks = new HashMap<Integer, String>();
        columns.forEach(
                (at, ids) -> {
                    for (int y = 0; y < ids.size() && y < sy; y++)
                        blocks.put(Snapshot.key(at.get(0), y, at.get(1)), ids.get(y));
                });
        var surface = new HashMap<Integer, String>();
        shellSurface.forEach((at, id) -> surface.put(Snapshot.key(at.get(0), 0, at.get(1)), id));
        var guarded = new HashSet<Integer>();
        for (var at : protectedCells) guarded.add(Snapshot.key(at.get(0), at.get(1), at.get(2)));
        return new Snapshot(sx, sy, sz, blocks, surface, guarded);
    }
}
