package com.northwind.aeoi.catalog;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.northwind.aeoi.catalog.persistence.CatalogRepository;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.Base64;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.ApplicationArguments;
import org.springframework.boot.ApplicationRunner;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.core.env.Environment;
import org.springframework.stereotype.Component;

/**
 * Phase 29 gate fix5 (ADR-044 amendment): "ready" means the REAL request path is warm.
 *
 * <p>Tempo traces on the Mac, 2026-10-29: a fresh catalog answered its first GET /v1/services in
 * 2.1, 3.5, 17.8 and 21.1 s while its readiness probe was already UP, so the first alert after
 * every catalog start got "tier unknown" (SEV2 instead of SEV1). Phase 21 fixed half of this (the
 * DB pool, readiness includes db); the rest was cold code: the security filter chain, the JWT
 * decoder's RSA verify, the SQL row mapping and the JSON writer.
 *
 * <p>Spring Boot calls ApplicationRunners BEFORE it publishes ReadinessState.ACCEPTING_TRAFFIC, so
 * the readiness probe stays down until this has run. It needs no secret: the HTTP call carries a
 * well-formed token with a WRONG signature (the filter chain and the RSA verify run, the answer is
 * 401), and the query asks for a key that cannot exist. Nothing here fails the start: a warm-up
 * error is logged and the service starts cold, as before. [Prod]: the same, plus a load test that
 * measures the first-request latency after a rollout.
 */
@Component
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET) // not in import mode
public class CatalogWarmup implements ApplicationRunner {

    private static final Logger LOG = LoggerFactory.getLogger(CatalogWarmup.class);
    static final String NO_SUCH_KEY = "warmup-no-such-service"; // matches SERVICE_KEY, never seeded
    static final Duration HTTP_TIMEOUT = Duration.ofSeconds(10);

    private final CatalogRepository repository;
    private final ObjectMapper json;
    private final Environment env;
    private volatile Result last;

    /** What the warm-up did: the self-call's status (401 expected), rows found (0), time taken. */
    public record Result(int httpStatus, int items, long millis, String error) {}

    public CatalogWarmup(CatalogRepository repository, ObjectMapper json, Environment env) {
        this.repository = repository;
        this.json = json;
        this.env = env;
    }

    public Result last() {
        return last;
    }

    @Override
    public void run(ApplicationArguments args) {
        long t0 = System.nanoTime();
        int status = -1;
        int items = -1;
        String error = null;
        try {
            // 1. the authorised path's tail: SQL + row mapping + the JSON writer
            var found = repository.find(NO_SUCH_KEY, null, null);
            json.writeValueAsBytes(found);
            items = found.items().size();
            // 2. the front: Tomcat -> security filter chain -> JWT decoder (RSA verify) -> 401
            String port = env.getProperty("local.server.port", env.getProperty("server.port", "8010"));
            HttpRequest req = HttpRequest.newBuilder(
                            URI.create("http://127.0.0.1:" + port + "/v1/services?service_key=" + NO_SUCH_KEY))
                    .timeout(HTTP_TIMEOUT)
                    .header("Authorization", "Bearer " + wrongSignatureToken())
                    .GET()
                    .build();
            HttpClient http = HttpClient.newBuilder().connectTimeout(HTTP_TIMEOUT).build();
            status = http.send(req, HttpResponse.BodyHandlers.discarding()).statusCode();
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            error = e.getClass().getSimpleName();
        } catch (Exception e) { // never fail the start; the TYPE only (no content in logs)
            error = e.getClass().getSimpleName();
        }
        long ms = (System.nanoTime() - t0) / 1_000_000;
        last = new Result(status, items, ms, error);
        if (error == null && status == 401) {
            LOG.info("catalog_warmup done in {} ms (self-call 401 as expected, {} rows)", ms, items);
        } else {
            LOG.warn("catalog_warmup incomplete in {} ms: status={} error={}", ms, status, error);
        }
    }

    /** RS256 header + plausible service claims + a signature of the right size that is wrong. */
    static String wrongSignatureToken() {
        Base64.Encoder b64 = Base64.getUrlEncoder().withoutPadding();
        long now = System.currentTimeMillis() / 1000;
        String header = "{\"alg\":\"RS256\",\"typ\":\"JWT\"}";
        String claims = "{\"iss\":\"warmup\",\"sub\":\"service:warmup\",\"aud\":\"warmup\","
                + "\"scope\":\"catalog:read\",\"iat\":" + now + ",\"exp\":" + (now + 60) + "}";
        return b64.encodeToString(header.getBytes(StandardCharsets.UTF_8)) + "."
                + b64.encodeToString(claims.getBytes(StandardCharsets.UTF_8)) + "."
                + b64.encodeToString(new byte[256]); // 2048-bit RSA size, all zeros: never valid
    }
}
