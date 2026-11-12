package blockfield.workshop.ai;

import com.lunares.blockfield.workshop.WorkshopGeometry;

import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Locale;
import java.util.Set;

/** Free geometric feedback in an empty-world projection; never guesses likeness or anatomy. */
record WorkshopAiPlanQuality(
        List<String> issues,
        int width,
        int height,
        int depth,
        int components,
        WorkshopAiFlatness flatness) {
    WorkshopAiPlanQuality(List<String> issues, int width, int height, int depth, int components) {
        this(issues, width, height, depth, components, WorkshopAiFlatness.EMPTY);
    }

    static WorkshopAiPlanQuality inspect(
            WorkshopAiPlan plan, String prompt, int sx, int sy, int sz) {
        return inspect(plan, prompt, sx, sy, sz, WorkshopAiFlatness.Settings.defaults());
    }

    static boolean architectural(String prompt) {
        String p = prompt.toLowerCase(Locale.ROOT);
        return WorkshopAiPlanning.subjectClass(prompt) != WorkshopAiPlanning.SubjectClass.ORGANIC
                && p.matches(
                        "(?s).*(architect|building|house|high.?rise|skyscraper|castle|palace|temple|bridge|wall|fence|colonnad|arcade|aqueduct|tower|gate|storeys|stories|floors|архитект|здани|дом|небоскр|многоэтаж|замок|дворец|храм|мост|стен[ауы]|забор|ограда|колоннад|аркад|акведук|башн|ворот).*");
    }

    static WorkshopAiPlanQuality inspect(
            WorkshopAiPlan plan,
            String prompt,
            int sx,
            int sy,
            int sz,
            WorkshopAiFlatness.Settings settings) {
        Set<Integer> solid = new HashSet<>();
        var materials = new java.util.HashMap<Integer, String>();
        int minWater = 128, maxWater = -1;
        List<String> issues = new ArrayList<>(plan.warnings());
        int[] min = {128, 128, 128}, max = {-1, -1, -1};
        for (var cell : plan.cells()) {
            String block = cell.resolve("minecraft:air", true);
            if ("minecraft:water".equals(block)) {
                minWater = Math.min(minWater, cell.y());
                maxWater = Math.max(maxWater, cell.y());
            }
            if (block == null
                    || Set.of(
                                    "minecraft:air",
                                    "minecraft:cave_air",
                                    "minecraft:void_air",
                                    "minecraft:water")
                            .contains(block)) continue;
            solid.add(WorkshopGeometry.index(cell.x(), cell.y(), cell.z()));
            materials.put(WorkshopGeometry.index(cell.x(), cell.y(), cell.z()), block);
            int[] p = {cell.x(), cell.y(), cell.z()};
            for (int axis = 0; axis < 3; axis++) {
                min[axis] = Math.min(min[axis], p[axis]);
                max[axis] = Math.max(max[axis], p[axis]);
            }
        }
        // shortcut: no live block registry in the sidecar, so the water projection
        // (WorkshopAiWaterPreview.emptyProjection) cannot run here; water_escape /
        // water_incomplete / flat_water detection stays mod-side at apply validation.
        // Revisit when the snapshot carries a flow-simulation surrogate.
        String text = prompt.toLowerCase(Locale.ROOT);
        if (solid.isEmpty()) return new WorkshopAiPlanQuality(List.copyOf(issues), 0, 0, 0, 0);
        int[] extent = {max[0] - min[0] + 1, max[1] - min[1] + 1, max[2] - min[2] + 1};
        var flatness = WorkshopAiFlatness.inspect(materials, min, max);
        if (architectural(prompt) && flatness.exceeds(settings)) issues.add("flatness");
        int components = components(solid);
        if (components > 1) issues.add("disconnected");
        if (mirrorGap(plan, solid, sx, sz)) issues.add("mirror_seam");
        boolean entrance =
                text.matches("(?s).*(entrance|doorway|entry|door|вход|двер|вхід|двері).*");
        if (entrance && !entrance(solid, min, max)) issues.add("entrance");
        return new WorkshopAiPlanQuality(
                List.copyOf(issues), extent[0], extent[1], extent[2], components, flatness);
    }

    private static Set<String> axes(WorkshopAiPlan plan) {
        Set<String> axes = new HashSet<>();
        if (plan.canonical().isBlank()) return axes;
        var root = com.google.gson.JsonParser.parseString(plan.canonical()).getAsJsonObject();
        for (var element : root.getAsJsonArray("operations")) {
            var op = element.getAsJsonObject();
            if (op.has("type") && op.get("type").getAsString().equals("mirror"))
                axes.add(op.get("axes").getAsString());
        }
        if (root.has("transforms"))
            for (var frame : root.getAsJsonArray("transforms"))
                if (frame.getAsJsonArray().get(2).getAsInt() % 2 != 0) {
                    var rotated = new HashSet<String>();
                    for (String axis : axes)
                        rotated.add(axis.equals("xz") ? axis : axis.equals("x") ? "z" : "x");
                    axes = rotated;
                }
        return axes;
    }

    private static boolean mirrorGap(WorkshopAiPlan plan, Set<Integer> solid, int sx, int sz) {
        var axes = axes(plan);
        var materials = new java.util.HashMap<Integer, String>();
        for (var cell : plan.cells())
            if (solid.contains(WorkshopGeometry.index(cell.x(), cell.y(), cell.z())))
                materials.put(
                        WorkshopGeometry.index(cell.x(), cell.y(), cell.z()),
                        cell.resolve("minecraft:air", true));
        for (int axis : new int[] {0, 2}) {
            if (axes.stream().noneMatch(a -> a.contains(axis == 0 ? "x" : "z"))) continue;
            int side = axis == 0 ? sx : sz, centre = (side - 1) / 2;
            for (int key : solid) {
                int x = WorkshopGeometry.x(key),
                        y = WorkshopGeometry.y(key),
                        z = WorkshopGeometry.z(key);
                int coordinate = axis == 0 ? x : z, gap = centre - coordinate;
                if (gap < 1 || gap > 2) continue;
                int twin =
                        axis == 0
                                ? WorkshopGeometry.index(side - 1 - x, y, z)
                                : WorkshopGeometry.index(x, y, side - 1 - z);
                if (!solid.contains(twin)
                        || !WorkshopAiPlan.id(materials.get(key))
                                .equals(WorkshopAiPlan.id(materials.get(twin)))) continue;
                boolean empty = true;
                for (int c = coordinate + 1; c < side - 1 - coordinate; c++)
                    if (solid.contains(
                            axis == 0
                                    ? WorkshopGeometry.index(c, y, z)
                                    : WorkshopGeometry.index(x, y, c))) {
                        empty = false;
                        break;
                    }
                if (empty) return true;
            }
        }
        return false;
    }

    private static int components(Set<Integer> solid) {
        Set<Integer> remaining = new HashSet<>(solid);
        ArrayDeque<Integer> queue = new ArrayDeque<>();
        int count = 0;
        while (!remaining.isEmpty()) {
            count++;
            int seed = remaining.iterator().next();
            remaining.remove(seed);
            queue.add(seed);
            while (!queue.isEmpty()) {
                int p = queue.removeFirst();
                for (int next : neighbors(p)) if (remaining.remove(next)) queue.addLast(next);
            }
        }
        return count;
    }

    /**
     * Full cell membership stays local; only bounded component summaries and silhouettes go
     * upstream.
     */
    static com.google.gson.JsonObject report(
            WorkshopAiPlan plan,
            String prompt,
            WorkshopAiPlanning.Layout layout,
            int sx,
            int sy,
            int sz) {
        return report(plan, prompt, layout, sx, sy, sz, WorkshopAiFlatness.Settings.defaults());
    }

    static com.google.gson.JsonObject report(
            WorkshopAiPlan plan,
            String prompt,
            WorkshopAiPlanning.Layout layout,
            int sx,
            int sy,
            int sz,
            WorkshopAiFlatness.Settings settings) {
        var report = new com.google.gson.JsonObject();
        var quality = inspect(plan, prompt, sx, sy, sz, settings);
        report.add("flatness", quality.flatness().report(settings));
        report.addProperty("cells", plan.cells().size());
        report.addProperty("components", quality.components());
        report.add(
                "extent",
                WorkshopAiPlanning.array(quality.width(), quality.height(), quality.depth()));
        var warnings = new com.google.gson.JsonArray();
        quality.issues().forEach(warnings::add);
        var blocks = new java.util.TreeMap<Integer, String>();
        for (var c : plan.cells()) {
            String b = c.resolve("minecraft:air", true);
            if (b != null
                    && !b.endsWith(":air")
                    && !b.endsWith("_air")
                    && !b.equals("minecraft:water"))
                blocks.put(WorkshopGeometry.index(c.x(), c.y(), c.z()), b);
        }
        var labels = new java.util.HashMap<Integer, Integer>();
        Set<Integer> remaining = new java.util.TreeSet<>(blocks.keySet());
        var components = new java.util.ArrayList<com.google.gson.JsonObject>();
        int label = 0;
        while (!remaining.isEmpty()) {
            int seed = remaining.iterator().next();
            remaining.remove(seed);
            ArrayDeque<Integer> queue = new ArrayDeque<>();
            queue.add(seed);
            int size = 0;
            int[] lo = {128, 128, 128}, hi = {-1, -1, -1};
            while (!queue.isEmpty()) {
                int key = queue.removeFirst();
                labels.put(key, label);
                size++;
                int[] p = {
                    WorkshopGeometry.x(key), WorkshopGeometry.y(key), WorkshopGeometry.z(key)
                };
                for (int axis = 0; axis < 3; axis++) {
                    lo[axis] = Math.min(lo[axis], p[axis]);
                    hi[axis] = Math.max(hi[axis], p[axis]);
                }
                for (int n : neighbors(key)) if (remaining.remove(n)) queue.add(n);
            }
            var c = new com.google.gson.JsonObject();
            c.addProperty("size", size);
            c.add("from", WorkshopAiPlanning.array(lo[0], lo[1], lo[2]));
            c.add("to", WorkshopAiPlanning.array(hi[0], hi[1], hi[2]));
            components.add(c);
            label++;
        }
        components.sort(java.util.Comparator.comparingInt(c -> -c.get("size").getAsInt()));
        var summaries = new com.google.gson.JsonArray();
        components.stream().limit(64).forEach(summaries::add);
        report.add("componentBoxes", summaries);
        report.addProperty("omittedComponents", Math.max(0, components.size() - 64));
        var anchors = new com.google.gson.JsonArray();
        var seams = new com.google.gson.JsonArray();
        if (layout != null) {
            for (var a : layout.anchors()) {
                for (int copy = 0; copy < 4; copy++) {
                    int mask =
                            layout.symmetry().equals("xz")
                                    ? 3
                                    : layout.symmetry().equals("x")
                                            ? 1
                                            : layout.symmetry().equals("z") ? 2 : 0;
                    if ((copy & mask) != copy) continue;
                    var f = a.first();
                    var s = a.second();
                    int fx = (copy & 1) == 0 ? f.x() : sx - 1 - f.x(),
                            fz = (copy & 2) == 0 ? f.z() : sz - 1 - f.z();
                    int tx = (copy & 1) == 0 ? s.x() : sx - 1 - s.x(),
                            tz = (copy & 2) == 0 ? s.z() : sz - 1 - s.z();
                    Integer first = labels.get(WorkshopGeometry.index(fx, f.y(), fz)),
                            second = labels.get(WorkshopGeometry.index(tx, s.y(), tz));
                    if (first == null || !first.equals(second)) {
                        var missing = new com.google.gson.JsonObject();
                        missing.addProperty("parts", a.a() + ":" + a.b());
                        missing.add("first", WorkshopAiPlanning.array(fx, f.y(), fz));
                        missing.add("second", WorkshopAiPlanning.array(tx, s.y(), tz));
                        anchors.add(missing);
                    }
                }
            }
            for (int i = 0; i < layout.parts().size(); i++)
                for (int j = i + 1; j < layout.parts().size(); j++) {
                    var a = layout.parts().get(i);
                    var b = layout.parts().get(j);
                    int[] lo = {
                        Math.max(a.from().x(), b.from().x()) - 1,
                        Math.max(a.from().y(), b.from().y()) - 1,
                        Math.max(a.from().z(), b.from().z()) - 1
                    };
                    int[] hi = {
                        Math.min(a.to().x(), b.to().x()) + 1,
                        Math.min(a.to().y(), b.to().y()) + 1,
                        Math.min(a.to().z(), b.to().z()) + 1
                    };
                    if (lo[0] <= hi[0] && lo[1] <= hi[1] && lo[2] <= hi[2]) {
                        var box = new com.google.gson.JsonObject();
                        box.add(
                                "from",
                                WorkshopAiPlanning.array(
                                        Math.max(0, lo[0]),
                                        Math.max(0, lo[1]),
                                        Math.max(0, lo[2])));
                        box.add(
                                "to",
                                WorkshopAiPlanning.array(
                                        Math.min(sx - 1, hi[0]),
                                        Math.min(sy - 1, hi[1]),
                                        Math.min(sz - 1, hi[2])));
                        seams.add(box);
                    }
                }
            if (layout.symmetry().contains("x"))
                seam(seams, (sx - 1) / 2, 0, 0, sx / 2, sy - 1, sz - 1);
            if (layout.symmetry().contains("z"))
                seam(seams, 0, 0, (sz - 1) / 2, sx - 1, sy - 1, sz / 2);
        }
        if (layout == null) {
            for (String axis : axes(plan)) {
                if (axis.contains("x"))
                    seam(
                            seams,
                            Math.max(0, (sx - 1) / 2 - 2),
                            0,
                            0,
                            Math.min(sx - 1, sx / 2 + 2),
                            sy - 1,
                            sz - 1);
                if (axis.contains("z"))
                    seam(
                            seams,
                            0,
                            0,
                            Math.max(0, (sz - 1) / 2 - 2),
                            sx - 1,
                            sy - 1,
                            Math.min(sz - 1, sz / 2 + 2));
            }
        }
        if (!anchors.isEmpty()) warnings.add("anchors");
        report.add("unconnectedAnchors", anchors);
        report.add("seamBoxes", seams);
        report.add("warnings", warnings);
        var legend = new com.google.gson.JsonObject();
        blocks.values().stream()
                .distinct()
                .sorted()
                .forEach(b -> legend.addProperty(b, String.valueOf(colour(b))));
        report.add("colourLetters", legend);
        report.addProperty("front", silhouette(blocks, sx, sy, sz, 0));
        report.addProperty("side", silhouette(blocks, sx, sy, sz, 1));
        report.addProperty("top", silhouette(blocks, sx, sy, sz, 2));
        return report;
    }

    private static void seam(
            com.google.gson.JsonArray seams, int x, int y, int z, int tx, int ty, int tz) {
        var box = new com.google.gson.JsonObject();
        box.add("from", WorkshopAiPlanning.array(x, y, z));
        box.add("to", WorkshopAiPlanning.array(tx, ty, tz));
        seams.add(box);
    }

    private static char colour(String b) {
        if (b.contains("white") || b.contains("quartz") || b.contains("snow")) return 'W';
        if (b.contains("red") || b.contains("brick")) return 'R';
        if (b.contains("yellow") || b.contains("glow") || b.contains("lantern")) return 'Y';
        if (b.contains("moss") || b.contains("leaves")) return 'G';
        if (b.contains("blue") || b.contains("ice") || b.contains("prismarine")) return 'B';
        if (b.contains("black") || b.contains("deepslate")) return 'K';
        if (b.contains("wood") || b.contains("log") || b.contains("planks") || b.contains("dirt"))
            return 'D';
        return 'S';
    }

    private static String silhouette(
            java.util.Map<Integer, String> blocks, int sx, int sy, int sz, int view) {
        int width = Math.min(64, view == 1 ? sz : sx), height = Math.min(64, view == 2 ? sz : sy);
        char[][] grid = new char[height][width];
        int[][] depth = new int[height][width];
        for (int i = 0; i < height; i++) {
            java.util.Arrays.fill(grid[i], '.');
            java.util.Arrays.fill(depth[i], -1);
        }
        blocks.forEach(
                (key, b) -> {
                    int x = WorkshopGeometry.x(key),
                            y = WorkshopGeometry.y(key),
                            z = WorkshopGeometry.z(key);
                    int col = (view == 1 ? z : x) * width / (view == 1 ? sz : sx),
                            row = (view == 2 ? z : y) * height / (view == 2 ? sz : sy),
                            d = view == 0 ? z : view == 1 ? x : y;
                    if (d >= depth[row][col]) {
                        depth[row][col] = d;
                        grid[row][col] = colour(b);
                    }
                });
        var text = new StringBuilder();
        for (int row = height - 1; row >= 0; row--) {
            text.append(grid[row]);
            if (row > 0) text.append('\n');
        }
        return text.toString();
    }

    private static int[] neighbors(int p) {
        int x = WorkshopGeometry.x(p), y = WorkshopGeometry.y(p), z = WorkshopGeometry.z(p);
        return new int[] {
            x > 0 ? p - 128 : -1,
            x < 127 ? p + 128 : -1,
            y > 0 ? p - 16384 : -1,
            y < 127 ? p + 16384 : -1,
            z > 0 ? p - 1 : -1,
            z < 127 ? p + 1 : -1
        };
    }

    private static boolean entrance(Set<Integer> solid, int[] min, int[] max) {
        // Only scan actual low-level two-high portals into the bounding footprint, not roof holes.
        for (int y = min[1] + 1; y <= Math.min(max[1] - 1, min[1] + 8); y++)
            for (int x = min[0]; x <= max[0]; x++)
                for (int z = min[2]; z <= max[2]; z++) {
                    if (x != min[0] && x != max[0] && z != min[2] && z != max[2]) continue;
                    if (!walkable(solid, x, y, z)) continue;
                    int dx = x == min[0] ? 1 : x == max[0] ? -1 : 0;
                    int dz = z == min[2] ? 1 : z == max[2] ? -1 : 0;
                    if (dx != 0
                            && z > min[2]
                            && z < max[2]
                            && x + 2 * dx > min[0]
                            && x + 2 * dx < max[0]
                            && walkable(solid, x + dx, y, z)
                            && walkable(solid, x + 2 * dx, y, z)) return true;
                    if (dz != 0
                            && x > min[0]
                            && x < max[0]
                            && z + 2 * dz > min[2]
                            && z + 2 * dz < max[2]
                            && walkable(solid, x, y, z + dz)
                            && walkable(solid, x, y, z + 2 * dz)) return true;
                }
        return false;
    }

    private static boolean walkable(Set<Integer> solid, int x, int y, int z) {
        if (x < 0 || z < 0 || x >= 128 || z >= 128 || y < 1 || y >= 127) return false;
        return solid.contains(WorkshopGeometry.index(x, y - 1, z))
                && !solid.contains(WorkshopGeometry.index(x, y, z))
                && !solid.contains(WorkshopGeometry.index(x, y + 1, z));
    }

    /**
     * Sidecar-local advice: issue keys only (no lang tables here). The mod localizes the same keys
     * for display; the polish gate reads {@link #issues}, never this text.
     */
    String advice(boolean ru) {
        return advice(ru, true);
    }

    String advice(boolean ru, boolean withWater) {
        if (issues.isEmpty()) return "";
        List<String> messages = new ArrayList<>(issues);
        return "quality[" + String.join(";", messages) + "]";
    }
}
