package com.northwind.aeoi.catalog;

import static org.assertj.core.api.Assertions.assertThat;

import com.fasterxml.jackson.databind.JsonNode;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

/** The query rules are the tool's (tool-gateway CatalogIn + the JSON adapter). */
class CatalogApiTest extends CatalogTestBase {

    final String token = serviceToken("catalog:read");

    @BeforeEach
    void data() throws Exception {
        load(List.of(
                svc("b-api", "t1", 2, "a-db"),
                svc("a-db", "t1", 1),
                svc("a-cache", "t1", 1),
                svc("ab-web", "t2", 2, "b-api", "a-db"),
                svc("c-worker", "t2", 3, "a-db", "b-api")));
    }

    JsonNode body(String query) throws Exception {
        var r = get("/v1/services?" + query, token);
        assertThat(r.statusCode()).as(r.body()).isEqualTo(200);
        return json.readTree(r.body());
    }

    static List<String> keys(JsonNode body) {
        List<String> out = new ArrayList<>();
        body.get("items").forEach(i -> out.add(i.get("key").asText()));
        return out;
    }

    @Test
    void serviceKeyIsExactAndCarriesBothDirections() throws Exception {
        JsonNode b = body("service_key=a-db");
        assertThat(keys(b)).containsExactly("a-db");
        JsonNode item = b.get("items").get(0);
        assertThat(item.get("depends_on")).isEmpty();
        // direct dependents, sorted in byte order ("ab-web" < "b-api" < "c-worker")
        assertThat(json.convertValue(item.get("dependents"), List.class))
                .containsExactly("ab-web", "b-api", "c-worker");
        assertThat(b.get("truncated").asBoolean()).isFalse();
    }

    @Test
    void dependsOnKeepsTheSourceOrder() throws Exception {
        JsonNode item = body("service_key=c-worker").get("items").get(0);
        assertThat(json.convertValue(item.get("depends_on"), List.class)).containsExactly("a-db", "b-api");
    }

    @Test
    void serviceKeyWinsOverTheOtherFilters() throws Exception {
        assertThat(keys(body("service_key=a-db&team=t2"))).containsExactly("a-db");
    }

    @Test
    void teamAndNameCombineWithAndSortedByTierThenKeyInByteOrder() throws Exception {
        assertThat(keys(body("team=t1"))).containsExactly("a-cache", "a-db", "b-api");
        assertThat(keys(body("team=t2&name_contains=WEB"))).containsExactly("ab-web");
        // the needle also matches the key, case-insensitively on the name
        assertThat(keys(body("name_contains=a-d"))).containsExactly("a-db");
    }

    @Test
    void keysSortInByteOrderLikeTheJsonSourceNotInLocaleOrder() throws Exception {
        // en_US ignores '-' at the first level ("adb" > "abweb"); Python and COLLATE "C" do not
        load(List.of(svc("ab-web", "t3", 1), svc("a-db", "t3", 1)));
        assertThat(keys(body("team=t3"))).containsExactly("a-db", "ab-web");
    }

    @Test
    void likeWildcardsAreJustCharacters() throws Exception {
        assertThat(keys(body("name_contains=%25%25"))).isEmpty(); // "%%" matches nothing
        assertThat(keys(body("name_contains=__"))).isEmpty();
    }

    @Test
    void noMatchIsAnEmptyListNotAnError() throws Exception {
        JsonNode b = body("service_key=nope-svc");
        assertThat(keys(b)).isEmpty();
        assertThat(b.get("truncated").asBoolean()).isFalse();
    }

    @Test
    void invalidQueriesAre400Problems() throws Exception {
        for (String q : List.of("", "team=", "service_key=UPPER", "service_key=a",
                "name_contains=x", "team=" + "t".repeat(81))) {
            var r = get("/v1/services?" + q, token);
            assertThat(r.statusCode()).as(q).isEqualTo(400);
            assertThat(r.headers().firstValue("content-type").orElse("")).as(q)
                    .startsWith("application/problem+json");
        }
    }

    @Test
    void atMost200ItemsAndTruncatedSaysSo() throws Exception {
        List<Map<String, Object>> many = new ArrayList<>();
        for (int i = 0; i < 201; i++) {
            many.add(svc("s-%03d".formatted(i), "big", 2));
        }
        load(many);
        JsonNode b = body("team=big");
        assertThat(b.get("items")).hasSize(200);
        assertThat(b.get("truncated").asBoolean()).isTrue();
        assertThat(keys(b).get(0)).isEqualTo("s-000");
    }
}
