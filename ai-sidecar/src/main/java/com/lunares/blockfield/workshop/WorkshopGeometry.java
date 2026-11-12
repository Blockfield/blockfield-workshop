package com.lunares.blockfield.workshop;

import com.google.gson.JsonArray;
import com.google.gson.JsonElement;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;

import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Set;
import java.util.function.Consumer;

/** Closed, deterministic grid language shared by server validation and client preview. */
public final class WorkshopGeometry {
    public static final int MAX_JSON = 65_536;
    public static final int MAX_OPERATIONS = 128;
    public static final int MAX_MERGED_JSON = 524_288;
    public static final int MAX_MERGED_OPERATIONS = 1024;
    public static final int MAX_INSTANCES = 4096;
    public static final int MAX_WORK = 8_388_608;
    public static final int SIDE = 128;
    public static final String ID = "[a-z0-9_.-]+:[a-z0-9_/.-]+";

    public record Voxel(int x, int y, int z, String block, String match) {}

    public record Box(int x0, int y0, int z0, int x1, int y1, int z1, String block) {}

    public record Write(String block, String match) {}

    /**
     * Sequential per-cell resolution shared by server snapshot and client preview: {@code air}
     * matches test the original world, block ids test the last successful planned value. Null when
     * no write applies, so a failed match keeps the earlier value instead of overwriting it.
     */
    public static String resolve(List<Write> writes, String beforeId, boolean beforeIsAir) {
        String result = null;
        for (var write : writes) {
            String current = result == null ? beforeId : result;
            boolean currentIsAir = result == null ? beforeIsAir : isAir(current);
            boolean applies =
                    "air".equals(write.match())
                            ? beforeIsAir
                            : matches(write.match(), current, currentIsAir);
            if (applies) result = write.block();
        }
        return result;
    }

    public static boolean matches(String match, String beforeId, boolean beforeIsAir) {
        if ("any".equals(match)) return true;
        if ("air".equals(match) || "minecraft:air".equals(match)) return beforeIsAir;
        return match.equals(WorkshopBlockStates.id(beforeId));
    }

    private static boolean isAir(String id) {
        return "minecraft:air".equals(id)
                || "minecraft:cave_air".equals(id)
                || "minecraft:void_air".equals(id);
    }

    public record Result(
            int version, String canonical, List<Box> boxes, int work, List<String> materials) {}

    private int work, templates, instances;
    private long seed;
    private final java.util.Map<String, WorkshopMaterials> materials =
            new java.util.LinkedHashMap<>();
    private int reflection;
    private final int operationLimit;
    private final int[] sides;
    private List<int[]> transforms = List.of();
    private final Consumer<Voxel> output;
    private final Set<Integer> visited = new HashSet<>();
    private final int maxCells;
    private final List<Box> boxes = new ArrayList<>();

    private WorkshopGeometry(
            int maxCells, int sx, int sy, int sz, Consumer<Voxel> output, int operationLimit) {
        if (maxCells < 1
                || maxCells > 32_768
                || sx < 1
                || sy < 1
                || sz < 1
                || sx > SIDE
                || sy > SIDE
                || sz > SIDE) throw new IllegalArgumentException("Invalid geometry limits");
        this.maxCells = maxCells;
        sides = new int[] {sx, sy, sz};
        this.output = output;
        this.operationLimit = operationLimit;
    }

    public static Result expand(
            String json, int maxCells, int sx, int sy, int sz, Consumer<Voxel> output) {
        return expand(json, maxCells, sx, sy, sz, output, false);
    }

    public static Result expandMerged(
            String json, int maxCells, int sx, int sy, int sz, Consumer<Voxel> output) {
        return expand(json, maxCells, sx, sy, sz, output, true);
    }

