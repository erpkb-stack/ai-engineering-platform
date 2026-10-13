"""Phase 17a (ADR-027, review M4): every service trusts the IdP and lets ITS settings decide
whether static user tokens are accepted. A service that forgets either would silently accept
dev user tokens in production - so this reads each main.py and fails on a missing wire."""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MAINS = sorted(ROOT.glob("services/*/src/aeoi_*/main.py"))
SETTINGS = ["aeoi_agents", "aeoi_api", "aeoi_audit", "aeoi_incident", "aeoi_llm",
            "aeoi_orchestrator", "aeoi_rag", "aeoi_tools"]  # fmt: skip


def test_all_eight_services_are_checked() -> None:
    assert len(MAINS) == 8, MAINS


@pytest.mark.parametrize("main", MAINS, ids=lambda p: p.parts[-4])
def test_authenticator_gets_the_idp_and_the_static_user_switch(main: Path) -> None:
    calls = [n for n in ast.walk(ast.parse(main.read_text()))
             if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "Authenticator"]  # fmt: skip
    assert len(calls) == 1, f"{main}: expected one Authenticator(...)"
    kw = {k.arg: ast.unparse(k.value) for k in calls[0].keywords}
    assert kw.get("oidc") == "oidc_verifier(settings)", kw
    assert kw.get("static_users") == "settings.static_user_tokens", kw


@pytest.mark.parametrize("pkg", SETTINGS)
def test_every_service_settings_refuses_dev_users_in_production(
    pkg: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The production guard lives in the base class: each subclass must keep it."""
    monkeypatch.setenv("AEOI_ENV", "production")
    settings_cls = importlib.import_module(f"{pkg}.config").Settings
    with pytest.raises(ValueError, match="ADR-027"):
        settings_cls()
