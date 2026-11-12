package blockfield.workshop.ai;

import com.lunares.blockfield.workshop.WorkshopGeometry;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Set;

/**
 * Closed grid language: no commands, expressions, URLs, NBT, entities or executable scripts.
 *
 * <p>Sidecar port of the mod {@code WorkshopAiPlan}. Registry membership ({@code
 * BuiltInRegistries}) cannot cross the process boundary, so the sidecar bounds blocks by the
 * uploaded palette and re-checks state-id SHAPE only; the mod re-validates every returned plan
 * against the block registry before apply (contract §plan JSON).
 */
record WorkshopAiPlan(
        List<Cell> cells,
        String canonical,
        int demolishVolume,
        double destructiveScore,
        List<String> warnings) {
    static final int MAX_JSON = WorkshopGeometry.MAX_MERGED_JSON;
    static final int MAX_OPERATIONS = WorkshopGeometry.MAX_OPERATIONS;
    // Matches WorkshopAiPreviewS2CPacket.MAX_SIDE in the mod (packet codec frozen; do not drift).
    static final int MAX_SIDE = 128;

    /** v2/v3 default to protecting the original world; v1 defaults to overwrite-all. */
    static final String MATCH_AIR = "air";

    static final String MATCH_ANY = "any";

    /** Same closed id shape the client blueprint enforces; registry membership checked mod-side. */
    static final String MATCH_ID = "[a-z0-9_.-]+:[a-z0-9_/.-]+";

    record Cell(
            int x, int y, int z, String block, String match, List<WorkshopGeometry.Write> writes) {
        Cell {
            writes = List.copyOf(writes);
        }

        Cell(int x, int y, int z, String block, String match) {
            this(x, y, z, block, match, List.of(new WorkshopGeometry.Write(block, match)));
        }

        /**
         * The air keyword protects the original world so overlapping construction still works;
         * block-id matches see the last successful planned write, including a planned air gap.
         * Shared with the client preview via {@link WorkshopGeometry#resolve}.
         */
        String resolve(String beforeId, boolean beforeIsAir) {
            return WorkshopGeometry.resolve(writes, beforeId, beforeIsAir);
        }
    }

    private static boolean isAir(String id) {
        return "minecraft:air".equals(id)
                || "minecraft:cave_air".equals(id)
                || "minecraft:void_air".equals(id);
    }

    /** Block id without a state suffix ({@code id[props]} -> {@code id}); pure string split. */
    static String id(String text) {
        int bracket = text.indexOf('[');
        return bracket < 0 ? text : text.substring(0, bracket);
    }

    WorkshopAiPlan {
        cells = List.copyOf(cells);
        warnings = List.copyOf(warnings);
    }

    WorkshopAiPlan(
            List<Cell> cells, String canonical, int demolishVolume, double destructiveScore) {
        this(cells, canonical, demolishVolume, destructiveScore, List.of());
    }

    static WorkshopAiPlan parse(
            String json, Set<String> palette, int maxBlocks, int sideX, int sideY, int sideZ) {
        return parse(json, palette, maxBlocks, sideX, sideY, sideZ, false);
    }

    static WorkshopAiPlan parseMerged(
            String json, Set<String> palette, int maxBlocks, int sideX, int sideY, int sideZ) {
        return parse(json, palette, maxBlocks, sideX, sideY, sideZ, true);
    }

    private static WorkshopAiPlan parse(
            String json,
            Set<String> palette,
            int maxBlocks,
            int sideX,
            int sideY,
            int sideZ,
            boolean merged) {
        if (json == null || json.length() > (merged ? MAX_JSON : WorkshopGeometry.MAX_JSON))
            throw new IllegalArgumentException("Plan size");
        var cells = new LinkedHashMap<Integer, List<WorkshopGeometry.Write>>();
        var demolition = new HashSet<Integer>();
        var checkedMatches = new HashSet<String>();
        java.util.function.Consumer<WorkshopGeometry.Voxel> output =
                voxel -> {
                    if (!palette.contains(id(voxel.block())))
                        throw new IllegalArgumentException("Block outside allowed palette");
                    if (voxel.block().contains("[")) checkStateSyntax(voxel.block());
                    String match = voxel.match();
                    if (!MATCH_AIR.equals(match)
                            && !MATCH_ANY.equals(match)
                            && checkedMatches.add(match)
                            && !registered(match))
                        throw new IllegalArgumentException("Unknown match block");
                    int key = WorkshopGeometry.index(voxel.x(), voxel.y(), voxel.z());
                    var writes = cells.computeIfAbsent(key, ignored -> new ArrayList<>());
                    writes.add(new WorkshopGeometry.Write(voxel.block(), match));
                    if (MATCH_ANY.equals(match) || isAir(voxel.block())) demolition.add(key);
                };
        var geometry =
                merged
                        ? WorkshopGeometry.expandMerged(
                                json, maxBlocks, sideX, sideY, sideZ, output)
                        : WorkshopGeometry.expand(json, maxBlocks, sideX, sideY, sideZ, output);
        for (String material : geometry.materials())
            if (!palette.contains(id(material)))
                throw new IllegalArgumentException("Block outside allowed palette");
        var ordered = new ArrayList<Cell>(cells.size());
        cells.forEach(
                (point, writes) -> {
                    var last = writes.getLast();
                    if (geometry.version() == 1) {
                        writes.clear();
                        writes.add(last);
                    }
                    ordered.add(
                            new Cell(
                                    WorkshopGeometry.x(point),
                                    WorkshopGeometry.y(point),
                                    WorkshopGeometry.z(point),
                                    last.block(),
                                    last.match(),
                                    writes));
                });
        ordered.sort(
                Comparator.comparingInt(Cell::y)
                        .thenComparingInt(cell -> cell.x() + cell.z())
                        .thenComparingInt(Cell::x));
        return new WorkshopAiPlan(ordered, geometry.canonical(), demolition.size(), 0);
    }

    /**
     * Reattaches the stored destruction score after a re-parse: {@link #parse} always returns 0
     * (the score lives in the guard response, not the plan JSON), so library load and placement
     * must restore it or an authorized plan silently loses its authorization.
     */
    static WorkshopAiPlan withDestructiveScore(WorkshopAiPlan plan, double score) {
        if (!Double.isFinite(score) || score < 0 || score > 1)
            throw new IllegalArgumentException("Invalid destructive score");
        return new WorkshopAiPlan(
                plan.cells(), plan.canonical(), plan.demolishVolume(), score, plan.warnings());
    }

    /**
     * Matches one operation against its chosen source (original world or planned value). The apply
     * journal separately re-checks the original world state before its final write. Shared with the
     * client preview via {@link WorkshopGeometry#matches}.
     */
    static boolean matches(String match, String beforeId, boolean beforeIsAir) {
        return WorkshopGeometry.matches(match, beforeId, beforeIsAir);
    }

    /**
     * Shape-only match-id check. The mod re-checks registry membership before apply; the sidecar
     * cannot (no registry here), so a well-shaped but unknown id passes here and fails there.
     */
    static boolean registered(String id) {
        return id != null && id.matches(MATCH_ID);
    }

    /**
     * State-suffix shape check ({@code id[k=v,...]}). The mod re-validates values against the block
     * registry before apply; the sidecar rejects only malformed suffixes.
     */
    static void checkStateSyntax(String block) {
        int open = block.indexOf('[');
        if (open < 0 || !block.endsWith("]") || open == block.length() - 1)
            throw new IllegalArgumentException("Bad block state");
        for (String prop : block.substring(open + 1, block.length() - 1).split(",")) {
            String[] kv = prop.split("=", -1);
            if (kv.length != 2 || !kv[0].matches("[a-z0-9_]+") || !kv[1].matches("[a-zA-Z0-9_.-]+"))
                throw new IllegalArgumentException("Bad block state");
        }
    }
}
