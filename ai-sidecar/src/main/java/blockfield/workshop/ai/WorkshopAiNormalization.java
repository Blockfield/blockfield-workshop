package blockfield.workshop.ai;

import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import com.lunares.blockfield.workshop.WorkshopGeometry;

import java.util.ArrayList;
import java.util.List;
import java.util.Set;

/** Model-output recovery only; stored plans and paid patches retain the strict parser. */
final class WorkshopAiNormalization {
    static WorkshopAiPlan parse(
            String json, Set<String> palette, int budget, int hardCap, int sx, int sy, int sz) {
        List<String> notes = new ArrayList<>();
        JsonObject root = object(json, WorkshopGeometry.MAX_JSON, notes);
        fields(root, Set.of("version", "operations"), notes);
        int version = WorkshopAiPlanning.integer(root.get("version"));
        if (version < 1 || version > 3) throw new IllegalArgumentException("Plan version");
        root.add(
                "operations",
                operations(
                        root.getAsJsonArray("operations"),
                        version,
                        new int[] {sx, sy, sz},
                        notes,
                        true,
                        new long[1]));
        WorkshopAiPlan plan;
        try {
            plan = WorkshopAiPlan.parse(root.toString(), palette, budget, sx, sy, sz);
        } catch (IllegalArgumentException failure) {
            if (!"Plan exceeds block budget".equals(failure.getMessage()) || budget >= hardCap)
                throw failure;
            // Only a plan already inside the hard world cap may lose trailing decorative ops.
            WorkshopAiPlan.parse(root.toString(), palette, hardCap, sx, sy, sz);
            JsonArray ops = root.getAsJsonArray("operations");
            while (true) {
                if (ops.size() <= 1) throw failure;
                ops.remove(ops.size() - 1);
                try {
                    plan = WorkshopAiPlan.parse(root.toString(), palette, budget, sx, sy, sz);
                    break;
                } catch (IllegalArgumentException retry) {
                    if (!"Plan exceeds block budget".equals(retry.getMessage())) throw retry;
                }
            }
            note(notes, "trimmed_details");
        }
        return new WorkshopAiPlan(plan.cells(), plan.canonical(), plan.demolishVolume(), 0, notes);
    }

    static JsonObject object(String json, int cap, List<String> notes) {
        try {
            return WorkshopGeometry.object(json, cap);
        } catch (RuntimeException invalid) {
            if (json == null || json.length() > cap) throw invalid;
            StringBuilder fixed = new StringBuilder();
            List<Character> closers = new ArrayList<>();
            boolean quoted = false, escaped = false;
            for (int i = 0; i < json.length(); i++) {
                char c = json.charAt(i);
                if (quoted) {
                    if (escaped) escaped = false;
                    else if (c == '\\') escaped = true;
                    else if (c == '"') quoted = false;
                } else if (c == '"') quoted = true;
                else if (c == '{' || c == '[') {
                    closers.add(c == '{' ? '}' : ']');
                    if (closers.size() > 16) throw invalid;
                } else if (c == '}' || c == ']') {
                    if (closers.isEmpty() || closers.removeLast() != c) throw invalid;
                } else if (c == ',') {
                    int next = i + 1;
                    while (next < json.length() && Character.isWhitespace(json.charAt(next)))
                        next++;
                    if (next < json.length()
                            && (json.charAt(next) == '}' || json.charAt(next) == ']')) continue;
                }
                fixed.append(c);
            }
            // A cut tail may omit closing containers, never a string, number or operation.
            String tail = fixed.toString().stripTrailing();
            if (quoted || (!closers.isEmpty() && !tail.endsWith("}") && !tail.endsWith("]")))
                throw invalid;
            for (int i = closers.size() - 1; i >= 0; i--) fixed.append(closers.get(i));
            JsonObject recovered = WorkshopGeometry.object(fixed.toString(), cap);
            note(notes, "json_recovered");
            return recovered;
        }
    }

