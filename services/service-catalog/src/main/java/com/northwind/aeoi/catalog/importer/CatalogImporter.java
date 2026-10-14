package com.northwind.aeoi.catalog.importer;

import com.fasterxml.jackson.annotation.JsonProperty;
import com.fasterxml.jackson.databind.ObjectMapper;
import java.io.IOException;
import java.nio.file.Path;
import java.util.LinkedHashSet;
import java.util.List;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.ApplicationArguments;
import org.springframework.boot.ApplicationRunner;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.stereotype.Component;
import org.springframework.transaction.support.TransactionTemplate;

/**
 * DEV ONLY: load data/generated/catalog.json (written by `make db-seed`) - the whole catalog in
 * ONE transaction: a bad file (unknown dependency, bad key) changes nothing. Runs as the schema
 * owner from `make catalog-import`; the serving login cannot write (SELECT-only grants).
 * [Prod] the catalog has its own write path and owners; AEOI only reads it.
 */
@Component
@ConditionalOnProperty("catalog.import-file")
public class CatalogImporter implements ApplicationRunner {

    private static final Logger log = LoggerFactory.getLogger(CatalogImporter.class);

    record Entry(String key, String name, String team, String type, Integer tier,
                 String language, String repository,
                 @JsonProperty("depends_on") List<String> dependsOn) {}

    record CatalogFile(Integer seed, List<Entry> services) {}

    private final JdbcClient jdbc;
    private final ObjectMapper json;
    private final TransactionTemplate tx;
    private final Path file;

    public CatalogImporter(JdbcClient jdbc, ObjectMapper json, TransactionTemplate tx,
                           @Value("${catalog.import-file}") Path file) {
        this.jdbc = jdbc;
        this.json = json;
        this.tx = tx;
        this.file = file;
    }

    @Override
    public void run(ApplicationArguments args) throws IOException {
        CatalogFile data = json.readValue(file.toFile(), CatalogFile.class);
        List<Entry> services = data.services() == null ? List.of() : data.services();
        int[] edges = {0};
        tx.executeWithoutResult(status -> {
            jdbc.sql("DELETE FROM catalog.dependencies").update();
            jdbc.sql("DELETE FROM catalog.services").update();
            for (Entry e : services) {
                jdbc.sql("INSERT INTO catalog.services (key, name, type, tier, team, language,"
                                + " repository) VALUES (:key, :name, :type, :tier, :team, :language,"
                                + " :repository)")
                        .param("key", e.key()).param("name", e.name()).param("type", e.type())
                        .param("tier", e.tier() == null ? 3 : e.tier()).param("team", e.team())
                        .param("language", e.language()).param("repository", e.repository())
                        .update();
            }
            for (Entry e : services) { // after ALL services: the FK needs the targets first
                int position = 0;
                for (String dep : new LinkedHashSet<>(e.dependsOn() == null ? List.of() : e.dependsOn())) {
                    jdbc.sql("INSERT INTO catalog.dependencies (service_key, depends_on, position)"
                                    + " VALUES (:s, :d, :p)")
                            .param("s", e.key()).param("d", dep).param("p", position++)
                            .update();
                    edges[0]++;
                }
            }
        });
        log.info("catalog imported: {} services, {} dependencies, seed {} from {}",
                services.size(), edges[0], data.seed(), file);
    }
}
