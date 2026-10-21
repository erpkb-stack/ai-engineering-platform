"""Phase 23c: the catalog latency measurement counts what it claims (fake catalog, real HTTP)."""

from __future__ import annotations

import importlib.util
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
TOKEN = "test-token-not-a-secret"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "catalog_latency", ROOT / "scripts/catalog_latency.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["catalog_latency"] = mod
    spec.loader.exec_module(mod)
    return mod


def _catalog() -> dict[str, Any]:
    return {"seed": 1, "services": [{"key": f"svc-{i}", "team": f"team-{i % 3}"} for i in range(5)]}


def test_queries_cover_keys_teams_and_names_round_robin() -> None:
    cl = _load()
    qs = cl.queries(_catalog(), 12)
    assert qs[:5] == [f"service_key=svc-{i}" for i in range(5)]
    assert qs[5:8] == ["team=team-0", "team=team-1", "team=team-2"]
    assert qs[8:11] == ["name_contains=api", "name_contains=worker", "name_contains=service"]
    assert qs[11] == "service_key=svc-0"  # wraps around


def test_a_run_counts_slow_and_failed_answers_and_never_writes_the_token(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cl = _load()
    seen: list[str] = []

    class Catalog(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            seen.append(self.headers.get("Authorization", ""))
            n = len(seen)
            if n == 3:
                time.sleep(0.25)  # over the (test) deadline
            code = 503 if n == 7 else 200
            body = b'{"items": []}'
            self.send_response(code)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_: object) -> None:
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Catalog)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    (tmp_path / "token.txt").write_text(TOKEN + "\n")
    (tmp_path / "catalog.json").write_text(json.dumps(_catalog()))
    try:
        rc = cl.main([
            "--label", "agent-on", "--n", "20", "--url", f"http://127.0.0.1:{srv.server_address[1]}",
            "--deadline-ms", "200", "--token-file", str(tmp_path / "token.txt"),
            "--catalog", str(tmp_path / "catalog.json"), "--out-dir", str(tmp_path / "out"),
        ])  # fmt: skip
    finally:
        srv.shutdown()
    assert rc == 1  # one 503: not every request succeeded
    assert seen == [f"Bearer {TOKEN}"] * 20
    (out,) = (tmp_path / "out").glob("catalog-latency-agent-on-*.json")
    result = json.loads(out.read_text())
    assert result["requests"] == 20
    assert result["outcomes"] == {"http:200": 19, "http:503": 1}
    assert result["over_deadline"] == 1
    assert len(result["first_requests_ms"]) == 10 and result["first_requests_ms"][2] >= 250
    assert result["ok_ms"]["max"] >= 250
    assert TOKEN not in out.read_text() and TOKEN not in capsys.readouterr().out


def test_missing_token_or_catalog_names_the_make_target(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cl = _load()
    assert cl.main(["--label", "x", "--token-file", str(tmp_path / "nope.txt")]) == 2
    assert "make tools-tokens" in capsys.readouterr().out
