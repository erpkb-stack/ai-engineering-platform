from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from aeoi_llm.config import RoutingConfig, load_routing

CONFIG = Path(__file__).resolve().parents[1] / "config"


@pytest.mark.parametrize(
    "name", ["routing.yaml", "routing.local.yaml", "routing.test.yaml", "routing.load.yaml"]
)
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


# ---------------------------------------------------------------- Phase 21 (ADR-033)
OLLAMA_ENV = "AEOI_LLM_PROVIDER_OLLAMA_BASE_URL"


@pytest.mark.parametrize("name", ["routing.yaml", "routing.local.yaml"])
def test_the_container_reaches_the_native_ollama_by_env(name: str) -> None:
    """docker-compose.yml sets exactly this variable; the provider stays LOCAL (no redaction is
    skipped for a remote host)."""
    url = "http://host.docker.internal:11434/v1"
    cfg = load_routing(CONFIG / name, env={OLLAMA_ENV: url})
    assert cfg.providers["ollama"].base_url == url
    assert cfg.providers["ollama"].hosted is False
    # without the variable the file's value is used (host runs are unchanged)
    assert (
        load_routing(CONFIG / name, env={})
        .providers["ollama"]
        .base_url.startswith("http://localhost:11434")
    )


@pytest.mark.parametrize("url", [
    "http://ollama.example.com:11434/v1",  # another machine: prompts were never redacted
    "http://10.0.0.5:11434/v1",            # a LAN host is still another machine
    "http://localhost.evil.test/v1",       # a name that only STARTS like a local one
    "file:///etc/passwd",                   # not http(s)
    "host.docker.internal:11434",           # no scheme: urlsplit sees no host
])  # fmt: skip
def test_a_local_provider_cannot_be_pointed_at_another_machine(url: str) -> None:
    with pytest.raises(ValueError, match="may only point to"):
        load_routing(CONFIG / "routing.yaml", env={OLLAMA_ENV: url})


def test_a_hosted_provider_url_cannot_change_by_env() -> None:
    """Otherwise one env var sends every prompt AND the API key to any host."""
    with pytest.raises(ValueError, match="hosted provider"):
        load_routing(CONFIG / "routing.yaml",
                     env={"AEOI_LLM_PROVIDER_ANTHROPIC_BASE_URL": "http://localhost:9/v1"})  # fmt: skip


def test_an_override_for_a_provider_the_policy_does_not_have_is_ignored() -> None:
    """compose always sets the Ollama variable; routing.test.yaml (fakes) has no Ollama."""
    cfg = load_routing(CONFIG / "routing.test.yaml", env={OLLAMA_ENV: "http://localhost:1/v1"})
    assert "ollama" not in cfg.providers


def test_simulated_latency_is_refused_unless_every_provider_is_fake() -> None:
    """Phase 27: a load-test wait must never slow down, or pass for, a real model call."""
    import yaml

    load = yaml.safe_load((CONFIG / "routing.load.yaml").read_text())
    assert RoutingConfig.model_validate(load).simulated_latency_ms["postmortem"] > 0
    real = yaml.safe_load((CONFIG / "routing.yaml").read_text())
    real["simulated_latency_ms"] = {"default": 100}
    with pytest.raises(ValueError, match="fake providers only"):
        RoutingConfig.model_validate(real)
    for shipped in ("routing.yaml", "routing.local.yaml", "routing.test.yaml"):
        assert not load_routing(CONFIG / shipped).simulated_latency_ms, shipped
