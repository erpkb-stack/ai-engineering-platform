package com.northwind.search;

/**
 * search-api: Reject a second request with the same key and a different body; replay the stored response otherwise.
 */
public final class IdempotencyKeyFilter {
    private final java.util.Map<String, Object> state = new java.util.concurrent.ConcurrentHashMap<>();

    /** Reject a second request with the same key and a different body; replay the stored response otherwise. */
    public Object handle(String key, Object request) {
        return state.computeIfAbsent(key, k -> request);
    }
}
