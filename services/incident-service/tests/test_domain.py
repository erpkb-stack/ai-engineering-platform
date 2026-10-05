from datetime import UTC, datetime
from uuid import uuid4

import pytest

from aeoi_common.errors import BadRequestError, ConflictError
from aeoi_incident.domain import (
    TRANSITIONS,
    check_transition,
    decode_cursor,
    encode_cursor,
    parse_if_match,
    parse_incident_ref,
    request_fingerprint,
)
from aeoi_models.api.incidents import IncidentStatus as S


@pytest.mark.parametrize(
    ("a", "b"), [(S.OPEN, S.INVESTIGATING), (S.RESOLVED, S.INVESTIGATING), (S.RESOLVED, S.CLOSED)]
)
def test_allowed_transitions(a: S, b: S) -> None:
    check_transition(a, b)


@pytest.mark.parametrize(
    ("a", "b"), [(S.CLOSED, S.INVESTIGATING), (S.OPEN, S.CLOSED), (S.CLOSED, S.OPEN)]
)
def test_forbidden_transitions(a: S, b: S) -> None:
    with pytest.raises(ConflictError):
        check_transition(a, b)


def test_every_status_has_a_rule() -> None:
    assert set(TRANSITIONS) == set(S)


def test_closed_is_terminal() -> None:
    assert TRANSITIONS[S.CLOSED] == frozenset()


def test_cursor_roundtrip() -> None:
    at, i = datetime(2026, 10, 2, 9, 55, tzinfo=UTC), uuid4()
    assert decode_cursor(encode_cursor(at, i)) == (at, i)


@pytest.mark.parametrize("bad", ["", "not-base64!!", "eyJ4IjoxfQ"])
def test_bad_cursor(bad: str) -> None:
    with pytest.raises(BadRequestError):
        decode_cursor(bad)


def test_incident_ref() -> None:
    assert parse_incident_ref("INC-10234") == 10234
    assert parse_incident_ref("inc-7") == 7
    u = uuid4()
    assert parse_incident_ref(str(u)) == u
    with pytest.raises(BadRequestError):
        parse_incident_ref("10234; DROP TABLE")


def test_fingerprint_ignores_key_order() -> None:
    assert request_fingerprint({"a": 1, "b": [1, 2]}) == request_fingerprint({"b": [1, 2], "a": 1})
    assert request_fingerprint({"a": 1}) != request_fingerprint({"a": 2})


def test_if_match() -> None:
    assert parse_if_match('"3"') == 3
    assert parse_if_match('W/"4"') == 4
    assert parse_if_match(None) is None
    with pytest.raises(BadRequestError):
        parse_if_match("*")
