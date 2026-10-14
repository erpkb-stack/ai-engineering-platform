package com.northwind.aeoi.catalog.api;

import com.northwind.aeoi.catalog.persistence.CatalogRepository;
import java.util.regex.Pattern;
import org.springframework.http.HttpStatus;
import org.springframework.http.MediaType;
import org.springframework.http.ProblemDetail;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

/**
 * GET /v1/services - the only read (openapi.yaml). At least one filter: no full dumps.
 * The rules are the tool's (tool-gateway CatalogIn) so both sources answer the same.
 */
@RestController
@RequestMapping("/v1")
public class ServicesController {

    static final Pattern SERVICE_KEY = Pattern.compile("^[a-z0-9][a-z0-9-]{1,99}$");

    private final CatalogRepository repository;

    public ServicesController(CatalogRepository repository) {
        this.repository = repository;
    }

    @GetMapping("/services")
    public ResponseEntity<?> listServices(
            @RequestParam(name = "service_key", required = false) String serviceKey,
            @RequestParam(name = "team", required = false) String team,
            @RequestParam(name = "name_contains", required = false) String nameContains) {
        if (serviceKey == null && team == null && nameContains == null) {
            return problem("give service_key, team or name_contains (no full dumps)");
        }
        if (serviceKey != null && !SERVICE_KEY.matcher(serviceKey).matches()) {
            return problem("service_key must match " + SERVICE_KEY.pattern());
        }
        if (team != null && (team.isEmpty() || team.length() > 80)) {
            return problem("team must be 1-80 characters");
        }
        if (nameContains != null && (nameContains.length() < 2 || nameContains.length() > 60)) {
            return problem("name_contains must be 2-60 characters");
        }
        return ResponseEntity.ok(repository.find(serviceKey, team, nameContains));
    }

    private static ResponseEntity<ProblemDetail> problem(String detail) {
        ProblemDetail body = ProblemDetail.forStatusAndDetail(HttpStatus.BAD_REQUEST, detail);
        body.setTitle("Invalid catalog query");
        return ResponseEntity.badRequest().contentType(MediaType.APPLICATION_PROBLEM_JSON).body(body);
    }
}
