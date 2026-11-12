package blockfield.workshop.ai;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;

import com.google.gson.JsonArray;
import com.google.gson.JsonNull;
import com.google.gson.JsonObject;

import org.junit.jupiter.api.Test;

import java.util.List;

/**
 * Fill wire round-trip: the z-axis encoding (axis 2) and the -1 seam sentinel the producer emits
 * must survive optionJson/parseOption, and a bridgeless bridge must still be rejected.
 */
class WorkshopAiFillTest {
    private static JsonObject sideJson(int k, int width, int sy, int period) {
        var side = new JsonObject();
        side.addProperty("k", k);
        side.addProperty("width", width);
        side.addProperty("slab", 1);
        side.addProperty("extent", 1);
        side.addProperty("period", period);
        side.addProperty("step", 0);
        side.addProperty("evidence", 1);
        if (period == 0) {
            side.add("module", JsonNull.INSTANCE);
        } else {
            var rows = new JsonArray();
            for (int r = 0; r < period; r++) {
                var row = new JsonArray();
                for (int i = 0; i < width * sy; i++) row.add("minecraft:stone");
                rows.add(row);
            }
            side.add("module", rows);
        }
        return side;
    }

    private static JsonObject optionJson(
            int axis, List<JsonObject> sides, boolean periodic, int seam) {
        var json = new JsonObject();
        json.addProperty("axis", axis);
        var array = new JsonArray();
        sides.forEach(array::add);
        json.add("sides", array);
        json.addProperty("touch", 7);
        json.addProperty("periodic", periodic);
        json.addProperty("seam", seam);
        json.add("plan", new JsonObject());
        json.addProperty("writes", 0);
        json.addProperty("opaque", 0);
        return json;
    }

    @Test
    void zAxisOptionRoundTrips() {
        // analyze emits axis 2 for z; optionJson round-trips it back through parseOption.
        JsonObject json = optionJson(2, List.of(sideJson(2, 4, 3, 0)), false, -1);
        WorkshopAiFill.Option option = WorkshopAiFill.parseOption(json, 3);
        assertEquals(2, option.axis());
        assertEquals(-1, option.seam());
        assertEquals(2, WorkshopAiFill.optionJson(option).get("axis").getAsInt());
    }

    @Test
    void bridgeOptionKeepsItsSeam() {
        JsonObject json =
                optionJson(0, List.of(sideJson(0, 4, 3, 1), sideJson(1, 4, 3, 1)), true, 2);
        WorkshopAiFill.Option option = WorkshopAiFill.parseOption(json, 3);
        assertEquals(2, option.seam());
    }

    @Test
    void bridgelessBridgeIsRejected() {
        JsonObject json =
                optionJson(0, List.of(sideJson(0, 4, 3, 1), sideJson(1, 4, 3, 1)), true, -1);
        assertThrows(IllegalArgumentException.class, () -> WorkshopAiFill.parseOption(json, 3));
    }
}
