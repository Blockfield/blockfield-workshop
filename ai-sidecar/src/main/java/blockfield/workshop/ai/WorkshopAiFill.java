package blockfield.workshop.ai;

/**
 * Fill capture/option data plus the seam continuity check, moved out of the game JVM (coord#20
 * U-AI2). The game keeps capture reading, analysis and player-facing notes; the sidecar only needs
 * the wire data, the extend request builder and the seam check. Note texts are message keys here
 * (the game renders them); only warnings cross back.
 */
import com.google.gson.JsonArray;
import com.google.gson.JsonObject;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import java.util.TreeMap;
import java.util.function.Function;

/**
 * Smart fill («Продлить»): continues the build that touches the selection into the selection's
 * empty cells. Pure data: the server thread feeds compact state texts (see {@code
 * WorkshopAi.fillText}), so capture order, period detection, phase alignment and plan emission run
 * without a world in tests.
 *
 * <p>Sides are the four horizontal faces: 0 = x-min (the build lies at x&lt;0 and continues toward
 * +x), 1 = x-max, 2 = z-min, 3 = z-max. Along a side the signed slice coordinate {@code a} counts
 * away from the build: selection slices a = 0..n-1 (0 touches the build), outside slab slices a =
 * -1, -2, ... (slab index i = -1-a).
 */
final class WorkshopAiFill {
    static final String AIR = "minecraft:air";

    /**
     * Prefix of a captured state the closed plan language cannot express: compared, never written.
     */
    static final String OPAQUE = "!";

    // ponytail: horizontal axes only, and only the slab outside the selection is a source (a build
    // wholly inside the selection never touches). Revisit when builders stack storeys upward or
    // select whole builds; storeys go through a normal generation with a hint until then.

    /** Outside slab depth along the extension axis, the face layer included. */
    static final int DEPTH = 32;

    /** Server-thread read cap of one capture: selection + four face layers + slabs. */
    static final int MAX_READS = 65_536;

    static final int MAX_VOLUME = 32_768;
    static final int MAX_PERIOD = DEPTH / 2;

    /** Ground-following: the whole cross-section may rise or fall this much per period. */
    static final int MAX_STEP = 2;

    /** A period must explain the first min(extent, EVIDENCE) source slices, not a short prefix. */
    static final int EVIDENCE = 8;

    /** Decoration noise: one slice may differ in 15% of its occupied cells, a run in 10%. */
    static final double SLICE_TOLERANCE = 0.15;

    static final double RUN_TOLERANCE = 0.10;

    /** Seam check: share of the source boundary's solid cells that must continue across it. */
    static final double SEAM_MIN = 0.6;

    static final int MAX_REQUEST_CHARS = 16_384;

    private WorkshopAiFill() {}

    /** Rebuilds a completed capture the mod read (server thread) for the extend path. */
    static Capture fromCells(int sx, int sy, int sz, Map<Long, String> cells) {
        if (cells.size() > MAX_READS) throw new IllegalArgumentException("Fill cells too many");
        Capture c = new Capture(sx, sy, sz);
        for (var entry : cells.entrySet()) {
            if (entry.getKey() == null || entry.getValue() == null)
                throw new IllegalArgumentException("Fill cell must have key and text");
            if (entry.getValue().length() > 256)
                throw new IllegalArgumentException("Fill cell text too long");
        }
        c.cells.putAll(cells);
        c.phase = 2;
        c.reads = cells.size();
        return c;
    }

    // Wire codecs for mode:"fill" (game serializes its server-thread capture + chosen option;
    // the sidecar rebuilds both and runs the paid extend).

    static JsonObject captureJson(Capture c) {
        var o = new JsonObject();
        o.addProperty("sx", c.sx);
        o.addProperty("sy", c.sy);
        o.addProperty("sz", c.sz);
        var cells = new JsonObject();
        for (var entry : c.cells().entrySet())
            cells.addProperty(Long.toString(entry.getKey()), entry.getValue());
        o.add("cells", cells);
        return o;
    }

    static Capture parseCapture(JsonObject o) {
        int sx = wireInt(o, "sx", 1, 128),
                sy = wireInt(o, "sy", 1, 128),
                sz = wireInt(o, "sz", 1, 128);
        if (!o.has("cells") || !o.get("cells").isJsonObject())
            throw new IllegalArgumentException("Fill capture cells must be an object");
        var cells = new HashMap<Long, String>();
        for (var entry : o.getAsJsonObject("cells").entrySet()) {
            final long key;
            try {
                key = Long.parseUnsignedLong(entry.getKey());
            } catch (NumberFormatException invalid) {
                throw new IllegalArgumentException("Fill cell key must be an unsigned long");
            }
            if (!entry.getValue().isJsonPrimitive()
                    || !entry.getValue().getAsJsonPrimitive().isString()
                    || entry.getValue().getAsString().length() > 256)
                throw new IllegalArgumentException("Fill cell text outside bounds");
            cells.put(key, entry.getValue().getAsString());
        }
        return fromCells(sx, sy, sz, cells);
    }