    private static JsonArray operations(
            JsonArray raw,
            int version,
            int[] sides,
            List<String> notes,
            boolean clip,
            long[] work) {
        if (raw == null || raw.isEmpty()) throw new IllegalArgumentException("Empty operations");
        JsonArray result = new JsonArray();
        for (var element : raw) {
            JsonObject op = element.getAsJsonObject();
            String type = version == 3 ? WorkshopAiPlanning.text(op.get("type"), 16) : "cuboid";
            if (type.equals("mirror") || type.equals("repeat")) {
                fields(
                        op,
                        type.equals("mirror")
                                ? Set.of("type", "axes", "operations")
                                : Set.of("type", "count", "offset", "operations"),
                        notes);
                op.add(
                        "operations",
                        operations(
                                op.getAsJsonArray("operations"),
                                version,
                                sides,
                                notes,
                                clip && type.equals("mirror"),
                                work));
                if (clip && type.equals("repeat")) {
                    try {
                        charge(
                                work,
                                WorkshopGeometry.expand(
                                                WorkshopAiPlanning.plan(one(op)).toString(),
                                                32768,
                                                sides[0],
                                                sides[1],
                                                sides[2],
                                                v -> {})
                                        .work());
                    } catch (IllegalArgumentException invalid) {
                        if (!Set.of("Operation outside selection", "Tube outside selection")
                                .contains(invalid.getMessage())) throw invalid;
                        int count = WorkshopAiPlanning.integer(op.get("count"));
                        int[] step = WorkshopAiPlanning.point(op.getAsJsonArray("offset")).values();
                        if (count < 1 || count > 128) throw invalid;
                        for (int value : step) if (Math.abs((long) value) >= 128) throw invalid;
                        for (int copy = 0; copy < count; copy++) {
                            JsonArray children = op.getAsJsonArray("operations").deepCopy();
                            WorkshopAiPlanning.shift(
                                    children,
                                    new WorkshopAiPlanning.Point(
                                            copy * step[0], copy * step[1], copy * step[2]));
                            for (var child :
                                    operations(children, version, sides, notes, true, work))
                                result.add(child);
                            if (result.size() > WorkshopGeometry.MAX_OPERATIONS)
                                throw new IllegalArgumentException("Too many operations");
                        }
                        note(notes, "clipped_operations");
                        continue;
                    }
                }
                result.add(op);
                continue;
            }
            if (!Set.of("cuboid", "ellipsoid", "tube").contains(type))
                throw new IllegalArgumentException("Unknown primitive");
            var keys = new java.util.HashSet<>(Set.of("block", "hollow"));
            if (version == 3) keys.add("type");
            if (version >= 2 && op.has("match")) keys.add("match");
            keys.addAll(
                    type.equals("tube")
                            ? Set.of("path", "curve", "radiusStart", "radiusEnd")
                            : Set.of("from", "to"));
            fields(op, keys, notes);
            if (op.has("match")) {
                String match = WorkshopAiPlanning.text(op.get("match"), 256);
                if (!match.equals("air")
                        && !match.equals("any")
                        && !WorkshopAiPlan.registered(match)) {
                    op.addProperty("match", "air");
                    note(notes, "match_normalized");
                }
            }
            if (!clip) {
                result.add(op);
                continue;
            }
            if (!type.equals("cuboid")) {
                try {
                    charge(
                            work,
                            WorkshopGeometry.expand(
                                            WorkshopAiPlanning.plan(one(op)).toString(),
                                            32768,
                                            sides[0],
                                            sides[1],
                                            sides[2],
                                            v -> {})
                                    .work());
                    result.add(op);
                } catch (IllegalArgumentException invalid) {
                    if (!Set.of("Operation outside selection", "Tube outside selection")
                            .contains(invalid.getMessage())) throw invalid;
                    for (var box : clippedCurve(op, sides, work)) result.add(box);
                    note(notes, "clipped_operations");
                }
                continue;
            }
            int[] from = WorkshopAiPlanning.point(op.getAsJsonArray("from")).values();
            int[] to = WorkshopAiPlanning.point(op.getAsJsonArray("to")).values();
            boolean outside = false, empty = false;
            int[] lo = new int[3], hi = new int[3];
            for (int a = 0; a < 3; a++) {
                if (from[a] > to[a])
                    throw new IllegalArgumentException("Operation outside selection");
                lo[a] = Math.max(0, from[a]);
                hi[a] = Math.min(sides[a] - 1, to[a]);
                empty |= lo[a] > hi[a];
                outside |= lo[a] != from[a] || hi[a] != to[a];
            }
            if (!outside) result.add(op);
            else {
                note(notes, "clipped_operations");
                if (empty) continue;
                if (!op.get("hollow").getAsBoolean()) result.add(box(op, lo, hi));
                else {
                    // Intersect the original six faces; clipping must not invent a new wall.
                    for (int a = 0; a < 3; a++)
                        for (int face : new int[] {from[a], to[a]}) {
                            if (face < lo[a] || face > hi[a]) continue;
                            int[] f = lo.clone(), t = hi.clone();
                            f[a] = t[a] = face;
                            result.add(box(op, f, t));
                        }
                }
            }
        }
        return result;
    }