    private static Result expand(
            String json,
            int maxCells,
            int sx,
            int sy,
            int sz,
            Consumer<Voxel> output,
            boolean merged) {
        if (json == null || json.length() > (merged ? MAX_MERGED_JSON : MAX_JSON))
            throw new IllegalArgumentException("Plan size");
        JsonObject plan = object(json, merged ? MAX_MERGED_JSON : MAX_JSON);
        var fields = new HashSet<>(Set.of("version", "operations"));
        if (plan.has("transforms")) fields.add("transforms");
        if (plan.has("seed")) fields.add("seed");
        keys(plan, fields);
        int version = integer(plan.get("version"));
        if (version < 1 || version > 3) throw new IllegalArgumentException("Plan version");
        if (version < 3 && json.length() > MAX_JSON)
            throw new IllegalArgumentException("Plan size");
        var transforms = new ArrayList<int[]>();
        int bx = sx, bz = sz;
        if (plan.has("transforms")) {
            if (version != 3) throw new IllegalArgumentException("Legacy transforms");
            var frames = plan.getAsJsonArray("transforms");
            if (frames.isEmpty() || frames.size() > 16)
                throw new IllegalArgumentException("Transform count");
            int expectedX = -1, expectedZ = -1;
            for (var entry : frames) {
                var frame = entry.getAsJsonArray();
                if (frame.size() != 4) throw new IllegalArgumentException("Transform frame");
                int[] f = {
                    integer(frame.get(0)),
                    integer(frame.get(1)),
                    integer(frame.get(2)),
                    integer(frame.get(3))
                };
                if (f[0] < 1
                        || f[0] > SIDE
                        || f[1] < 1
                        || f[1] > SIDE
                        || f[2] < 0
                        || f[2] > 3
                        || f[3] < 0
                        || f[3] > 1
                        || expectedX >= 0 && (f[0] != expectedX || f[1] != expectedZ))
                    throw new IllegalArgumentException("Transform footprint");
                if (transforms.isEmpty()) {
                    bx = f[0];
                    bz = f[1];
                }
                expectedX = f[f[2] % 2 == 0 ? 0 : 1];
                expectedZ = f[f[2] % 2 == 0 ? 1 : 0];
                transforms.add(f);
            }
            if (expectedX != sx || expectedZ != sz)
                throw new IllegalArgumentException("Transform footprint");
        }
        WorkshopGeometry geometry =
                new WorkshopGeometry(
                        maxCells,
                        bx,
                        sy,
                        bz,
                        output,
                        merged && version == 3 ? MAX_MERGED_OPERATIONS : MAX_OPERATIONS);
        geometry.transforms = List.copyOf(transforms);
        if (plan.has("seed")) {
            if (version != 3
                    || !plan.get("seed").isJsonPrimitive()
                    || !plan.getAsJsonPrimitive("seed").isNumber())
                throw new IllegalArgumentException("Invalid material seed");
            geometry.seed = plan.get("seed").getAsBigDecimal().longValueExact();
        } else geometry.seed = plan.toString().hashCode();
        JsonArray ops = plan.getAsJsonArray("operations");
        geometry.countTemplates(ops, version, false, false);
        for (JsonElement op : ops) geometry.operation(op.getAsJsonObject(), version, new int[3]);
        if (geometry.materials.values().stream().anyMatch(m -> m.blocks().size() > 1))
            plan.addProperty("seed", geometry.seed);
        return new Result(
                version,
                plan.toString(),
                geometry.boxes.stream().map(geometry::placedBox).toList(),
                geometry.work,
                geometry.materials.values().stream()
                        .flatMap(m -> m.blocks().stream())
                        .distinct()
                        .toList());
    }

    private void countTemplates(JsonArray ops, int version, boolean repeated, boolean mirrored) {
        if (ops == null || ops.isEmpty()) throw new IllegalArgumentException("Empty operations");
        for (JsonElement element : ops) {
            if (++templates > operationLimit)
                throw new IllegalArgumentException("Too many operations");
            JsonObject op = element.getAsJsonObject();
            if (version == 3 && "repeat".equals(string(op.get("type")))) {
                if (repeated) throw new IllegalArgumentException("Nested repeat");
                countTemplates(op.getAsJsonArray("operations"), version, true, mirrored);
            } else if (version == 3 && "mirror".equals(string(op.get("type")))) {
                if (repeated || mirrored) throw new IllegalArgumentException("Nested mirror");
                countTemplates(op.getAsJsonArray("operations"), version, false, true);
            }
        }
    }

