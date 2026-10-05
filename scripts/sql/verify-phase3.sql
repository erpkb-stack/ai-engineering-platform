-- make db-verify : human-readable proof that Phase 3 works. Read-only.
\pset footer off
\echo '== 1. migration version'
SELECT version_num AS alembic_head FROM public.alembic_version;

\echo '== 2. row counts (seed v1)'
SELECT 'identity.users' AS t, count(*) FROM identity.users
UNION ALL SELECT 'devdata.deployments', count(*) FROM devdata.deployments
UNION ALL SELECT 'devdata.commits', count(*) FROM devdata.commits
UNION ALL SELECT 'devdata.log_events', count(*) FROM devdata.log_events
UNION ALL SELECT 'devdata.metric_points', count(*) FROM devdata.metric_points
UNION ALL SELECT 'rag.documents', count(*) FROM rag.documents
UNION ALL SELECT 'rag.historical_incidents', count(*) FROM rag.historical_incidents
UNION ALL SELECT 'rag.runbooks', count(*) FROM rag.runbooks;

\echo '== 3. demo: what changed before the incident? (deploy -> commit)'
SELECT d.deploy_key, d.version, to_char(d.started_at AT TIME ZONE 'UTC', 'HH24:MI') AS utc,
       d.config_diff, left(c.message, 60) AS commit
FROM devdata.deployments d JOIN devdata.commits c ON c.sha = d.commit_sha
WHERE d.service_key = 'checkout-api' ORDER BY d.started_at DESC LIMIT 2;

\echo '== 4. demo: signal order 09:47-09:54 UTC (latency moves before errors, DB last)'
SELECT to_char(ts AT TIME ZONE 'UTC', 'HH24:MI') AS utc,
       round(max(value) FILTER (WHERE metric = 'p95_latency_ms')::numeric, 0)      AS app_p95_ms,
       round(max(value) FILTER (WHERE metric = 'http_5xx_per_min')::numeric, 0)    AS http_5xx,
       round(max(value) FILTER (WHERE metric = 'db_pool_utilization')::numeric, 2) AS pool
FROM devdata.metric_points
WHERE service_key = 'checkout-api' AND ts BETWEEN '2026-10-02 09:47Z' AND '2026-10-02 09:54Z'
GROUP BY ts ORDER BY ts;

\echo '== 5. demo: first ERR_POOL_TIMEOUT log'
SELECT to_char(min(ts) AT TIME ZONE 'UTC', 'HH24:MI:SS') AS first_utc, count(*) AS n
FROM devdata.log_events WHERE error_code = 'ERR_POOL_TIMEOUT';

\echo '== 6. similar past incidents (keyword search, no embeddings yet)'
SELECT incident_key, root_cause_category, left(title, 50) AS title
FROM rag.historical_incidents
WHERE tsv @@ plainto_tsquery('english', 'ERR_POOL_TIMEOUT') AND 'checkout-api' = ANY(service_keys)
ORDER BY occurred_at DESC LIMIT 5;

\echo '== 7. permission filter: restricted doc visible to engineers? (expect 0)'
SELECT count(*) AS restricted_rows_for_eng_all FROM rag.documents
WHERE title = 'Internal Security Architecture' AND allowed_groups && ARRAY['eng-all']::varchar[];

\echo '== 8. every explicit index documented (expect 0 rows)'
SELECT n.nspname || '.' || ci.relname AS undocumented_index
FROM pg_index i JOIN pg_class ci ON ci.oid = i.indexrelid JOIN pg_class ct ON ct.oid = i.indrelid
JOIN pg_namespace n ON n.oid = ci.relnamespace
WHERE n.nspname IN ('identity','incident','orchestrator','rag','devdata','tools','llm','audit','eval')
  AND NOT ct.relispartition
  AND NOT EXISTS (SELECT 1 FROM pg_constraint c WHERE c.conindid = i.indexrelid)
  AND obj_description(ci.oid, 'pg_class') IS NULL;
