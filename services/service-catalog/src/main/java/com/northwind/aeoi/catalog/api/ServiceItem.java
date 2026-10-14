package com.northwind.aeoi.catalog.api;

import com.fasterxml.jackson.annotation.JsonProperty;
import java.util.List;

/** One service, exactly as openapi.yaml#/components/schemas/ServiceItem (CatalogContractTest). */
public record ServiceItem(
        String key,
        String name,
        String type,
        int tier,
        String team,
        String language,
        String repository,
        @JsonProperty("depends_on") List<String> dependsOn,
        List<String> dependents) {}
