"""Phase 20 (ADR-032): the service-catalog contract is services/service-catalog/openapi.yaml.

Python side of the drift check: the committed client models are exactly what the generator
makes from the contract today. (Java side: CatalogContractTest compares live responses with the
same file.)"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import yaml

ROOT = Path(__file__).resolve().parents[2]
SPEC = ROOT / "services" / "service-catalog" / "openapi.yaml"


def _generator() -> ModuleType:
    s = importlib.util.spec_from_file_location(
        "catalog_contract", ROOT / "scripts/catalog_contract.py"
    )
    assert s and s.loader
    m = importlib.util.module_from_spec(s)
    s.loader.exec_module(m)
    return m


def test_the_committed_models_are_generated_from_the_contract() -> None:
    gen = _generator()
    assert gen.TARGET.read_text() == gen.generate(), "stale: run `make catalog-contract`"


def test_the_check_notices_a_contract_change(tmp_path: Path) -> None:
    """The drift check must see a change in the contract (a guard that cannot fail proves nothing)."""
    spec = yaml.safe_load(SPEC.read_text())
    spec["components"]["schemas"]["ServiceItem"]["properties"]["owner"] = {"type": "string"}
    changed = tmp_path / "openapi.yaml"
    changed.write_text(yaml.safe_dump(spec))
    gen = _generator()
    assert gen.generate(changed) != gen.TARGET.read_text()


def test_every_item_is_closed_and_every_list_is_bounded() -> None:
    """The contract rules the generated models rely on: no unknown fields, no unbounded lists."""
    schemas = yaml.safe_load(SPEC.read_text())["components"]["schemas"]
    for name in ("ServiceList", "ServiceItem"):
        assert schemas[name]["additionalProperties"] is False, name
        for field, prop in schemas[name]["properties"].items():
            if prop.get("type") == "array":
                assert "maxItems" in prop, f"{name}.{field}"
