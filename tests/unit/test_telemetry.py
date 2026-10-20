"""Phase 23a (ADR-035): telemetry is safe, continuous and quiet.

The real code paths run in a subprocess (tests/unit/telemetry_probe.py): OTel providers are
process-global, and switching them on here would instrument every other test.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from functools import cache
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
PROBE = ROOT / "tests" / "unit" / "telemetry_probe.py"
sys.path.insert(0, str(PROBE.parent))
import telemetry_probe as tp  # noqa: E402


@cache
def run(mode: str = "normal") -> dict[str, Any]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("OTEL_", "AEOI_OTEL"))}
    r = subprocess.run(  # noqa: S603 - our own script, fixed args
        [sys.executable, str(PROBE), mode], cwd=ROOT, env=env,
        capture_output=True, text=True, timeout=120, check=False,
    )  # fmt: skip
    assert r.returncode == 0, r.stderr[-2000:]
    out: dict[str, Any] = json.loads(r.stdout.strip().splitlines()[-1])
    out["_stdout"] = r.stdout
    return out


def stdout_without_tracebacks(out: dict[str, Any]) -> str:
    """The stdout JSON log lines minus their `exception` block: a traceback's exception VALUE
    stays on stdout (unchanged since Phase 4, for whoever reads the pod log) and is never
    exported (to_otel drops the key; test below). Recorded as an open item in phase-23.md."""
    kept = []
    for line in out["_stdout"].splitlines()[:-1]:
        try:
            doc = json.loads(line)
        except ValueError:
            kept.append(line)
            continue
        doc.pop("exception", None)
        kept.append(json.dumps(doc))
    return "\n".join(kept)


def test_a_traceback_never_reaches_the_exported_logs() -> None:
    (line,) = [entry for entry in run()["logs"] if entry["body"] == "unhandled_error"]
    assert "exception" not in line["attributes"]
    assert tp.SECRET_TEXT not in json.dumps(line)


def spans(kind: str | None = None) -> list[dict[str, Any]]:
    return [s for s in run()["spans"] if kind is None or s["kind"] == kind]


# ---------------------------------------------------------------- nothing secret leaves


def test_no_planted_secret_reaches_any_span_log_or_stdout() -> None:
    out = run()
    exported = json.dumps({"spans": out["spans"], "logs": out["logs"]})
    for planted in (tp.SECRET_BEARER, tp.SECRET_PW, tp.SECRET_TEXT, tp.SECRET_QUERY):
        assert planted not in exported, planted
        assert planted not in stdout_without_tracebacks(out), planted
    assert "authorization" not in exported.lower()
    assert out["boom_status"] == 500  # the planted row text was IN the exception


def test_an_exception_keeps_its_type_but_not_its_message_or_trace() -> None:
    (db,) = [s for s in spans() if s["name"] == "db op"]
    assert db["status"] == {"code": "ERROR", "description": None}
    (ev,) = db["events"]
    assert ev["name"] == "exception"
    assert ev["attributes"]["exception.type"] == "RuntimeError"
    assert "exception.message" not in ev["attributes"]
    assert "exception.stacktrace" not in ev["attributes"]


def test_the_public_edge_ignores_a_client_named_trace_but_internal_calls_continue() -> None:
    out = run()
    assert out["edge_status"] == out["trusted_status"] == 200
    (edge,) = [s for s in spans("SERVER") if s["name"] == "GET /api/v1/edge"]
    (inner,) = [s for s in spans("SERVER") if s["name"] == "GET /internal/v1/trusted"]
    assert edge["trace_id"] != tp.FOREIGN_TRACE and edge["parent"] is None
    assert inner["trace_id"] == tp.FOREIGN_TRACE  # orchestrator -> api STS keeps the trace


def test_the_real_engine_run_continues_the_stored_trace() -> None:
    out = run()
    runs = [s for s in spans() if s["name"] == "investigation.run"
            and s["attributes"].get("aeoi.investigation_id") == out["engine_investigation"]]  # fmt: skip
    assert len(runs) == 1
    assert runs[0]["trace_id"] == tp.ENGINE_STORED.split("-")[1]
    assert runs[0]["parent"] == tp.ENGINE_STORED.split("-")[2]
    assert runs[0]["attributes"]["aeoi.resumed_from_checkpoint"] == "True"
    (line,) = [entry for entry in out["logs"] if entry["body"] == "engine_node_ran"]
    assert line["attributes"]["correlation_id"] == "corr-engine-0005"
    assert line["trace_id"] == runs[0]["trace_id"]


def test_query_strings_are_removed_on_server_and_client_spans() -> None:
    (server,) = [s for s in spans("SERVER") if s["name"] == "POST /echo"]
    assert server["attributes"]["url.query"] == "[query removed]"
    (client,) = spans("CLIENT")
    assert client["attributes"]["url.full"] == "http://127.0.0.1:9/thing"


def test_log_lines_are_redacted_before_export_and_carry_the_trace() -> None:
    (line,) = [entry for entry in run()["logs"] if entry["body"] == "echo_called"]
    assert line["attributes"]["password"] == "[REDACTED]"
    assert line["attributes"]["correlation_id"] == "corr-probe-0001"
    (server,) = [s for s in spans("SERVER") if s["name"] == "POST /echo"]
    assert line["trace_id"] == server["trace_id"]  # Loki line -> Tempo trace in one click


# ---------------------------------------------------------------- one trace


def test_a_downstream_call_is_a_child_of_the_request() -> None:
    (server,) = [s for s in spans("SERVER") if s["name"] == "POST /echo"]
    (client,) = spans("CLIENT")
    assert client["trace_id"] == server["trace_id"]
    assert server["attributes"]["aeoi.correlation_id"] == "corr-probe-0001"


def test_work_stored_for_later_carries_the_request_trace() -> None:
    out = run()
    (server,) = [s for s in spans("SERVER") if s["name"] == "POST /echo"]
    assert out["traceparent_in_request"].split("-")[1] == server["trace_id"]
    assert out["outbox_headers"]["traceparent"] == out["traceparent_in_request"]
    assert out["outbox_headers"]["correlation_id"] == "x"


def test_a_resumed_run_continues_the_stored_trace_and_correlation_id() -> None:
    (run_span,) = [
        s
        for s in spans()
        if s["name"] == "investigation.run" and "aeoi.investigation_id" not in s["attributes"]
    ]  # the probe's own block
    assert run_span["trace_id"] == "0af7651916cd43dd8448eb211c80319c"
    assert run_span["parent"] == "b7ad6b7169203331"
    (line,) = [entry for entry in run()["logs"] if entry["body"] == "resumed_step"]
    assert line["attributes"]["correlation_id"] == "corr-stored-0002"  # was missing before 23a
    assert line["trace_id"] == run_span["trace_id"]


def test_a_kafka_consumer_continues_the_producer_trace() -> None:
    (consumer,) = spans("CONSUMER")
    assert consumer["trace_id"] == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert consumer["parent"] == "00f067aa0ba902b7"
    assert consumer["attributes"]["messaging.destination.name"] == "incident.lifecycle"
    assert run()["consumer_trace"] == [consumer["trace_id"]]  # the handler ran inside it
    (line,) = [entry for entry in run()["logs"] if entry["body"] == "consumed"]
    assert line["attributes"]["correlation_id"] == "corr-kafka-0003"


# ---------------------------------------------------------------- quiet


def test_health_probes_and_background_pings_make_no_spans() -> None:
    out = run()
    assert out["health_status"] == 200 and out["background"] == "refused"
    names = [s["name"] for s in out["spans"]]
    assert not any("health" in n for n in names)
    assert len(spans("CLIENT")) == 1  # the root ping was dropped, the request's call kept
    assert not any(n.endswith((" http receive", " http send")) for n in names)


def test_request_metrics_use_the_stable_names_the_dashboards_query() -> None:
    assert "http.server.request.duration" in run()["metrics"]


def test_a_dead_collector_never_fails_a_request() -> None:
    out = run("failing")
    assert out["echo_status"] == 200
    assert out["spans"] == []


# ---------------------------------------------------------------- unit pieces


def test_off_unless_the_endpoint_is_set(monkeypatch: pytest.MonkeyPatch) -> None:
    from aeoi_observability import telemetry

    monkeypatch.delenv(telemetry.ENDPOINT_ENV, raising=False)
    monkeypatch.setattr(telemetry, "_ACTIVE", None)
    assert telemetry.setup_telemetry("x") is None
    assert telemetry.current_traceparent() is None  # no span here: nothing to store


def test_the_root_sampler_drops_only_client_and_producer_roots() -> None:
    from opentelemetry.sdk.trace.sampling import Decision
    from opentelemetry.trace import SpanKind

    from aeoi_observability.telemetry import DropRootClients

    s = DropRootClients()
    for kind, want in ((SpanKind.CLIENT, Decision.DROP), (SpanKind.PRODUCER, Decision.DROP),
                       (SpanKind.SERVER, Decision.RECORD_AND_SAMPLE),
                       (SpanKind.CONSUMER, Decision.RECORD_AND_SAMPLE),
                       (SpanKind.INTERNAL, Decision.RECORD_AND_SAMPLE)):  # fmt: skip
        assert s.should_sample(None, 1, "n", kind).decision == want


def test_continue_from_without_a_stored_context_starts_clean() -> None:
    from aeoi_common.correlation import get_correlation_id
    from aeoi_observability.telemetry import continue_from

    with continue_from(None, None):
        assert get_correlation_id() is None
    with continue_from(None, "corr-only-0004"):
        assert get_correlation_id() == "corr-only-0004"
    assert get_correlation_id() is None  # restored afterwards


# ---------------------------------------------------------------- the stack config and dashboards

import re as _re  # noqa: E402

import yaml as _yaml  # noqa: E402

OBS = ROOT / "infrastructure" / "observability"


def _load_script(name: str) -> Any:
    import importlib.util

    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_the_collector_strips_headers_and_tokens_before_any_backend() -> None:
    cfg = _yaml.safe_load((OBS / "otel-collector.yaml").read_text())
    pipes = cfg["service"]["pipelines"]
    for signal in ("traces", "logs"):
        procs = pipes[signal]["processors"]
        assert procs[0] == "memory_limiter" and procs[-1] == "batch"
        assert procs.index("attributes/strip") < procs.index("redaction")
    strip = cfg["processors"]["attributes/strip"]["actions"]
    assert {"key": "url.query", "action": "delete"} in strip
    assert any(
        a.get("pattern", "").startswith("^http\\.(request|response)\\.header") for a in strip
    )
    blocked = cfg["processors"]["redaction"]["blocked_values"]
    assert _re.search(blocked[0], tp.SECRET_BEARER)  # the JWT pattern matches a real-shaped JWT
    assert cfg["receivers"]["otlp"]["protocols"] == {"http": {"endpoint": "0.0.0.0:4318"}}


def test_every_dashboard_answers_a_question_and_uses_known_sources() -> None:
    ds = _yaml.safe_load((OBS / "grafana/provisioning/datasources/datasources.yaml").read_text())
    uids = {d["uid"] for d in ds["datasources"]}
    files = sorted((OBS / "grafana/dashboards").glob("*.json"))
    assert [f.name for f in files] == [
        "investigation-funnel.json",
        "llm-cost.json",
        "platform-health.json",
    ]
    for f in files:
        d = json.loads(f.read_text())
        assert d["description"].endswith("?") or "?" in d["description"], f.name  # the question
        assert d["editable"] is False
        for p in d["panels"]:
            assert p["datasource"]["uid"] in uids, (f.name, p["title"])
            for t in p["targets"]:
                assert t["datasource"]["uid"] in uids
                expr = t.get("expr", "")
                for metric in _re.findall(
                    r"\b(http_[a-z_]+)\s*[\[{]", expr
                ):  # a metric, not a label
                    assert metric.startswith("http_server_request_duration_seconds_"), metric


def test_dashboards_read_only_the_columns_grafana_may_read() -> None:
    """obs_reader gets SELECT on named COLUMNS (migration 0026); a dashboard naming any other
    column of those tables would fail on the Mac - and would mean widening the grant."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "m0026", ROOT / "libs/db/alembic/versions/0026_investigation_trace_and_obs_reader.py"
    )
    assert spec and spec.loader
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)
    from aeoi_db.models.llm import ModelUsage
    from aeoi_db.models.orchestrator import Investigation

    tables = {"orchestrator.investigations": Investigation, "llm.model_usage": ModelUsage}
    for f in (OBS / "grafana/dashboards").glob("*.json"):
        for p in json.loads(f.read_text())["panels"]:
            for t in p["targets"]:
                q = t.get("rawSql")
                if not q:
                    continue
                used = [name for name in tables if name in q]
                assert used, (f.name, p["title"])
                for name in used:
                    forbidden = {c.name for c in tables[name].__table__.columns} - set(
                        mig.READABLE[name]
                    )
                    words = set(_re.findall(r"[a-z_]+", q))
                    assert not (forbidden & words), (f.name, p["title"], forbidden & words)


