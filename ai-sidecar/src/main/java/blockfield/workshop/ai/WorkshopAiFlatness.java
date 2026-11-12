package blockfield.workshop.ai;

import com.google.gson.JsonObject;
import com.lunares.blockfield.workshop.WorkshopGeometry;

import java.util.HashMap;
import java.util.List;
import java.util.Map;

/** Cell-depth evidence, independent of material colour and model calls. */
record WorkshopAiFlatness(List<Double> facades, double roofPlane, double flatTop) {
    static final WorkshopAiFlatness EMPTY = new WorkshopAiFlatness(List.of(0d, 0d, 0d, 0d), 0, 0);

    record Settings(
            boolean enabled,
            double facadeThreshold,
            double roofThreshold,
            double flatTopThreshold,
            int opCap) {
        static Settings defaults() {
            return new Settings(true, 0.85, 0.75, 0.80, 24);
        }

        Settings {
            for (double value : new double[] {facadeThreshold, roofThreshold, flatTopThreshold})
                if (!Double.isFinite(value) || value < 0 || value > 1)
                    throw new IllegalArgumentException("Invalid flatness threshold");
            if (opCap < 1 || opCap > 128) throw new IllegalArgumentException("Invalid detail cap");
        }

        static Settings parse(JsonObject o) {
            var d = defaults();
            if (o.has("detailPassEnabled")
                    && (!o.get("detailPassEnabled").isJsonPrimitive()
                            || !o.getAsJsonPrimitive("detailPassEnabled").isBoolean()))
                throw new IllegalArgumentException("Invalid detail switch");
            return new Settings(
                    o.has("detailPassEnabled")
                            ? o.get("detailPassEnabled").getAsBoolean()
                            : d.enabled,
                    number(o, "flatnessFacadeThreshold", d.facadeThreshold),
                    number(o, "flatnessRoofThreshold", d.roofThreshold),
                    number(o, "flatnessTopThreshold", d.flatTopThreshold),
                    o.has("detailOpCap")
                            ? WorkshopAiPlanning.integer(o.get("detailOpCap"))
                            : d.opCap);
        }

        private static double number(JsonObject o, String key, double fallback) {
            if (!o.has(key)) return fallback;
            if (!o.get(key).isJsonPrimitive() || !o.getAsJsonPrimitive(key).isNumber())
                throw new IllegalArgumentException("Invalid flatness threshold");
            return o.get(key).getAsDouble();
        }
    }

    static WorkshopAiFlatness inspect(Map<Integer, String> blocks, int[] min, int[] max) {
        if (blocks.isEmpty()) return EMPTY;
        List<Map<Integer, Integer>> faces =
                List.of(new HashMap<>(), new HashMap<>(), new HashMap<>(), new HashMap<>());
        var tops = new HashMap<Integer, Integer>();
        int height = max[1] - min[1] + 1;
        int facadeBottom = min[1] + Math.max(1, height / 10),
                roofBottom = min[1] + Math.max(1, height * 3 / 4);
        for (int key : blocks.keySet()) {
            int x = WorkshopGeometry.x(key),
                    y = WorkshopGeometry.y(key),
                    z = WorkshopGeometry.z(key);
            tops.merge(x * 128 + z, y, Math::max);
            // Exclude the plinth and roof band; their overhang cannot disguise a flat wall.
            if (y < facadeBottom || y >= roofBottom) continue;
            int yz = y * 128 + z, xy = x * 128 + y;
            faces.get(0).merge(yz, x, Math::min);
            faces.get(1).merge(yz, x, Math::max);
            faces.get(2).merge(xy, z, Math::min);
            faces.get(3).merge(xy, z, Math::max);
        }
        var roofs = new HashMap<Integer, Integer>();
        int flat = 0, total = 0;
        for (var entry : tops.entrySet()) {
            int y = entry.getValue();
            if (y < roofBottom) continue;
            total++;
            roofs.merge(y, 1, Integer::sum);
            String b =
                    blocks.get(
                            WorkshopGeometry.index(entry.getKey() / 128, y, entry.getKey() % 128));
            if (!com.lunares.blockfield.workshop.WorkshopBlockStates.id(b).endsWith("_stairs"))
                flat++;
        }
        return new WorkshopAiFlatness(
                faces.stream().map(f -> dominant(f.values(), 12)).toList(),
                total < 16 || height < 4
                        ? 0
                        : roofs.values().stream().mapToInt(Integer::intValue).max().orElse(0)
                                / (double) total,
                total == 0 ? 0 : flat / (double) total);
    }

    private static double dominant(java.util.Collection<Integer> planes, int minimum) {
        if (planes.size() < minimum) return 0;
        var histogram = new HashMap<Integer, Integer>();
        for (int plane : planes) histogram.merge(plane, 1, Integer::sum);
        return histogram.values().stream().mapToInt(Integer::intValue).max().orElse(0)
                / (double) planes.size();
    }

    boolean exceeds(Settings settings) {
        return settings.enabled
                && (facades.stream().anyMatch(v -> v > settings.facadeThreshold)
                        || roofPlane > settings.roofThreshold
                                && flatTop > settings.flatTopThreshold);
    }

    JsonObject report(Settings settings) {
        var out = new JsonObject();
        var faces = new JsonObject();
        String[] names = {"west", "east", "north", "south"};
        for (int i = 0; i < 4; i++) faces.addProperty(names[i], facades.get(i));
        out.add("facades", faces);
        out.addProperty("roofPlane", roofPlane);
        out.addProperty("flatTop", flatTop);
        var thresholds = new JsonObject();
        thresholds.addProperty("facade", settings.facadeThreshold);
        thresholds.addProperty("roofPlane", settings.roofThreshold);
        thresholds.addProperty("flatTop", settings.flatTopThreshold);
        out.add("thresholds", thresholds);
        out.addProperty("exceedsThreshold", exceeds(settings));
        return out;
    }
}
