"""Contract lint: every tool input is BOUNDED, closed, and every tool is fully specified.
A new tool that forgets a max_length fails here before it ever reaches review."""

from __future__ import annotations

import typing
from pathlib import Path
from types import UnionType
from typing import Any, get_args, get_origin

import pytest
from pydantic import BaseModel
from pydantic.fields import FieldInfo

from aeoi_tools import schemas as s
from aeoi_tools.adapters.catalog import CatalogAdapter
from aeoi_tools.contracts import SideEffect, ToolSpec
from aeoi_tools.policy import PolicyConfig
from aeoi_tools.registry import build_registry, describe

AGENTS = Path(__file__).resolve().parents[1] / "config" / "agents.yaml"
EXPECTED = {
    "query_service_catalog", "search_logs", "query_metrics", "get_commit", "get_pull_request",
    "search_repository", "get_deployment", "get_config_diff", "search_runbooks", "search_docs",
    "search_incidents", "rollback_deployment",
}  # fmt: skip


def registry() -> dict[str, ToolSpec]:
    return build_registry(CatalogAdapter(Path("/nonexistent")), None, None)  # type: ignore[arg-type]


def _constraints(info: FieldInfo, name: str) -> set[str]:
    out = set()
    for m in info.metadata:
        for attr in ("max_length", "le", "lt", "pattern"):
            if getattr(m, attr, None) is not None:
                out.add(attr)
    return out


def _leaf_types(tp: Any) -> list[Any]:
    origin = get_origin(tp)
    if origin in (typing.Union, UnionType):
        return [t for a in get_args(tp) for t in _leaf_types(a)]
    return [tp]


def _problems(model: type[BaseModel], seen: set[type] | None = None) -> list[str]:
    seen = seen or set()
    if model in seen:
        return []
    seen.add(model)
    found = []
    if model.model_config.get("extra") != "forbid":
        found.append(f"{model.__name__}: extra fields not forbidden")
    for name, info in model.model_fields.items():
        cons = _constraints(info, name)
        for tp in _leaf_types(info.annotation):
            origin = get_origin(tp)
            if tp is str and not ({"max_length", "pattern"} & cons):
                found.append(f"{model.__name__}.{name}: unbounded str")
            if tp is int and not ({"le", "lt"} & cons):
                found.append(f"{model.__name__}.{name}: unbounded int")
            if origin is list and "max_length" not in cons:
                found.append(f"{model.__name__}.{name}: unbounded list")
            if origin is dict:
                found.append(f"{model.__name__}.{name}: free-form dict input")
            if isinstance(tp, type) and issubclass(tp, BaseModel):
                found += _problems(tp, seen)
    return found


def test_registry_has_exactly_the_planned_tools() -> None:
    assert set(registry()) == EXPECTED


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_every_input_is_bounded_and_closed(name: str) -> None:
    assert _problems(registry()[name].input_model) == []


def test_lint_catches_an_unbounded_field() -> None:
    """The lint itself must work (a lint that passes everything proves nothing)."""

    class Bad(s.In):
        query: str
        tags: list[str]

    assert len(_problems(Bad)) == 2


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_outputs_have_items_with_evidence_ids(name: str) -> None:
    out = registry()[name].output_model
    assert "items" in out.model_fields and "truncated" in out.model_fields
    item = get_args(out.model_fields["items"].annotation)[0]
    assert "evidence_id" in item.model_fields


def test_only_rollback_is_consequential_and_it_never_retries() -> None:
    reg = registry()
    assert {n for n, t in reg.items() if t.side_effect is not SideEffect.READ} == {
        "rollback_deployment"
    }
    assert reg["rollback_deployment"].max_attempts == 1
    assert not reg["rollback_deployment"].idempotent


def test_spec_refuses_incomplete_tools() -> None:
    good = registry()["get_commit"]
    with pytest.raises(ValueError, match="permission"):
        ToolSpec(**{**good.__dict__, "permissions": frozenset()})
    with pytest.raises(ValueError, match="only READ"):
        ToolSpec(**{**good.__dict__, "side_effect": SideEffect.WRITE, "max_attempts": 2})
    with pytest.raises(ValueError, match="audit_fields"):
        ToolSpec(**{**good.__dict__, "audit_fields": ("nope",)})


def test_agents_yaml_matches_registry_and_architecture() -> None:
    cfg = PolicyConfig.load(AGENTS, set(registry()))
    assert cfg.agents["log_analysis"].tools == {"search_logs"}
    assert cfg.agents["code_intelligence"].tools == {
        "get_commit", "get_pull_request", "search_repository"
    }  # fmt: skip
    # the ONLY agent that may hold a consequential tool
    holders = {a for a, p in cfg.agents.items() if "rollback_deployment" in p.tools}
    assert holders == {"action_executor"}


def test_agents_yaml_with_unknown_tool_fails_at_boot(tmp_path: Path) -> None:
    bad = tmp_path / "agents.yaml"
    bad.write_text("agents:\n  x:\n    tools: [drop_database]\n")
    with pytest.raises(ValueError, match="unknown tools"):
        PolicyConfig.load(bad, set(registry()))


def test_describe_exposes_json_schemas() -> None:
    d = describe(registry()["search_logs"])
    assert d["input_schema"]["additionalProperties"] is False
    assert d["input_schema"]["properties"]["limit"]["maximum"] == 200