def test_grafana_logs_in_with_the_narrow_login_and_never_phones_home() -> None:
    ds = _yaml.safe_load((OBS / "grafana/provisioning/datasources/datasources.yaml").read_text())
    (pg,) = [d for d in ds["datasources"] if d["uid"] == "postgres"]
    assert pg["user"] == "grafana_ro"
    assert pg["secureJsonData"]["password"].startswith("$__file{/run/secrets/")
    from aeoi_db.users import SERVICE_LOGINS

    assert SERVICE_LOGINS["grafana_ro"] == "obs_reader"
    compose = _yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    env = compose["services"]["grafana"]["environment"]
    for key in ("GF_ANALYTICS_REPORTING_ENABLED", "GF_ANALYTICS_CHECK_FOR_UPDATES",
                "GF_ANALYTICS_CHECK_FOR_PLUGIN_UPDATES", "GF_NEWS_NEWS_FEED_ENABLED",
                "GF_AUTH_ANONYMOUS_ENABLED", "GF_USERS_ALLOW_SIGN_UP"):  # fmt: skip
        assert env[key] == "false", key
    assert env["GF_PLUGINS_PREINSTALL_DISABLED"] == "true"
    assert env["GF_LOG_MODE"] == "console"  # read-only root: no log file
    assert "GF_SECURITY_ADMIN_PASSWORD" not in env  # only the __FILE form


