"""Compare preflight must judge the model that will REALLY answer (Mac finding, Phase 8)."""

from aeoi_orchestrator.__main__ import _usable_chains


def _gateway(anthropic_configured: bool) -> dict:
    return {
        "routes": {
            "fast": {"chain": ["claude-haiku-4-5-20251001", "llama3.2:3b"]},
            "local": {"chain": ["llama3.2:3b"]},
        },
        "models": {"claude-haiku-4-5-20251001": "anthropic", "llama3.2:3b": "ollama"},
        "providers": {
            "anthropic": {"configured": anthropic_configured},
            "ollama": {"configured": True},
        },
    }


def test_no_key_means_fast_is_really_llama() -> None:
    usable = _usable_chains(_gateway(False), ["local", "fast"])
    assert usable["fast"] == ["llama3.2:3b"]
    assert {u[0] for u in usable.values()} == {"llama3.2:3b"}  # -> compare refuses


def test_with_key_fast_is_claude() -> None:
    usable = _usable_chains(_gateway(True), ["local", "fast"])
    assert usable["fast"][0] == "claude-haiku-4-5-20251001"
    assert len({u[0] for u in usable.values()}) == 2


def test_facts_equal_needs_facts() -> None:
    """Mac finding: two runs that never reached the agent printed 'facts identical: True'."""
    from aeoi_orchestrator.__main__ import _facts_equal

    assert _facts_equal([{"facts": ["a"]}, {"facts": ["a"]}])
    assert not _facts_equal([{"facts": ["a"]}, {"facts": ["b"]}])
    assert not _facts_equal([{"status": "FAILED"}, {"status": "FAILED"}])
    assert not _facts_equal([{"facts": []}, {"facts": []}])
