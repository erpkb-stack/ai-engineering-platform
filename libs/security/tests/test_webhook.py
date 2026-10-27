"""Phase 29b (ADR-044): the alert webhook signature."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from aeoi_common.errors import PayloadTooLargeError, UnauthorizedError, UpstreamUnavailableError
from aeoi_security.webhook import MAX_BODY, load_secret, read_capped, sign, verify

SECRET = b"s" * 32
NOW = 1_790_000_000
BODY = b'{"status":"firing","alerts":[]}'


def _ok(**over: object) -> int:
    kw: dict[str, object] = {"timestamp": str(NOW), "signature": sign(SECRET, NOW, BODY),
                             "body": BODY, "now": NOW} | over  # fmt: skip
    return verify(SECRET, **kw)  # type: ignore[arg-type]


def test_a_correct_signature_passes() -> None:
    assert _ok() == NOW


def test_unsigned_requests_are_refused() -> None:
    for over in ({"signature": None}, {"timestamp": None}, {"signature": ""}):
        with pytest.raises(UnauthorizedError, match="Missing"):
            _ok(**over)


def test_a_changed_body_or_a_wrong_secret_is_refused() -> None:
    with pytest.raises(UnauthorizedError, match="Bad signature"):
        _ok(body=BODY.replace(b"firing", b"resolved"))
    with pytest.raises(UnauthorizedError, match="Bad signature"):
        _ok(signature=sign(b"t" * 32, NOW, BODY))


def test_the_timestamp_is_signed_so_it_cannot_be_moved() -> None:
    """An attacker who replays an old request cannot just put a fresh timestamp on it."""
    with pytest.raises(UnauthorizedError, match="Bad signature"):
        _ok(timestamp=str(NOW + 1))


def test_a_stale_or_future_request_is_refused_even_with_a_right_signature() -> None:
    old = NOW - 301
    with pytest.raises(UnauthorizedError, match="Stale"):
        _ok(timestamp=str(old), signature=sign(SECRET, old, BODY))
    new = NOW + 301
    with pytest.raises(UnauthorizedError, match="Stale"):
        _ok(timestamp=str(new), signature=sign(SECRET, new, BODY))
    edge = NOW - 300
    assert _ok(timestamp=str(edge), signature=sign(SECRET, edge, BODY)) == edge


def test_a_malformed_timestamp_is_refused() -> None:
    for ts in ("-5", "1e9", "abc", "9" * 13, "\u0661\u0662\u0663"):
        with pytest.raises(UnauthorizedError):
            _ok(timestamp=ts)


def test_an_oversized_body_is_refused_before_hashing() -> None:
    big = b"x" * (MAX_BODY + 1)
    with pytest.raises(PayloadTooLargeError):
        _ok(body=big, signature=sign(SECRET, NOW, big))


def test_no_secret_or_a_short_secret_is_not_configured_never_accept_all(tmp_path: Path) -> None:
    with pytest.raises(UpstreamUnavailableError, match="not configured"):
        load_secret(tmp_path / "missing.txt")
    short = tmp_path / "short.txt"
    short.write_text("abc\n")
    with pytest.raises(UpstreamUnavailableError, match="too short"):
        load_secret(short)
    good = tmp_path / "good.txt"
    good.write_text("a" * 64 + "\n")
    assert load_secret(good) == b"a" * 64  # the trailing newline is not part of the secret


def test_the_body_stream_stops_at_the_cap() -> None:
    async def chunks(n: int) -> AsyncIterator[bytes]:
        for _ in range(n):
            yield b"x" * 1024

    assert len(asyncio.run(read_capped(chunks(64)))) == MAX_BODY
    with pytest.raises(PayloadTooLargeError):
        asyncio.run(read_capped(chunks(65)))
