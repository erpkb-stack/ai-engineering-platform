"""Phase 32a: AEOI's own services in the catalog stay TRUE - the same apps and the same calls as
the compose configuration (the graph the network policies are built from), merged on top of the
generated Northwind catalog with no key in both, every dependency a real entry."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

from aeoi_tools import schemas as s
from aeoi_tools.adapters.catalog import CatalogAdapter, load_services
from aeoi_tools.contracts import ToolUnavailableError

ROOT = Path(__file__).resolve().parents[2]


def _load(path: str, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# the same compose resolution the netpol tests use
t = _load("tests/unit/test_k8s_manifests.py", "k8s_manifests_for_catalog")
AEOI = ROOT / "services" / "tool-gateway" / "config" / "catalog-aeoi.json"
GENERATED = ROOT / "data" / "generated" / "catalog.json"
INFRA_KEYS = {"postgres": "aeoi-postgres", "kafka": "aeoi-kafka", "keycloak": "aeoi-keycloak"}


def entries() -> dict[str, dict]:  # type: ignore[type-arg]
    return {e["key"]: e for e in json.loads(AEOI.read_text())["services"]}


def test_the_services_and_their_calls_are_the_compose_graph() -> None:
    g = t.km.graph(t.resolved("switches on"))
    got = entries()
    assert set(got) == set(g.apps) | set(INFRA_KEYS.values())
    for app in g.apps:
        want = sorted(g.calls.get(app, set())) + sorted(
            INFRA_KEYS[i] for i in g.infra.get(app, {}) if i in INFRA_KEYS
        )
        assert got[app]["depends_on"] == want, app


def test_every_dependency_is_an_entry_and_the_tiers_are_the_stated_ones() -> None:
    got = entries()
    for e in got.values():
        assert set(e["depends_on"]) <= set(got), e["key"]  # the Java FK needs every target
        assert e["team"] == "team-aeoi"
    assert {k for k, e in got.items() if e["tier"] == 1} == {"api", "incident-service",
                                                              "aeoi-postgres"}  # fmt: skip


@pytest.mark.skipif(not GENERATED.exists(), reason="no generated catalog (make db-seed)")
def test_no_key_is_in_both_files() -> None:
    merged = load_services(GENERATED, AEOI)
    assert len(merged) == len(json.loads(GENERATED.read_text())["services"]) + len(entries())


def test_a_key_in_two_files_is_refused(tmp_path: Path) -> None:
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    a.write_text(json.dumps({"services": [{"key": "x1", "name": "X"}]}))
    b.write_text(json.dumps({"services": [{"key": "x1", "name": "Y"}]}))
    with pytest.raises(ToolUnavailableError, match="two files"):
        load_services(a, b)


async def test_the_adapter_answers_tier_and_blast_radius_for_a_real_service(
    tmp_path: Path,
) -> None:
    base = tmp_path / "catalog.json"
    base.write_text(json.dumps({"services": []}))
    adapter = CatalogAdapter(base, (AEOI,))
    (api,) = (await adapter.query(s.CatalogIn(service_key="api"))).items
    assert api.tier == 1 and "aeoi-postgres" in api.depends_on
    (pg,) = (await adapter.query(s.CatalogIn(service_key="aeoi-postgres"))).items
    assert {"api", "incident-service", "audit"} <= set(pg.dependents)
    team = (await adapter.query(s.CatalogIn(team="team-aeoi"))).items
    assert len(team) == 12


async def test_a_missing_extra_file_is_named(tmp_path: Path) -> None:
    base = tmp_path / "catalog.json"
    base.write_text(json.dumps({"services": []}))
    with pytest.raises(ToolUnavailableError, match=r"nope\.json"):
        await CatalogAdapter(base, (tmp_path / "nope.json",)).query(s.CatalogIn(service_key="api"))
