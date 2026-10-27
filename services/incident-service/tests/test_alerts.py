"""Phase 29b (ADR-044): alert triage is a rule table in code, and the catalog never loses an
alert. Pure: no DB, no network (the catalog is an httpx.MockTransport)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from aeoi_incident.alerts import AlertRejectedError, dedupe_key, service_of, triage
from aeoi_incident.catalog import CatalogClient, CatalogEntry
from aeoi_models.api.alerts import AlertBatch, AlertIn
from aeoi_models.api.incidents import Severity

START = datetime(2026, 10, 2, 9, 50, tzinfo=UTC)
TIER1 = CatalogEntry(key="checkout-api", tier=1, dependents=["returns-api", "chat-worker"])
TIER2 = CatalogEntry(key="cart-cache", tier=2, dependents=[])


def alert(**labels: str) -> AlertIn:
    return AlertIn(
        status="firing",
        labels={"alertname": "CheckoutHigh5xx", "service": "checkout-api"} | labels,
        annotations={"summary": "Checkout 5xx above 5% for 5 minutes"},
        startsAt=START,
        fingerprint="a1b2c3d4e5f60718",
    )


@pytest.mark.parametrize(("label", "tier2", "tier1"), [
    ("critical", Severity.SEV1, Severity.SEV1),  # already the top: never above SEV1
    ("error", Severity.SEV2, Severity.SEV1),
    ("high", Severity.SEV2, Severity.SEV1),
    ("warning", Severity.SEV3, Severity.SEV2),
    ("info", Severity.SEV4, Severity.SEV3),
    ("WARNING", Severity.SEV3, Severity.SEV2),  # case does not matter
])  # fmt: skip
def test_severity_is_the_label_raised_one_level_for_tier_1(
    label: str, tier2: Severity, tier1: Severity
) -> None:
    assert triage(alert(severity=label), TIER2, "catalog").incident.severity is tier2
    assert triage(alert(severity=label), TIER1, "catalog").incident.severity is tier1


def test_an_unknown_or_missing_severity_is_sev3_and_says_so() -> None:
    for a in (alert(severity="page-everyone"), alert()):
        t = triage(a, TIER2, "catalog")
        assert t.incident.severity is Severity.SEV3
        assert "unknown label: default" in t.rule


def test_dependents_are_the_blast_radius_never_the_affected_services() -> None:
    """The orchestrator investigates the first 3 affected services: dependents there would
    send the agents to services that are victims, not the cause."""
    t = triage(alert(severity="error"), TIER1, "catalog")
    assert t.incident.affected_services == ["checkout-api"]
    assert t.blast_radius == ["chat-worker", "returns-api"]
    assert "Blast radius (catalog dependents of checkout-api): chat-worker, returns-api" in (
        t.incident.description
    )


def test_catalog_down_falls_back_to_the_label_and_says_so() -> None:
    t = triage(alert(severity="error"), None, "catalog unreachable (ConnectError)")
    assert t.incident.severity is Severity.SEV2  # no tier: no raise
    assert "tier unknown (catalog unreachable (ConnectError))" in t.rule
    assert t.incident.affected_services == ["checkout-api"]


def test_title_detected_at_and_the_rule_come_from_the_alert() -> None:
    t = triage(alert(severity="error"), TIER1, "catalog")
    assert t.incident.title == "Checkout 5xx above 5% for 5 minutes"
    assert t.incident.detected_at == START
    assert t.rule == "label severity=error -> SEV2; tier 1 -> raised to SEV1"
    bare = alert(severity="error")
    bare.annotations.clear()
    assert triage(bare, None, "x").incident.title == "CheckoutHigh5xx on checkout-api"


def test_the_service_label_must_be_a_service_key() -> None:
    assert service_of(alert(service="", service_key="cart-cache")) == "cart-cache"
    assert service_of(AlertIn(status="firing", labels={"job": "orders-api"}, startsAt=START,
                              fingerprint="ff")) == "orders-api"  # fmt: skip
    with pytest.raises(AlertRejectedError, match="not a service key"):
        service_of(alert(service="Checkout API; DROP TABLE"))
    with pytest.raises(AlertRejectedError, match="no service label"):
        service_of(AlertIn(status="firing", labels={"alertname": "X"}, startsAt=START,
                           fingerprint="ff"))  # fmt: skip


def test_the_dedupe_key_is_the_alert_identity_and_fits_the_idempotency_rules() -> None:
    a, b = alert(severity="error"), alert(severity="critical")  # labels changed, same alert
    assert dedupe_key(a) == dedupe_key(b) == "alert:a1b2c3d4e5f60718:1790934600"
    later = alert()
    later.startsAt = START.replace(hour=11)  # it fired AGAIN later: a new incident
    assert dedupe_key(later) != dedupe_key(a)
    longest = AlertIn(status="firing", labels={}, startsAt=START, fingerprint="f" * 64)
    assert len(dedupe_key(longest)) <= 128


def test_alertmanager_fields_we_do_not_read_are_ignored_and_bounds_hold() -> None:
    raw: dict[str, Any] = {
        "version": "4", "status": "firing", "receiver": "aeoi", "groupKey": "{}:{}",
        "externalURL": "http://am", "commonLabels": {}, "truncatedAlerts": 0,
        "alerts": [{"status": "firing", "labels": {"service": "checkout-api"},
                    "annotations": {}, "startsAt": "2026-10-02T09:50:00Z",
                    "endsAt": "0001-01-01T00:00:00Z", "generatorURL": "http://p",
                    "fingerprint": "abc123"}],
    }  # fmt: skip
    assert AlertBatch.model_validate(raw).alerts[0].fingerprint == "abc123"
    with pytest.raises(ValidationError, match="at most 20"):
        AlertBatch.model_validate(raw | {"alerts": raw["alerts"] * 21})
    with pytest.raises(ValidationError, match="at least 1"):
        AlertBatch.model_validate(raw | {"alerts": []})
    too_long = dict(raw["alerts"][0], annotations={"summary": "x" * 2001})
    with pytest.raises(ValidationError, match="too long"):
        AlertBatch.model_validate(raw | {"alerts": [too_long]})


# ------------------------------------------------------------------ catalog client


def _client(tmp_path: Path, handler: Any, token: bool = True) -> CatalogClient:
    tok = tmp_path / "incident_catalog_token.txt"
    if token:
        tok.write_text("tok\n")
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return CatalogClient(http, "http://service-catalog:8010", tok)


def test_the_catalog_answers_tier_and_dependents_with_the_service_token(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []

    def ok(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json={"truncated": False, "items": [{
            "key": "checkout-api", "name": "Checkout Api", "type": "api", "tier": 1,
            "team": "team-checkout", "depends_on": [], "dependents": ["returns-api"]}]})  # fmt: skip

    entry, note = asyncio.run(_client(tmp_path, ok).lookup("checkout-api"))
    assert entry == CatalogEntry(key="checkout-api", tier=1, dependents=["returns-api"])
    assert note == "catalog"
    assert seen[0].headers["authorization"] == "Bearer tok"
    assert seen[0].url.params["service_key"] == "checkout-api"


@pytest.mark.parametrize(("handler", "note"), [
    (lambda r: (_ for _ in ()).throw(httpx.ConnectError("x")), "catalog unreachable (ConnectError)"),
    (lambda r: httpx.Response(503), "catalog answered 503"),
    (lambda r: httpx.Response(403), "catalog answered 403"),
    (lambda r: httpx.Response(200, json={"items": [{"key": "x"}]}), "catalog answer did not match the contract"),
    (lambda r: httpx.Response(200, json={"items": []}), "service not in the catalog"),
])  # fmt: skip
def test_every_catalog_failure_is_an_answer_never_an_exception(
    tmp_path: Path, handler: Any, note: str
) -> None:
    assert asyncio.run(_client(tmp_path, handler).lookup("checkout-api")) == (None, note)


def test_no_catalog_or_no_token_is_said_plainly(tmp_path: Path) -> None:
    off = CatalogClient(None, None, tmp_path / "t.txt")
    assert asyncio.run(off.lookup("checkout-api")) == (None, "catalog not configured")
    no_token = _client(tmp_path, lambda r: httpx.Response(200), token=False)
    assert asyncio.run(no_token.lookup("checkout-api")) == (
        None, "catalog token missing (make tools-tokens)",
    )  # fmt: skip
