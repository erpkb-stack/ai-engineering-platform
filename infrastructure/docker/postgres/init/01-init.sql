-- Runs ONCE, on first start with an empty data volume (docker-entrypoint-initdb.d).
-- Schema/table creation is NOT done here: Alembic owns that from Phase 3.
-- Re-run after changes: make clean CONFIRM=1 && make up

-- pgvector: embeddings + HNSW (ADR-002). pg_trgm: fuzzy matching on names/error codes.
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- Read-only role used ONLY by the local Postgres MCP server in .mcp.json, so Claude Code
-- can inspect data but never write. Local dev only; its password is not a secret.
-- Phase 3 migrations must GRANT USAGE ON SCHEMA <schema> TO aeoi_readonly per schema.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'aeoi_readonly') THEN
    CREATE ROLE aeoi_readonly LOGIN PASSWORD 'aeoi_readonly'
      NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
  END IF;
END
$$;

GRANT CONNECT ON DATABASE aeoi TO aeoi_readonly;
ALTER ROLE aeoi_readonly SET default_transaction_read_only = on;
ALTER ROLE aeoi_readonly SET statement_timeout = '15s';
-- Real control = privileges (SELECT only; PG16 has no CREATE on public). read_only above is
-- a second layer (the role could SET it off, but still has no write privilege - tested).
-- Tables that the app owner (aeoi) creates later are readable, never writable.
ALTER DEFAULT PRIVILEGES FOR ROLE aeoi GRANT SELECT ON TABLES TO aeoi_readonly;
