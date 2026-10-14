package com.northwind.aeoi.catalog;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.nimbusds.jose.JOSEException;
import com.nimbusds.jose.JWSAlgorithm;
import com.nimbusds.jose.JWSHeader;
import com.nimbusds.jose.crypto.RSASSASigner;
import com.nimbusds.jwt.JWTClaimsSet;
import com.nimbusds.jwt.SignedJWT;
import com.northwind.aeoi.catalog.importer.CatalogImporter;
import java.io.IOException;
import java.io.UncheckedIOException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.KeyPair;
import java.security.KeyPairGenerator;
import java.security.NoSuchAlgorithmException;
import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.SQLException;
import java.time.Instant;
import java.util.Base64;
import java.util.Date;
import java.util.List;
import java.util.Map;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.web.server.LocalServerPort;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.test.context.DynamicPropertyRegistry;
import org.springframework.test.context.DynamicPropertySource;
import org.springframework.transaction.support.TransactionTemplate;

/**
 * The compose Postgres (`make up`), like the Python integration tests: one throwaway database
 * `aeoi_catalog_test_<random>` per test run, dropped at the end - no Testcontainers, so no
 * dependency on Docker's API version (ADR-032). One RSA key pair; tokens are signed exactly like
 * libs/security issue_service_token.
 */
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT)
public abstract class CatalogTestBase {

    static final String PG_HOST = env("AEOI_PG_HOST", "localhost");
    static final String PG_PORT = env("AEOI_PG_PORT", "5433");
    static final String PG_USER = env("AEOI_PG_USER", "aeoi");
    static final String PG_PASSWORD = ownerPassword();
    static final String DB = "aeoi_catalog_test_" + Long.toHexString(System.nanoTime());
    static final KeyPair KEYS = rsa();
    static final Path PUBLIC_KEY = pem(KEYS);
    static final HttpClient HTTP = HttpClient.newHttpClient();

    static {
        admin("CREATE DATABASE " + DB);
        Runtime.getRuntime().addShutdownHook(new Thread(() -> admin("DROP DATABASE IF EXISTS " + DB + " WITH (FORCE)")));
    }

    @DynamicPropertySource
    static void props(DynamicPropertyRegistry r) {
        r.add("spring.datasource.url", CatalogTestBase::jdbcUrl);
        r.add("spring.datasource.username", () -> PG_USER);
        r.add("spring.datasource.password", () -> PG_PASSWORD);
        r.add("spring.flyway.enabled", () -> "true");
        r.add("spring.flyway.user", () -> PG_USER);
        r.add("spring.flyway.password", () -> PG_PASSWORD);
        r.add("catalog.jwt.public-key-file", () -> PUBLIC_KEY.toUri().toString());
    }

    static String jdbcUrl() {
        return "jdbc:postgresql://" + PG_HOST + ":" + PG_PORT + "/" + DB;
    }

    private static void admin(String sql) {
        String url = "jdbc:postgresql://" + PG_HOST + ":" + PG_PORT + "/postgres";
        try (Connection c = DriverManager.getConnection(url, PG_USER, PG_PASSWORD)) {
            c.createStatement().execute(sql);
        } catch (SQLException e) {
            throw new IllegalStateException(sql + " failed (is the compose Postgres up? make up): " + e.getMessage(), e);
        }
    }

    private static String env(String name, String fallback) {
        String v = System.getenv(name);
        return v == null || v.isBlank() ? fallback : v;
    }

    private static String ownerPassword() {
        String file = env("AEOI_PG_PASSWORD_FILE", env("AEOI_REPO_ROOT", "../..") + "/secrets/postgres_password.txt");
        try {
            return Files.readString(Path.of(file)).strip();
        } catch (IOException e) {
            throw new UncheckedIOException("no Postgres owner password at " + file + " (make setup)", e);
        }
    }

    @LocalServerPort
    int port;

    @Autowired
    JdbcClient jdbc;

    @Autowired
    ObjectMapper json;

    @Autowired
    TransactionTemplate tx;

    /** Replace the catalog with these services (through the real importer). */
    void load(List<Map<String, Object>> services) throws IOException {
        Path file = Files.createTempFile("catalog", ".json");
        file.toFile().deleteOnExit();
        Files.writeString(file, json.writeValueAsString(Map.of("seed", 1, "services", services)));
        new CatalogImporter(jdbc, json, tx, file).run(null);
    }

    static Map<String, Object> svc(String key, String team, int tier, String... dependsOn) {
        return Map.of("key", key, "name", key.replace('-', ' '), "team", team, "type", "api",
                "tier", tier, "language", "java", "repository", "northwind/" + team,
                "depends_on", List.of(dependsOn));
    }

    HttpResponse<String> get(String pathAndQuery, String token) throws Exception {
        HttpRequest.Builder b = HttpRequest.newBuilder(
                URI.create("http://localhost:" + port + pathAndQuery)).GET();
        if (token != null) {
            b.header("Authorization", "Bearer " + token);
        }
        return HTTP.send(b.build(), HttpResponse.BodyHandlers.ofString());
    }

    static String serviceToken(String scope) {
        return sign(claims("service:tool-gateway").claim("scope", scope).build());
    }

    static JWTClaimsSet.Builder claims(String subject) {
        Instant now = Instant.now();
        return new JWTClaimsSet.Builder()
                .issuer("aeoi-dev-issuer")
                .audience("aeoi-api")
                .subject(subject)
                .issueTime(Date.from(now))
                .expirationTime(Date.from(now.plusSeconds(300)));
    }

    static String sign(JWTClaimsSet claims) {
        return sign(claims, KEYS);
    }

    static String sign(JWTClaimsSet claims, KeyPair keys) {
        try {
            SignedJWT jwt = new SignedJWT(new JWSHeader(JWSAlgorithm.RS256), claims);
            jwt.sign(new RSASSASigner(keys.getPrivate()));
            return jwt.serialize();
        } catch (JOSEException e) {
            throw new IllegalStateException(e);
        }
    }

    static KeyPair rsa() {
        try {
            KeyPairGenerator g = KeyPairGenerator.getInstance("RSA");
            g.initialize(2048);
            return g.generateKeyPair();
        } catch (NoSuchAlgorithmException e) {
            throw new IllegalStateException(e);
        }
    }

    private static Path pem(KeyPair keys) {
        String body = Base64.getMimeEncoder(64, "\n".getBytes(StandardCharsets.US_ASCII))
                .encodeToString(keys.getPublic().getEncoded()); // X.509 SubjectPublicKeyInfo
        try {
            Path file = Files.createTempFile("jwt_public", ".pem");
            file.toFile().deleteOnExit();
            Files.writeString(file, "-----BEGIN PUBLIC KEY-----\n" + body + "\n-----END PUBLIC KEY-----\n");
            return file;
        } catch (IOException e) {
            throw new UncheckedIOException(e);
        }
    }
}
