package com.lunares.blockfield.workshop;

import com.google.gson.JsonElement;

import java.util.ArrayList;
import java.util.List;
import java.util.Set;

/** Weighted textures sample expanded source cells, before any player placement transform. */
public record WorkshopMaterials(List<String> blocks, List<Integer> weights) {
    public static WorkshopMaterials parse(JsonElement value, int version) {
        if (value != null && value.isJsonPrimitive() && value.getAsJsonPrimitive().isString())
            return new WorkshopMaterials(List.of(state(value.getAsString(), version)), List.of(1));
        if (version != 3 || value == null || !value.isJsonObject())
            throw new IllegalArgumentException("Invalid material");
        var mix = value.getAsJsonObject();
        if (!mix.keySet().equals(Set.of("mix")) || !mix.get("mix").isJsonArray())
            throw new IllegalArgumentException("Invalid material mix");
        var entries = mix.getAsJsonArray("mix");
        if (entries.size() < 2 || entries.size() > 6)
            throw new IllegalArgumentException("Material mix size");
        var blocks = new ArrayList<String>();
        var weights = new ArrayList<Integer>();
        for (var entry : entries) {
            if (!entry.isJsonArray() || entry.getAsJsonArray().size() != 2)
                throw new IllegalArgumentException("Invalid mix entry");
            var pair = entry.getAsJsonArray();
            if (!pair.get(0).isJsonPrimitive()
                    || !pair.get(0).getAsJsonPrimitive().isString()
                    || !pair.get(1).isJsonPrimitive()
                    || !pair.get(1).getAsJsonPrimitive().isNumber())
                throw new IllegalArgumentException("Invalid mix entry");
            String block = state(pair.get(0).getAsString(), version);
            int weight = pair.get(1).getAsBigDecimal().intValueExact();
            if (weight < 1) throw new IllegalArgumentException("Mix weight must be positive");
            blocks.add(block);
            weights.add(weight);
        }
        return new WorkshopMaterials(List.copyOf(blocks), List.copyOf(weights));
    }

    public static boolean legacy(String block) {
        return !block.endsWith("_stairs") && !block.endsWith("_trapdoor");
    }

    private static String state(String block, int version) {
        if (version < 3 && !legacy(block))
            throw new IllegalArgumentException("Material requires v3");
        if (!(version == 3
                ? block.matches(WorkshopGeometry.ID + "(?:\\[[a-z0-9_=,]+\\])?")
                : block.matches(WorkshopGeometry.ID)))
            throw new IllegalArgumentException("Invalid material");
        if (block.contains("[")) WorkshopBlockStates.parse(block);
        return block;
    }

    public String at(int x, int y, int z, long seed) {
        if (blocks.size() == 1) return blocks.getFirst();
        long hash =
                seed
                        ^ (x * 0x9e3779b97f4a7c15L)
                        ^ (y * 0xbf58476d1ce4e5b9L)
                        ^ (z * 0x94d049bb133111ebL);
        hash = (hash ^ (hash >>> 30)) * 0xbf58476d1ce4e5b9L;
        hash = (hash ^ (hash >>> 27)) * 0x94d049bb133111ebL;
        hash ^= hash >>> 31;
        long total = 0;
        for (int weight : weights) total += weight;
        long chosen = Long.remainderUnsigned(hash, total);
        for (int i = 0; i < blocks.size(); i++) {
            if (chosen < weights.get(i)) return blocks.get(i);
            chosen -= weights.get(i);
        }
        throw new AssertionError("Weighted material selection");
    }
}