    private void operation(JsonObject op, int version, int[] offset) {
        String type = version == 3 ? string(op.get("type")) : "cuboid";
        if (type.equals("mirror")) {
            keys(op, Set.of("type", "axes", "operations"));
            String axes = string(op.get("axes"));
            if (!Set.of("x", "z", "xz").contains(axes))
                throw new IllegalArgumentException("Invalid mirror axes");
            int mask = axes.equals("x") ? 1 : axes.equals("z") ? 2 : 3;
            for (int copy = 0; copy <= 3; copy++) {
                if ((copy & mask) != copy) continue;
                reflection = copy;
                for (JsonElement child : op.getAsJsonArray("operations"))
                    operation(child.getAsJsonObject(), version, offset);
            }
            reflection = 0;
            return;
        }
        if (type.equals("repeat")) {
            keys(op, Set.of("type", "count", "offset", "operations"));
            int count = integer(op.get("count"));
            int[] step = point(op.getAsJsonArray("offset"));
            if (count < 1 || count > SIDE) throw new IllegalArgumentException("Repeat count");
            for (int axis = 0; axis < 3; axis++)
                if (Math.abs((long) step[axis]) >= SIDE)
                    throw new IllegalArgumentException("Repeat offset");
            for (int i = 0; i < count; i++) {
                int[] at = {
                    offset[0] + i * step[0], offset[1] + i * step[1], offset[2] + i * step[2]
                };
                for (JsonElement child : op.getAsJsonArray("operations"))
                    operation(child.getAsJsonObject(), version, at);
            }
            return;
        }
        Set<String> fields = new HashSet<>(Set.of("block", "hollow"));
        if (version == 3) fields.add("type");
        if (version >= 2 && op.has("match")) fields.add("match");
        if (type.equals("tube")) fields.addAll(Set.of("path", "curve", "radiusStart", "radiusEnd"));
        else if (type.equals("cuboid") || type.equals("ellipsoid"))
            fields.addAll(Set.of("from", "to"));
        else throw new IllegalArgumentException("Unknown primitive");
        keys(op, fields);
        JsonElement material = op.get("block");
        var parsed = WorkshopMaterials.parse(material, version);
        String block = material.isJsonPrimitive() ? material.getAsString() : material.toString();
        materials.putIfAbsent(block, parsed);
        String match = op.has("match") ? string(op.get("match")) : version == 1 ? "any" : "air";
        if (!match.equals("air") && !match.equals("any") && !match.matches(ID))
            throw new IllegalArgumentException("Invalid match");
        JsonElement flag = op.get("hollow");
        if (!flag.isJsonPrimitive() || !flag.getAsJsonPrimitive().isBoolean())
            throw new IllegalArgumentException("Invalid hollow flag");
        boolean hollow = flag.getAsBoolean();
        if (type.equals("tube")) {
            if (++instances > MAX_INSTANCES)
                throw new IllegalArgumentException("Too many instances");
            tube(op, offset, hollow, block, match);
            return;
        }
        int[] from = shifted(point(op.getAsJsonArray("from")), offset);
        int[] to = shifted(point(op.getAsJsonArray("to")), offset);
        for (int axis = 0; axis < 3; axis++)
            if (from[axis] < 0 || from[axis] > to[axis] || to[axis] >= sides[axis])
                throw new IllegalArgumentException("Operation outside selection");
        if ((reflection & 1) != 0 && from[0] == to[0] && from[0] * 2 == sides[0] - 1
                || (reflection & 2) != 0 && from[2] == to[2] && from[2] * 2 == sides[2] - 1) return;
        if (++instances > MAX_INSTANCES) throw new IllegalArgumentException("Too many instances");
        if (type.equals("cuboid") && boxes.size() < MAX_OPERATIONS) {
            int x0 = (reflection & 1) == 0 ? from[0] : sides[0] - 1 - to[0];
            int x1 = (reflection & 1) == 0 ? to[0] + 1 : sides[0] - from[0];
            int z0 = (reflection & 2) == 0 ? from[2] : sides[2] - 1 - to[2];
            int z1 = (reflection & 2) == 0 ? to[2] + 1 : sides[2] - from[2];
            boxes.add(new Box(x0, from[1], z0, x1, to[1] + 1, z1, block));
        }
        for (int y = from[1]; y <= to[1]; y++)
            for (int x = from[0]; x <= to[0]; x++)
                for (int z = from[2]; z <= to[2]; z++) {
                    charge();
                    if (type.equals("ellipsoid")) {
                        if (!inEllipsoid(x, y, z, from, to)) continue;
                        if (hollow
                                && inEllipsoid(x - 1, y, z, from, to)
                                && inEllipsoid(x + 1, y, z, from, to)
                                && inEllipsoid(x, y - 1, z, from, to)
                                && inEllipsoid(x, y + 1, z, from, to)
                                && inEllipsoid(x, y, z - 1, from, to)
                                && inEllipsoid(x, y, z + 1, from, to)) continue;
                    } else if (hollow
                            && x > from[0]
                            && x < to[0]
                            && y > from[1]
                            && y < to[1]
                            && z > from[2]
                            && z < to[2]) continue;
                    emit(x, y, z, block, match);
                }
    }

