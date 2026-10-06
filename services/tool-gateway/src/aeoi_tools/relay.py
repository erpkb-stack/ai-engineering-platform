"""Audit relay: tools.audit_outbox -> audit service (at-least-once, in order, batched).

- FOR UPDATE SKIP LOCKED: two gateway replicas never send the same batch at once.
- ONE HTTP call per batch; the audit service inserts with ON CONFLICT DO NOTHING on the event
  id, so a resend after a crash between "sent" and "marked delivered" is harmless.
- On failure: count the attempt, keep the rows, back off (max 30s). Tool calls keep working:
  the outbox is the buffer. Alert on backlog age, not on single failures (Phase 21).
- Poison event (receiver says 4xx for the batch): resend one by one; events the receiver still
  rejects are marked `rejected: ...` and skipped, so ONE bad event can't block all audit
  delivery. They stay in the table (never deleted) for a human to inspect.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path

import httpx
import structlog
from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aeoi_common.correlation import CORRELATION_HEADER
from aeoi_db.models.tools import AuditOutbox

log = structlog.get_logger(__name__)
REJECTED = "rejected: "
_NOT_REJECTED = or_(AuditOutbox.last_error.is_(None), ~AuditOutbox.last_error.startswith(REJECTED))


def _is_poison(exc: BaseException) -> bool:
    """4xx that is about the CONTENT (not auth/limits): resending the same batch never works."""
    if not isinstance(exc, httpx.HTTPStatusError):
        return False
    code = exc.response.status_code
    return 400 <= code < 500 and code not in (401, 403, 408, 429)


class FileToken:
    """Reads a service token from a file (dev: `make tools-tokens`). Re-read when it changes,
    so a rotated token is picked up without a restart. [Prod] workload identity."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._cached: tuple[float, str] | None = None

    async def __call__(self) -> str:
        mtime = self._path.stat().st_mtime
        if self._cached is None or self._cached[0] != mtime:
            self._cached = (mtime, self._path.read_text().strip())
        return self._cached[1]


class AuditRelay:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        client: httpx.AsyncClient,
        token: FileToken,
        *,
        batch_size: int = 200,
        interval_s: float = 1.0,
        max_backoff_s: float = 30.0,
    ) -> None:
        self._sessions = sessions
        self._client = client
        self._token = token
        self._batch = batch_size
        self._interval = interval_s
        self._max_backoff = max_backoff_s
        self._stopping = asyncio.Event()

    async def run_once(self) -> int:
        failure: Exception | None = None
        async with self._sessions() as session, session.begin():
            rows = (
                await session.scalars(
                    select(AuditOutbox)
                    .where(AuditOutbox.delivered_at.is_(None), _NOT_REJECTED)
                    .order_by(AuditOutbox.created_at, AuditOutbox.id)
                    .limit(self._batch)
                    .with_for_update(skip_locked=True)
                )
            ).all()
            if not rows:
                return 0
            ids = [r.id for r in rows]
            try:
                await self._send([r.event for r in rows], ids[0])
            except Exception as exc:
                if _is_poison(exc) and len(rows) > 1:
                    return await self._isolate(session, list(rows))
                failure = exc  # record the attempt and COMMIT it (raising here would roll back)
                error = REJECTED + _describe(exc) if _is_poison(exc) else _describe(exc)
                await session.execute(
                    update(AuditOutbox)
                    .where(AuditOutbox.id.in_(ids))
                    .values(attempts=AuditOutbox.attempts + 1, last_error=error)
                )
            else:
                await session.execute(
                    update(AuditOutbox)
                    .where(AuditOutbox.id.in_(ids))
                    .values(delivered_at=datetime.now(UTC), last_error=None)
                )
        if failure is not None:
            raise failure
        return len(rows)

    async def _send(self, events: list[dict[str, object]], first_id: object) -> None:
        resp = await self._client.post(
            "/v1/events",
            json={"events": events},
            headers={
                "Authorization": f"Bearer {await self._token()}",
                CORRELATION_HEADER: f"audit-relay-{first_id}",
            },
        )
        resp.raise_for_status()

    async def _isolate(self, session: AsyncSession, rows: list[AuditOutbox]) -> int:
        """Send one by one inside the same locked transaction; mark the poison rows."""
        delivered = 0
        for row in rows:
            try:
                await self._send([row.event], row.id)
            except Exception as exc:
                error = REJECTED + _describe(exc) if _is_poison(exc) else _describe(exc)
                row.attempts += 1
                row.last_error = error
                log.error("audit_event_rejected", outbox_id=str(row.id), error=error)
                if not _is_poison(exc):
                    break  # the receiver itself is failing now: stop, retry later
            else:
                row.delivered_at, row.last_error = datetime.now(UTC), None
                delivered += 1
        return delivered

    async def backlog(self) -> tuple[int, float | None]:
        """(undelivered events, age in seconds of the oldest) - the number to alert on."""
        async with self._sessions() as session:
            n, oldest = (
                await session.execute(
                    select(func.count(), func.min(AuditOutbox.created_at)).where(
                        AuditOutbox.delivered_at.is_(None), _NOT_REJECTED
                    )
                )
            ).one()
        age = (datetime.now(UTC) - oldest).total_seconds() if oldest else None
        return int(n), age

    async def rejected(self) -> int:
        """Events the audit service refused (need a human). Alert if > 0."""
        async with self._sessions() as session:
            n = await session.scalar(
                select(func.count()).where(
                    AuditOutbox.delivered_at.is_(None), AuditOutbox.last_error.startswith(REJECTED)
                )
            )
        return int(n or 0)

    async def run_forever(self) -> None:
        backoff = self._interval
        while not self._stopping.is_set():
            try:
                n = await self.run_once()
                backoff = self._interval
                if n == self._batch:
                    continue
            except Exception as exc:
                log.warning("audit_relay_failed", error=_describe(exc), retry_in_s=backoff)
                backoff = min(backoff * 2, self._max_backoff)
            with suppress(TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), timeout=backoff)

    def stop(self) -> None:
        self._stopping.set()


def _describe(exc: BaseException) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    return type(exc).__name__
