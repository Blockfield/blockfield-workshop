package blockfield.workshop.ai;

import com.google.gson.JsonObject;

/**
 * The effective config snapshot the mod sends inside each generate request: only the sidecar-owned
 * half (models, providers, owner caps, planning). The game JVM keeps validating the full config;
 * the sidecar re-checks just these bounds and fails the request (never the ledger) when they are
 * missing or malformed. Key files stay env-referenced (see {@link AiSidecar}); no key value ever
 * crosses this boundary.
 */
record SidecarConfig(
        String generationModel,
        String generationProvider,
        String transcriptionModel,
        String transcriptionProvider,
        String cleanupModel,
        String cleanupProvider,
        int maxBlocks,
        double generationPromptCapUsdPerM,
        double generationCompletionCapUsdPerM,
        WorkshopAiPlanning.Settings planning,
        String effort) {
    static final String MODEL = "[a-zA-Z0-9._-]{1,64}/[a-zA-Z0-9._/:-]{1,126}";
    static final String NATIVE_MODEL = "[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,99}";

    SidecarConfig withMaxBlocks(int cap) {
        return new SidecarConfig(
                generationModel,
                generationProvider,
                transcriptionModel,
                transcriptionProvider,
                cleanupModel,
                cleanupProvider,
                cap,
                generationPromptCapUsdPerM,
                generationCompletionCapUsdPerM,
                planning,
                effort);
    }

    static SidecarConfig parse(JsonObject config) {
        if (config == null) throw new IllegalArgumentException("config must be an object");
        String generationProvider = string(config, "generationProvider");
        if (!ProviderHttp.validProvider(generationProvider))
            throw new IllegalArgumentException(
                    "generationProvider must be openrouter, vercel-gateway or custom");
        String transcriptionProvider = string(config, "transcriptionProvider");
        if (!ProviderHttp.validProvider(transcriptionProvider))
            throw new IllegalArgumentException(
                    "transcriptionProvider must be openrouter, vercel-gateway or custom");
        String cleanupProvider = string(config, "cleanupProvider");
        if (!ProviderHttp.validProvider(cleanupProvider))
            throw new IllegalArgumentException(
                    "cleanupProvider must be openrouter, vercel-gateway or custom");
        String generationModel = model(config, "generationModel", generationProvider);
        String transcriptionModel = model(config, "transcriptionModel", transcriptionProvider);
        String cleanupModel = optionalModel(config, "cleanupModel", cleanupProvider);
        int maxBlocks = config.has("maxBlocks") ? config.get("maxBlocks").getAsInt() : 0;
        if (maxBlocks < 1 || maxBlocks > 32768)
            throw new IllegalArgumentException("maxBlocks must be 1..32768");
        double promptCap = capped(config, "generationPromptCapUsdPerM");
        double completionCap = capped(config, "generationCompletionCapUsdPerM");
        WorkshopAiPlanning.Settings planning;
        if (config.has("planning") && config.get("planning").isJsonObject()) {
            try {
                planning = WorkshopAiPlanning.Settings.parse(config.getAsJsonObject("planning"));
            } catch (RuntimeException invalid) {
                throw new IllegalArgumentException("planning: " + invalid.getMessage());
            }
        } else {
            throw new IllegalArgumentException("planning must be an object");
        }
        String effort = config.has("effort") ? string(config, "effort") : "high";
        if (effort.isEmpty() || effort.length() > 16)
            throw new IllegalArgumentException("effort outside bounds");
        return new SidecarConfig(
                generationModel,
                generationProvider,
                transcriptionModel,
                transcriptionProvider,
                cleanupModel,
                cleanupProvider,
                maxBlocks,
                promptCap,
                completionCap,
                planning,
                effort);
    }

    private static String model(JsonObject config, String field, String provider) {
        String value = string(config, field);
        boolean vendorShape = value.matches(MODEL);
        boolean nativeShape = value.matches(NATIVE_MODEL);
        if (!vendorShape && !(nativeShape && ProviderHttp.CUSTOM.equals(provider)))
            throw new IllegalArgumentException(field + " outside bounds");
        return value;
    }

    private static String optionalModel(JsonObject config, String field, String provider) {
        if (!config.has(field)
                || config.get(field).isJsonNull()
                || config.get(field).getAsString().isEmpty()) return "";
        return model(config, field, provider);
    }

    private static String string(JsonObject config, String field) {
        if (!config.has(field)
                || !config.get(field).isJsonPrimitive()
                || !config.getAsJsonPrimitive(field).isString()
                || config.get(field).getAsString().isEmpty())
            throw new IllegalArgumentException(field + " must be a non-empty string");
        return config.get(field).getAsString();
    }

    private static double capped(JsonObject config, String field) {
        double value =
                config.has(field) && config.get(field).isJsonPrimitive()
                        ? config.get(field).getAsDouble()
                        : Double.NaN;
        if (!Double.isFinite(value) || value < 0 || value > 1000)
            throw new IllegalArgumentException(field + " must be 0..1000");
        return value;
    }
}