    private static void charge(long[] work, long count) {
        work[0] += count;
        if (work[0] > WorkshopGeometry.MAX_WORK)
            throw new IllegalArgumentException("Plan exceeds expansion work budget");
    }

    private static JsonArray one(JsonObject op) {
        JsonArray array = new JsonArray();
        array.add(op);
        return array;
    }

    private static JsonArray clippedCurve(JsonObject op, int[] sides, long[] work) {
        int[] lo = {Integer.MAX_VALUE, Integer.MAX_VALUE, Integer.MAX_VALUE},
                hi = {Integer.MIN_VALUE, Integer.MIN_VALUE, Integer.MIN_VALUE};
        boolean tube = op.get("type").getAsString().equals("tube");
        JsonArray points = tube ? op.getAsJsonArray("path") : new JsonArray();
        if (!tube) {
            points.add(op.get("from"));
            points.add(op.get("to"));
        }
        int radius =
                tube
                        ? Math.max(
                                WorkshopAiPlanning.integer(op.get("radiusStart")),
                                WorkshopAiPlanning.integer(op.get("radiusEnd")))
                        : 0;
        if (radius < 0 || radius > 64) throw new IllegalArgumentException("Invalid tube radius");
        for (var point : points) {
            int[] p = WorkshopAiPlanning.point(point.getAsJsonArray()).values();
            for (int a = 0; a < 3; a++) {
                if (p[a] < -128 || p[a] > 255)
                    throw new IllegalArgumentException("Operation outside selection");
                lo[a] = Math.min(lo[a], p[a] - radius);
                hi[a] = Math.max(hi[a], p[a] + radius);
            }
        }
        for (int a = 0; a < 3; a++)
            if ((long) hi[a] - lo[a] >= 128)
                throw new IllegalArgumentException("Operation outside selection");
        JsonArray shifted = one(op.deepCopy());
        WorkshopAiPlanning.shift(shifted, new WorkshopAiPlanning.Point(-lo[0], -lo[1], -lo[2]));
        // Rasterize unchanged curved geometry, then intersect; clamping its axes would reshape it.
        var rows = new java.util.TreeMap<Integer, java.util.TreeSet<Integer>>();
        var geometry =
                WorkshopGeometry.expand(
                        WorkshopAiPlanning.plan(shifted).toString(),
                        32768,
                        128,
                        128,
                        128,
                        v -> {
                            int x = v.x() + lo[0], y = v.y() + lo[1], z = v.z() + lo[2];
                            if (x >= 0
                                    && y >= 0
                                    && z >= 0
                                    && x < sides[0]
                                    && y < sides[1]
                                    && z < sides[2])
                                rows.computeIfAbsent(y * 128 + x, k -> new java.util.TreeSet<>())
                                        .add(z);
                        });
        charge(work, geometry.work());
        JsonArray result = new JsonArray();
        JsonObject template = op.deepCopy();
        template.remove("path");
        template.remove("curve");
        template.remove("radiusStart");
        template.remove("radiusEnd");
        template.addProperty("type", "cuboid");
        rows.forEach(
                (row, zs) -> {
                    int start = -1, last = -1;
                    for (int z : zs) {
                        if (start >= 0 && z != last + 1) {
                            result.add(
                                    box(
                                            template,
                                            new int[] {row % 128, row / 128, start},
                                            new int[] {row % 128, row / 128, last}));
                            start = -1;
                        }
                        if (start < 0) start = z;
                        last = z;
                    }
                    if (start >= 0)
                        result.add(
                                box(
                                        template,
                                        new int[] {row % 128, row / 128, start},
                                        new int[] {row % 128, row / 128, last}));
                });
        if (result.size() > WorkshopGeometry.MAX_OPERATIONS)
            throw new IllegalArgumentException("Too many operations");
        return result;
    }

    private static JsonObject box(JsonObject source, int[] from, int[] to) {
        JsonObject copy = source.deepCopy();
        copy.add("from", WorkshopAiPlanning.array(from[0], from[1], from[2]));
        copy.add("to", WorkshopAiPlanning.array(to[0], to[1], to[2]));
        copy.addProperty("hollow", false);
        return copy;
    }

    private static void fields(JsonObject object, Set<String> keys, List<String> notes) {
        for (String key : List.copyOf(object.keySet()))
            if (!keys.contains(key)) {
                object.remove(key);
                note(notes, "unknown_fields_removed");
            }
    }

    private static void note(List<String> notes, String note) {
        if (!notes.contains(note)) notes.add(note);
    }
}
