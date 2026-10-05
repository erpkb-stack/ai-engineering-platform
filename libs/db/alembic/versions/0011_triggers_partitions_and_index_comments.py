"""Triggers, audit partitions and index documentation.

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-05

- updated_at triggers (shared function from 0001).
- rag.sync_chunk_acl: chunks inherit allowed_groups from their document (insert AND when
  the document ACL changes) - app code can never get the permission filter out of sync.
- audit.audit_events: append-only trigger + monthly partitions via audit.ensure_partitions().
- COMMENT ON INDEX for every explicit index: the "why" lives in the database itself
  (tests/integration/db/test_schema_rules.py fails if an index has no comment).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPDATED_AT_TABLES = ("incident.incidents", "rag.documents")

INDEX_COMMENTS: dict[str, str] = {
    "audit.ix_audit_events_actor_occurred_at": 'serves: "everything actor X did" (security investigations), newest first',
    "audit.ix_audit_events_correlation_id": "serves: follow one request end-to-end across services",
    "audit.ix_audit_events_incident_id_occurred_at": "serves: audit trail tab of an incident",
    "devdata.ix_commits_repository_committed_at": "serves: get_commit history for a repository, newest first",
    "devdata.ix_commits_service_key_committed_at": 'serves: "what changed in service X between two deploys"',
    "devdata.ix_deployments_service_key_environment_started_at": 'serves: get_deployment(service, env, before T) - "recent deploys of X, newest first"',
    "devdata.ix_deployments_started_at": "serves: correlate deploys in a time window across all services (incident triage)",
    "devdata.ix_log_events_errors": "serves: error-only log scans (most investigation queries); partial index = much smaller",
    "devdata.ix_log_events_service_key_ts": "serves: search_logs(service, time range) - the main access path",
    "devdata.ix_log_events_ts_brin": "serves: cross-service time-window scans; BRIN is tiny on append-only time data",
    "devdata.ix_pull_requests_merge_commit_sha": 'serves: "PR that produced commit X" (release risk analysis)',
    "eval.ix_eval_runs_dataset_id_started_at": 'serves: "runs of dataset X, newest first" (A/B comparison picker)',
    "eval.ix_evaluations_case_id": 'serves: FK lookup + "how did case X score across runs"',
    "identity.ix_role_permissions_permission_name": 'serves: "which roles grant permission X" (PK covers role -> permissions)',
    "identity.ix_user_groups_group_name": 'serves: "members of group X" (ACL audits); PK covers user -> groups at login',
    "identity.ix_user_roles_role_name": 'serves: "who has role X" (e.g. list incident commanders); PK covers user -> roles',
    "identity.ix_users_email": "serves: login lookup by email (case-normalised)",
    "incident.ix_approvals_incident_id_requested_at": "serves: approval history per incident (UI + audit)",
    "incident.ix_evidence_incident_id_kind": "serves: evidence explorer filtered by kind within one incident",
    "incident.ix_feedback_incident_id": "serves: FK lookups / feedback per incident",
    "incident.ix_feedback_target_type_target_id": "serves: feedback for a given finding/report (eval + UI)",
    "incident.ix_hypotheses_incident_id_rank": "serves: ranked hypotheses for an incident (report + UI)",
    "incident.ix_hypotheses_superseded_by": "serves: FK lookup when following supersession chains",
    "incident.ix_hypothesis_evidence_evidence_id": 'serves: reverse edge "which hypotheses use this evidence" (graph view, impact of retracting evidence); the PK already serves hypothesis -> evidence',
    "incident.ix_incident_events_incident_id_occurred_at": "serves: timeline reconstruction = range scan per incident in time order",
    "incident.ix_incidents_affected_services": 'serves: "incidents affecting service X" (array containment @>)',
    "incident.ix_incidents_status_severity_created_at": 'serves: dashboard "open incidents by severity, newest first"',
    "incident.ix_outbox_unpublished": "serves: outbox relay polling of unpublished events; partial so it holds only the backlog",
    "incident.uq_approvals_pending_action": "enforces: at most one PENDING approval for the same action+args on an incident",
    "llm.ix_model_usage_created_at_brin": "serves: time-window rollups across all models; BRIN fits append-only data",
    "llm.ix_model_usage_investigation_id": "serves: cost per investigation (KPI)",
    "llm.ix_model_usage_model_created_at": "serves: cost/latency dashboards by model over time",
    "orchestrator.ix_agent_executions_agent_name_started_at": "serves: per-agent latency/cost dashboards over time",
    "orchestrator.ix_agent_executions_task_id": "serves: agent trace view for a task",
    "orchestrator.ix_investigations_incident_id_started_at": 'serves: "investigations for incident X, latest first" (incident page)',
    "orchestrator.ix_tasks_investigation_id": 'serves: fan-in "all tasks of investigation X"',
    "orchestrator.ix_tasks_live_deadline": "serves: reaper finding stuck PENDING/RUNNING tasks past deadline; partial = live tasks only",
    "orchestrator.uq_investigations_one_running": "enforces: one RUNNING investigation per incident (duplicate deliveries cannot start a second)",
    "rag.ix_document_chunks_allowed_groups": "serves: permission filter allowed_groups && <caller groups> in the same statement as ranking",
    "rag.ix_document_chunks_embedding_hnsw": "serves: semantic ANN retrieval (cosine) for RAG",
    "rag.ix_document_chunks_tsv": 'serves: keyword retrieval (error codes, class names) - the "BM25 side" of hybrid',
    "rag.ix_documents_allowed_groups": 'serves: admin/ACL views "documents visible to group X"',
    "rag.ix_documents_department_owner": "serves: browse by owning department / team",
    "rag.ix_historical_incidents_embedding_hnsw": "serves: similar-incident semantic search (cosine)",
    "rag.ix_historical_incidents_occurred_at": "serves: recency filter / timeline of past incidents",
    "rag.ix_historical_incidents_service_keys": 'serves: "past incidents of service X" (array containment)',
    "rag.ix_historical_incidents_tsv": "serves: keyword search on error codes / symptoms",
    "rag.ix_runbooks_document_id": "serves: FK lookup (runbook -> its indexed document)",
    "rag.ix_runbooks_service_keys": 'serves: "runbooks for service X"',
    "tools.ix_tool_calls_denied": "serves: security dashboard recent tool denials; partial, denials are rare",
    "tools.ix_tool_calls_incident_id_created_at": "serves: agent trace / evidence view per incident in time order",
    "tools.ix_tool_calls_tool_name_created_at": "serves: per-tool success/latency dashboards",
}


def upgrade() -> None:
    for table in UPDATED_AT_TABLES:
        name = table.split(".")[1]
        op.execute(
            f"CREATE TRIGGER trg_{name}_updated_at BEFORE UPDATE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION public.set_updated_at()"
        )

    # --- permission-aware RAG: chunk ACL always equals its document ACL ---------------
    op.execute(
        """
        CREATE FUNCTION rag.chunk_acl_from_document() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          SELECT d.allowed_groups INTO NEW.allowed_groups
          FROM rag.documents d WHERE d.id = NEW.document_id;
          IF NEW.allowed_groups IS NULL THEN
            RAISE EXCEPTION 'document % not found for chunk', NEW.document_id;
          END IF;
          RETURN NEW;
        END $$;
        """
    )
    op.execute(
        "CREATE TRIGGER trg_document_chunks_acl BEFORE INSERT OR UPDATE OF allowed_groups, "
        "document_id ON rag.document_chunks FOR EACH ROW EXECUTE FUNCTION rag.chunk_acl_from_document()"
    )
    op.execute(
        """
        CREATE FUNCTION rag.propagate_document_acl() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          UPDATE rag.document_chunks SET allowed_groups = NEW.allowed_groups
          WHERE document_id = NEW.id;
          RETURN NEW;
        END $$;
        """
    )
    op.execute(
        "CREATE TRIGGER trg_documents_acl_propagate AFTER UPDATE OF allowed_groups ON rag.documents "
        "FOR EACH ROW WHEN (OLD.allowed_groups IS DISTINCT FROM NEW.allowed_groups) "
        "EXECUTE FUNCTION rag.propagate_document_acl()"
    )

    # --- audit: append-only + monthly partitions -------------------------------------
    op.execute(
        """
        CREATE FUNCTION audit.reject_modification() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          RAISE EXCEPTION 'audit.audit_events is append-only (% rejected)', TG_OP
            USING ERRCODE = 'insufficient_privilege';
        END $$;
        """
    )
    op.execute(
        "CREATE TRIGGER trg_audit_events_append_only BEFORE UPDATE OR DELETE ON audit.audit_events "
        "FOR EACH ROW EXECUTE FUNCTION audit.reject_modification()"
    )
    op.execute(
        """
        CREATE FUNCTION audit.ensure_partitions(months_ahead int DEFAULT 2) RETURNS int
        LANGUAGE plpgsql AS $$
        DECLARE
          m date := date_trunc('month', now() - interval '1 month')::date;
          stop date := (date_trunc('month', now()) + make_interval(months => months_ahead))::date;
          created int := 0;
          part text;
        BEGIN
          WHILE m <= stop LOOP
            part := format('audit_events_y%sm%s', to_char(m, 'YYYY'), to_char(m, 'MM'));
            IF to_regclass('audit.' || part) IS NULL THEN
              EXECUTE format(
                'CREATE TABLE audit.%I PARTITION OF audit.audit_events FOR VALUES FROM (%L) TO (%L)',
                part, m, (m + interval '1 month')::date);
              created := created + 1;
            END IF;
            m := (m + interval '1 month')::date;
          END LOOP;
          RETURN created;
        END $$;
        """
    )
    op.execute(
        "COMMENT ON FUNCTION audit.ensure_partitions(int) IS "
        "'Creates monthly partitions from last month to N months ahead. Run monthly (cron/K8s CronJob).'"
    )
    # Safety net: rows outside existing partitions land here instead of failing the write.
    op.execute("CREATE TABLE audit.audit_events_default PARTITION OF audit.audit_events DEFAULT")
    op.execute("SELECT audit.ensure_partitions(2)")

    for qualified, text in INDEX_COMMENTS.items():
        safe = text.replace("'", "''")
        op.execute(f"COMMENT ON INDEX {qualified} IS '{safe}'")


def downgrade() -> None:
    for qualified in INDEX_COMMENTS:
        op.execute(f"COMMENT ON INDEX {qualified} IS NULL")
    op.execute(
        """
        DO $$ DECLARE r record; BEGIN
          FOR r IN SELECT c.relname FROM pg_inherits i
                   JOIN pg_class c ON c.oid = i.inhrelid
                   WHERE i.inhparent = 'audit.audit_events'::regclass LOOP
            EXECUTE format('DROP TABLE audit.%I', r.relname);
          END LOOP;
        END $$;
        """
    )
    op.execute("DROP FUNCTION audit.ensure_partitions(int)")
    op.execute("DROP TRIGGER trg_audit_events_append_only ON audit.audit_events")
    op.execute("DROP FUNCTION audit.reject_modification()")
    op.execute("DROP TRIGGER trg_documents_acl_propagate ON rag.documents")
    op.execute("DROP FUNCTION rag.propagate_document_acl()")
    op.execute("DROP TRIGGER trg_document_chunks_acl ON rag.document_chunks")
    op.execute("DROP FUNCTION rag.chunk_acl_from_document()")
    for table in UPDATED_AT_TABLES:
        name = table.split(".")[1]
        op.execute(f"DROP TRIGGER trg_{name}_updated_at ON {table}")