    static JsonObject optionJson(Option o) {
        var json = new JsonObject();
        json.addProperty("axis", o.axis);
        var sides = new JsonArray();
        for (Side side : o.sides) {
            var s = new JsonObject();
            s.addProperty("k", side.k);
            s.addProperty("width", side.width);
            s.addProperty("slab", side.slab);
            s.addProperty("extent", side.extent);
            s.addProperty("period", side.period);
            s.addProperty("step", side.step);
            s.addProperty("evidence", side.evidence);
            if (side.module == null) s.add("module", com.google.gson.JsonNull.INSTANCE);
            else {
                var rows = new JsonArray();
                for (String[] row : side.module) {
                    var r = new JsonArray();
                    for (String cell : row) r.add(cell);
                    rows.add(r);
                }
                s.add("module", rows);
            }
            sides.add(s);
        }
        json.add("sides", sides);
        json.addProperty("touch", o.touch);
        json.addProperty("periodic", o.periodic);
        json.addProperty("seam", o.seam);
        var plan = new JsonObject();
        for (var entry : o.plan.entrySet())
            plan.addProperty(Long.toString(entry.getKey()), entry.getValue());
        json.add("plan", plan);
        json.addProperty("writes", o.writes);
        json.addProperty("opaque", o.opaque);
        return json;
    }

    static Option parseOption(JsonObject o, int sy) {
        // analyze emits axis 0 (x) and 2 (z); consumers treat any non-zero axis as z.
        int axis = wireInt(o, "axis", 0, 2);
        if (!o.has("sides") || !o.get("sides").isJsonArray() || o.getAsJsonArray("sides").isEmpty())
            throw new IllegalArgumentException("Fill option sides must be a non-empty array");
        var sides = new ArrayList<Side>();
        for (var element : o.getAsJsonArray("sides")) {
            if (!element.isJsonObject())
                throw new IllegalArgumentException("Fill side outside bounds");
            var s = element.getAsJsonObject();
            int k = wireInt(s, "k", 0, 3);
            int width = wireInt(s, "width", 1, 128);
            int slab = wireInt(s, "slab", 0, 128);
            int extent = wireInt(s, "extent", 0, 1 << 20);
            int period = wireInt(s, "period", 0, MAX_PERIOD);
            int step = wireInt(s, "step", -MAX_STEP, MAX_STEP);
            int evidence = wireInt(s, "evidence", 0, 1 << 20);
            String[][] module = null;
            if (s.has("module") && !s.get("module").isJsonNull()) {
                if (!s.get("module").isJsonArray())
                    throw new IllegalArgumentException("Fill side module must be an array");
                var rows = s.getAsJsonArray("module");
                if (period == 0 || rows.size() != period)
                    throw new IllegalArgumentException("Fill side module must hold period rows");
                module = new String[rows.size()][];
                for (int i = 0; i < rows.size(); i++) {
                    if (!rows.get(i).isJsonArray()
                            || rows.get(i).getAsJsonArray().size() != width * sy)
                        throw new IllegalArgumentException("Fill side module row has wrong width");
                    module[i] = new String[width * sy];
                    int j = 0;
                    for (var cell : rows.get(i).getAsJsonArray()) {
                        if (!cell.isJsonPrimitive()
                                || !cell.getAsJsonPrimitive().isString()
                                || cell.getAsString().length() > 256)
                            throw new IllegalArgumentException("Fill module cell outside bounds");
                        module[i][j++] = cell.getAsString();
                    }
                }
            } else if (period != 0)
                throw new IllegalArgumentException("Periodic fill side needs its module");
            sides.add(new Side(k, width, slab, extent, period, step, evidence, module));
        }
        if (sides.size() > 4)
            throw new IllegalArgumentException("Fill option holds at most 4 sides");
        int touch = wireInt(o, "touch", 0, 1 << 20);
        boolean periodic = wireBool(o, "periodic");
        // option() emits -1 for single-side and non-periodic options; only a periodic
        // bridge (two sides) carries a real split.
        int seam = wireInt(o, "seam", -1, 1 << 20);
        if (seam < 0 && periodic && sides.size() == 2)
            throw new IllegalArgumentException("Bridge fill option needs a seam");
        if (!o.has("plan") || !o.get("plan").isJsonObject())
            throw new IllegalArgumentException("Fill option plan must be an object");
        var plan = new HashMap<Long, String>();
        for (var entry : o.getAsJsonObject("plan").entrySet()) {
            final long key;
            try {
                key = Long.parseUnsignedLong(entry.getKey());
            } catch (NumberFormatException invalid) {
                throw new IllegalArgumentException("Fill plan key must be an unsigned long");
            }
            if (!entry.getValue().isJsonPrimitive()
                    || !entry.getValue().getAsJsonPrimitive().isString()
                    || entry.getValue().getAsString().length() > 256)
                throw new IllegalArgumentException("Fill plan text outside bounds");
            plan.put(key, entry.getValue().getAsString());
        }
        int writes = wireInt(o, "writes", 0, 1 << 20);
        int opaque = wireInt(o, "opaque", 0, 1 << 20);
        return new Option(axis, List.copyOf(sides), touch, periodic, seam, plan, writes, opaque);
    }

    private static int wireInt(JsonObject o, String field, int min, int max) {
        if (!o.has(field)
                || !o.get(field).isJsonPrimitive()
                || !o.getAsJsonPrimitive(field).isNumber())
            throw new IllegalArgumentException("Fill field " + field + " must be a number");
        int value;
        try {
            value = o.get(field).getAsInt();
        } catch (NumberFormatException | ArithmeticException invalid) {
            throw new IllegalArgumentException("Fill field " + field + " outside bounds");
        }
        if (value < min || value > max)
            throw new IllegalArgumentException("Fill field " + field + " outside bounds");
        return value;
    }

