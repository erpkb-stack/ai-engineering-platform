"""Phase 29b (ADR-044): signed webhooks. An alert sender has no user and no JWT; it proves
who it is with a shared secret, an HMAC-SHA256 over the timestamp AND the exact body bytes.

    X-AEOI-Timestamp: <unix seconds>
    X-AEOI-Signature: sha256=<hex of HMAC(secret, f"{ts}.".encode() + body)>

- The timestamp is inside the signed bytes, so it cannot be changed; a request older (or
  newer) than `TOLERANCE_S` is refused - a captured request cannot be replayed later.
- Inside the window a replay is still possible: the RECEIVER dedupes on the alert's own
  identity (fingerprint + startsAt), so a replay opens no second incident.
- Compared in constant time (hmac.compare_digest). The secret is never logged or echoed.
- The body is verified as BYTES, before it is parsed: a parser difference cannot change what
  was signed. Callers read at most MAX_BODY bytes first (413 beyond it).
"""

from __future__ import annotations

import hashlib
import hmac
import time
from collections.abc import AsyncIterator
from pathlib import Path

from aeoi_common.errors import PayloadTooLargeError, UnauthorizedError, UpstreamUnavailableError

SIGNATURE_HEADER = "x-aeoi-signature"
TIMESTAMP_HEADER = "x-aeoi-timestamp"
TOLERANCE_S = 300
MAX_BODY = 64 * 1024
MIN_SECRET = 32  # bytes: a short secret is a configuration error, refused at use
_PREFIX = "sha256="


def sign(secret: bytes, ts: int, body: bytes) -> str:
    mac = hmac.new(secret, f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return f"{_PREFIX}{mac}"


def load_secret(path: Path) -> bytes:
    """The shared secret, or 503 'not configured' (never a silent accept-all)."""
    try:
        secret = path.read_bytes().strip()
    except OSError as exc:
        raise UpstreamUnavailableError(
            "Alert webhook is not configured (no secret): run make alert-secret."
        ) from exc
    if len(secret) < MIN_SECRET:
        raise UpstreamUnavailableError("Alert webhook secret is too short (need 32+ bytes).")
    return secret


def verify(
    secret: bytes,
    *,
    timestamp: str | None,
    signature: str | None,
    body: bytes,
    now: float | None = None,
) -> int:
    """Raise 401 unless the signature is right and fresh. Returns the timestamp."""
    if len(body) > MAX_BODY:
        raise PayloadTooLargeError(f"Body larger than {MAX_BODY} bytes.")
    if not timestamp or not signature:
        raise UnauthorizedError("Missing X-AEOI-Timestamp or X-AEOI-Signature.")
    if not (timestamp.isascii() and timestamp.isdigit()) or len(timestamp) > 12:
        raise UnauthorizedError("Bad X-AEOI-Timestamp.")
    ts = int(timestamp)
    clock = time.time() if now is None else now
    if abs(clock - ts) > TOLERANCE_S:
        raise UnauthorizedError("Stale or future X-AEOI-Timestamp (more than 300 s off).")
    if not hmac.compare_digest(sign(secret, ts, body).encode(), signature.strip().encode()):
        raise UnauthorizedError("Bad signature.")
    return ts


async def read_capped(chunks: AsyncIterator[bytes], limit: int = MAX_BODY) -> bytes:
    """Read an ASGI body stream, stopping (413) as soon as it passes `limit`."""
    buf = bytearray()
    async for chunk in chunks:
        buf += chunk
        if len(buf) > limit:
            raise PayloadTooLargeError(f"Body larger than {limit} bytes.")
    return bytes(buf)
