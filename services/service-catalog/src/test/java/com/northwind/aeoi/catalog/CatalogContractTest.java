package com.northwind.aeoi.catalog;

import static org.assertj.core.api.Assertions.assertThat;

import com.fasterxml.jackson.databind.JsonNode;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import org.junit.jupiter.api.Test;
import org.yaml.snakeyaml.Yaml;

/**
 * Java side of the contract (ADR-032): live responses have EXACTLY the fields openapi.yaml
 * declares, and the served contract is the committed file. (Python side:
 * tests/unit/test_catalog_contract.py; both sides: the Mac parity test.)
 */
class CatalogContractTest extends CatalogTestBase {

    static final Path SPEC = Path.of("openapi.yaml"); // Maven runs tests in the module directory

    @SuppressWarnings("unchecked")
    static Map<String, Object> schema(String name) throws Exception {
        Map<String, Object> spec = new Yaml().load(Files.readString(SPEC));
        Map<String, Object> components = (Map<String, Object>) spec.get("components");
        return (Map<String, Object>) ((Map<String, Object>) components.get("schemas")).get(name);
    }

    @SuppressWarnings("unchecked")
    static Set<String> properties(String name) throws Exception {
        return ((Map<String, Object>) schema(name).get("properties")).keySet();
    }

    @SuppressWarnings("unchecked")
    static List<String> required(String name) throws Exception {
        return (List<String>) schema(name).get("required");
    }

    static Set<String> fields(JsonNode node) {
        Set<String> out = new HashSet<>();
        node.fieldNames().forEachRemaining(out::add);
        return out;
    }

    @Test
    void responsesHaveExactlyTheDeclaredFields() throws Exception {
        load(List.of(svc("a-db", "t1", 1), svc("b-api", "t1", 2, "a-db")));
        var r = get("/v1/services?team=t1", serviceToken("catalog:read"));
        assertThat(r.statusCode()).isEqualTo(200);
        JsonNode body = json.readTree(r.body());
        assertThat(fields(body)).isEqualTo(properties("ServiceList"));
        for (JsonNode item : body.get("items")) {
            // every declared field is present (nullable ones as null), and nothing else
            assertThat(fields(item)).isEqualTo(properties("ServiceItem"));
            for (String f : required("ServiceItem")) {
                assertThat(item.get(f).isNull()).as(f).isFalse();
            }
        }
    }

    @Test
    void theServedContractIsTheCommittedFile() throws Exception {
        var r = get("/v1/openapi.yaml", null);
        assertThat(r.statusCode()).isEqualTo(200);
        assertThat(r.body()).isEqualTo(Files.readString(SPEC));
    }
}
