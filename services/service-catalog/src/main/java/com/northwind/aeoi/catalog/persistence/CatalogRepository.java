package com.northwind.aeoi.catalog.persistence;

import com.northwind.aeoi.catalog.api.ServiceItem;
import com.northwind.aeoi.catalog.api.ServiceList;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.stereotype.Repository;

/**
 * Reads catalog.services + catalog.dependencies. Plain SQL (JdbcClient), no ORM: three queries.
 *
 * <p>Ordering is COLLATE "C" (byte order) on purpose: the JSON source sorts in Python's code-point
 * order, and the database's default collation (en_US) orders '-' differently. Both sources must
 * give the SAME answer (the parity test on the Mac).
 */
@Repository
public class CatalogRepository {

    public static final int MAX_ITEMS = 200;
    public static final int MAX_DEPENDS_ON = 100;
    public static final int MAX_DEPENDENTS = 200;

    private record Row(String key, String name, String type, int tier, String team,
                       String language, String repository) {}

    private final JdbcClient jdbc;

    public CatalogRepository(JdbcClient jdbc) {
        this.jdbc = jdbc;
    }

    public ServiceList find(String serviceKey, String team, String nameContains) {
        List<String> where = new ArrayList<>();
        Map<String, Object> params = new HashMap<>();
        if (serviceKey != null) { // exact, and wins over the other filters (as the tool does)
            where.add("s.key = :key");
            params.put("key", serviceKey);
        } else {
            if (team != null) {
                where.add("s.team = :team");
                params.put("team", team);
            }
            if (nameContains != null) { // substring, not LIKE: '%' and '_' are just characters
                where.add("(strpos(lower(s.name), :needle) > 0 OR strpos(s.key, :needle) > 0)");
                params.put("needle", nameContains.toLowerCase(Locale.ROOT));
            }
        }
        // only fixed fragments are concatenated; every value is a bound parameter
        String sql = "SELECT s.key, s.name, s.type, s.tier, s.team, s.language, s.repository"
                + " FROM catalog.services s WHERE " + String.join(" AND ", where)
                + " ORDER BY s.tier, s.key COLLATE \"C\" LIMIT " + (MAX_ITEMS + 1);
        List<Row> rows = jdbc.sql(sql).params(params)
                .query((rs, n) -> new Row(rs.getString("key"), rs.getString("name"),
                        rs.getString("type"), rs.getInt("tier"), rs.getString("team"),
                        rs.getString("language"), rs.getString("repository")))
                .list();
        boolean truncated = rows.size() > MAX_ITEMS;
        List<Row> page = truncated ? rows.subList(0, MAX_ITEMS) : rows;
        if (page.isEmpty()) {
            return new ServiceList(List.of(), false);
        }
        List<String> keys = page.stream().map(Row::key).toList();

        Map<String, List<String>> dependsOn = new HashMap<>();
        jdbc.sql("SELECT service_key, depends_on FROM catalog.dependencies"
                        + " WHERE service_key IN (:keys) ORDER BY service_key, position")
                .param("keys", keys)
                .query(rs -> {
                    dependsOn.computeIfAbsent(rs.getString(1), k -> new ArrayList<>())
                            .add(rs.getString(2));
                });
        Map<String, List<String>> dependents = new HashMap<>();
        jdbc.sql("SELECT depends_on, service_key FROM catalog.dependencies"
                        + " WHERE depends_on IN (:keys) ORDER BY depends_on, service_key COLLATE \"C\"")
                .param("keys", keys)
                .query(rs -> {
                    dependents.computeIfAbsent(rs.getString(1), k -> new ArrayList<>())
                            .add(rs.getString(2));
                });

        List<ServiceItem> items = page.stream()
                .map(r -> new ServiceItem(r.key(), r.name(), r.type(), r.tier(), r.team(),
                        r.language(), r.repository(),
                        head(dependsOn.getOrDefault(r.key(), List.of()), MAX_DEPENDS_ON),
                        head(dependents.getOrDefault(r.key(), List.of()), MAX_DEPENDENTS)))
                .toList();
        return new ServiceList(items, truncated);
    }

    private static List<String> head(List<String> list, int max) {
        return list.size() > max ? List.copyOf(list.subList(0, max)) : List.copyOf(list);
    }
}