    private static boolean wireBool(JsonObject o, String field) {
        if (!o.has(field)
                || !o.get(field).isJsonPrimitive()
                || !o.getAsJsonPrimitive(field).isBoolean())
            throw new IllegalArgumentException("Fill field " + field + " must be a boolean");
        return o.get(field).getAsBoolean();
    }

    static long key(int x, int y, int z) {
        return ((long) (x + 512) << 22) | ((long) (y + 512) << 11) | (z + 512);
    }

    static boolean air(String text) {
        return text == null || AIR.equals(text);
    }

    /** Selection-local position of cross-section (u, y) at signed slice {@code a} of side k. */
    static int[] pos(int sx, int sz, int k, int a, int u, int y) {
        return switch (k) {
            case 0 -> new int[] {a, y, u};
            case 1 -> new int[] {sx - 1 - a, y, u};
            case 2 -> new int[] {u, y, a};
            default -> new int[] {u, y, sz - 1 - a};
        };
    }

    static String sideName(int k) {
        return switch (k) {
            case 0 -> "+x";
            case 1 -> "-x";
            case 2 -> "+z";
            default -> "-z";
        };
    }

    /**
     * Bounded server-thread capture. Phase 1 reads the selection and the one-cell layer outside
     * each horizontal face; phase 2 reads up to {@link #DEPTH} slices outward on every touched
     * side, shallower when the read cap would be exceeded. A side's slab ends at its first slice
     * with an unreadable cell (rights, unloaded chunk, world height).
     */
    static final class Capture {
        final int sx, sy, sz;
        private final Map<Long, String> cells = new HashMap<>();
        private final int volume;
        private final int[] slab = new int[4];
        private final boolean[] stopped = new boolean[4];
        private int cursor, phase, depth, side, slice, cell, reads;

        Capture(int sx, int sy, int sz) {
            if (sx < 1 || sy < 1 || sz < 1 || sx > 128 || sy > 128 || sz > 128)
                throw new IllegalArgumentException("Fill sides outside limits");
            if ((long) sx * sy * sz > MAX_VOLUME)
                throw new IllegalArgumentException("Fill selection too large");
            this.sx = sx;
            this.sy = sy;
            this.sz = sz;
            volume = sx * sy * sz;
        }

        int width(int k) {
            return k < 2 ? sz : sx;
        }

        int length(int k) {
            return k < 2 ? sx : sz;
        }

        boolean inside(int[] p) {
            return p[0] >= 0 && p[0] < sx && p[1] >= 0 && p[1] < sy && p[2] >= 0 && p[2] < sz;
        }

        /** Next position to read (not advanced), or null once the capture is complete. */
        int[] next() {
            if (phase == 0) {
                if (cursor < volume)
                    return new int[] {cursor % sx, cursor / sx % sy, cursor / (sx * sy)};
                int rest = cursor - volume;
                for (int k = 0; k < 4; k++) {
                    int layer = width(k) * sy;
                    if (rest < layer) return pos(sx, sz, k, -1, rest % width(k), rest / width(k));
                    rest -= layer;
                }
                startSlabs();
            }
            while (phase == 1 && side < 4) {
                if (stopped[side] || slice >= depth || slab[side] == 0) {
                    side++;
                    slice = 1;
                    cell = 0;
                    continue;
                }
                if (cell >= width(side) * sy) {
                    slab[side] = slice + 1;
                    slice++;
                    cell = 0;
                    continue;
                }
                return pos(sx, sz, side, -1 - slice, cell % width(side), cell / width(side));
            }
            phase = 2;
            return null;
        }

        /** Records the state text at {@link #next()}; null marks an unreadable cell. */
        void accept(String text) {
            int[] at = next();
            if (at == null) throw new IllegalStateException("Fill capture already complete");
            reads++;
            if (text != null) cells.put(key(at[0], at[1], at[2]), text);
            if (phase == 0) {
                if (text == null && cursor < volume)
                    throw new IllegalArgumentException("Selection cell unreadable");
                cursor++;
            } else if (text == null) stopped[side] = true;
            else cell++;
        }

        private void startSlabs() {
            phase = 1;
            int layers = 0;
            for (int k = 0; k < 4; k++) {
                slab[k] = touched(k) ? 1 : 0;
                if (slab[k] == 1) layers += width(k) * sy;
            }
            depth = layers == 0 ? 1 : Math.min(DEPTH, 1 + (MAX_READS - reads) / layers);
            side = 0;
            slice = 1;
            cell = 0;
        }

        /** A side touches when its outside layer is fully readable and holds a non-air cell. */
        boolean touched(int k) {
            boolean any = false;
            for (int y = 0; y < sy; y++)
                for (int u = 0; u < width(k); u++) {
                    int[] p = pos(sx, sz, k, -1, u, y);
                    String text = cells.get(key(p[0], p[1], p[2]));
                    if (text == null && !cells.containsKey(key(p[0], p[1], p[2]))) return false;
                    if (!air(text)) any = true;
                }
            return any;
        }

        boolean done() {
            return next() == null;
        }

        int reads() {
            return reads;
        }

        String at(int x, int y, int z) {
            return cells.get(key(x, y, z));
        }

        /** Read cells for the wire: keys are {@link #key} longs, values state texts. */
        Map<Long, String> cells() {
            return Map.copyOf(cells);
        }

        /** Fully read slab slices of side k (0 when the side does not touch). */
        int slab(int k) {
            return phase == 2 ? slab[k] : 0;
        }

