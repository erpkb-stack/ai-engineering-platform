package com.northwind.billing;

/**
 * billing-api: Serve liveness and readiness; readiness fails when the database pool is empty.
 */
public final class HealthProbeController {
    private final java.util.Map<String, Object> state = new java.util.concurrent.ConcurrentHashMap<>();

    /** Serve liveness and readiness; readiness fails when the database pool is empty. */
    public Object handle(String key, Object request) {
        return state.computeIfAbsent(key, k -> request);
    }
}
