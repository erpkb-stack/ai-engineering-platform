"""Idempotent POSTs, stored in Postgres in the same transaction as the result.

Why DB and not only Redis: the stored response must commit atomically with the incident
it describes. If they were in different stores, a crash between the two writes would
either lose the key (duplicate incident on retry) or keep a key for a rolled-back incident.
[Prod] Redis can sit in front as a cache for fast replays; the DB stays the source of truth.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aeoi_common.errors import BadRequestError, PreconditionRequiredError, ValidationFailedError
from aeoi_db.models.incident import IdempotencyKey
from aeoi_incident.domain import request_fingerprint

log = structlog.get_logger("aeoi.idempotency")
_KEY = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")
Work = Callable[[AsyncSession], Awaitable[tuple[int, dict[str, Any]]]]


@dataclass(frozen=True)
class Result:
    status: int
    body: dict[str, Any]
    replayed: bool


def validate_key(key: str | None, *, required: bool) -> str | None:
    if key is None:
        if required:
            raise PreconditionRequiredError(
                "Idempotency-Key header is required for this operation (8-128 chars)."
            )
        return None
    if not _KEY.match(key):
        raise BadRequestError("Idempotency-Key must be 8-128 chars of [A-Za-z0-9._:-].")
    return key


async def run_idempotent(
    sessions: async_sessionmaker[AsyncSession],
    *,
    principal: str,
    scope: str,
    key: str | None,
    body: Any,
    ttl: timedelta,
    work: Work,
) -> Result:
    if key is None:
        async with sessions() as session, session.begin():
            status, resp = await work(session)
        return Result(status, resp, replayed=False)

    fingerprint = request_fingerprint(body)
    for attempt in (1, 2):
        try:
            async with sessions() as session, session.begin():
                now = datetime.now(UTC)
                existing = await session.get(IdempotencyKey, (principal, scope, key))
                if existing is not None and existing.expires_at > now:
                    if existing.request_sha256 != fingerprint:
                        raise ValidationFailedError(
                            "This Idempotency-Key was already used with a different request body."
                        )
                    return Result(existing.response_status, existing.response_body, replayed=True)
                if existing is not None:  # expired: forget it
                    await session.delete(existing)
                    await session.flush()
                status, resp = await work(session)
                session.add(
                    IdempotencyKey(
                        principal=principal,
                        scope=scope,
                        key=key,
                        request_sha256=fingerprint,
                        response_status=status,
                        response_body=resp,
                        expires_at=now + ttl,
                    )
                )
            return Result(status, resp, replayed=False)
        except IntegrityError as exc:
            # A concurrent request with the same key committed first. Our whole transaction
            # (including the incident we tried to create) rolled back. Loop once to replay it.
            if attempt == 1 and "pk_idempotency_keys" in str(exc.orig):
                log.info("idempotency_race_lost", scope=scope)  # the winner's result is replayed
                continue
            raise
    raise RuntimeError("unreachable")  # pragma: no cover
