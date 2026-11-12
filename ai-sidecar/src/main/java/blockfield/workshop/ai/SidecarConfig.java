package blockfield.workshop.ai;

import org.json.JSONObject;

/**
 * The effective {@code WorkshopAiConfig} snapshot the mod sends inside each generate request: only
 * the provider half the sidecar owns (model, provider, owner caps, effort). The game JVM keeps
 * validating the full config; the sidecar re-checks just these bounds and fails the request (never
 * the ledger) when they are missing or malformed. Key files stay env-referenced (see {@link
 * AiSidecar}); no key value ever crosses this boundary.
 */
record SidecarConfig(
        String generationModel,
        String generationProvider,
        double generationPromptCapUsdPerM,
        double generationCompletionCapUsdPerM,
        String effort) {
    private static final String MODEL = "[a-zA-Z0-9._-]{1,64}/[a-zA-Z0-9._/:-]{1,126}";
    private static final String NATIVE_MODEL = "[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,99}";

    static SidecarConfig parse(JSONObject config) {
        if (config == null) throw new IllegalArgumentException("config must be an object");
        String provider = string(config, "generationProvider");
        if (!ProviderHttp.validProvider(provider))
            throw new IllegalArgumentException(
                    "generationProvider must be openrouter," + " vercel-gateway or custom");
        String model = string(config, "generationModel");
        boolean vendorShape = model.matches(MODEL);
        boolean nativeShape = model.matches(NATIVE_MODEL);
        if (!vendorShape && !(nativeShape && ProviderHttp.CUSTOM.equals(provider)))
            throw new IllegalArgumentException("generationModel outside bounds");
        double promptCap = capped(config, "generationPromptCapUsdPerM");
        double completionCap = capped(config, "generationCompletionCapUsdPerM");
        String effort = config.optString("effort", "high");
        if (effort.isEmpty() || effort.length() > 16)
            throw new IllegalArgumentException("effort outside bounds");
        return new SidecarConfig(model, provider, promptCap, completionCap, effort);
    }

    private static String string(JSONObject config, String field) {
        Object value = config.opt(field);
        if (!(value instanceof String) || ((String) value).isEmpty())
            throw new IllegalArgumentException(field + " must be a non-empty string");
        return (String) value;
    }

    private static double capped(JSONObject config, String field) {
        double value = config.optDouble(field, Double.NaN);
        if (!Double.isFinite(value) || value < 0 || value > 1000)
            throw new IllegalArgumentException(field + " must be 0..1000");
        return value;
    }
}
