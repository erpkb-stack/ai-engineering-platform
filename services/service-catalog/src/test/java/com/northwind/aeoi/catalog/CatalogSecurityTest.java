package com.northwind.aeoi.catalog;

import static org.assertj.core.api.Assertions.assertThat;

import java.util.List;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

/** The same rules as libs/security Authenticator: who may read the catalog, and who may not. */
class CatalogSecurityTest extends CatalogTestBase {

    static final String Q = "/v1/services?service_key=a-db";

    @BeforeEach
    void data() throws Exception {
        load(List.of(svc("a-db", "t1", 1)));
    }

    int status(String token) throws Exception {
        return get(Q, token).statusCode();
    }

    @Test
    void aServiceTokenWithTheScopeReads() throws Exception {
        assertThat(status(serviceToken("catalog:read"))).isEqualTo(200);
        assertThat(status(serviceToken("audit:write catalog:read"))).isEqualTo(200);
    }

    @Test
    void noTokenOrABadTokenIs401() throws Exception {
        assertThat(status(null)).isEqualTo(401);
        assertThat(status("not-a-jwt")).isEqualTo(401);
        // signed by another key
        assertThat(status(sign(claims("service:tool-gateway").claim("scope", "catalog:read").build(), rsa())))
                .isEqualTo(401);
    }

    @Test
    void wrongIssuerAudienceOrMissingTimesAre401() throws Exception {
        assertThat(status(sign(claims("service:x").claim("scope", "catalog:read").issuer("evil").build())))
                .isEqualTo(401);
        assertThat(status(sign(claims("service:x").claim("scope", "catalog:read").audience("other").build())))
                .isEqualTo(401);
        assertThat(status(sign(claims("service:x").claim("scope", "catalog:read").expirationTime(null).build())))
                .isEqualTo(401);
        assertThat(status(sign(claims("service:x").claim("scope", "catalog:read").issueTime(null).build())))
                .isEqualTo(401);
        assertThat(status(sign(claims("service:x").claim("scope", "catalog:read")
                .issueTime(java.util.Date.from(java.time.Instant.now().plusSeconds(600))).build())))
                .isEqualTo(401); // issued in the future
        assertThat(status(sign(claims("service:x").claim("scope", "catalog:read")
                .expirationTime(java.util.Date.from(java.time.Instant.now().minusSeconds(120))).build())))
                .isEqualTo(401); // expired beyond the 30 s leeway
    }

    @Test
    void aDelegatedTokenIsNeverABearer() throws Exception {
        assertThat(status(sign(claims("service:x").claim("scope", "catalog:read")
                .claim("act", java.util.Map.of("sub", "service:orchestrator")).build()))).isEqualTo(401);
        assertThat(status(sign(claims("service:x").claim("scope", "catalog:read")
                .claim("token_use", "delegated").build()))).isEqualTo(401);
    }

    @Test
    void userTokensAreRefusedEvenWithTheScope() throws Exception {
        assertThat(status(sign(claims("0198f846-1800-750e-b4bc-886dc1e8ccad")
                .claim("scope", "catalog:read").build()))).isEqualTo(401);
        assertThat(status(sign(claims("service:").claim("scope", "catalog:read").build()))).isEqualTo(401);
    }

    @Test
    void aServiceWithoutTheScopeIs403() throws Exception {
        assertThat(status(serviceToken("audit:write"))).isEqualTo(403);
        assertThat(status(serviceToken("catalog:readx"))).isEqualTo(403);
    }

    @Test
    void healthAndTheContractAreOpenEverythingElseIsClosed() throws Exception {
        assertThat(get("/actuator/health", null).statusCode()).isEqualTo(200);
        assertThat(get("/v1/openapi.yaml", null).statusCode()).isEqualTo(200);
        assertThat(get("/actuator/env", serviceToken("catalog:read")).statusCode()).isIn(401, 403, 404);
        assertThat(get("/v1/other", serviceToken("catalog:read")).statusCode()).isIn(401, 403, 404);
    }
}