        /** Registry ids per selection column bottom-up, for the sandbox world tools. */
        Map<List<Integer>, List<String>> columns() {
            var columns = new HashMap<List<Integer>, List<String>>();
            for (int x = 0; x < sx; x++)
                for (int z = 0; z < sz; z++) {
                    var ids = new ArrayList<String>(sy);
                    for (int y = 0; y < sy; y++) ids.add(id(at(x, y, z)));
                    columns.put(List.of(x, z), ids);
                }
            return columns;
        }
    }

    static String id(String text) {
        if (text == null) return AIR;
        String id = text.startsWith(OPAQUE) ? text.substring(1) : text;
        int bracket = id.indexOf('[');
        return bracket < 0 ? id : id.substring(0, bracket);
    }

    /** One analysed build side: its readable slab, extent and the chosen translational period. */
    record Side(
            int k,
            int width,
            int slab,
            int extent,
            int period,
            int step,
            int evidence,
            String[][] module) {
        boolean periodic() {
            return period > 0;
        }

        /** Continuation at selection slice t (>= 0): t - kP lands in the module, shifted k*step. */
        String predict(int t, int u, int y, int sy) {
            int copies = t / period + 1;
            int i = copies * period - 1 - t;
            int v = y - copies * step;
            return v < 0 || v >= sy ? null : module[i][u + width * v];
        }
    }

    /** One candidate axis: one side, or a bridge between both. Fill holds plan-ready writes. */
    record Option(
            int axis,
            List<Side> sides,
            int touch,
            boolean periodic,
            int seam,
            Map<Long, String> plan,
            int writes,
            int opaque) {
        boolean bridge() {
            return sides.size() == 2;
        }
    }

    record Analysis(List<Option> options, Option best, Option fallback) {
        /** Deterministic choice, or null when no touched axis has a clean period. */
        Option deterministic() {
            return best;
        }

        /** The axis the AI path extends along: the periodic choice, else the most touched. */
        Option axis() {
            return best != null ? best : fallback;
        }

        String log() {
            var text = new StringBuilder();
            for (Option option : options) {
                if (!text.isEmpty()) text.append(';');
                text.append(option.axis == 0 ? "x" : "z");
                for (Side side : option.sides)
                    text.append(' ')
                            .append(sideName(side.k))
                            .append(" slab=")
                            .append(side.slab)
                            .append(" extent=")
                            .append(side.extent)
                            .append(" period=")
                            .append(side.period)
                            .append(" step=")
                            .append(side.step)
                            .append(" evidence=")
                            .append(side.evidence);
                if (option.bridge()) text.append(" seam=").append(option.seam);
                text.append(" writes=")
                        .append(option.writes)
                        .append(" opaque=")
                        .append(option.opaque);
            }
            return text.toString();
        }
    }

    static Analysis analyze(Capture capture) {
        if (!capture.done()) throw new IllegalStateException("Fill capture incomplete");
        var options = new ArrayList<Option>();
        for (int axis = 0; axis <= 2; axis += 2) {
            var sides = new ArrayList<Side>();
            int touch = 0;
            for (int k = axis == 0 ? 0 : 2, end = k + 2; k < end; k++) {
                if (capture.slab(k) == 0) continue;
                sides.add(side(capture, k));
                for (int u = 0; u < capture.width(k); u++)
                    for (int y = 0; y < capture.sy; y++) {
                        int[] p = pos(capture.sx, capture.sz, k, -1, u, y);
                        if (!air(capture.at(p[0], p[1], p[2]))) touch++;
                    }
            }
            if (!sides.isEmpty()) options.add(option(capture, axis, sides, touch));
        }
        Option best = null, fallback = null;
        for (Option option : options) {
            if (option.periodic
                    && option.writes > 0
                    && (best == null || option.writes > best.writes)) best = option;
            if (fallback == null || option.touch > fallback.touch) fallback = option;
        }
        return new Analysis(List.copyOf(options), best, fallback);
    }

    private static Side side(Capture c, int k) {
        int w = c.width(k), h = c.sy, m = c.slab(k);
        var slices = new String[m][w * h];
        int extent = 0;
        for (int i = 0; i < m; i++)
            for (int y = 0; y < h; y++)
                for (int u = 0; u < w; u++) {
                    int[] p = pos(c.sx, c.sz, k, -1 - i, u, y);
                    String text = c.at(p[0], p[1], p[2]);
                    slices[i][u + w * y] = text == null ? AIR : text;
                    if (!air(text)) extent = i + 1;
                }
        int[] found = period(slices, w, h, extent);
        if (found == null) return new Side(k, w, m, extent, 0, 0, 0, null);
        int period = found[0], step = found[1], evidence = found[2];
        var module = new String[period][w * h];
        for (int r = 0; r < period; r++)
            for (int y = 0; y < h; y++)
                for (int u = 0; u < w; u++)
                    module[r][u + w * y] = vote(slices, r, u, y, w, h, period, step, evidence);
        return new Side(k, w, m, extent, period, step, evidence, module);
    }