def test_the_stack_binds_to_loopback_and_drops_capabilities() -> None:
    compose = _yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    for name in ("otel-collector", "tempo", "prometheus", "loki", "grafana"):
        svc = compose["services"][name]
        assert svc["profiles"] == ["observability"], name
        assert svc.get("cap_drop") == ["ALL"] and svc.get("read_only") is True, name
        for port in svc.get("ports", []):
            assert port.startswith("127.0.0.1:"), (name, port)
        assert "memory" in svc["deploy"]["resources"]["limits"], name
    # every app exports to the SAME address, only when .env switches it on
    assert (
        compose["x-app-env"]["AEOI_OTEL_ENDPOINT"]
        == "${AEOI_OTEL_ENDPOINT:+http://otel-collector:4318}"
    )
    assert "otel-collector" in compose["x-app-env"]["NO_PROXY"]


def test_obs_smoke_reads_services_and_runs_from_a_tempo_trace() -> None:
    smoke = _load_script("obs_smoke")
    trace = {"batches": [
        {"resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "api"}}]},
         "scopeSpans": [{"spans": [{"name": "POST /x"}]}]},
        {"resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "orchestrator"}}]},
         "scopeSpans": [{"spans": [{"name": "investigation.run"}, {"name": "investigation.run"}]}]},
    ]}  # fmt: skip
    assert smoke.services_in(trace) == {"api", "orchestrator"}
    assert smoke.spans_named(trace, "investigation.run") == 2
    assert smoke.leaks(["abc", "xyz SECRET"], ["SECRET", "nope"]) == ["secret #0"]
    assert smoke.count_spans(trace) == 3


