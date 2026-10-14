"""The HTTP catalog adapter (Phase 20, ADR-032): the same tool answers, its own identity only,
loud failures. A fake catalog speaks the contract; the real one is tested on the Mac."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from aeoi_tools import schemas as s
from aeoi_tools.adapters.catalog import CatalogAdapter
from aeoi_tools.adapters.catalog_contract import ServiceItem as ContractItem
from aeoi_tools.adapters.catalog_http import CatalogHttpAdapter
from aeoi_tools.adapters.http import make_client
from aeoi_tools.contracts import EgressBlockedError, ToolExecutionError, ToolUnavailableError

BASE = "http://localhost:8010"
ITEM = {"key": "checkout-api", "name": "Checkout Api", "type": "api", "tier": 1,
        "team": "team-checkout", "language": "java", "repository": "northwind/checkout",
        "depends_on": ["checkout-db"], "dependents": ["web-bff"]}  # fmt: skip


async def token() -> str:
    return "svc-token"


def adapter(handler: Any, allowed: tuple[str, ...] = ("localhost:8010",)) -> CatalogHttpAdapter:
    client = make_client(frozenset(allowed), timeout_s=2.0, transport=httpx.MockTransport(handler))
    return CatalogHttpAdapter(client, BASE, token)


async def test_maps_the_contract_to_the_tool_output_with_its_own_token_only() -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json={"items": [ITEM], "truncated": False})

    out = await adapter(handler).query(s.CatalogIn(team="team-checkout", name_contains="api"))
    assert out == s.CatalogOut(items=[s.ServiceItem(**ITEM)], truncated=False)
    [req] = seen
    assert req.url.path == "/v1/services"
    assert dict(req.url.params) == {"team": "team-checkout", "name_contains": "api"}  # no nulls
    assert req.headers["authorization"] == "Bearer svc-token"
    assert "x-on-behalf-of" not in req.headers  # the user's token never leaves the gateway


@pytest.mark.parametrize(("response", "error", "status", "why"), [
    (httpx.Response(503), ToolUnavailableError, None, "returned 503"),
    (httpx.Response(429), ToolUnavailableError, None, "returned 429"),
    (httpx.Response(403), ToolExecutionError, 502, "refused"),  # a scope mismatch is OUR bug
    (httpx.Response(400, json={"title": "bad", "status": 400}), ToolExecutionError, 502, "refused"),
    # a contract break is loud: an extra field, a missing field, a wrong type
    (httpx.Response(200, json={"items": [{**ITEM, "owner": "x"}], "truncated": False}),
     ToolExecutionError, 502, "contract"),
    (httpx.Response(200, json={"items": [{k: v for k, v in ITEM.items() if k != "tier"}],
                               "truncated": False}), ToolExecutionError, 502, "contract"),
    (httpx.Response(200, content=b"<html>proxy error</html>"), ToolExecutionError, 502, "contract"),
])  # fmt: skip
async def test_failures_are_loud_and_classified(
    response: httpx.Response, error: type[Exception], status: int | None, why: str
) -> None:
    with pytest.raises(error, match=why) as exc:
        await adapter(lambda _r: response).query(s.CatalogIn(service_key="checkout-api"))
    if status is not None:
        assert getattr(exc.value, "status", None) == status


async def test_unreachable_is_unavailable_not_empty() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=req)

    with pytest.raises(ToolUnavailableError, match="make run-catalog"):
        await adapter(handler).query(s.CatalogIn(service_key="checkout-api"))


async def test_a_missing_token_file_says_how_to_fix_it() -> None:
    async def no_token() -> str:
        raise FileNotFoundError("secrets/tools_catalog_token.txt")

    client = make_client(frozenset({"localhost:8010"}), timeout_s=2.0,
                         transport=httpx.MockTransport(lambda _r: httpx.Response(200)))  # fmt: skip
    with pytest.raises(ToolExecutionError, match="make tools-tokens"):
        await CatalogHttpAdapter(client, BASE, no_token).query(s.CatalogIn(team="t"))


async def test_the_catalog_host_must_be_in_the_egress_allow_list() -> None:
    hit: list[int] = []

    def handler(_r: httpx.Request) -> httpx.Response:
        hit.append(1)
        return httpx.Response(200, json={"items": [], "truncated": False})

    with pytest.raises(EgressBlockedError):
        await adapter(handler, allowed=("localhost:8004",)).query(s.CatalogIn(team="t"))
    assert hit == []  # refused BEFORE a connection


def test_the_contract_item_has_exactly_the_tool_item_fields() -> None:
    """The adapter builds the tool item from the contract item field by field: a field added to
    one side only must fail here, not as a runtime error in an investigation."""
    tool_fields = set(s.ServiceItem.model_fields) - {"evidence_id"}  # set by the gateway
    assert set(ContractItem.model_fields) == tool_fields


async def test_the_json_and_http_sources_agree_on_the_same_data(tmp_path: Path) -> None:
    """The fake catalog answers from the same JSON the file adapter reads - with the contract's
    rules (sort by tier then key, direct dependents sorted). The real Java service is held to
    the same answers by the Mac integration test."""
    services = [
        {"key": "b-api", "name": "B Api", "team": "t1", "type": "api", "tier": 2,
         "language": "go", "repository": "r/b", "depends_on": ["a-db"]},
        {"key": "a-db", "name": "A Db", "team": "t1", "type": "database", "tier": 1,
         "language": None, "repository": "r/a", "depends_on": []},
        {"key": "c-web", "name": "C Web", "team": "t2", "type": "web", "tier": 2,
         "language": "ts", "repository": "r/c", "depends_on": ["b-api", "a-db"]},
    ]  # fmt: skip
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps({"seed": 1, "services": services}))

    def fake_catalog(req: httpx.Request) -> httpx.Response:
        q = dict(req.url.params)
        if "service_key" in q:
            found = [x for x in services if x["key"] == q["service_key"]]
        else:
            needle = q.get("name_contains", "").lower()
            found = [x for x in services if ("team" not in q or x["team"] == q["team"])
                     and (not needle or needle in x["name"].lower() or needle in x["key"])]  # fmt: skip
        found.sort(key=lambda x: (x["tier"], x["key"]))
        items = [{**x, "dependents": sorted(y["key"] for y in services
                                            if x["key"] in y["depends_on"])} for x in found]  # fmt: skip
        return httpx.Response(200, json={"items": items, "truncated": False})

    http, file = adapter(fake_catalog), CatalogAdapter(path)
    for q in (s.CatalogIn(service_key="a-db"), s.CatalogIn(team="t1"),
              s.CatalogIn(name_contains="API"), s.CatalogIn(team="t1", name_contains="db"),
              s.CatalogIn(service_key="nope")):  # fmt: skip
        assert await http.query(q) == await file.query(q), q


def test_an_empty_team_is_invalid_not_a_missing_filter() -> None:
    """Both sources must answer the same: the JSON adapter matched team "" to nothing, the HTTP
    adapter dropped it and searched every team (review L3). Now it never reaches either."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        s.CatalogIn(team="", name_contains="api")
