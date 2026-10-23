"""Phase 25c (ADR-037): the Evaluation dashboard may read the eval schema's NUMBERS.

obs_reader (0026) gets SELECT on ONLY these columns:
- eval.datasets: id, name, version, created_at (not the description)
- eval.eval_cases: id, dataset_id, case_key (NOT input, NOT expected - the planted truth stays
  readable only by its owner)
- eval.eval_runs: id, dataset_id, status, started_at, finished_at, git_sha, summary (not the
  config). `summary` is written only by our own code: the runner's holds counts ("11/16"),
  outcome counts, latency and case keys; `make eval-import` writes numbers only (a test).
  Never a statement, a hypothesis or the planted truth.
- eval.evaluations: run_id, case_id, metric, value (NOT details: it holds hypothesis text)

Downgrade revokes the grants; the role stays (server-wide, see 0024).

Revision ID: 0028
Revises: 0027
Create Date: 2026-10-23
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0028"
down_revision: str | None = "0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ROLE = "obs_reader"
# table -> the columns a dashboard may read (tests/unit checks the dashboards use only these)
READABLE = {
    "eval.datasets": ("id", "name", "version", "created_at"),
    "eval.eval_cases": ("id", "dataset_id", "case_key"),
    "eval.eval_runs": (
        "id", "dataset_id", "status", "started_at", "finished_at", "git_sha", "summary",
    ),
    "eval.evaluations": ("run_id", "case_id", "metric", "value"),
}  # fmt: skip


def upgrade() -> None:
    op.execute(f"GRANT USAGE ON SCHEMA eval TO {ROLE}")
    for table, columns in READABLE.items():
        op.execute(f"GRANT SELECT ({', '.join(columns)}) ON {table} TO {ROLE}")


def downgrade() -> None:
    for table, columns in READABLE.items():
        op.execute(f"REVOKE SELECT ({', '.join(columns)}) ON {table} FROM {ROLE}")
    op.execute(f"REVOKE USAGE ON SCHEMA eval FROM {ROLE}")
