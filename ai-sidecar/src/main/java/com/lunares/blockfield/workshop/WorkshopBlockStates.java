package com.lunares.blockfield.workshop;

/**
 * String-level half of the mod's block-state helper for the sidecar. The game JVM keeps the
 * registry-backed {@code parse/shape/view} calls; generation only needs id extraction and the
 * orientation rotation that symmetric plan expansion applies.
 *
 * <p>shortcut: stair inner/outer shape variants and other multi-property orientation details are
 * left unchanged on rotate/mirror (facing/axis/rotation are exact); the mod re-validates before
 * apply and neighbor updates recompute stair shapes. Upgrade to a shared pure-string table if
 * rotated symmetric plans show wrong stair corners.
 */
public final class WorkshopBlockStates {
    private WorkshopBlockStates() {}

    public static String id(String text) {
        int bracket = text.indexOf('[');
        return bracket < 0 ? text : text.substring(0, bracket);
    }

    /**
     * Syntax-only validation for material strings the plans carry. Registry checks (known block,
     * known property, unsafe open/powered/fluid states) stay mod-side; the geometry copy validates
     * shape here so malformed states fail before they reach the ledger.
     */
    static void parse(String text) {
        String id = id(text);
        if (!id.matches(WorkshopGeometry.ID))
            throw new IllegalArgumentException("Invalid block id");
        if (id.equals(text) || !text.endsWith("]"))
            throw new IllegalArgumentException("Unsupported block state");
        String properties = text.substring(id.length() + 1, text.length() - 1);
        if (properties.isEmpty()) throw new IllegalArgumentException("Empty block properties");
        var seen = new java.util.HashSet<String>();
        for (String pair : properties.split(",", -1)) {
            String[] parts = pair.split("=", -1);
            if (parts.length != 2
                    || parts[0].isEmpty()
                    || parts[1].isEmpty()
                    || !seen.add(parts[0]))
                throw new IllegalArgumentException("Invalid or duplicate block property");
        }
    }

    public static String transform(String text, int rotY, boolean x, boolean z) {
        // Same fast paths as the mod: no-op transforms and blocks without orientation pass through.
        if ((rotY & 3) == 0 && !x && !z) return text;
        String id = id(text);
        if (id.equals(text) && !mayHaveOrientation(id)) return text;
        if (id.equals(text)) return text;
        int start = text.indexOf('[');
        int end = text.lastIndexOf(']');
        if (start < 0 || end < start) return text;
        String[] props = text.substring(start + 1, end).split(",");
        StringBuilder out = new StringBuilder(id).append('[');
        for (int i = 0; i < props.length; i++) {
            String prop = props[i];
            int eq = prop.indexOf('=');
            if (i > 0) out.append(',');
            if (eq < 0) {
                out.append(prop);
                continue;
            }
            String key = prop.substring(0, eq);
            String value = prop.substring(eq + 1);
            // Same order as the mod: x mirror, then z mirror, then Y rotation.
            if (x && key.equals("facing")) value = mirrorFacing(value, true);
            if (z && key.equals("facing")) value = mirrorFacing(value, false);
            value = rotateValue(key, value, rotY & 3);
            out.append(key).append('=').append(value);
        }
        return out.append(']').toString();
    }

    private static String mirrorFacing(String facing, boolean frontBack) {
        // FRONT_BACK swaps north/south, LEFT_RIGHT swaps east/west; up/down never change.
        return switch (facing) {
            case "north" -> frontBack ? "south" : "north";
            case "south" -> frontBack ? "north" : "south";
            case "east" -> frontBack ? "east" : "west";
            case "west" -> frontBack ? "west" : "east";
            default -> facing;
        };
    }

    private static String rotateValue(String key, String value, int rot) {
        if (rot == 0) return value;
        if (key.equals("facing")) return rotateFacing(value, rot);
        if (key.equals("axis")) return rotateAxis(value, rot);
        if (key.equals("rotation")) return rotateInt(value, rot);
        return value;
    }

    private static String rotateFacing(String facing, int rot) {
        String[] order = {"north", "east", "south", "west"};
        for (int i = 0; i < order.length; i++) {
            if (order[i].equals(facing)) return order[(i + rot) & 3];
        }
        return facing;
    }

    private static String rotateAxis(String axis, int rot) {
        // Quarter turns swap x/z; half turns leave every axis alone.
        if ((rot & 1) == 0) return axis;
        return switch (axis) {
            case "x" -> "z";
            case "z" -> "x";
            default -> axis;
        };
    }

    private static String rotateInt(String value, int rot) {
        try {
            return Integer.toString((Integer.parseInt(value) + rot * 4) & 15);
        } catch (NumberFormatException e) {
            return value;
        }
    }

    private static boolean mayHaveOrientation(String id) {
        return id.startsWith("minecraft:")
                && (id.endsWith("_stairs")
                        || id.endsWith("_slab")
                        || id.endsWith("_wall")
                        || id.endsWith("_fence")
                        || id.endsWith("_pane")
                        || id.endsWith("_trapdoor")
                        || id.endsWith("_log")
                        || id.endsWith("_wood")
                        || id.endsWith("_pillar")
                        || id.endsWith("_stem")
                        || id.endsWith("_hyphae")
                        || id.endsWith("lantern")
                        || id.equals("minecraft:chain")
                        || id.equals("minecraft:iron_bars")
                        || id.equals("minecraft:hay_block")
                        || id.equals("minecraft:basalt")
                        || id.equals("minecraft:polished_basalt")
                        || id.equals("minecraft:bone_block"));
    }
}