def test_obs_smoke_searches_for_every_service_token_without_printing_them(tmp_path: Path) -> None:
    smoke = _load_script("obs_smoke")
    (tmp_path / "a_token.txt").write_text("t" * 40 + "\n")
    (tmp_path / "short_token.txt").write_text("x")
    (tmp_path / "pw_password.txt").write_text("p" * 40)  # not a token: not used
    assert smoke.service_tokens(str(tmp_path)) == ["t" * 40]
    assert {"api", "orchestrator", "agents", "tool-gateway", "rag",
                              "llm-gateway", "incident-service"} == smoke.REQUIRED  # fmt: skip


def test_obs_smoke_readiness_treats_a_closed_or_refused_port_as_not_ready() -> None:
    """Mac fix2: Docker's port proxy accepted, then closed, while Tempo started -> crash."""
    import socket
    import threading

    smoke = _load_script("obs_smoke")
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen()
    port = srv.getsockname()[1]

    def accept_and_close() -> None:
        conn, _ = srv.accept()
        conn.close()

    threading.Thread(target=accept_and_close, daemon=True).start()
    assert smoke.status(f"http://127.0.0.1:{port}/ready") == 0  # closed without a response
    srv.close()
    assert smoke.status(f"http://127.0.0.1:{port}/ready") == 0  # refused