    private static boolean inEllipsoid(int x, int y, int z, int[] from, int[] to) {
        int[] p = {x, y, z};
        double sum = 0;
        for (int axis = 0; axis < 3; axis++) {
            double d = (2.0 * p[axis] - from[axis] - to[axis]) / (to[axis] - from[axis] + 1.0);
            sum += d * d;
        }
        return sum <= 1.0;
    }

    private void tube(JsonObject op, int[] offset, boolean hollow, String block, String match) {
        JsonArray path = op.getAsJsonArray("path");
        String curve = string(op.get("curve"));
        if (path == null
                || path.size() < 2
                || path.size() > 16
                || !curve.equals("polyline") && !curve.equals("bezier")
                || curve.equals("bezier") && path.size() != 4)
            throw new IllegalArgumentException("Invalid tube path");
        int r0 = integer(op.get("radiusStart")), r1 = integer(op.get("radiusEnd"));
        if (r0 < 0 || r1 < 0 || r0 > 64 || r1 > 64 || r0 + r1 == 0)
            throw new IllegalArgumentException("Invalid tube radius");
        var control = new ArrayList<double[]>();
        for (JsonElement element : path) {
            int[] p = shifted(point(element.getAsJsonArray()), offset);
            for (int axis = 0; axis < 3; axis++)
                if (p[axis] < 0 || p[axis] >= sides[axis])
                    throw new IllegalArgumentException("Tube outside selection");
            control.add(new double[] {p[0], p[1], p[2]});
        }
        List<double[]> points = control;
        if (curve.equals("bezier")) {
            points = new ArrayList<>();
            for (int i = 0; i <= 64; i++) {
                double t = i / 64.0, u = 1 - t;
                double[] p = new double[3];
                for (int axis = 0; axis < 3; axis++)
                    p[axis] =
                            u * u * u * control.get(0)[axis]
                                    + 3 * u * u * t * control.get(1)[axis]
                                    + 3 * u * t * t * control.get(2)[axis]
                                    + t * t * t * control.get(3)[axis];
                points.add(p);
            }
        }
        double[] lengths = new double[points.size()];
        for (int i = 1; i < points.size(); i++)
            lengths[i] = lengths[i - 1] + distance(points.get(i - 1), points.get(i));
        if (lengths[lengths.length - 1] == 0)
            throw new IllegalArgumentException("Zero-length tube");
        Set<Integer> shape = new HashSet<>();
        for (int i = 1; i < points.size(); i++) {
            double[] a = points.get(i - 1), b = points.get(i);
            double ra = r0 + (r1 - r0) * lengths[i - 1] / lengths[lengths.length - 1];
            double rb = r0 + (r1 - r0) * lengths[i] / lengths[lengths.length - 1];
            int[] lo = new int[3], hi = new int[3];
            for (int axis = 0; axis < 3; axis++) {
                lo[axis] = (int) Math.ceil(Math.min(a[axis] - ra, b[axis] - rb));
                hi[axis] = (int) Math.floor(Math.max(a[axis] + ra, b[axis] + rb));
            }
            double len2 = 0;
            for (int axis = 0; axis < 3; axis++) len2 += (b[axis] - a[axis]) * (b[axis] - a[axis]);
            for (int y = lo[1]; y <= hi[1]; y++)
                for (int x = lo[0]; x <= hi[0]; x++)
                    for (int z = lo[2]; z <= hi[2]; z++) {
                        charge();
                        double t =
                                len2 == 0
                                        ? 0
                                        : Math.clamp(
                                                ((x - a[0]) * (b[0] - a[0])
                                                                + (y - a[1]) * (b[1] - a[1])
                                                                + (z - a[2]) * (b[2] - a[2]))
                                                        / len2,
                                                0.0,
                                                1.0);
                        double radius = ra + (rb - ra) * t;
                        double dx = x - a[0] - t * (b[0] - a[0]),
                                dy = y - a[1] - t * (b[1] - a[1]),
                                dz = z - a[2] - t * (b[2] - a[2]);
                        if (dx * dx + dy * dy + dz * dz > radius * radius + 1e-9) continue;
                        checkInside(x, y, z);
                        shape.add(index(x, y, z));
                        if (!hollow && shape.size() > maxCells)
                            throw new IllegalArgumentException("Plan exceeds block budget");
                    }
        }
        for (int key : shape.stream().sorted().toList()) {
            int x = x(key), y = y(key), z = z(key);
            if (hollow
                    && contains(shape, x - 1, y, z)
                    && contains(shape, x + 1, y, z)
                    && contains(shape, x, y - 1, z)
                    && contains(shape, x, y + 1, z)
                    && contains(shape, x, y, z - 1)
                    && contains(shape, x, y, z + 1)) continue;
            emit(x, y, z, block, match);
        }
    }

