"""Phase 18a smoke helper (DEV ONLY, read-only): audit rows written for an incident by Kafka.

  uv run python scripts/phase18_audit_rows.py <incident uuid> [EventType]  ->  "<count> <event_id or ->"
Only rows that came through the broker (`details._ingested_by` = kafka:...) are counted.
"""

from __future__ import annotations

import os
import sys

import psycopg

from aeoi_db.config import libpq_dsn


def main() -> int:
    if os.environ.get("AEOI_ENV") == "production" or len(sys.argv) not in (2, 3):
        print(
            "usage (dev only): phase18_audit_rows.py <incident uuid> [EventType]", file=sys.stderr
        )
        return 2
    action = f"event.{sys.argv[2] if len(sys.argv) == 3 else 'IncidentCreated'}"
    with psycopg.connect(libpq_dsn()) as c:
        rows = c.execute(
            "SELECT id FROM audit.audit_events WHERE incident_id = %s"
            " AND details->>'_ingested_by' LIKE 'kafka:%%' AND action = %s",
            (sys.argv[1], action),
        ).fetchall()
    print(len(rows), rows[0][0] if rows else "-")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
