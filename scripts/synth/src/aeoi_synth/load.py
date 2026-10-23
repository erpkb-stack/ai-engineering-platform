"""Load a generated Dataset into Postgres with COPY (fast) in ONE transaction.

Idempotent: seeded tables are truncated first, so `make db-seed` can be re-run.
User-created data (incident, orchestrator, tools, llm, audit, eval schemas) is never touched.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

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
    # Phase 14: after commits (FK). What each commit changed - from aeoi_synth.codegen
    "devdata.commit_diffs": ("sha", "path", "patch", "content_after", "additions", "deletions"),
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
    "devdata.commit_diffs",
    "devdata.commits",
    # Phase 9: grants reference users; a reload replaces every user, so their grants (and the
    # grants' history) go too. Otherwise `make db-seed` fails after the first investigation.
    "identity.delegation_events",
    "identity.delegation_grants",
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


def load_pack(conninfo: str, seed: int) -> dict[str, int]:
    """Phase 25a (ADR-037): the investigation scenario pack, ON TOP of `make db-seed`.

    Idempotent and contained: it deletes only its own rows (its deploy keys; logs and metrics
    of its services inside the pack's time range, where the main dataset has none), then
    inserts them again, and registers the dataset + cases (with the planted truth) in `eval`.
    `make db-seed` truncates devdata, so run this AFTER it.
    """
    from aeoi_synth import scenarios

    if os.environ.get("AEOI_ENV", "development") == "production":
        raise RuntimeError("refusing to load synthetic data with AEOI_ENV=production")
    pack = scenarios.build(seed)
    lo, hi = scenarios.pack_range()
    services = sorted({s for sc in pack for s in sc.services})
    keys = [d["deploy_key"] for sc in pack for d in sc.deployments]
    n = {"deployments": 0, "log_events": 0, "metric_points": 0, "cases": 0}
    with psycopg.connect(conninfo) as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM devdata.deployments WHERE deploy_key = ANY(%s)", (keys,))
        for table in ("devdata.log_events", "devdata.metric_points"):
            cur.execute(
                f"DELETE FROM {table} WHERE service_key = ANY(%s) AND ts BETWEEN %s AND %s",  # noqa: S608 (constant table names)
                (services, lo, hi),
            )
        for sc in pack:
            for d in sc.deployments:
                cur.execute(
                    "SELECT sha FROM devdata.commits WHERE service_key = %s"
                    " ORDER BY committed_at DESC, sha LIMIT 1",
                    (d["service_key"],),
                )
                row = cur.fetchone()
                if row is None:
                    raise RuntimeError(f"no commit for {d['service_key']}: run make db-seed first")
                cols = TABLE_COLUMNS["devdata.deployments"]
                cur.execute(
                    f"INSERT INTO devdata.deployments ({', '.join(cols)}) "  # noqa: S608 (constant columns)
                    f"VALUES ({', '.join(['%s'] * len(cols))})",
                    [_value(c, row[0] if c == "commit_sha" else d.get(c)) for c in cols],
                )
                n["deployments"] += 1
            for table, rows in (
                ("devdata.log_events", sc.logs),
                ("devdata.metric_points", sc.metrics),
            ):
                cols = TABLE_COLUMNS[table]
                with cur.copy(f"COPY {table} ({', '.join(cols)}) FROM STDIN") as copy:
                    for r in rows:
                        copy.write_row([_value(c, r.get(c)) for c in cols])
                n[table.split(".")[1]] += len(rows)
        cur.execute(
            "INSERT INTO eval.datasets (id, name, version, description) VALUES (%s, %s, %s, %s)"
            " ON CONFLICT (name, version) DO UPDATE SET description = EXCLUDED.description"
            " RETURNING id",
            (uuid5(NAMESPACE_URL, f"aeoi:eval:dataset:{scenarios.PACK_NAME}:"
                   f"{scenarios.PACK_VERSION}"), scenarios.PACK_NAME, scenarios.PACK_VERSION,
             f"{len(pack)} synthetic incidents with a planted cause (seed {seed}); "
             "aeoi_synth.scenarios"),
        )  # fmt: skip
        dataset_id = cur.fetchone()[0]  # type: ignore[index]
        for sc in pack:
            cur.execute(
                "INSERT INTO eval.eval_cases (id, dataset_id, case_key, input, expected)"
                " VALUES (%s, %s, %s, %s, %s) ON CONFLICT (dataset_id, case_key) DO UPDATE"
                " SET input = EXCLUDED.input, expected = EXCLUDED.expected",
                (uuid5(NAMESPACE_URL, f"aeoi:eval:case:{sc.key}"), dataset_id, sc.key,
                 Jsonb(sc.case_input()), Jsonb(sc.expected)),
            )  # fmt: skip
            n["cases"] += 1
        cur.execute("ANALYZE devdata.deployments, devdata.log_events, devdata.metric_points")
    return n
