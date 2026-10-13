"""Phase 17b: the console's types come from frontend/openapi.json - it must match the api."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _export():  # type: ignore[no-untyped-def]
    s = importlib.util.spec_from_file_location("export_openapi", ROOT / "scripts/export_openapi.py")
    assert s and s.loader
    m = importlib.util.module_from_spec(s)
    s.loader.exec_module(m)
    return m


def test_the_committed_contract_is_the_api_contract() -> None:
    committed = (ROOT / "frontend/openapi.json").read_text()
    assert committed == _export().render(), "frontend/openapi.json is stale: run `make openapi`"


def test_the_browser_contract_has_no_internal_routes() -> None:
    paths = json.loads((ROOT / "frontend/openapi.json").read_text())["paths"]
    assert paths and all(p.startswith("/api/v1/") for p in paths)
    assert "/api/v1/approvals" in paths  # the inbox
