"""Load a generated Dataset into Postgres with COPY (fast) in ONE transaction.

Idempotent: seeded tables are truncated first, so `make db-seed` can be re-run.
User-created data (incident, orchestrator, tools, llm, audit, eval schemas) is never touched.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from aeoi_synth.generate import Dataset

# Load order respects FKs; truncate order is the reverse.
TABLE_COLUMNS: dict[str, tuple[str, ...]] = {
    "identity.groups": ("name", "description"),
    "identity.users": ("id", "external_subject", "email", "display_name", "is_active"),
    "identity.user_roles": ("user_id", "role_name"),
    "identity.user_groups": ("user_id", "group_name"),
    "devdata.commits": (
        "sha",
        "repository",
        "service_key",
        "author",
        "message",
        "files_changed",
        "additions",
        "deletions",
        "committed_at",
    ),
    "devdata.pull_requests": (
        "id",
        "repository",
        "number",
        "title",
        "body",
        "author",
        "state",
        "changed_files",
        "merge_commit_sha",
        "created_at",
        "merged_at",
    ),
    "devdata.deployments": (
        "id",
        "deploy_key",
        "service_key",
        "version",
        "environment",
        "status",
        "commit_sha",
        "deployed_by",
        "config_diff",
        "started_at",
        "finished_at",
    ),
    "devdata.log_events": (
        "ts",
        "service_key",
        "level",
        "message",
        "error_code",
        "trace_id",
        "attributes",
    ),
    "devdata.metric_points": ("service_key", "metric", "ts", "value"),
    "rag.historical_incidents": (
        "id",
        "incident_key",
        "title",
        "summary",
        "root_cause",
        "root_cause_category",
        "remediation",
        "service_keys",
        "severity",
        "occurred_at",
        "resolved_at",
        "allowed_groups",
    ),
    "rag.documents": (
        "id",
        "source",
        "source_uri",
        "title",
        "department",
        "owner",
        "version",
        "content",
        "content_sha256",
        "allowed_groups",
        "sensitivity",
        "quarantined",
    ),
    "rag.runbooks": (
        "id",
        "runbook_key",
        "version",
        "title",
        "service_keys",
        "document_id",
        "steps",
    ),
}
TRUNCATE_ORDER = [
    "rag.runbooks",
    "rag.document_chunks",
    "rag.documents",
    "rag.historical_incidents",
    "devdata.metric_points",
    "devdata.log_events",
    "devdata.deployments",
    "devdata.pull_requests",
    "devdata.commits",
    "identity.user_groups",
    "identity.user_roles",
    "identity.users",
    "identity.groups",
]
JSON_COLUMNS = {"config_diff", "attributes", "steps"}


def _value(col: str, v: Any) -> Any:
    return Jsonb(v) if col in JSON_COLUMNS else v


def load(ds: Dataset, conninfo: str) -> dict[str, int]:
    if os.environ.get("AEOI_ENV", "development") == "production":
        raise RuntimeError("refusing to load synthetic data with AEOI_ENV=production")
    loaded: dict[str, int] = {}
    with psycopg.connect(conninfo) as conn, conn.cursor() as cur:
        cur.execute("SET LOCAL statement_timeout = '10min'")
        cur.execute("TRUNCATE " + ", ".join(TRUNCATE_ORDER) + " RESTART IDENTITY")
        for table, cols in TABLE_COLUMNS.items():
            rows = ds.tables.get(table, [])
            with cur.copy(f"COPY {table} ({', '.join(cols)}) FROM STDIN") as copy:
                for row in rows:
                    copy.write_row([_value(c, row.get(c)) for c in cols])
            loaded[table] = len(rows)
        cur.execute("ANALYZE")
    return loaded


def write_catalog(ds: Dataset, out_dir: Path) -> Path:
    """The service catalog lives in the Java service from Phase 20; until then, a JSON file."""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "catalog.json"
    path.write_text(json.dumps({"seed": ds.seed, "services": ds.catalog}, indent=1, sort_keys=True))
    return path
