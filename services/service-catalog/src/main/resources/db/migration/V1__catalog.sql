-- Phase 20 (ADR-005, ADR-032): the catalog schema, owned by this service (Flyway; Alembic
-- excludes it - ADR-013). Read-only at runtime: the serving login only gets SELECT.
-- The primary key is the service KEY, not a UUIDv7: the key is the identifier every other
-- service already uses (devdata, incidents, agents reference services by key).

CREATE TABLE catalog.services (
    key        text PRIMARY KEY CHECK (key ~ '^[a-z0-9][a-z0-9-]{1,99}$'),
    name       text NOT NULL,
    type       text NOT NULL,
    tier       smallint NOT NULL CHECK (tier BETWEEN 1 AND 9),
    team       text NOT NULL,
    language   text,
    repository text,
    updated_at timestamptz NOT NULL DEFAULT now()
);
-- "services of team X" is one of the three filters
CREATE INDEX services_team_idx ON catalog.services (team);
COMMENT ON INDEX catalog.services_team_idx IS 'serves: GET /v1/services?team= (the tool''s team filter)';

CREATE TABLE catalog.dependencies (
    service_key text NOT NULL REFERENCES catalog.services (key) ON DELETE CASCADE,
    depends_on  text NOT NULL REFERENCES catalog.services (key),
    position    smallint NOT NULL,  -- keeps depends_on in the order the source gave it
    PRIMARY KEY (service_key, depends_on)
);
-- "who depends on X" (dependents / blast radius) reads by the target, not the PK's first column
CREATE INDEX dependencies_depends_on_idx ON catalog.dependencies (depends_on);
COMMENT ON INDEX catalog.dependencies_depends_on_idx IS 'serves: dependents of a service (blast radius) - lookup by the target';

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'svc_catalog') THEN
        CREATE ROLE svc_catalog NOLOGIN;
    END IF;
END $$;
GRANT USAGE ON SCHEMA catalog TO svc_catalog;
-- by name: Flyway's own history table (created before this file runs) is not for the service
GRANT SELECT ON catalog.services, catalog.dependencies TO svc_catalog;
ALTER DEFAULT PRIVILEGES IN SCHEMA catalog GRANT SELECT ON TABLES TO svc_catalog;
