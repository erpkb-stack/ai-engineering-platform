"""The simulated CD system behind rollback_deployment (Phase 16, ADR-026).

A rollback = a NEW deployment of the previous successful release of that service (its commit
and the config the undone deploy changed, restored), written through a login that can ONLY
insert into devdata.deployments (`cd_svc` ∈ svc_cd). Reads go through the gateway's normal
devdata login. The new deploy key is derived from the approval id, so a retry of the same
approved execution can never deploy twice (INSERT ... ON CONFLICT DO NOTHING).

Refuses (never guesses): an unknown deploy, a deploy that is no longer the running one of its
service (a newer release would be silently undone), a deploy with nothing earlier to go back to.
[P] the real CD (Argo/Spinnaker/GitHub deployments) is a Production concern; the contract -
idempotent per approval, refuses a stale target - is the part that carries over.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql.expression import bindparam

from aeoi_common.ids import uuid7
from aeoi_tools import schemas as s
from aeoi_tools.contracts import ToolContext, ToolExecutionError

_DEPLOY = text("""
    SELECT deploy_key, service_key, environment, status, commit_sha, deployed_by,
           config_diff, started_at
    FROM devdata.deployments WHERE deploy_key = :key
""")
_NEWER = text("""
    SELECT deploy_key FROM devdata.deployments
    WHERE service_key = :svc AND environment = :env AND status = 'SUCCEEDED'
      AND started_at > :at ORDER BY started_at DESC LIMIT 1
""")
_PREVIOUS = text("""
    SELECT deploy_key, commit_sha FROM devdata.deployments
    WHERE service_key = :svc AND environment = :env AND status = 'SUCCEEDED'
      AND started_at < :at ORDER BY started_at DESC LIMIT 1
""")
_INSERT = text("""
    INSERT INTO devdata.deployments (id, deploy_key, service_key, version, environment, status,
                                     commit_sha, deployed_by, config_diff, started_at, finished_at)
    VALUES (:id, :key, :svc, :version, :env, 'SUCCEEDED', :sha, :by, :config, :now, :now)
    ON CONFLICT DO NOTHING
""").bindparams(bindparam("config", type_=JSONB))
# no conflict TARGET on purpose: `ON CONFLICT (deploy_key)` needs SELECT on deploy_key, and the
# CD login is INSERT-only (found in the Phase 16 e2e). The only unique keys are id (fresh
# uuid7) and deploy_key (derived from the approval), so "any conflict" = "this approval ran".


_LOCK = text("SELECT pg_advisory_xact_lock(hashtext(:k))")


def rollback_key(approval_id: UUID) -> str:
    """Deterministic per approval: DEPLOY-9 + 18 digits (seeded keys are 4-digit; the column
    holds 32). 18 digits of the uuid7's random bits: review M1 - 9 digits collided at ~30k
    approvals. A collision is still CHECKED (`_mine`), never trusted."""
    return f"DEPLOY-9{approval_id.int % 10**18:018d}"


def _mine(row: Any, approval_id: UUID, service_key: str) -> bool:
    return bool(
        row["deployed_by"] == f"approval:{approval_id}" and row["service_key"] == service_key
    )


def inverse_config(diff: dict[str, Any]) -> dict[str, list[Any]]:
    """The config the undone deploy changed, set back: {key: [after, before]}."""
    out: dict[str, list[Any]] = {}
    for k, v in (diff or {}).items():
        if isinstance(v, list) and len(v) == 2 and v[0] != v[1]:
            out[k] = [v[1], v[0]]
    return out


class Deployer:
    def __init__(
        self,
        read: async_sessionmaker[AsyncSession],
        write: async_sessionmaker[AsyncSession] | None,
    ) -> None:
        self._read = read
        self._write = write

    async def rollback(self, q: s.RollbackIn, ctx: ToolContext) -> s.RollbackOut:
        if ctx.approval_id is None:  # policy guarantees it; never act without one
            raise ToolExecutionError("no approval", status=403)
        if self._write is None:
            raise ToolExecutionError("the CD login (cd_svc) is not configured", status=503)
        aid, key = ctx.approval_id, rollback_key(ctx.approval_id)
        # the read transaction stays open across the insert: it holds the per-service lock
        # (review M2 - two approvals for the same service must not both pass the "still the
        # running deploy" check), released only after the CD record is committed
        async with self._read() as session, session.begin():
            target = (await session.execute(_DEPLOY, {"key": q.deploy_key})).mappings().first()
            if target is None:
                raise ToolExecutionError(f"{q.deploy_key} not found", status=404)
            svc, env = target["service_key"], target["environment"]
            await session.execute(_LOCK, {"k": f"rollback:{svc}:{env}"})
            done = (await session.execute(_DEPLOY, {"key": key})).mappings().first()
            if done is not None:  # this approval already ran: the same answer again
                if not _mine(done, aid, svc):
                    raise ToolExecutionError(f"deploy key {key} is taken by another record: "
                                             "nothing was deployed", status=409)  # fmt: skip
                return self._out(done, target, key)
            if target["status"] != "SUCCEEDED":
                raise ToolExecutionError(
                    f"{q.deploy_key} is {target['status']}: only a running (SUCCEEDED) deploy "
                    "can be rolled back", status=409)  # fmt: skip
            args = {"svc": svc, "env": env, "at": target["started_at"]}
            newer = (await session.execute(_NEWER, args)).scalar_one_or_none()
            if newer is not None:
                raise ToolExecutionError(
                    f"{q.deploy_key} is not the running deploy of {svc} "
                    f"({newer} is newer): rolling it back would undo {newer}", status=409)  # fmt: skip
            prev = (await session.execute(_PREVIOUS, args)).mappings().first()
            if prev is None:
                raise ToolExecutionError(
                    f"no earlier successful deploy of {svc} to go back to", status=409)  # fmt: skip
            now = datetime.now(UTC)
            config = inverse_config(target["config_diff"])
            async with self._write() as w, w.begin():
                res = await w.execute(_INSERT, {
                    "id": uuid7(), "key": key, "svc": svc,
                    "version": f"rollback-of-{q.deploy_key}"[:40], "env": env,
                    "sha": prev["commit_sha"], "by": f"approval:{aid}",
                    "config": config, "now": now,
                })  # fmt: skip
            if getattr(res, "rowcount", 1) != 1:  # ON CONFLICT: ours (a concurrent retry)?
                row = (await session.execute(_DEPLOY, {"key": key})).mappings().first()
                if row is None or not _mine(row, aid, svc):
                    raise ToolExecutionError(f"deploy key {key} is taken by another record: "
                                             "nothing was deployed", status=409)  # fmt: skip
                return self._out(row, target, key)
        return s.RollbackOut(items=[s.RollbackItem(
            deploy_key=key, rolled_back_to=prev["commit_sha"], rolled_back_from=q.deploy_key,
            service_key=svc, environment=env, config_restored=sorted(config))])  # fmt: skip

    @staticmethod
    def _out(done: Any, target: Any, key: str) -> s.RollbackOut:
        return s.RollbackOut(items=[s.RollbackItem(
            deploy_key=key, rolled_back_to=done["commit_sha"],
            rolled_back_from=target["deploy_key"], service_key=done["service_key"],
            environment=done["environment"],
            config_restored=sorted((done["config_diff"] or {}).keys()))])  # fmt: skip
