package com.northwind.aeoi.catalog.api;

import java.util.List;

/** openapi.yaml#/components/schemas/ServiceList. */
public record ServiceList(List<ServiceItem> items, boolean truncated) {}