    /**
     * Smallest translational period (P, step) that explains the longest source prefix: slice i must
     * equal slice i+P lifted by step, within the noise tolerances. Longest evidence wins, then the
     * flattest step, then the smallest period, so multiples never beat their base. Returns {P,
     * step, evidence} or null.
     */
    static int[] period(String[][] slices, int w, int h, int extent) {
        int m = slices.length;
        int[] best = null;
        for (int p = 1; p <= Math.min(MAX_PERIOD, m / 2); p++)
            for (int step = -MAX_STEP; step <= MAX_STEP; step++) {
                int run = 0, diffs = 0, union = 0;
                for (int i = 0; i + p < m; i++) {
                    int[] d = compare(slices[i], slices[i + p], step, w, h);
                    if (d[0] > (int) Math.floor(d[1] * SLICE_TOLERANCE)) break;
                    if (diffs + d[0] > (int) Math.floor((union + d[1]) * RUN_TOLERANCE)) break;
                    diffs += d[0];
                    union += d[1];
                    run++;
                }
                int evidence = p + run;
                if (evidence < 2 * p || evidence < Math.min(extent, EVIDENCE) || union == 0)
                    continue;
                if (best == null
                        || evidence > best[2]
                        || evidence == best[2] && Math.abs(step) < Math.abs(best[1])
                        || evidence == best[2]
                                && Math.abs(step) == Math.abs(best[1])
                                && p < best[0]) best = new int[] {p, step, evidence};
            }
        return best;
    }

    /** {differing, occupied} cells of near vs far lifted by step; air-air pairs never count. */
    static int[] compare(String[] near, String[] far, int step, int w, int h) {
        int diff = 0, union = 0;
        for (int y = 0; y < h; y++) {
            int v = y - step;
            if (v < 0 || v >= h) continue;
            for (int u = 0; u < w; u++) {
                String a = near[u + w * y], b = far[u + w * v];
                if (air(a) && air(b)) continue;
                union++;
                if (!a.equals(b)) diff++;
            }
        }
        return new int[] {diff, union};
    }

    /** Consensus of one module cell over every repetition inside the evidence; ties go nearest. */
    private static String vote(
            String[][] slices, int r, int u, int y, int w, int h, int p, int step, int evidence) {
        var counts = new LinkedHashMap<String, Integer>();
        for (int j = 0; r + j * p < evidence; j++) {
            int v = y - j * step;
            if (v < 0 || v >= h) continue;
            counts.merge(slices[r + j * p][u + w * v], 1, Integer::sum);
        }
        String best = null;
        int many = 0;
        for (var entry : counts.entrySet())
            if (entry.getValue() > many) {
                best = entry.getKey();
                many = entry.getValue();
            }
        return best;
    }

    private static Option option(Capture c, int axis, List<Side> sides, int touch) {
        boolean periodic = sides.stream().allMatch(Side::periodic);
        int n = axis == 0 ? c.sx : c.sz, w = axis == 0 ? c.sz : c.sx;
        if (!periodic) return new Option(axis, sides, touch, false, -1, Map.of(), 0, 0);
        Function<Integer, String[]> line;
        int seam = -1;
        if (sides.size() == 1) {
            Side side = sides.getFirst();
            line = t -> slicePrediction(side, side.k % 2 == 0 ? t : n - 1 - t, w, c.sy);
        } else {
            Side low = sides.get(0), high = sides.get(1);
            seam = seam(c, low, high, n, w);
            int split = seam;
            line =
                    a ->
                            a < split
                                    ? slicePrediction(low, a, w, c.sy)
                                    : slicePrediction(high, n - 1 - a, w, c.sy);
        }
        var plan = new TreeMap<Long, String>();
        int writes = 0, opaque = 0;
        for (int a = 0; a < n; a++) {
            String[] slice = line.apply(a);
            for (int y = 0; y < c.sy; y++)
                for (int u = 0; u < w; u++) {
                    String text = slice[u + w * y];
                    if (air(text)) continue;
                    int[] p = pos(c.sx, c.sz, axis == 0 ? 0 : 2, a, u, y);
                    boolean empty = air(c.at(p[0], p[1], p[2]));
                    if (text.startsWith(OPAQUE)) {
                        if (empty) opaque++;
                        continue;
                    }
                    plan.put(key(p[0], p[1], p[2]), text);
                    if (empty) writes++;
                }
        }
        return new Option(axis, sides, touch, true, seam, plan, writes, opaque);
    }

    /** Predicted cross-section of selection slice t counted from this side's build. */
    private static String[] slicePrediction(Side side, int t, int w, int h) {
        var slice = new String[w * h];
        for (int y = 0; y < h; y++)
            for (int u = 0; u < w; u++) slice[u + w * y] = side.predict(t, u, y, h);
        return slice;
    }

    /**
     * Bridge seam: slices a &lt; split continue the low side, the rest the high side. Each split
     * costs the disagreement of both continuations on the two slices meeting at it (the real
     * boundary slices stand in beyond the selection); the cheapest split nearest the middle wins,
     * so two equal periods in phase join with no visible seam at all.
     */
    static int seam(Capture c, Side low, Side high, int n, int w) {
        int h = c.sy, best = -1, bestCost = Integer.MAX_VALUE;
        for (int split = 0; split <= n; split++) {
            int cost = 0;
            for (int a : new int[] {split - 1, split}) {
                String[] lowSlice = a < 0 ? boundary(c, low.k, w) : slicePrediction(low, a, w, h);
                String[] highSlice =
                        a >= n ? boundary(c, high.k, w) : slicePrediction(high, n - 1 - a, w, h);
                for (int cell = 0; cell < w * h; cell++) {
                    String x = lowSlice[cell], y = highSlice[cell];
                    if (x == null || y == null || air(x) && air(y)) continue;
                    if (!x.equals(y)) cost++;
                }
            }
            if (cost < bestCost
                    || cost == bestCost && Math.abs(2 * split - n) < Math.abs(2 * best - n)) {
                bestCost = cost;
                best = split;
            }
        }
        return best;
    }

