package com.northwind.aeoi.catalog;

import static org.assertj.core.api.Assertions.assertThat;

import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.actuate.health.HealthEndpointGroups;

/**
 * Phase 21 (ADR-033), found on the Mac: the container was "healthy" while its database pool was
 * still closed, so the FIRST real request paid for opening it (3.9 s) and the gateway's 2 s tool
 * timeout failed it. Readiness must include the database.
 */
class CatalogReadinessTest extends CatalogTestBase {

    @Autowired HealthEndpointGroups groups;

    @Test
    void readinessIncludesTheDatabase() {
        var readiness = groups.get("readiness");
        assertThat(readiness).as("the readiness group exists (probes enabled)").isNotNull();
        assertThat(readiness.isMember("db")).as("db is part of readiness").isTrue();
        assertThat(readiness.isMember("readinessState")).isTrue();
        // liveness must NOT include the database: a DB outage would restart a healthy JVM
        assertThat(groups.get("liveness").isMember("db")).isFalse();
    }

    @Test
    void readyIsOpenAndUpWhenTheDatabaseAnswers() throws Exception {
        var r = get("/actuator/health/readiness", null);
        assertThat(r.statusCode()).as(r.body()).isEqualTo(200);
        assertThat(r.body()).contains("\"UP\"");
    }
}
