package blockfield.workshop.ai;

import com.google.gson.JsonArray;
import com.google.gson.JsonObject;

import java.util.List;
import java.util.Set;

/** Closed structural schemas; geometry, work, ACL and money still need native validation. */
final class WorkshopAiSchema {
    static JsonObject plan(
            int version, Set<String> palette, int opCap, boolean empty, boolean mirror) {
        JsonObject defs = new JsonObject();
        JsonObject point = array(integer(0, 127), 3, 3);
        JsonObject material =
                version == 3
                        ? any(
                                stateMaterial(palette),
                                object(
                                        "mix",
                                        array(
                                                array(
                                                        any(
                                                                stateMaterial(palette),
                                                                integer(1, Integer.MAX_VALUE)),
                                                        2,
                                                        2),
                                                2,
                                                6)))
                        : values(
                                palette.stream()
                                        .filter(
                                                com.lunares.blockfield.workshop.WorkshopMaterials
                                                        ::legacy)
                                        .sorted()
                                        .toList());
        JsonObject cuboid =
                object("from", point, "to", point, "block", material, "hollow", type("boolean"));
        if (version >= 2) add(cuboid, "match", type("string"));
        if (version == 3) {
            add(cuboid, "type", values(List.of("cuboid")));
            JsonObject ellipsoid = cuboid.deepCopy();
            ellipsoid.getAsJsonObject("properties").add("type", values(List.of("ellipsoid")));
            defs.add("cuboid", cuboid);
            defs.add("ellipsoid", ellipsoid);
            defs.add(
                    "tube",
                    object(
                            "type",
                            values(List.of("tube")),
                            "path",
                            array(point, 2, 16),
                            "curve",
                            values(List.of("polyline", "bezier")),
                            "radiusStart",
                            integer(0, 64),
                            "radiusEnd",
                            integer(0, 64),
                            "block",
                            material,
                            "hollow",
                            type("boolean"),
                            "match",
                            type("string")));
            defs.add("leaf", any(ref("cuboid"), ref("ellipsoid"), ref("tube")));
            defs.add(
                    "repeat",
                    object(
                            "type",
                            values(List.of("repeat")),
                            "count",
                            integer(1, 128),
                            "offset",
                            array(integer(-127, 127), 3, 3),
                            "operations",
                            array(ref("leaf"), 1, opCap)));
            defs.add("source", any(ref("leaf"), ref("repeat")));
            defs.add(
                    "mirror",
                    object(
                            "type",
                            values(List.of("mirror")),
                            "axes",
                            values(List.of("x", "z", "xz")),
                            "operations",
                            array(ref("source"), 1, opCap)));
        } else defs.add("leaf", cuboid);
        JsonObject operation =
                version == 3
                        ? mirror ? any(ref("source"), ref("mirror")) : ref("source")
                        : ref("leaf");
        JsonObject root =
                object(
                        "version",
                        number(version),
                        "operations",
                        array(operation, empty ? 0 : 1, opCap));
        root.add("$defs", defs);
        return format("workshop_plan_v" + version, root);
    }

    private static JsonObject stateMaterial(Set<String> palette) {
        var state = type("string");
        String ids =
                String.join(
                        "|", palette.stream().sorted().map(id -> id.replace(".", "\\.")).toList());
        state.addProperty("pattern", "^(?:" + ids + ")(?:\\[[a-z0-9_=,]+\\])?$");
        return state;
    }

    static JsonObject layout(int count, boolean mirror) {
        JsonObject point = array(integer(0, 127), 3, 3);
        JsonObject part =
                object(
                        "id",
                        text(24),
                        "role",
                        text(160),
                        "from",
                        point,
                        "to",
                        point,
                        "priority",
                        integer(0, count - 1),
                        "budget",
                        integer(1, 32768));
        JsonObject anchor = object("parts", array(text(24), 2, 2), "cells", array(point, 2, 2));
        return format(
                "workshop_layout",
                object(
                        "version",
                        number(1),
                        "symmetry",
                        values(mirror ? List.of("none", "x", "z", "xz") : List.of("none")),
                        "style",
                        text(1024),
                        "parts",
                        array(part, count, count),
                        "anchors",
                        array(anchor, 0, 24)));
    }

    private static JsonObject format(String name, JsonObject schema) {
        JsonObject root = new JsonObject(), contract = new JsonObject();
        root.addProperty("type", "json_schema");
        contract.addProperty("name", name);
        contract.addProperty("strict", true);
        contract.add("schema", schema);
        root.add("json_schema", contract);
        return root;
    }

    private static JsonObject type(String type) {
        var result = new JsonObject();
        result.addProperty("type", type);
        return result;
    }

    private static JsonObject integer(int min, int max) {
        var result = type("integer");
        result.addProperty("minimum", min);
        result.addProperty("maximum", max);
        return result;
    }

    private static JsonObject number(int value) {
        var result = type("integer");
        var values = new JsonArray();
        values.add(value);
        result.add("enum", values);
        return result;
    }

    private static JsonObject text(int cap) {
        var result = type("string");
        result.addProperty("minLength", 1);
        result.addProperty("maxLength", cap);
        return result;
    }

    private static JsonObject values(List<String> values) {
        var result = type("string");
        var list = new JsonArray();
        values.forEach(list::add);
        result.add("enum", list);
        return result;
    }

    private static JsonObject array(JsonObject item, int min, int max) {
        var result = type("array");
        result.add("items", item);
        result.addProperty("minItems", min);
        result.addProperty("maxItems", max);
        return result;
    }

    private static JsonObject ref(String name) {
        var result = new JsonObject();
        result.addProperty("$ref", "#/$defs/" + name);
        return result;
    }

    private static JsonObject any(JsonObject... choices) {
        var result = new JsonObject();
        var alternatives = new JsonArray();
        for (var choice : choices) alternatives.add(choice);
        result.add("anyOf", alternatives);
        return result;
    }

    private static JsonObject object(Object... pairs) {
        var result = type("object");
        result.add("properties", new JsonObject());
        result.add("required", new JsonArray());
        result.addProperty("additionalProperties", false);
        for (int i = 0; i < pairs.length; i += 2)
            add(result, (String) pairs[i], (JsonObject) pairs[i + 1]);
        return result;
    }

    private static void add(JsonObject object, String name, JsonObject schema) {
        object.getAsJsonObject("properties").add(name, schema);
        object.getAsJsonArray("required").add(name);
    }
}