    private static String[] boundary(Capture c, int k, int w) {
        var slice = new String[w * c.sy];
        for (int y = 0; y < c.sy; y++)
            for (int u = 0; u < w; u++) {
                int[] p = pos(c.sx, c.sz, k, -1, u, y);
                String text = c.at(p[0], p[1], p[2]);
                slice[u + w * y] = text == null ? AIR : text;
            }
        return slice;
    }

    /**
     * Candidate v3 plans in preference order: one repeated module plus a clipped tail when a single
     * flat-stepped side allows it, then every cell greedily merged into cuboids. The caller keeps
     * the first one its validator accepts; existing cells stay protected by the default {@code air}
     * match.
     */
    static List<String> plans(Capture c, Option option) {
        var plans = new ArrayList<String>();
        if (!option.bridge()) {
            Side side = option.sides.getFirst();
            int n = option.axis == 0 ? c.sx : c.sz;
            if (side.step == 0 && n / side.period >= 2) plans.add(repeated(c, option, side, n));
        }
        plans.add(document(boxes(option.plan)));
        return plans;
    }

    private static String repeated(Capture c, Option option, Side side, int n) {
        int p = side.period, copies = n / p;
        var module = new TreeMap<Long, String>();
        var tail = new TreeMap<Long, String>();
        option.plan.forEach(
                (key, text) -> {
                    int[] at = unpack(key);
                    int along = option.axis == 0 ? at[0] : at[2];
                    int t = side.k % 2 == 0 ? along : n - 1 - along;
                    if (t < p) module.put(key, text);
                    else if (t >= copies * p) tail.put(key, text);
                });
        var operations = new JsonArray();
        var repeat = new JsonObject();
        repeat.addProperty("type", "repeat");
        repeat.addProperty("count", copies);
        var offset = new JsonArray();
        int delta = side.k % 2 == 0 ? p : -p;
        offset.add(option.axis == 0 ? delta : 0);
        offset.add(0);
        offset.add(option.axis == 0 ? 0 : delta);
        repeat.add("offset", offset);
        var inner = new JsonArray();
        boxes(module).forEach(inner::add);
        repeat.add("operations", inner);
        if (!inner.isEmpty()) operations.add(repeat);
        boxes(tail).forEach(operations::add);
        var root = new JsonObject();
        root.addProperty("version", 3);
        root.add("operations", operations);
        return root.toString();
    }

    private static String document(List<JsonObject> boxes) {
        var operations = new JsonArray();
        boxes.forEach(operations::add);
        var root = new JsonObject();
        root.addProperty("version", 3);
        root.add("operations", operations);
        return root.toString();
    }

    static int[] unpack(long key) {
        return new int[] {
            (int) (key >> 22) - 512, (int) (key >> 11 & 0x7FF) - 512, (int) (key & 0x7FF) - 512
        };
    }

    /** Greedy cuboid mesh: grow x, then z, then y over unused cells of one state. */
    static List<JsonObject> boxes(Map<Long, String> cells) {
        var used = new java.util.HashSet<Long>();
        var boxes = new ArrayList<JsonObject>();
        var ordered = new ArrayList<>(cells.keySet());
        ordered.sort(
                java.util.Comparator.<Long>comparingInt(k -> unpack(k)[1])
                        .thenComparingInt(k -> unpack(k)[2])
                        .thenComparingInt(k -> unpack(k)[0]));
        for (long start : ordered) {
            if (used.contains(start)) continue;
            String text = cells.get(start);
            int[] o = unpack(start);
            int x1 = o[0], z1 = o[2], y1 = o[1];
            while (free(cells, used, text, x1 + 1, o[1], o[1], o[2], o[2], x1 + 1)) x1++;
            while (free(cells, used, text, o[0], o[1], o[1], z1 + 1, z1 + 1, x1)) z1++;
            while (free(cells, used, text, o[0], y1 + 1, y1 + 1, o[2], z1, x1)) y1++;
            for (int x = o[0]; x <= x1; x++)
                for (int y = o[1]; y <= y1; y++)
                    for (int z = o[2]; z <= z1; z++) used.add(key(x, y, z));
            var box = new JsonObject();
            box.addProperty("type", "cuboid");
            box.add("from", point(o[0], o[1], o[2]));
            box.add("to", point(x1, y1, z1));
            box.addProperty("block", text);
            box.addProperty("hollow", false);
            boxes.add(box);
        }
        return boxes;
    }

    private static boolean free(
            Map<Long, String> cells,
            Set<Long> used,
            String text,
            int x0,
            int y0,
            int y1,
            int z0,
            int z1,
            int x1) {
        for (int x = x0; x <= x1; x++)
            for (int y = y0; y <= y1; y++)
                for (int z = z0; z <= z1; z++) {
                    long key = key(x, y, z);
                    if (used.contains(key) || !text.equals(cells.get(key))) return false;
                }
        return true;
    }

    private static JsonArray point(int x, int y, int z) {
        var point = new JsonArray();
        point.add(x);
        point.add(y);
        point.add(z);
        return point;
    }