def test_obs_smoke_waits_for_backends_that_answer_like_the_real_ones(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mac fix3: Tempo/Loki answer /ready with plain text "ready" -> JSONDecodeError. Runs the
    REAL wait_stack() and ready() against a local server: text, JSON, a 503 "starting" text,
    and a closed connection all behave as the real backends do."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    smoke = _load_script("obs_smoke")
    calls: dict[str, int] = {}

    class Backend(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            n = calls[self.path] = calls.get(self.path, 0) + 1
            if self.path == "/drop" and n == 1:  # Docker's proxy while the container starts
                self.close_connection = True
                return
            if self.path == "/loki" and n == 1:  # Loki: 503 + text while the ring forms
                body, code, kind = b"Ingester not ready: waiting for 15s", 503, "text/plain"
            elif self.path == "/grafana":
                body, code, kind = b'{"database": "ok"}', 200, "application/json"
            else:
                body, code, kind = b"ready\n", 200, "text/plain"
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_: object) -> None:
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Backend)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        assert smoke.http("GET", f"{base}/tempo") == (200, "ready\n")
        assert smoke.http("GET", f"{base}/grafana") == (200, {"database": "ok"})
        monkeypatch.setattr(
            smoke, "STACK", {n: f"{base}/{n}" for n in ("tempo", "loki", "drop", "grafana")}
        )
        monkeypatch.setattr(smoke.time, "sleep", lambda _s: None)  # no real waiting here
        assert smoke.wait_stack(timeout_s=30) == 0
        assert calls["/loki"] >= 2 and calls["/drop"] >= 2  # it waited, it did not crash
        # the smoke's own readiness check uses the same probe
        for name in ("TEMPO", "LOKI", "PROM", "API"):
            monkeypatch.setattr(smoke, name, base)
        assert smoke.ready(smoke.Smoke()) is True
    finally:
        srv.shutdown()


def test_obs_smoke_refuses_at_once_when_the_apps_switch_is_off(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Mac fix4: .env had no AEOI_OTEL_ENDPOINT; the smoke ran a full investigation first."""
    smoke = _load_script("obs_smoke")
    monkeypatch.setenv("TOKEN", "t")
    monkeypatch.delenv("AEOI_OTEL_ENDPOINT", raising=False)
    monkeypatch.setattr(smoke, "http", lambda *a, **k: pytest.fail("no request when OFF"))
    assert smoke.main([]) == 2
    assert "telemetry is OFF" in capsys.readouterr().out
    monkeypatch.setenv("AEOI_OTEL_ENDPOINT", "  ")
    assert smoke.switch_on() is False
    monkeypatch.setenv("AEOI_OTEL_ENDPOINT", "http://localhost:4318")
    assert smoke.switch_on() is True


def test_obs_smoke_pads_tempo_trace_ids_and_reads_v2_tag_values() -> None:
    """Mac fix5, both shapes taken from the real Tempo 2.10: search returns a trace id without
    its leading zero (Loki then finds 0 lines), and only the v2 tag API lists service names."""
    smoke = _load_script("obs_smoke")
    tempo_search = {"traces": [{"traceID": "636db42cf692059863e0a5df903a08e"},
                               {"traceID": "B178EE10C53AA3BACD26F85A39A02B47"}]}  # fmt: skip
    assert smoke.trace_ids(tempo_search) == [
        "0636db42cf692059863e0a5df903a08e",
        "b178ee10c53aa3bacd26f85a39a02b47",
    ]
    assert all(len(t) == 32 for t in smoke.trace_ids(tempo_search))
    assert smoke.trace_ids({"traces": []}) == [] and smoke.trace_ids("down") == []
    v2 = {"tagValues": [{"type": "string", "value": "service-catalog"},
                        {"type": "string", "value": "api"}]}  # fmt: skip
    assert smoke.known_services(v2) == ["api", "service-catalog"]
    assert smoke.known_services({"tagValues": []}) == [] and smoke.known_services(None) == []
    assert "/api/v2/search/tag/resource.service.name/values" in smoke.SERVICES_URL


def test_obs_smoke_names_the_services_that_log_another_correlation_id() -> None:
    smoke = _load_script("obs_smoke")
    streams = [
        {"stream": {"service_name": "agents", "correlation_id": "ours"}, "values": [[1, "a"]] * 3},
        {"stream": {"service_name": "llm-gateway", "correlation_id": "x1"}, "values": [[1, "b"]]},
        {"stream": {"service_name": "llm-gateway", "correlation_id": "x2"}, "values": [[1, "c"]] * 2},
        {"stream": {"service_name": "audit"}, "values": [[1, "d"]]},  # no id at all: not foreign
    ]  # fmt: skip
    assert smoke.foreign_correlation(streams, "ours") == {"llm-gateway": 3}
    assert smoke.foreign_correlation(streams[:1], "ours") == {}
