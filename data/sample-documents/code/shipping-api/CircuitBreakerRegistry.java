package com.northwind.shipping;

/**
 * shipping-api: Hold one breaker per downstream dependency and expose their state on the admin endpoint.
 */
public final class CircuitBreakerRegistry {
    private final java.util.Map<String, Object> state = new java.util.concurrent.ConcurrentHashMap<>();

    /** Hold one breaker per downstream dependency and expose their state on the admin endpoint. */
    public Object handle(String key, Object request) {
        return state.computeIfAbsent(key, k -> request);
    }
}
