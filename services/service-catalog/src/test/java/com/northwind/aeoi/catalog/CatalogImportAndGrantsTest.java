package com.northwind.aeoi.catalog;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.SQLException;
import java.util.List;
import org.junit.jupiter.api.Test;

class CatalogImportAndGrantsTest extends CatalogTestBase {

    long count(String table) {
        return jdbc.sql("SELECT count(*) FROM catalog." + table).query(Long.class).single();
    }

    @Test
    void aReimportReplacesTheCatalog() throws Exception {
        load(List.of(svc("a-db", "t1", 1), svc("b-api", "t1", 2, "a-db", "a-db"))); // dup edge
        assertThat(count("services")).isEqualTo(2);
        assertThat(count("dependencies")).isEqualTo(1);
        load(List.of(svc("c-db", "t9", 1)));
        assertThat(count("services")).isEqualTo(1);
        assertThat(count("dependencies")).isZero();
    }

    @Test
    void aBadFileChangesNothing() throws Exception {
        load(List.of(svc("a-db", "t1", 1)));
        // a dependency on a service that does not exist: the FK fails, the transaction rolls back
        assertThatThrownBy(() -> load(List.of(svc("x-db", "t1", 1), svc("y-api", "t1", 2, "missing-svc"))))
                .isNotNull();
        assertThat(jdbc.sql("SELECT key FROM catalog.services").query(String.class).list())
                .containsExactly("a-db");
    }

    @Test
    void theServingRoleCanReadButNeverWrite() throws Exception {
        // a throwaway login in the serving role, on the SHARED cluster: random name and password,
        // always dropped (a known-password login must never stay behind on the dev Postgres)
        String role = "catalog_t_" + Long.toHexString(System.nanoTime());
        String password = java.util.UUID.randomUUID().toString();
        jdbc.sql("CREATE ROLE " + role + " LOGIN PASSWORD '" + password + "' IN ROLE svc_catalog").update();
        try {
            load(List.of(svc("a-db", "t1", 1)));
            try (Connection c = DriverManager.getConnection(jdbcUrl(), role, password)) {
                try (var rs = c.createStatement().executeQuery("SELECT count(*) FROM catalog.services")) {
                    assertThat(rs.next()).isTrue();
                    assertThat(rs.getLong(1)).isEqualTo(1);
                }
                for (String write : List.of(
                        "INSERT INTO catalog.services (key, name, type, tier, team) VALUES ('z-db','z','db',1,'t')",
                        "UPDATE catalog.services SET tier = 2",
                        "DELETE FROM catalog.dependencies",
                        "SELECT count(*) FROM catalog.flyway_schema_history")) { // not granted either
                    assertThatThrownBy(() -> c.createStatement().execute(write)).as(write)
                            .isInstanceOf(SQLException.class)
                            .satisfies(e -> assertThat(((SQLException) e).getSQLState()).isEqualTo("42501"));
                }
            }
        } finally {
            jdbc.sql("DROP ROLE IF EXISTS " + role).update();
        }
    }
}
