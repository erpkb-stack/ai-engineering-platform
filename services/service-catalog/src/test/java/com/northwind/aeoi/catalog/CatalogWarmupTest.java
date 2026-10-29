package com.northwind.aeoi.catalog;

import static org.assertj.core.api.Assertions.assertThat;

import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;

/**
 * Phase 29 gate fix5 (ADR-044 amendment): a fresh catalog answered its first real request in
 * 2.1-21.1 s on the Mac while its readiness probe was UP. The warm-up runs the real path before
 * Spring publishes ACCEPTING_TRAFFIC; its probe token must never be accepted.
 */
class CatalogWarmupTest extends CatalogTestBase {

    @Autowired CatalogWarmup warmup;

    @Test
    void theWarmupRanThePathBeforeReadyAndWasRefusedAt401() throws Exception {
        CatalogWarmup.Result r = warmup.last();
        assertThat(r).as("ApplicationRunners run before ACCEPTING_TRAFFIC").isNotNull();
        assertThat(r.error()).isNull();
        assertThat(r.httpStatus()).as("through the filter chain and the RSA verify").isEqualTo(401);
        assertThat(r.items()).as("a key that is never seeded").isZero();
        var ready = get("/actuator/health/readiness", null);
        assertThat(ready.statusCode()).isEqualTo(200);
    }

    @Test
    void theWarmupTokenIsNeverAccepted() throws Exception {
        // well-formed, RS256, catalog:read scope - but a zero signature: must stay a 401
        var r = get("/v1/services?service_key=checkout-api", CatalogWarmup.wrongSignatureToken());
        assertThat(r.statusCode()).isEqualTo(401);
        // the probe key passes the controller's own rule (ServicesController.SERVICE_KEY)
        assertThat(CatalogWarmup.NO_SUCH_KEY).matches("^[a-z0-9][a-z0-9-]{1,99}$");
    }
}