    private static boolean contains(Set<Integer> shape, int x, int y, int z) {
        return x >= 0
                && y >= 0
                && z >= 0
                && x < SIDE
                && y < SIDE
                && z < SIDE
                && shape.contains(index(x, y, z));
    }

    private void charge() {
        if (++work > MAX_WORK)
            throw new IllegalArgumentException("Plan exceeds expansion work budget");
    }

    private void checkInside(int x, int y, int z) {
        if (x < 0 || y < 0 || z < 0 || x >= sides[0] || y >= sides[1] || z >= sides[2])
            throw new IllegalArgumentException("Operation outside selection");
    }

    private void emit(int x, int y, int z, String block, String match) {
        // A centre-plane cell belongs to an earlier copy; do not repeat its conditional writes.
        if ((reflection & 1) != 0 && x * 2 == sides[0] - 1
                || (reflection & 2) != 0 && z * 2 == sides[2] - 1) return;
        if ((reflection & 1) != 0) x = sides[0] - 1 - x;
        if ((reflection & 2) != 0) z = sides[2] - 1 - z;
        charge();
        visited.add(index(x, y, z));
        if (visited.size() > maxCells)
            throw new IllegalArgumentException("Plan exceeds block budget");
        block = materials.get(block).at(x, y, z, seed);
        block =
                WorkshopBlockStates.transform(
                        block, 0, (reflection & 1) != 0, (reflection & 2) != 0);
        for (int[] f : transforms) {
            int[] p = mapCell(x, y, z, f[0], f[1], f[2], f[3] != 0);
            x = p[0];
            y = p[1];
            z = p[2];
            block = WorkshopBlockStates.transform(block, f[2], f[3] != 0, false);
        }
        output.accept(new Voxel(x, y, z, block, match));
    }

    public static int[] mapCell(int x, int y, int z, int sx, int sz, int rotY, boolean mirror) {
        x = mirror ? sx - 1 - x : x;
        for (int i = 0; i < (rotY & 3); i++) {
            int next = sz - 1 - z;
            z = x;
            x = next;
            int swap = sx;
            sx = sz;
            sz = swap;
        }
        return new int[] {x, y, z};
    }

    private Box placedBox(Box box) {
        int[] a = {box.x0(), box.y0(), box.z0()}, b = {box.x1() - 1, box.y1() - 1, box.z1() - 1};
        for (int[] f : transforms) {
            a = mapCell(a[0], a[1], a[2], f[0], f[1], f[2], f[3] != 0);
            b = mapCell(b[0], b[1], b[2], f[0], f[1], f[2], f[3] != 0);
        }
        return new Box(
                Math.min(a[0], b[0]),
                Math.min(a[1], b[1]),
                Math.min(a[2], b[2]),
                Math.max(a[0], b[0]) + 1,
                Math.max(a[1], b[1]) + 1,
                Math.max(a[2], b[2]) + 1,
                box.block());
    }

