package com.northwind.aeoi.catalog;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.context.ConfigurableApplicationContext;

/**
 * The service catalog (Phase 20, ADR-005, ADR-032): a read-only system of record for services
 * and their dependencies. Started with --catalog.import-file=... it migrates, loads the file and
 * exits (dev only); otherwise it serves GET /v1/services.
 */
@SpringBootApplication
public class CatalogApplication {

    public static void main(String[] args) {
        ConfigurableApplicationContext ctx = SpringApplication.run(CatalogApplication.class, args);
        if (ctx.getEnvironment().containsProperty("catalog.import-file")) {
            System.exit(SpringApplication.exit(ctx)); // a one-shot command, not a server
        }
    }
}