    /**
     * Seam continuity at every build side of the AI result: the share of the source boundary's
     * solid cells whose neighbour across it (existing or planned) is solid, and of those with the
     * same block id. Returns null when every side continues at least {@link #SEAM_MIN}, else a
     * bounded report naming the open cells for the one repair patch.
     */
    static String seamReport(Capture c, Option option, Function<int[], String> planned) {
        var report = new StringBuilder();
        boolean broken = false;
        for (Side side : option.sides) {
            int w = side.width, solid = 0, kept = 0, same = 0;
            var open = new StringBuilder();
            int listed = 0;
            for (int y = 0; y < c.sy; y++)
                for (int u = 0; u < w; u++) {
                    int[] out = pos(c.sx, c.sz, side.k, -1, u, y);
                    String source = c.at(out[0], out[1], out[2]);
                    if (air(source)) continue;
                    solid++;
                    int[] in = pos(c.sx, c.sz, side.k, 0, u, y);
                    String existing = c.at(in[0], in[1], in[2]);
                    String after = air(existing) ? planned.apply(in) : existing;
                    if (air(after)) {
                        if (listed++ < 32)
                            open.append(" [")
                                    .append(in[0])
                                    .append(',')
                                    .append(in[1])
                                    .append(',')
                                    .append(in[2])
                                    .append(']');
                        continue;
                    }
                    kept++;
                    if (id(after).equals(id(source))) same++;
                }
            double continuity = solid == 0 ? 1 : kept / (double) solid;
            if (continuity < SEAM_MIN) broken = true;
            report.append(
                    String.format(
                            Locale.ROOT,
                            "side %s continuity %.2f material %.2f solid %d;",
                            sideName(side.k),
                            continuity,
                            solid == 0 ? 1 : same / (double) solid,
                            solid));
            if (!open.isEmpty()) report.append(" open").append(open).append(';');
        }
        return broken ? report.toString() : null;
    }

    /** Plan-ready source ids of the chosen axis (opaque states excluded), for the AI palette. */
    static Set<String> sourceIds(Capture c, Option option) {
        var ids = new java.util.TreeSet<String>();
        for (Side side : option.sides)
            for (int i = 0; i < side.slab; i++)
                for (int y = 0; y < c.sy; y++)
                    for (int u = 0; u < side.width; u++) {
                        int[] p = pos(c.sx, c.sz, side.k, -1 - i, u, y);
                        String text = c.at(p[0], p[1], p[2]);
                        if (!air(text) && !text.startsWith(OPAQUE)) ids.add(id(text));
                    }
        return ids;
    }

    private static final java.util.regex.Pattern REPLACE =
            java.util.regex.Pattern.compile(
                    "(?iu).*(замен|снес|снос|убер|убрат|разбер|перестр|replace|remove|demolish|tear\\s*down|rebuild).*",
                    java.util.regex.Pattern.DOTALL);

    /** Existing blocks stay fixed unless the hint itself asks to replace or remove them. */
    static boolean replaces(String hint) {
        return REPLACE.matcher(hint).matches();
    }

    static String seamReport(Capture c, Option option, WorkshopAiPlan plan) {
        var cells = new HashMap<Long, String>();
        for (var cell : plan.cells()) cells.put(key(cell.x(), cell.y(), cell.z()), cell.block());
        return seamReport(c, option, at -> cells.get(key(at[0], at[1], at[2])));
    }

    /**
     * Message keys, not rendered text: the game owns the localizations and renders notes itself;
     * the sidecar only returns plans (warnings ride the plan). Args are dropped.
     */
    private static String msg(boolean ru, String key, Object... args) {
        return key;
    }

    /** Builder-facing fill outcome (appended to the preview) or refusal, ru/en. */
    record Note(String ru, String en) {
        static final Note NO_SELECTION =
                new Note(
                        msg(true, "blockfield.workshop.ai.msg.fill_no_selection"),
                        msg(false, "blockfield.workshop.ai.msg.fill_no_selection"));
        static final Note TOO_LARGE =
                new Note(
                        msg(true, "blockfield.workshop.ai.msg.fill_too_large", MAX_VOLUME),
                        msg(false, "blockfield.workshop.ai.msg.fill_too_large", MAX_VOLUME));
        static final Note NO_SOURCE =
                new Note(
                        msg(true, "blockfield.workshop.ai.msg.fill_no_source"),
                        msg(false, "blockfield.workshop.ai.msg.fill_no_source"));
        static final Note NO_PERIOD =
                new Note(
                        msg(true, "blockfield.workshop.ai.msg.fill_no_period"),
                        msg(false, "blockfield.workshop.ai.msg.fill_no_period"));
        static final Note TOO_DETAILED =
                new Note(
                        msg(true, "blockfield.workshop.ai.msg.fill_too_detailed"),
                        msg(false, "blockfield.workshop.ai.msg.fill_too_detailed"));
        static final Note NOTHING =
                new Note(
                        msg(true, "blockfield.workshop.ai.msg.fill_nothing"),
                        msg(false, "blockfield.workshop.ai.msg.fill_nothing"));

        String text(boolean russian) {
            return russian ? ru : en;
        }

        static Note deterministic(Option option, boolean hintIgnored) {
            return new Note(
                    compose(true, option, hintIgnored), compose(false, option, hintIgnored));
        }

