"""Phase 22a (ADR-034): the deploy script, with a fake runner (no Docker, no cluster).

The Mac runs it for real; here: it reads the cluster facts right, never touches another kubectl
context, refuses the known-bad states, and judges the netpol matrix without fooling itself.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load(path: str, name: str) -> ModuleType:
    s = importlib.util.spec_from_file_location(name, ROOT / path)
    assert s and s.loader
    m = importlib.util.module_from_spec(s)
    sys.modules[name] = m
    s.loader.exec_module(m)
    return m


kd = _load("scripts/k8s_deploy.py", "k8s_deploy_t")
km = kd.km


class FakeRunner(kd.Runner):  # type: ignore[misc,name-defined]
    def __init__(self, answers: dict[str, tuple[int, str]]) -> None:
        super().__init__()
        self.answers = answers

    def run(self, args: Sequence[str], *, stdin: str | None = None, check: bool = True,
            quiet: bool = False, live: bool = False) -> subprocess.CompletedProcess[str]:  # fmt: skip
        self.log.append(list(args))
        line = " ".join(args)
        code, out = next((v for k, v in self.answers.items() if k in line), (0, ""))
        if check and code:
            raise kd.DeployError(f"failed: {line}")
        return subprocess.CompletedProcess(list(args), code, out, "")


# ---------------------------------------------------------------- facts


def test_facts_are_read_from_docker_and_the_node_ipv4_only() -> None:
    network = [{"Subnet": "172.19.0.0/16", "Gateway": "172.19.0.1"}, {"Subnet": "fd00::/64"}]
    host = "192.168.65.254  STREAM host.docker.internal\n192.168.65.254  DGRAM\n"
    nodes = {
        "items": [
            {
                "spec": {"podCIDR": "10.244.0.0/24"},
                "status": {
                    "addresses": [
                        {"type": "InternalIP", "address": "172.18.0.2"},
                        {"type": "Hostname", "address": "x"},
                    ]
                },
            }
        ]
    }
    addrs = ("1: lo    inet 127.0.0.1/8 scope host lo\n"
             "2: eth0    inet 172.18.0.2/16 brd 172.18.255.255 scope global eth0\n"
             "3: eth1    inet 172.19.0.5/16 brd 172.19.255.255 scope global eth1\n"
             "4: vethab    inet 10.244.0.1/32 scope global vethab\n")  # fmt: skip
    kind = [{"Subnet": "172.18.0.0/16", "Gateway": "172.18.0.1"}, {"Subnet": "fc00:f853::/64"}]
    ids = {"aeoi-py:dev": "sha256:1"}
    facts = kd.facts_from(network, host, nodes, addrs, kind, ids)
    assert facts == {"compose_subnets": ["172.19.0.0/16"], "host_gateway": "192.168.65.254",
                     "node_sources": ["10.244.0.1", "172.18.0.2", "172.19.0.5"],
                     "dev_clients": ["172.18.0.1"], "image_ids": ids}  # fmt: skip
    km.Facts.from_json(facts)  # valid input for the generator
    with pytest.raises(kd.DeployError, match="IPv4"):
        kd.facts_from([{"Subnet": "fd00::/64"}], host, nodes, addrs, kind, ids)
    with pytest.raises(kd.DeployError, match=r"host\.docker\.internal"):
        kd.facts_from(network, "", nodes, addrs, kind, ids)
    with pytest.raises(kd.DeployError, match="kind"):
        kd.facts_from(network, host, nodes, addrs, [], ids)
    # Docker Desktop may leave Gateway out: the first host of the subnet
    assert kd.ipv4_gateway([{"Subnet": "172.18.0.0/16"}]) == ["172.18.0.1"]


def test_the_pod_gateway_is_the_first_address_of_the_node_range() -> None:
    assert kd.pod_gateway("10.244.0.0/24") == "10.244.0.1"
    assert kd.pod_gateway("10.244.3.0/24") == "10.244.3.1"


# ---------------------------------------------------------------- never another context


def test_every_kubectl_call_names_the_kind_context() -> None:
    r = FakeRunner({})
    r.kubectl("get", "nodes")
    r.kns("get", "pods")
    assert r.log[0][:3] == ["kubectl", "--context", "kind-aeoi"]
    assert r.log[1][:5] == ["kubectl", "--context", "kind-aeoi", "-n", km.NAMESPACE]


def _calls(tree: ast.AST, fn: str) -> list[ast.Call]:
    return [n for n in ast.walk(tree) if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute) and n.func.attr == fn]  # fmt: skip


def test_the_script_never_calls_kubectl_or_a_shell_directly() -> None:
    tree = ast.parse((ROOT / "scripts" / "k8s_deploy.py").read_text())
    pinned = next(
        n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "kubectl"
    )
    allowed = {id(n) for n in ast.walk(pinned)}
    for call in _calls(tree, "run"):
        if id(call) in allowed:
            continue
        first = call.args[0] if call.args else None
        if isinstance(first, ast.List) and first.elts:
            head = first.elts[0]
            assert not (isinstance(head, ast.Constant) and head.value == "kubectl"), (
                "use Runner.kubectl (it pins --context)"
            )
        assert not any(k.arg == "shell" for k in call.keywords)
    assert "shell=True" not in (ROOT / "scripts" / "k8s_deploy.py").read_text()
    assert "os.system" not in (ROOT / "scripts" / "k8s_deploy.py").read_text()


# ---------------------------------------------------------------- up / preflight


def test_up_refuses_busy_app_ports_before_creating_anything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(kd.shutil, "which", lambda _t: "/usr/bin/x")
    r = FakeRunner({"kind get clusters": (0, "other\n")})
    with pytest.raises(kd.DeployError, match=r"\[8001\].*apps-down"):
        kd.up(r, busy=lambda p: p == 8001)
    assert not any("create" in a for a in r.log for a in a)


def test_up_creates_the_cluster_and_joins_the_compose_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(kd.shutil, "which", lambda _t: "/usr/bin/x")
    r = FakeRunner({"kind get clusters": (0, ""),
                    "NetworkSettings": (0, json.dumps({"kind": {}}))})  # fmt: skip
    done = kd.up(r, busy=lambda _p: False)
    assert done == ["cluster aeoi created", "node joined aeoi_default"]
    create = next(c for c in r.log if c[:3] == ["kind", "create", "cluster"])
    assert create[create.index("--config") + 1] == str(kd.KIND_CONFIG)
    assert ["docker", "network", "connect", "aeoi_default", "aeoi-control-plane"] in r.log


def test_up_without_the_compose_network_says_make_up(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(kd.shutil, "which", lambda _t: "/usr/bin/x")
    r = FakeRunner({"network inspect aeoi_default": (1, "")})
    with pytest.raises(kd.DeployError, match="make up"):
        kd.up(r, busy=lambda _p: False)


def _config() -> dict[str, Any]:
    cfg: dict[str, Any] = json.loads(json.dumps(_tm.resolved("switches off")))
    return cfg


_tm = _load("tests/unit/test_k8s_manifests.py", "test_k8s_manifests_for_deploy")


def _preflight_runner(**over: tuple[int, str]) -> FakeRunner:
    answers = {
        "kind get clusters": (0, "aeoi\n"),
        "NetworkSettings": (0, json.dumps({"kind": {}, "aeoi_default": {}})),
        "ps --format json postgres": (0, json.dumps({"Service": "postgres", "Health": "healthy"})),
        "--status running": (0, "postgres\n"),
    }
    answers.update(over)
    return FakeRunner(answers)


@pytest.fixture
def no_local_check(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(kd.apps21, "check", lambda *_a, **_k: [])


@pytest.mark.usefixtures("no_local_check")
def test_preflight_passes_on_a_good_state() -> None:
    kd.preflight(_preflight_runner(), _config())


@pytest.mark.usefixtures("no_local_check")
@pytest.mark.parametrize(("over", "match"), [
    ({"kind get clusters": (0, "")}, "kind-up"),
    ({"NetworkSettings": (0, json.dumps({"kind": {}}))}, "kind-up"),
    ({"ps --format json postgres": (0, json.dumps({"Health": "starting"}))}, "make up"),
    ({"ps --format json postgres": (0, "")}, "make up"),
    ({"--status running": (0, "postgres\napi\n")}, "apps-down"),
])  # fmt: skip
def test_preflight_refuses_known_bad_states(over: dict[str, tuple[int, str]], match: str) -> None:
    with pytest.raises(kd.DeployError, match=match):
        kd.preflight(_preflight_runner(**over), _config())


@pytest.mark.usefixtures("no_local_check")
def test_preflight_needs_kafka_running_when_env_turns_it_on() -> None:
    cfg = _config()
    cfg["services"]["incident-service"]["environment"]["AEOI_INCIDENT_EVENT_TRANSPORT"] = "kafka"
    with pytest.raises(kd.DeployError, match="PROFILE=kafka"):
        kd.preflight(_preflight_runner(), cfg)
    kd.preflight(_preflight_runner(**{"--status running": (0, "postgres\nkafka\n")}), cfg)


def test_preflight_reads_old_compose_ps_arrays_too() -> None:
    r = FakeRunner({"ps --format json": (0, json.dumps([{"Health": "healthy"}]))})
    assert kd.ps_rows(r, "postgres") == [{"Health": "healthy"}]


def test_preflight_keeps_every_phase21_problem_except_the_644_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(kd.apps21, "check", lambda *_a, **_k: [
        "missing secrets/x.txt (for api): make db-users",
        "2 secret file(s) only YOU can read (a, b): ... Run make apps-secrets-perms"])  # fmt: skip
    with pytest.raises(kd.DeployError) as exc:
        kd.preflight(_preflight_runner(), _config())
    assert "x.txt" in str(exc.value) and "apps-secrets-perms" not in str(exc.value)


# ---------------------------------------------------------------- netpol judging


def _graph() -> Any:
    cfg = _config()
    return km.graph(cfg, km.apps_of(cfg))


def test_the_matrix_targets_cover_every_app_and_infra_port() -> None:
    t = kd.targets(_graph())
    assert {"api:8000", "audit:8008", "postgres:5432", "kafka:19092", "host.docker.internal:11434",
            kd.INTERNET_TARGET} <= set(t)  # fmt: skip


def test_judge_needs_an_allowed_source_to_prove_a_target_is_up() -> None:
    g = _graph()
    res = {
        "api": {
            "postgres:5432": "open",
            "audit:8008": "open",
            "agents:8003": "timeout",
            "kafka:19092": "timeout",
        },
        "agents": {"postgres:5432": "timeout", "audit:8008": "refused", "kafka:19092": "timeout"},
        "incident-service": {"kafka:19092": "dns"},  # kafka not running
        "orchestrator": {"agents:8003": "open"},  # an ALLOWED source proves agents is up
    }
    fail, skip, ok = kd.judge(g, res)
    assert fail == []
    assert any("agents -> postgres:5432" in s for s in ok)  # forbidden, and postgres is UP
    assert any("agents -> audit:8008" in s for s in ok)
    assert any("api -> agents:8003" in s for s in ok)
    assert any("agents -> kafka:19092" in s for s in skip)  # kafka down: proves nothing
    assert any("incident-service -> kafka" in s for s in skip)
    fail, skip, _ = kd.judge(g, {k: v for k, v in res.items() if k != "orchestrator"})
    assert any("api -> agents:8003" in s for s in skip)  # nobody proved agents up


UP = {"api": {"postgres:5432": "open"}}  # an allowed pod proves postgres is up


@pytest.mark.parametrize(("res", "why"), [
    ({"agents": {"postgres:5432": "open"}}, "FORBIDDEN but open"),
    ({"api": {"incident-service:8001": "timeout"}}, "ALLOWED call blocked"),
    ({"rag": {"postgres:5432": "timeout"}}, "ALLOWED call blocked"),  # postgres IS up
    ({kd.PROBE: {"audit:8008": "open"}}, "no app admits it"),
    ({kd.PROBE: {"audit:8008": "dns"}}, "probe: name or network broken"),  # never a silent pass
    ({"agents": {"audit:8008": "error:OSError"}}, "name or network broken"),
])  # fmt: skip
def test_judge_fails_a_hole_and_a_broken_call(res: dict[str, dict[str, str]], why: str) -> None:
    fail, _, _ = kd.judge(_graph(), {**UP, **res} if "api" not in res else {
        "api": {**UP["api"], **res["api"]}})  # fmt: skip
    assert len(fail) == 1 and why in fail[0], fail


def test_judge_fails_when_postgres_was_never_reached() -> None:
    fail, _, _ = kd.judge(_graph(), {"agents": {"postgres:5432": "timeout"}})
    assert fail == [
        f"{kd.MUST_BE_UP} was not reached by ANY allowed pod: the deny checks prove nothing"
    ]


def test_judge_skips_a_pod_calling_its_own_service() -> None:
    fail, skip, _ = kd.judge(_graph(), {**UP, "agents": {"agents:8003": "open"}})
    assert fail == [] and any("its own Service" in s for s in skip)


def test_the_probe_pod_is_restricted_and_may_only_send_inside_the_namespace() -> None:
    pol, pod = kd.probe_objects("aeoi-py:dev")
    assert pol["spec"]["egress"] == [{"to": [{"podSelector": {}}]}]  # no ipBlock: no infra
    assert pol["spec"]["policyTypes"] == ["Egress"]
    spec = pod["spec"]
    assert spec["securityContext"] == km.pod_security()
    assert spec["containers"][0]["securityContext"] == km.container_security()
    assert spec["automountServiceAccountToken"] is False
    assert pod["metadata"]["namespace"] == km.NAMESPACE


def test_probe_code_is_one_argument_and_connects_only() -> None:
    compile(kd.PROBE_CODE, "probe", "exec")
    assert "send" not in kd.PROBE_CODE and "subprocess" not in kd.PROBE_CODE


# ---------------------------------------------------------------- rollout demo


def test_the_demo_paces_under_the_api_rate_limit() -> None:
    from aeoi_api.config import Settings

    assert Settings().rate_limit_per_minute > 60 / kd.DEMO_EVERY_S  # a 429 would be OUR bug


def test_summary_counts_every_non_200() -> None:
    total, bad = kd.summarize(
        [(0, "200"), (1, "503"), (2, "error:ConnectionResetError"), (3, "200")]
    )
    assert total == 4 and bad == {"503": 1, "error:ConnectionResetError": 1}


# ---------------------------------------------------------------- apply order


class ApplyRunner(FakeRunner):
    def __init__(self, dry_stderr: str = "") -> None:
        nodes = {"items": [{"spec": {"podCIDR": "10.244.0.0/24"},
                            "status": {"addresses": [{"type": "InternalIP", "address": "172.18.0.2"}]}}]}  # fmt: skip
        super().__init__({
            "config --format json": (0, json.dumps(_config())),
            "kind get clusters": (0, "aeoi\n"),
            "{{json .IPAM.Config}}": (0, json.dumps([{"Subnet": "172.19.0.0/16"}])),
            "getent ahostsv4": (0, "192.168.65.254 STREAM host.docker.internal\n"),
            "get nodes -o json": (0, json.dumps(nodes)),
            "ip -4 -o addr": (0, "2: eth0    inet 172.18.0.2/16 scope global eth0\n"),
            "image inspect --format": (0, "sha256:abc\n"),
        })  # fmt: skip
        self.dry_stderr = dry_stderr
        self.stdins: list[str | None] = []

    def run(self, args: Sequence[str], *, stdin: str | None = None, check: bool = True,
            quiet: bool = False, live: bool = False) -> subprocess.CompletedProcess[str]:  # fmt: skip
        self.stdins.append(stdin)
        r = super().run(args, stdin=stdin, check=check, quiet=quiet, live=live)
        if "--dry-run=server" in args:
            r.stderr = self.dry_stderr
        return r


def _apply(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, runner: ApplyRunner) -> list[str]:
    monkeypatch.setattr(kd, "WORK", tmp_path)
    monkeypatch.setattr(kd, "preflight", lambda _r, _c: None)
    data = tmp_path / "data"
    data.mkdir()
    (data / "catalog.json").write_text("{}")
    orig = kd.km.data_plan
    monkeypatch.setattr(kd.km, "data_plan", lambda c, *a: [
        {**i, "directory": str(data)} if i["kind"] == "ConfigMap" else i for i in orig(c, *a)])  # fmt: skip
    done: list[str] = kd.apply(runner)
    return done


def test_apply_validates_on_the_server_before_any_secret_or_pod(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    r = ApplyRunner()
    done = _apply(monkeypatch, tmp_path, r)
    k = [c for c in r.log if c[:1] == ["kubectl"]]
    assert all(c[1:3] == ["--context", "kind-aeoi"] for c in k)
    first_dry = next(i for i, c in enumerate(k) if "--dry-run=server" in c)
    first_secret = next(i for i, c in enumerate(k) if "secret" in c)
    first_real = next(i for i, c in enumerate(k) if c[3:5] == ["apply", "--server-side"]
                      and "--dry-run=server" not in c and "00-namespace.yaml" not in " ".join(c)
                      and c[-1] != "-")  # fmt: skip
    assert first_dry < first_secret < first_real
    assert all("--server-side" in c for c in k if "apply" in c)  # no last-applied secret copy
    assert sum("rollout" in c for c in k) == 9
    assert done[-1] == "tool-gateway available"
    assert (tmp_path / "manifests" / "00-namespace.yaml").exists()


def test_a_pod_security_warning_stops_apply_before_secrets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    r = ApplyRunner(dry_stderr='Warning: would violate PodSecurity "restricted:latest"')
    with pytest.raises(kd.DeployError, match="Pod Security"):
        _apply(monkeypatch, tmp_path, r)
    assert not any("secret" in c for c in r.log)


def test_compose_config_adds_the_profiles_the_switches_need() -> None:
    on = _config()
    on["services"]["tool-gateway"]["environment"]["AEOI_TOOLS_CATALOG_URL"] = "http://x:8010"
    r = FakeRunner({"config --format json": (0, json.dumps(on))})
    kd.compose_config(r)
    assert r.log[-1] == ["docker", "compose", "--profile", "apps", "--profile", "apps-catalog",
                         "config", "--format", "json"]  # fmt: skip


def test_an_unreachable_answer_counts_as_closed_for_a_target_that_is_up() -> None:
    """Mac, 22a fix2: forbidden pods got EHOSTUNREACH for api.anthropic.com:443 while the
    llm-gateway opened it - a real denial, first mislabelled 'target down'."""
    res = {**UP, "llm-gateway": {kd.INTERNET_TARGET: "open"},
           "agents": {kd.INTERNET_TARGET: "unreachable"},
           "rag": {kd.INTERNET_TARGET: "error:EIO"}}  # fmt: skip
    fail, skip, ok = kd.judge(_graph(), res)
    assert fail == []
    assert any(f"agents -> {kd.INTERNET_TARGET}" in s for s in ok)
    assert any("rag" in s and "target IS up" in s for s in skip)  # never a silent pass


def test_the_probe_names_unreachable_and_errno() -> None:
    assert '"unreachable"' in kd.PROBE_CODE and "errno.EHOSTUNREACH" in kd.PROBE_CODE
