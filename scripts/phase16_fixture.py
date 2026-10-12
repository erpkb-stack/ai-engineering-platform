"""Phase 16 smoke fixture (DEV ONLY): a throwaway service with two releases to roll back.

The smoke must never roll back a demo deploy (DEPLOY-4821 is checkout-api's running release:
a real rollback would change the demo data that risk-scan and earlier smokes count).

  plant  -> prints  SERVICE NEW_DEPLOY OLD_SHA   (service key starts with smoke-p16-)
  clean  -> deletes devdata.deployments rows of smoke-p16-* services (incl. the rollback)

Approval, incident and tool-call records stay: they are the audit trail of what the smoke did.
"""

from __future__ import annotations

import os
import secrets
import sys
from datetime import UTC, datetime, timedelta

import psycopg

from aeoi_db.config import libpq_dsn

PREFIX = "smoke-p16-"


def plant(c: psycopg.Connection) -> str:
    svc = PREFIX + secrets.token_hex(3)
    now = datetime.now(UTC)
    old, new = (f"DEPLOY-77{secrets.randbelow(10**6):06d}" for _ in range(2))
    old_sha, new_sha = secrets.token_hex(20), secrets.token_hex(20)
    for key, sha, at, diff in (
        (old, old_sha, now - timedelta(days=1), "{}"),
        (new, new_sha, now - timedelta(minutes=30), '{"feature_flags.smoke_path": [false, true]}'),
    ):
        c.execute(
            "INSERT INTO devdata.deployments (id, deploy_key, service_key, version, environment,"
            " status, commit_sha, deployed_by, config_diff, started_at, finished_at) VALUES"
            " (gen_random_uuid(), %s, %s, 'smoke', 'production', 'SUCCEEDED', %s, 'smoke-p16',"
            " %s::jsonb, %s, %s)",
            (key, svc, sha, diff, at, at + timedelta(minutes=2)),
        )
    return f"{svc} {new} {old_sha}"


def clean(c: psycopg.Connection) -> str:
    n = c.execute("DELETE FROM devdata.deployments WHERE service_key LIKE %s",
                  (PREFIX + "%",)).rowcount  # fmt: skip
    return f"removed {n} smoke deploy row(s)"


def main() -> int:
    if os.environ.get("AEOI_ENV") == "production" or len(sys.argv) != 2:
        print("usage (dev only): phase16_fixture.py plant|clean", file=sys.stderr)
        return 2
    with psycopg.connect(libpq_dsn(), autocommit=True) as c:
        print(plant(c) if sys.argv[1] == "plant" else clean(c))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
