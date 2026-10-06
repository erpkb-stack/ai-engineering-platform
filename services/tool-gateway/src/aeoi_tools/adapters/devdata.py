"""Simulated "external systems" (logs, metrics, deploys, git) backed by the devdata schema.

In production each method would be a client for a real system (Loki/Splunk, Prometheus,
Argo/Spinnaker, GitHub). The CONTRACT (schemas.py) is what agents see, so swapping a backend
does not change any agent. Every query is bounded by the validated input (window <= 24h,
LIMIT), runs as the least-privilege tools_svc login, and is parameterised (no string SQL).
"""

from __future__ import annotations

import math
from datetime import timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aeoi_security.redaction import REDACTED, is_sensitive_key
from aeoi_tools import schemas as s
from aeoi_tools.contracts import ToolExecutionError, ToolUnavailableError


def _like(value: str) -> str:
    """Escape LIKE wildcards: user text is a literal substring, never a pattern."""
    return "%" + value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _levels_at_least(level: str) -> list[str]:
    return list(s.LEVELS[s.LEVELS.index(level) :])


class DevDataAdapter:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def _rows(self, sql: Any, params: dict[str, Any]) -> list[dict[str, Any]]:
        try:
            async with self._sessions() as session:
                result = await session.execute(sql, params)
                return [dict(r) for r in result.mappings().all()]
        except (OperationalError, PoolTimeoutError) as exc:
            raise ToolUnavailableError(f"devdata unavailable: {type(exc).__name__}") from exc
        except DBAPIError as exc:
            if exc.connection_invalidated:
                raise ToolUnavailableError("devdata connection lost") from exc
            raise

    # ------------------------------------------------------------ logs
    async def search_logs(self, q: s.LogsIn) -> s.LogsOut:
        where = """service_key = :svc AND ts >= :start AND ts < :end AND level = ANY(:levels)
                   AND (CAST(:code AS text) IS NULL OR error_code = CAST(:code AS text))
                   AND (CAST(:contains AS text) IS NULL OR message ILIKE CAST(:contains AS text))"""
        params = {
            "svc": q.service_key,
            "start": q.start,
            "end": q.end,
            "levels": _levels_at_least(q.min_level),
            "code": q.error_code,
            "contains": _like(q.contains) if q.contains else None,
            "limit": q.limit,
        }
        order = "DESC" if q.order == "desc" else "ASC"
        rows = await self._rows(
            text(
                f"SELECT ts, level, message, error_code, trace_id FROM devdata.log_events "  # noqa: S608 - order is a Literal
                f"WHERE {where} ORDER BY ts {order}, id {order} LIMIT :limit"
            ),
            params,
        )
        counts = await self._rows(
            text(
                f"SELECT coalesce(error_code, '(none)') AS code, count(*) AS n "  # noqa: S608
                f"FROM devdata.log_events WHERE {where} GROUP BY 1 ORDER BY 2 DESC LIMIT 50"
            ),
            params,
        )
        total = sum(int(c["n"]) for c in counts)
        return s.LogsOut(
            items=[s.LogItem(**r) for r in rows],
            total_matching=total,
            counts_by_error_code={str(c["code"]): int(c["n"]) for c in counts},
            truncated=total > len(rows),
        )

    # ------------------------------------------------------------ metrics
    async def query_metrics(self, q: s.MetricsIn) -> s.MetricsOut:
        seconds = (q.end - q.start).total_seconds()
        # whole minutes, so buckets line up with the 1-minute source resolution
        bucket = max(60, math.ceil(seconds / q.max_points / 60) * 60)
        rows = await self._rows(
            text("""
                SELECT date_bin(CAST(:bucket AS interval), ts, CAST(:start AS timestamptz)) AS ts,
                       avg(value) AS value
                FROM devdata.metric_points
                WHERE service_key = :svc AND metric = :metric AND ts >= :start AND ts < :end
                GROUP BY 1 ORDER BY 1
            """),
            {
                "bucket": timedelta(seconds=bucket),
                "svc": q.service_key,
                "metric": q.metric,
                "start": q.start,
                "end": q.end,
            },
        )
        values = sorted(float(r["value"]) for r in rows)
        stats: dict[str, float | None] = {"min": None, "max": None, "mean": None, "p95": None}
        if values:
            stats = {
                "min": round(values[0], 4),
                "max": round(values[-1], 4),
                "mean": round(sum(values) / len(values), 4),
                # nearest-rank p95 over BUCKET means (stated in the description)
                "p95": round(values[min(len(values) - 1, math.ceil(0.95 * len(values)) - 1)], 4),
            }
        if not rows:
            return s.MetricsOut(items=[])
        return s.MetricsOut(
            items=[
                s.MetricSeriesItem(
                    service_key=q.service_key,
                    metric=q.metric,
                    bucket_seconds=bucket,
                    points=[s.MetricPointOut(ts=r["ts"], value=round(r["value"], 4)) for r in rows],
                    stats=stats,
                )
            ]
        )

    # ------------------------------------------------------------ deployments
    async def get_deployments(self, q: s.DeploymentsIn) -> s.DeploymentsOut:
        if q.deploy_key:
            sql = text("SELECT * FROM devdata.deployments WHERE deploy_key = :key")
            params: dict[str, Any] = {"key": q.deploy_key}
        else:
            sql = text("""
                SELECT * FROM devdata.deployments
                WHERE service_key = :svc AND environment = :env
                  AND started_at >= :start AND started_at < :end
                ORDER BY started_at DESC LIMIT :limit
            """)
            params = {
                "svc": q.service_key,
                "env": q.environment,
                "start": q.start,
                "end": q.end,
                "limit": q.limit,
            }
        rows = await self._rows(sql, params)
        return s.DeploymentsOut(
            items=[
                s.DeploymentItem(
                    deploy_key=r["deploy_key"],
                    service_key=r["service_key"],
                    version=r["version"],
                    environment=r["environment"],
                    status=r["status"],
                    commit_sha=r["commit_sha"],
                    deployed_by=r["deployed_by"],
                    started_at=r["started_at"],
                    finished_at=r["finished_at"],
                    config_changed_keys=sorted(_changed(r["config_diff"])),
                )
                for r in rows
            ]
        )

    async def get_config_diff(self, q: s.ConfigDiffIn) -> s.ConfigDiffOut:
        rows = await self._rows(
            text(
                "SELECT deploy_key, service_key, version, config_diff "
                "FROM devdata.deployments WHERE deploy_key = :key"
            ),
            {"key": q.deploy_key},
        )
        if not rows:
            return s.ConfigDiffOut(items=[])
        r = rows[0]
        diff: dict[str, Any] = r["config_diff"] or {}
        changed = _changed(diff)  # [20, 20] is "touched, not changed": not a change
        changes = [
            # a changed secret is a fact worth knowing; its value is not
            s.ConfigChange(key=k, before=REDACTED, after=REDACTED)
            if is_sensitive_key(k)
            else s.ConfigChange(key=k, before=_pair(v)[0], after=_pair(v)[1])
            for k, v in sorted(diff.items())
            if k in changed
        ]
        return s.ConfigDiffOut(
            items=[
                s.ConfigDiffItem(
                    deploy_key=r["deploy_key"],
                    service_key=r["service_key"],
                    version=r["version"],
                    changes=changes,
                )
            ]
        )

    # ------------------------------------------------------------ code
    async def get_commit(self, q: s.CommitIn) -> s.CommitOut:
        # Prefix lookup as a btree RANGE on the primary key (LIKE 'abc%' can't use a btree
        # index under a non-C collation). Hex digits sort the same in every collation.
        rows = await self._rows(
            text("""
                SELECT * FROM devdata.commits
                WHERE sha >= :lo AND sha <= :hi ORDER BY sha LIMIT 2
            """),
            {"lo": q.sha.ljust(40, "0"), "hi": q.sha.ljust(40, "f")},
        )
        if len(rows) > 1:
            raise ToolExecutionError(f"sha prefix '{q.sha}' is ambiguous; give more characters")
        return s.CommitOut(items=[s.CommitItem(**r) for r in rows])

    async def get_pull_request(self, q: s.PullRequestIn) -> s.PullRequestOut:
        rows = await self._rows(
            text("""
                SELECT repository, number, title, body, author, state, changed_files,
                       merge_commit_sha, created_at, merged_at
                FROM devdata.pull_requests WHERE repository = :repo AND number = :n
            """),
            {"repo": q.repository, "n": q.number},
        )
        return s.PullRequestOut(items=[s.PullRequestItem(**r) for r in rows])

    async def search_repository(self, q: s.RepoSearchIn) -> s.RepoSearchOut:
        """Substring search over commit messages / PR titles+bodies of ONE repo or service.

        [P] ILIKE over an index-scoped range (repo/service + time). No trigram index on purpose:
        the scope keeps it to a few hundred rows. [Prod] a code-search backend (Phase 14).
        """
        params = {
            "repo": q.repository,
            "svc": q.service_key,
            "since": q.since,
            "pat": _like(q.query),
            "limit": q.limit,
        }
        commits = await self._rows(
            text("""
                SELECT sha, repository, message, author, committed_at, files_changed
                FROM devdata.commits
                WHERE (CAST(:repo AS text) IS NULL OR repository = CAST(:repo AS text))
                  AND (CAST(:svc AS text) IS NULL OR service_key = CAST(:svc AS text))
                  AND (CAST(:since AS timestamptz) IS NULL
                       OR committed_at >= CAST(:since AS timestamptz))
                  AND message ILIKE :pat
                ORDER BY committed_at DESC LIMIT :limit
            """),
            params,
        )
        prs = await self._rows(
            text("""
                SELECT p.repository, p.number, p.title, p.author, p.created_at, p.changed_files
                FROM devdata.pull_requests p
                WHERE (CAST(:repo AS text) IS NULL OR p.repository = CAST(:repo AS text))
                  AND (CAST(:svc AS text) IS NULL OR p.repository IN (
                        SELECT DISTINCT repository FROM devdata.commits
                        WHERE service_key = CAST(:svc AS text)))
                  AND (CAST(:since AS timestamptz) IS NULL
                       OR p.created_at >= CAST(:since AS timestamptz))
                  AND (p.title ILIKE :pat OR p.body ILIKE :pat)
                ORDER BY p.created_at DESC LIMIT :limit
            """),
            params,
        )
        hits = [
            s.RepoHitItem(
                kind="commit",
                ref=c["sha"],
                repository=c["repository"],
                title=c["message"].splitlines()[0][:300] if c["message"] else "",
                author=c["author"],
                at=c["committed_at"],
                files=list(c["files_changed"])[:100],
            )
            for c in commits
        ] + [
            s.RepoHitItem(
                kind="pull_request",
                ref=f"{p['repository']}#{p['number']}",
                repository=p["repository"],
                title=p["title"],
                author=p["author"],
                at=p["created_at"],
                files=list(p["changed_files"])[:100],
            )
            for p in prs
        ]
        hits.sort(key=lambda h: h.at, reverse=True)
        return s.RepoSearchOut(items=hits[: q.limit], truncated=len(hits) > q.limit)


def _pair(value: Any) -> tuple[Any, Any]:
    if isinstance(value, list | tuple) and len(value) == 2:
        return value[0], value[1]
    return None, value  # unknown shape: report as "after" only, never crash


def _changed(diff: dict[str, Any] | None) -> set[str]:
    out = set()
    for key, value in (diff or {}).items():
        before, after = _pair(value)
        if before != after:
            out.add(key)
    return out
