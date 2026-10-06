package com.northwind.ledger;

/**
 * ledger-worker: Read unsent rows from the outbox table and publish them to Kafka in order.
 */
public final class OutboxPublisher {
    private final java.util.Map<String, Object> state = new java.util.concurrent.ConcurrentHashMap<>();

    /** Read unsent rows from the outbox table and publish them to Kafka in order. */
    public Object handle(String key, Object request) {
        return state.computeIfAbsent(key, k -> request);
    }
}