        private static String compose(boolean ru, Option option, boolean hintIgnored) {
            var out = new StringBuilder(msg(ru, "blockfield.workshop.ai.msg.fill_pattern"));
            for (Side side : option.sides) {
                out.append(msg(ru, "blockfield.workshop.ai.msg.fill_step", side.period));
                if (side.step != 0)
                    out.append(msg(ru, "blockfield.workshop.ai.msg.fill_rise", side.step));
            }
            if (option.bridge()) out.append(msg(ru, "blockfield.workshop.ai.msg.fill_bridge"));
            if (option.opaque > 0)
                out.append(msg(ru, "blockfield.workshop.ai.msg.fill_opaque", option.opaque));
            if (hintIgnored) out.append(msg(ru, "blockfield.workshop.ai.msg.fill_hint_ignored"));
            return out.toString();
        }

        static Note ai(List<String> warnings) {
            return new Note(composeAi(true, warnings), composeAi(false, warnings));
        }

        private static String composeAi(boolean ru, List<String> warnings) {
            String text = msg(ru, "blockfield.workshop.ai.msg.fill_ai");
            if (warnings.contains("seam_repaired"))
                text += msg(ru, "blockfield.workshop.ai.msg.fill_seam_fixed");
            else if (warnings.contains("seam_open"))
                text += msg(ru, "blockfield.workshop.ai.msg.fill_seam_open");
            return text;
        }
    }

    /**
     * The AI «extend» request: exact boundary voxels as per-column runs of legend indices, the
     * selection's own existing cells, the empty target box, axis, hint and seam rules. Slices are
     * added from the boundary outward while the text stays under {@link #MAX_REQUEST_CHARS}; the
     * first slice of every side is always present.
     */
    static String request(Capture c, Option option, String hint) {
        var legend = new LinkedHashMap<String, Integer>();
        var text = new StringBuilder("EXTEND the existing build into this selection.");
        text.append(" Target box local x 0..")
                .append(c.sx - 1)
                .append(", y 0..")
                .append(c.sy - 1)
                .append(", z 0..")
                .append(c.sz - 1)
                .append('.');
        text.append(option.bridge() ? " BRIDGE along " : " Axis ")
                .append(option.axis == 0 ? "x" : "z")
                .append(':');
        for (Side side : option.sides) {
            text.append(" build at ")
                    .append(
                            side.k % 2 == 0
                                    ? (option.axis == 0 ? "x<0" : "z<0")
                                    : (option.axis == 0 ? "x>=" + c.sx : "z>=" + c.sz))
                    .append(" continues toward ")
                    .append(sideName(side.k));
            if (side.periodic())
                text.append(" (period ")
                        .append(side.period)
                        .append(", rise ")
                        .append(side.step)
                        .append(" per period)");
            text.append(';');
        }
        text.append("\nHINT ").append(hint.isBlank() ? "-" : hint.strip());
        text.append(
                "\nRULES Continue seamlessly from the source: existing cells are fixed, write only"
                        + " into empty cells of the target box. Match materials, heights, rhythm"
                        + " and style; the cross-section must continue across the boundary with no"
                        + " gap or step. Coordinates below are local to the target box (outside"
                        + " cells have negative or >= side coordinates). End with the hinted"
                        + " feature if any; a bridge must join both ends.");
        var body = new StringBuilder();
        int budget = MAX_REQUEST_CHARS - text.length() - 2048;
        var inside = runs(c, legend, 0, c.sx - 1, 0, c.sz - 1);
        for (Side side : option.sides)
            for (int i = 0; i < side.slab; i++) {
                int[] a = pos(c.sx, c.sz, side.k, -1 - i, 0, 0);
                int[] b = pos(c.sx, c.sz, side.k, -1 - i, side.width - 1, 0);
                String slice =
                        runs(
                                c,
                                legend,
                                Math.min(a[0], b[0]),
                                Math.max(a[0], b[0]),
                                Math.min(a[2], b[2]),
                                Math.max(a[2], b[2]));
                if (i > 0 && body.length() + slice.length() + inside.length() > budget) break;
                body.append(slice);
            }
        text.append("\nLEGEND");
        legend.forEach(
                (state, index) ->
                        text.append(' ')
                                .append(index)
                                .append('=')
                                .append(
                                        state.startsWith(OPAQUE)
                                                ? state.substring(1) + "(context only)"
                                                : state));
        text.append("\nSOURCE x,z:y0-y1=index runs;").append(body);
        if (!inside.isEmpty()) text.append("\nINSIDE existing (fixed)").append(inside);
        return text.length() <= MAX_REQUEST_CHARS
                ? text.toString()
                : text.substring(0, MAX_REQUEST_CHARS);
    }

    private static String runs(
            Capture c, Map<String, Integer> legend, int x0, int x1, int z0, int z1) {
        var out = new StringBuilder();
        for (int x = x0; x <= x1; x++)
            for (int z = z0; z <= z1; z++) {
                var column = new StringBuilder();
                int start = -1;
                String open = null;
                for (int y = 0; y <= c.sy; y++) {
                    String text = y < c.sy ? c.at(x, y, z) : null;
                    if (!air(text) && text.equals(open)) continue;
                    if (open != null)
                        column.append(column.isEmpty() ? "" : ",")
                                .append(start)
                                .append('-')
                                .append(y - 1)
                                .append('=')
                                .append(legend.computeIfAbsent(open, s -> legend.size()));
                    open = air(text) ? null : text;
                    start = y;
                }
                if (!column.isEmpty())
                    out.append(' ')
                            .append(x)
                            .append(',')
                            .append(z)
                            .append(':')
                            .append(column)
                            .append(';');
            }
        return out.toString();
    }
}
