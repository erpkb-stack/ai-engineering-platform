from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from aeoi_llm.config import RoutingConfig, load_routing

CONFIG = Path(__file__).resolve().parents[1] / "config"


@pytest.mark.parametrize("name", ["routing.yaml", "routing.local.yaml", "routing.test.yaml"])
def test_shipped_policies_are_valid(name: str) -> None:
    cfg = load_routing(CONFIG / name)
    assert "reasoning" in cfg.routes
    assert cfg.routes["embed"].operation == "embed"


def test_local_policy_never_uses_a_hosted_provider() -> None:
    cfg = load_routing(CONFIG / "routing.local.yaml")
    assert all(not p.hosted for p in cfg.providers.values())


def test_local_route_has_no_hosted_fallback() -> None:
    """'local' means the data must not leave the machine - a hosted fallback would break that."""
    cfg = load_routing(CONFIG / "routing.yaml")
    for model in cfg.chain("local"):
        assert not cfg.providers[cfg.models[model].provider].hosted


def _raw() -> dict:
    return yaml.safe_load((CONFIG / "routing.test.yaml").read_text())


def test_embed_route_with_fallback_is_rejected() -> None:
    raw = _raw()
    raw["routes"]["embed"]["fallbacks"] = ["fake-small"]
    with pytest.raises(ValidationError, match="must not have fallbacks"):
        RoutingConfig.model_validate(raw)


def test_unknown_model_in_route_is_rejected() -> None:
    raw = _raw()
    raw["routes"]["reasoning"]["fallbacks"] = ["does-not-exist"]
    with pytest.raises(ValidationError, match="unknown model"):
        RoutingConfig.model_validate(raw)


def test_unknown_key_is_rejected() -> None:
    raw = _raw()
    raw["routes"]["reasoning"]["fallback"] = ["fake-small"]  # typo: singular
    with pytest.raises(ValidationError):
        RoutingConfig.model_validate(raw)
