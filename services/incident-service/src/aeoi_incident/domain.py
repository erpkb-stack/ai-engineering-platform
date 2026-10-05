"""Pure domain rules: no IO, fully unit-tested."""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime
from typing import Any
from uuid import UUID

from aeoi_common.errors import BadRequestError, ConflictError
from aeoi_models.api.incidents import IncidentStatus as S

# Allowed status transitions. Anything else is a 409 - e.g. you cannot jump from CLOSED
# back to INVESTIGATING (open a new incident instead), or skip from OPEN to CLOSED.
TRANSITIONS: dict[S, frozenset[S]] = {
    S.OPEN: frozenset({S.INVESTIGATING, S.MITIGATED, S.RESOLVED}),
    S.INVESTIGATING: frozenset({S.AWAITING_REVIEW, S.MITIGATED, S.RESOLVED}),
    S.AWAITING_REVIEW: frozenset({S.INVESTIGATING, S.MITIGATED, S.RESOLVED}),
    S.MITIGATED: frozenset({S.INVESTIGATING, S.RESOLVED}),
    S.RESOLVED: frozenset({S.INVESTIGATING, S.CLOSED}),  # reopen allowed until closed
    S.CLOSED: frozenset(),
}
INVESTIGABLE = frozenset({S.OPEN, S.INVESTIGATING, S.AWAITING_REVIEW, S.MITIGATED, S.RESOLVED})


def check_transition(current: S, target: S) -> None:
    if target == current:
        return
    if target not in TRANSITIONS[current]:
        raise ConflictError(f"Status cannot change from {current} to {target}.")


def incident_key(number: int) -> str:
    return f"INC-{number}"


def parse_incident_ref(ref: str) -> UUID | int:
    """Accept a UUID or a human key 'INC-10234'."""
    if ref.upper().startswith("INC-") and ref[4:].isdigit():
        return int(ref[4:])
    try:
        return UUID(ref)
    except ValueError as exc:
        raise BadRequestError("Incident reference must be a UUID or INC-<number>.") from exc


def encode_cursor(created_at: datetime, row_id: UUID) -> str:
    raw = json.dumps({"c": created_at.isoformat(), "i": str(row_id)}).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode()))
        return datetime.fromisoformat(data["c"]), UUID(data["i"])
    except (ValueError, KeyError, TypeError) as exc:
        raise BadRequestError("Invalid cursor.") from exc


def request_fingerprint(body: Any) -> str:
    """Canonical sha256 of a request body (key order and whitespace don't matter)."""
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def parse_if_match(header: str | None) -> int | None:
    """`If-Match: "3"` or `W/"3"` -> 3."""
    if header is None:
        return None
    value = header.strip().removeprefix("W/").strip('"')
    if not value.isdigit():
        raise BadRequestError('If-Match must be the incident version, e.g. "3".')
    return int(value)