    private static double distance(double[] a, double[] b) {
        double sum = 0;
        for (int i = 0; i < 3; i++) sum += (a[i] - b[i]) * (a[i] - b[i]);
        return Math.sqrt(sum);
    }

    private static int[] shifted(int[] p, int[] offset) {
        for (int i = 0; i < 3; i++) p[i] = Math.addExact(p[i], offset[i]);
        return p;
    }

    public static int index(int x, int y, int z) {
        return (y * SIDE + x) * SIDE + z;
    }

    public static int x(int key) {
        return key / SIDE % SIDE;
    }

    public static int y(int key) {
        return key / (SIDE * SIDE);
    }

    public static int z(int key) {
        return key % SIDE;
    }

    private static void checkDepth(String json) {
        int depth = 0;
        boolean quoted = false, escape = false;
        for (int i = 0; i < json.length(); i++) {
            char c = json.charAt(i);
            if (quoted) {
                if (escape) escape = false;
                else if (c == '\\') escape = true;
                else if (c == '"') quoted = false;
            } else if (c == '"') quoted = true;
            else if (c == '{' || c == '[') {
                if (++depth > 16) throw new IllegalArgumentException("Plan nesting");
            } else if (c == '}' || c == ']') depth--;
        }
    }

    /** Duplicate keys cannot hide an invalid field at a model or storage trust boundary. */
    public static JsonObject object(String json, int max) {
        if (json == null || json.length() > max) throw new IllegalArgumentException("Plan size");
        checkDepth(json);
        try (var reader = new com.google.gson.stream.JsonReader(new java.io.StringReader(json))) {
            checkJson(reader, 0);
            if (reader.peek() != com.google.gson.stream.JsonToken.END_DOCUMENT)
                throw new IllegalArgumentException("Extra JSON");
        } catch (java.io.IOException invalid) {
            throw new IllegalArgumentException("Invalid JSON", invalid);
        }
        return JsonParser.parseString(json).getAsJsonObject();
    }

    private static void checkJson(com.google.gson.stream.JsonReader reader, int depth)
            throws java.io.IOException {
        if (depth > 16) throw new IllegalArgumentException("Plan nesting");
        switch (reader.peek()) {
            case BEGIN_OBJECT -> {
                reader.beginObject();
                Set<String> fields = new HashSet<>();
                while (reader.hasNext()) {
                    if (!fields.add(reader.nextName()))
                        throw new IllegalArgumentException("Duplicate JSON field");
                    checkJson(reader, depth + 1);
                }
                reader.endObject();
            }
            case BEGIN_ARRAY -> {
                reader.beginArray();
                while (reader.hasNext()) checkJson(reader, depth + 1);
                reader.endArray();
            }
            case STRING, NUMBER -> reader.nextString();
            case BOOLEAN -> reader.nextBoolean();
            case NULL -> reader.nextNull();
            default -> throw new IllegalArgumentException("Invalid JSON value");
        }
    }

    private static void keys(JsonObject object, Set<String> expected) {
        if (!object.keySet().equals(expected))
            throw new IllegalArgumentException("Unknown or missing plan field");
    }

    private static String string(JsonElement value) {
        if (value == null || !value.isJsonPrimitive() || !value.getAsJsonPrimitive().isString())
            throw new IllegalArgumentException("Expected string");
        return value.getAsString();
    }

    private static int[] point(JsonArray point) {
        if (point == null || point.size() != 3)
            throw new IllegalArgumentException("Expected three coordinates");
        return new int[] {integer(point.get(0)), integer(point.get(1)), integer(point.get(2))};
    }

    private static int integer(JsonElement value) {
        if (value == null || !value.isJsonPrimitive() || !value.getAsJsonPrimitive().isNumber())
            throw new IllegalArgumentException("Expected integer");
        return value.getAsBigDecimal().intValueExact();
    }
}
