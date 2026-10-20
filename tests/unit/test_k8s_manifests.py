"""Phase 22a (ADR-034): the Kubernetes manifests are checked against compose AND the code.

The generator (scripts/k8s_manifests.py) reads `docker compose config`. Here the same input is
built from docker-compose.yml with the Phase 21 interpolation, under both .env states, and - when
Docker is there - compared with the real `docker compose config` output.
Truth for the network graph comes from the Settings classes (like test_compose_contract), not
from the generator's own URL scan, so a policy is never checked against itself.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.parse import urlsplit

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


def _load(path: str, name: str) -> ModuleType:
    s = importlib.util.spec_from_file_location(name, ROOT / path)
    assert s and s.loader
    m = importlib.util.module_from_spec(s)
    import sys

    sys.modules[name] = m
    s.loader.exec_module(m)
    return m


km = _load("scripts/k8s_manifests.py", "k8s_manifests")
kd = _load("scripts/k8s_deploy.py", "k8s_deploy")
wf = _load("infrastructure/docker/wait_for.py", "wait_for")
cc = _load("tests/unit/test_compose_contract.py", "compose_contract_for_k8s")

RAW = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
FACTS = km.Facts.from_json({"compose_subnets": ["172.19.0.0/16"], "host_gateway": "192.168.65.254",
                            "node_sources": ["172.18.0.2", "10.244.0.1"],
                            "dev_clients": ["172.18.0.1"]})  # fmt: skip
STATES = sorted(cc.ENV_STATES)
PY_APPS = sorted(cc.APPS)
ALL_APPS = [*PY_APPS, "service-catalog"]


def _walk(v: Any, env: dict[str, str]) -> Any:
    if isinstance(v, str):
        return cc.interpolate(v, env)
    if isinstance(v, list):
        return [_walk(x, env) for x in v]
    if isinstance(v, dict):
        return {k: _walk(x, env) for k, x in v.items()}
    return v


def resolved(state: str, profiles: tuple[str, ...] = ("apps", "apps-catalog")) -> dict[str, Any]:
    """What `docker compose --profile ... config` gives, built from the file."""
    env = cc.ENV_STATES[state]
    services = {n: _walk(s, env) for n, s in RAW["services"].items()
                if set(s.get("profiles") or []) & set(profiles)}  # fmt: skip
    secrets = {n: {"file": str((ROOT / s["file"]).resolve())} for n, s in RAW["secrets"].items()}
    return {"services": services, "secrets": secrets}


def objects(state: str, **kw: Any) -> list[dict[str, Any]]:
    return [o for objs in km.build(resolved(state, **kw), FACTS).values() for o in objs]


def by_kind(objs: list[dict[str, Any]], kind: str) -> dict[str, dict[str, Any]]:
    return {o["metadata"]["name"]: o for o in objs if o["kind"] == kind}


def pod(state: str, app: str) -> dict[str, Any]:
    spec: dict[str, Any] = by_kind(objects(state), "Deployment")[app]["spec"]["template"]["spec"]
    return spec


def container(state: str, app: str) -> dict[str, Any]:
    c: dict[str, Any] = pod(state, app)["containers"][0]
    return c


# ---------------------------------------------------------------- same config as compose


@pytest.mark.parametrize("state", STATES)
def test_every_compose_app_becomes_a_deployment_service_and_policy(state: str) -> None:
    objs = objects(state)
    assert sorted(by_kind(objs, "Deployment")) == sorted(ALL_APPS)
    services = by_kind(objs, "Service")
    assert sorted(n for n in services if not n.endswith("-host")) == sorted(ALL_APPS)
    assert sorted(n for n in by_kind(objs, "NetworkPolicy") if n.startswith("app-")) == sorted(
        f"app-{a}" for a in ALL_APPS
    )
    assert {o["metadata"].get("namespace", km.NAMESPACE) for o in objs} == {km.NAMESPACE}
    # the Java switch off: no catalog objects at all (like make apps-up without apps-catalog)
    off = objects(state, profiles=("apps",))
    assert "service-catalog" not in by_kind(off, "Deployment")


@pytest.mark.parametrize("state", STATES)
@pytest.mark.parametrize("app", ALL_APPS)
def test_command_env_and_port_are_exactly_compose(state: str, app: str) -> None:
    spec = resolved(state)["services"][app]
    c = container(state, app)
    assert c.get("args") == spec.get("command")  # compose command = k8s args (entrypoint kept)
    assert "command" not in c
    want = {k: "" if v is None else str(v) for k, v in (spec.get("environment") or {}).items()}
    assert {e["name"]: e["value"] for e in c["env"]} == want
    assert all("valueFrom" not in e for e in c["env"])
    assert [p["containerPort"] for p in c["ports"]] == [km.port_target(spec["ports"][0])]
    assert c["image"] == spec["image"] and c["imagePullPolicy"] == "Never"


def test_the_generator_input_here_equals_real_docker_compose_config(tmp_path: Path) -> None:
    """The fixture above must be what `docker compose config` gives (memory in bytes, ports as
    objects, absolute paths). Skipped without Docker; runs on the Mac."""
    if shutil.which("docker") is None:
        pytest.skip("no docker CLI")
    empty = tmp_path / "empty.env"
    empty.write_text("")
    env = {k: os.environ[k] for k in ("PATH", "HOME") if k in os.environ}
    r = subprocess.run(  # noqa: S603
        ["docker", "compose", "--env-file", str(empty), "--profile", "apps", "--profile",  # noqa: S607
         "apps-catalog", "config", "--format", "json"],
        cwd=ROOT, env=env, capture_output=True, text=True, check=False, timeout=60,
    )  # fmt: skip
    if r.returncode != 0:
        pytest.skip(f"docker compose config failed here: {r.stderr[-200:]}")
    real = km.build(json.loads(r.stdout), FACTS)
    assert real == km.build(resolved("switches off"), FACTS)


# ---------------------------------------------------------------- secrets


@pytest.mark.parametrize("app", ALL_APPS)
def test_each_pod_mounts_exactly_its_compose_secret_files(app: str) -> None:
    spec = resolved("switches off")["services"][app]
    plan = [p for p in km.data_plan(resolved("switches off")) if p["app"] == app]
    secrets = [p for p in plan if p["kind"] == "Secret"]
    assert len(secrets) == 1
    want = {Path(ref["target"]).name: (ROOT / RAW["secrets"][ref["source"]]["file"]).resolve()
            for ref in spec["secrets"]}  # fmt: skip
    assert {k: Path(v) for k, v in secrets[0]["files"].items()} == want
    p = pod("switches off", app)
    vol = next(v for v in p["volumes"] if v["name"] == f"{app}-files")
    assert vol["secret"] == {"secretName": f"{app}-files", "defaultMode": 0o440}
    mount = next(m for m in p["containers"][0]["volumeMounts"] if m["name"] == f"{app}-files")
    assert mount == {"name": f"{app}-files", "mountPath": "/app/secrets", "readOnly": True}
    assert p["securityContext"]["fsGroup"] == km.UID  # 0440 group-readable: no 644 needed


def test_secret_files_go_to_kubectl_never_into_manifests() -> None:
    text = json.dumps(objects("switches on"))
    assert "secrets/" not in text.replace("/app/secrets", "")  # no host path in any object
    for item in km.data_plan(resolved("switches off")):
        if item["kind"] != "Secret":
            continue
        cmd = kd.data_command(item)
        assert cmd[:4] == ["create", "secret", "generic", item["name"]]
        files = [a for a in cmd if a.startswith("--from-file=")]  # kubectl reads the files
        assert len(files) == len(item["files"])
        assert cmd[len(files) + 4 :] == ["-n", km.NAMESPACE, "--dry-run=client", "-o", "yaml"]


def test_the_catalog_folder_becomes_a_configmap_mounted_where_compose_mounted_it(
    tmp_path: Path,
) -> None:
    item = next(i for i in km.data_plan(resolved("switches off")) if i["kind"] == "ConfigMap")
    assert item["app"] == "tool-gateway" and item["mount"] == "/app/data/generated"
    (tmp_path / "catalog.json").write_text("{}")
    cmd = kd.data_command({**item, "directory": str(tmp_path)})
    assert f"--from-file=catalog.json={tmp_path / 'catalog.json'}" in cmd
    with pytest.raises(kd.DeployError, match="db-seed"):
        kd.data_command({**item, "directory": str(tmp_path / "missing")})
    (tmp_path / "big.json").write_text("x" * (kd.CONFIGMAP_MAX + 1))
    with pytest.raises(kd.DeployError, match="too big"):
        kd.data_command({**item, "directory": str(tmp_path)})


# ---------------------------------------------------------------- hardening


@pytest.mark.parametrize("app", ALL_APPS)
def test_every_pod_is_restricted_non_root_and_has_no_api_token(app: str) -> None:
    p = pod("switches on", app)
    assert p["securityContext"] == {
        "runAsNonRoot": True, "runAsUser": 10001, "runAsGroup": 10001, "fsGroup": 10001,
        "seccompProfile": {"type": "RuntimeDefault"},
    }  # fmt: skip
    assert p["automountServiceAccountToken"] is False
    assert p["enableServiceLinks"] is False
    for key in ("hostNetwork", "hostPID", "hostIPC", "serviceAccountName", "nodeName"):
        assert key not in p
    for c in [*p["containers"], *p.get("initContainers", [])]:
        assert c["securityContext"] == {
            "allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
            "capabilities": {"drop": ["ALL"]}, "runAsNonRoot": True,
        }  # fmt: skip
        assert c["imagePullPolicy"] == "Never"
        assert "memory" in c["resources"]["limits"]
    for v in p["volumes"]:  # Pod Security "restricted" volume types only
        assert set(v) - {"name"} <= {"secret", "configMap", "emptyDir"}


def test_the_namespace_enforces_pod_security_restricted() -> None:
    ns = by_kind(objects("switches off"), "Namespace")[km.NAMESPACE]
    for mode in ("enforce", "warn", "audit"):
        assert ns["metadata"]["labels"][f"pod-security.kubernetes.io/{mode}"] == "restricted"


def test_the_uid_matches_both_dockerfiles() -> None:
    for name in ("python.Dockerfile", "catalog.Dockerfile"):
        text = (ROOT / "infrastructure" / "docker" / name).read_text()
        assert f"USER {km.UID}:{km.UID}" in text, name


@pytest.mark.parametrize("app", ALL_APPS)
def test_tmpfs_is_a_memory_emptydir_of_the_compose_size(app: str) -> None:
    p = pod("switches off", app)
    tmp = next(v for v in p["volumes"] if v["name"] == "tmp0")
    assert tmp["emptyDir"] == {"medium": "Memory", "sizeLimit": "64Mi"}
    assert {"name": "tmp0", "mountPath": "/tmp"} in p["containers"][0]["volumeMounts"]  # noqa: S108


# ---------------------------------------------------------------- probes, resources, rollout


@pytest.mark.parametrize("app", ALL_APPS)
def test_probes_ready_checks_deps_live_never_does(app: str) -> None:
    c = container("switches off", app)
    java = app == "service-catalog"
    ready = "/actuator/health/readiness" if java else "/health/ready"
    live = "/actuator/health/liveness" if java else "/health/live"
    assert c["readinessProbe"]["httpGet"] == {"path": ready, "port": "http"}
    # a DB outage must not RESTART every pod: liveness never checks dependencies
    assert c["livenessProbe"]["httpGet"] == {"path": live, "port": "http"}
    assert c["startupProbe"]["httpGet"]["path"] == live
    start = c["startupProbe"]["periodSeconds"] * c["startupProbe"]["failureThreshold"]
    assert start >= (180 if java else 60)  # the JVM needed 39-47 s on 1 CPU (Phase 21)


@pytest.mark.parametrize("app", ALL_APPS)
def test_memory_limit_is_compose_and_request_is_the_measured_peak(app: str) -> None:
    c = container("switches off", app)
    limit = "512Mi" if app == "service-catalog" else "256Mi"
    assert c["resources"]["limits"]["memory"] == limit
    req = "192Mi" if app == "service-catalog" else "128Mi"  # peaks 173 / 125 MiB (ADR-033)
    assert c["resources"]["requests"]["memory"] == req
    assert c["resources"]["limits"]["cpu"] == ("2000m" if app == "service-catalog" else "1000m")


def test_a_measurement_limit_from_env_reaches_the_pods() -> None:
    cfg = resolved("switches off")
    for s in cfg["services"].values():
        if s.get("pull_policy") == "never":
            s["deploy"]["resources"]["limits"]["memory"] = "2147483648"  # AEOI_APP_MEM_LIMIT=2g
    dep = by_kind([o for v in km.build(cfg, FACTS).values() for o in v], "Deployment")["api"]
    assert (
        dep["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"]["memory"]
        == "2048Mi"
    )


@pytest.mark.parametrize(("value", "mi"), [
    ("256M", "256Mi"), ("268435456", "256Mi"), ("2g", "2048Mi"), ("512m", "512Mi"),
    ("1G", "1024Mi"), (536870912, "512Mi"),
])  # fmt: skip
def test_memory_units_are_binary_like_docker(value: Any, mi: str) -> None:
    assert km.k8s_memory(value) == mi


@pytest.mark.parametrize("bad", ["256MB?", "abc", "1000000", ""])
def test_unreadable_or_odd_memory_fails(bad: str) -> None:
    with pytest.raises(km.ComposeError):
        km.k8s_memory(bad)


@pytest.mark.parametrize("app", ALL_APPS)
def test_rollout_keeps_one_ready_pod_and_drains_before_sigterm(app: str) -> None:
    dep = by_kind(objects("switches off"), "Deployment")[app]
    assert dep["spec"]["replicas"] == 1
    assert dep["spec"]["strategy"] == {
        "type": "RollingUpdate",
        "rollingUpdate": {"maxSurge": 1, "maxUnavailable": 0},
    }
    p = dep["spec"]["template"]["spec"]
    sleep = p["containers"][0]["lifecycle"]["preStop"]["sleep"]["seconds"]
    assert 0 < sleep < p["terminationGracePeriodSeconds"]
    assert dep["spec"]["selector"]["matchLabels"] == {km.NAME: app}
    assert dep["spec"]["template"]["metadata"]["labels"][km.NAME] == app


# ---------------------------------------------------------------- depends_on -> initContainer


@pytest.mark.parametrize("app", ALL_APPS)
def test_init_containers_wait_for_exactly_the_compose_app_dependencies(app: str) -> None:
    deps = RAW["services"][app].get("depends_on") or {}
    want = sorted(d for d in deps if d in ALL_APPS)
    p = pod("switches off", app)
    inits = p.get("initContainers", [])
    if not want:
        assert inits == []
        return
    (init,) = inits
    urls = [a for a in init["command"] if a.startswith("http://")]
    assert sorted(urlsplit(u).hostname or "" for u in urls) == want
    assert all(u.endswith("/health/ready") for u in urls)
    assert init["command"][:2] == ["python", "/app/infrastructure/docker/wait_for.py"]


def test_the_orchestrator_waits_for_what_a_resumed_run_calls() -> None:
    init = pod("switches off", "orchestrator")["initContainers"][0]
    assert [a for a in init["command"] if a.startswith("http")] == [
        "http://agents:8003/health/ready", "http://api:8000/health/ready",
        "http://incident-service:8001/health/ready"]  # fmt: skip


def test_the_image_contains_the_wait_script() -> None:
    docker = ROOT / "infrastructure" / "docker"
    assert "COPY infrastructure/docker/wait_for.py" in (docker / "python.Dockerfile").read_text()
    ignore = (docker / "python.Dockerfile.dockerignore").read_text().splitlines()
    assert "!infrastructure/docker/wait_for.py" in ignore


def test_wait_for_waits_until_all_ready_or_the_deadline() -> None:
    now = [0.0]
    seen: list[str] = []

    def probe(url: str) -> bool:
        seen.append(url)
        return url == "a" or now[0] >= 4

    def sleep(s: float) -> None:
        now[0] += s

    assert (
        wf.wait(["a", "b", "b"], timeout_s=10, probe=probe, clock=lambda: now[0], sleep=sleep) == []
    )
    assert seen.count("a") == 1  # a ready url is not asked again
    now[0] = 0
    left = wf.wait(["b"], timeout_s=3, probe=lambda _u: False, clock=lambda: now[0], sleep=sleep)
    assert left == ["b"]
    assert wf.main(["https://x"]) == 2  # only in-cluster http readiness urls


# ---------------------------------------------------------------- network policies


def policy(objs: list[dict[str, Any]], app: str) -> dict[str, Any]:
    spec: dict[str, Any] = by_kind(objs, "NetworkPolicy")[f"app-{app}"]["spec"]
    return spec


def present(objs: list[dict[str, Any]]) -> list[str]:
    return sorted(by_kind(objs, "Deployment"))


def pod_edges(objs: list[dict[str, Any]]) -> set[tuple[str, str, int]]:
    """(from, to, port) allowed by EGRESS pod selectors."""
    out = set()
    for app in present(objs):
        for rule in policy(objs, app).get("egress", []):
            for to in rule["to"]:
                if "podSelector" in to:
                    target = to["podSelector"]["matchLabels"][km.NAME]
                    out |= {(app, target, p["port"]) for p in rule["ports"]}
    return out


def ingress_edges(objs: list[dict[str, Any]]) -> set[tuple[str, str, int]]:
    out = set()
    for app in ALL_APPS:
        for rule in policy(objs, app)["ingress"]:
            for frm in rule["from"]:
                if "podSelector" in frm:
                    src = frm["podSelector"]["matchLabels"][km.NAME]
                    out |= {(src, app, p["port"]) for p in rule["ports"]}
    return out


def expected_edges(state: str) -> set[tuple[str, str, int]]:
    """From the SETTINGS classes (like the Phase 21 contract), plus the egress allow-list."""
    ports = cc.service_ports()
    out = set()
    for app in PY_APPS:
        cls, env = cc.settings_class(app), cc.env_of(app, cc.ENV_STATES[state])
        for f in cls.model_fields:
            if f.endswith("_url") and f not in cc.NOT_SERVICE_CALLS:
                host = urlsplit(env[cc.env_name(cls, f)]).hostname
                out.add((app, host, ports[host]))
        if app == "tool-gateway":
            for item in json.loads(env["AEOI_TOOLS_EGRESS_ALLOWLIST"]):
                host, port = item.split(":")
                out.add((app, host, int(port)))
            if env.get("AEOI_TOOLS_CATALOG_URL"):
                out.add((app, "service-catalog", 8010))
    return out


@pytest.mark.parametrize("state", STATES)
def test_pod_to_pod_rules_are_exactly_the_calls_the_settings_make(state: str) -> None:
    objs = objects(state)
    want = expected_edges(state)
    assert pod_edges(objs) == want  # egress: nothing missing (a broken call), nothing extra
    assert ingress_edges(objs) == want  # and the callee admits exactly those callers


def test_with_the_java_switch_off_the_catalog_is_neither_deployed_nor_reachable() -> None:
    objs = objects("switches off", profiles=("apps",))
    assert all(t != "service-catalog" for _, t, _ in pod_edges(objs))


def egress_ip(objs: list[dict[str, Any]], app: str) -> set[tuple[str, int]]:
    out = set()
    for rule in policy(objs, app).get("egress", []):
        for to in rule["to"]:
            if "ipBlock" in to:
                out |= {(to["ipBlock"]["cidr"], p["port"]) for p in rule["ports"]}
    return out


@pytest.mark.parametrize("state", STATES)
def test_only_db_logins_reach_postgres_and_agents_never_does(state: str) -> None:
    objs = objects(state)
    db = {a for a in ALL_APPS
          if any(r["target"].endswith("_svc_password.txt") for r in RAW["services"][a]["secrets"])}  # fmt: skip
    for app in ALL_APPS:
        assert (("172.19.0.0/16", 5432) in egress_ip(objs, app)) == (app in db), app
    assert "agents" not in db  # the Verify demo: agents -> postgres must FAIL


def _imports_messaging(app: str) -> bool:
    pkg = cc.APPS[app].split(".")[0]
    src = next(ROOT.glob(f"services/*/src/{pkg}"))
    return any(
        re.search(r"\b(aeoi_messaging|aiokafka)\b", p.read_text()) for p in src.rglob("*.py")
    )


def test_kafka_clients_are_the_services_whose_code_uses_kafka() -> None:
    assert {a for a in PY_APPS if _imports_messaging(a)} == km.KAFKA_CLIENTS
    objs = objects("switches on")
    for app in ALL_APPS:
        assert (("172.19.0.0/16", 19092) in egress_ip(objs, app)) == (app in km.KAFKA_CLIENTS)


@pytest.mark.parametrize("state", STATES)
def test_keycloak_only_when_the_issuer_is_on_and_only_for_the_jwks(state: str) -> None:
    objs = objects(state)
    on = bool(cc.ENV_STATES[state].get("AEOI_OIDC_ISSUER"))
    for app in ALL_APPS:
        has = ("172.19.0.0/16", 8080) in egress_ip(objs, app)
        assert has == (on and app in PY_APPS), app


@pytest.mark.parametrize("state", STATES)
def test_the_host_and_the_internet_only_for_the_llm_gateway(state: str) -> None:
    objs = objects(state)
    for app in ALL_APPS:
        ips = egress_ip(objs, app)
        assert (("192.168.65.254/32", 11434) in ips) == (app == "llm-gateway")
        assert (("0.0.0.0/0", 443) in ips) == (app == "llm-gateway")
        assert {port for _, port in ips} <= {5432, 19092, 8080, 11434, 443, 4318}
    rule = next(r for r in policy(objs, "llm-gateway")["egress"]
                if r["to"][0].get("ipBlock", {}).get("cidr") == "0.0.0.0/0")  # fmt: skip
    excepted = rule["to"][0]["ipBlock"]["except"]
    for private in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16"):
        assert private in excepted  # no LAN, no compose subnet, no cloud metadata on 443
    assert rule["ports"] == [{"protocol": "TCP", "port": 443}]


def test_default_deny_and_dns_are_the_only_namespace_wide_policies() -> None:
    pols = by_kind(objects("switches off"), "NetworkPolicy")
    assert pols["default-deny"]["spec"] == {"podSelector": {}, "policyTypes": ["Ingress", "Egress"]}
    dns = pols["allow-dns"]["spec"]
    assert dns["policyTypes"] == ["Egress"] and "ingress" not in dns
    (rule,) = dns["egress"]
    assert {p["port"] for p in rule["ports"]} == {53}
    assert rule["to"][0]["podSelector"] == {"matchLabels": {"k8s-app": "kube-dns"}}
    wide = [n for n, p in pols.items() if p["spec"]["podSelector"] == {}]
    assert sorted(wide) == ["allow-dns", "default-deny"]
    for name in ALL_APPS:
        assert pols[f"app-{name}"]["spec"]["policyTypes"] == ["Ingress", "Egress"]


@pytest.mark.parametrize("app", ALL_APPS)
def test_node_ingress_is_the_node_addresses_on_the_own_port_only(app: str) -> None:
    objs = objects("switches off")
    port = cc.service_ports()[app]
    ip_rules = [r for r in policy(objs, app)["ingress"] if "ipBlock" in r["from"][0]]
    assert len(ip_rules) == 1
    assert [f["ipBlock"]["cidr"] for f in ip_rules[0]["from"]] == ["172.18.0.2/32", "10.244.0.1/32"]
    assert ip_rules[0]["ports"] == [{"protocol": "TCP", "port": port}]


def test_addresses_come_from_the_facts_never_from_the_generator() -> None:
    other = km.Facts.from_json({"compose_subnets": ["10.99.0.0/24"], "host_gateway": "10.1.1.1",
                                "node_sources": ["10.2.2.2"], "dev_clients": ["10.3.3.3"]})  # fmt: skip
    text = json.dumps(km.build(resolved("switches on"), other))
    for ip in ("172.19.", "192.168.65", "172.18.", "10.244."):
        assert ip not in text
    assert "10.99.0.0/24" in text and "10.1.1.1/32" in text and "10.2.2.2/32" in text
    assert "10.3.3.3/32" in text
    good = {"compose_subnets": ["10.0.0.0/8"], "host_gateway": "1.1.1.1",
            "node_sources": ["1.1.1.1"], "dev_clients": ["1.1.1.2"]}  # fmt: skip
    for bad in ({**good, "compose_subnets": ["x"]}, {**good, "compose_subnets": []},
                {**good, "node_sources": []}, {**good, "dev_clients": []},
                {**good, "dev_clients": ["nope"]}):  # fmt: skip
        with pytest.raises((ValueError, km.ComposeError)):
            km.Facts.from_json(bad)


# ---------------------------------------------------------------- strict: never guess


def _mutated(path: list[str], value: Any) -> dict[str, Any]:
    cfg = resolved("switches off")
    node: Any = cfg["services"]
    for key in path[:-1]:
        node = node[key]
    if value is _DELETE:
        del node[path[-1]]
    else:
        node[path[-1]] = value
    return cfg


_DELETE = object()


@pytest.mark.parametrize(("path", "value", "match"), [
    (["api", "env_file"], [".env"], "does not handle"),
    (["api", "privileged"], True, "does not handle"),
    (["api", "user"], "0", "does not handle"),
    (["api", "cap_add"], ["NET_ADMIN"], "does not handle"),
    (["api", "entrypoint"], ["sh"], "entrypoint"),
    (["api", "read_only"], False, "read_only"),
    (["api", "cap_drop"], [], "capabilities"),
    (["api", "security_opt"], [], "no-new-privileges"),
    (["api", "ports"], ["8000:8000", "9000:9000"], "exactly one port"),
    (["api", "environment", "AEOI_API_RAG_SERVICE_URL"], "http://rag:9999", "listens on"),
    (["api", "environment", "AEOI_API_RAG_SERVICE_URL"], "http://evil.example:80", "unknown target"),
    (["api", "environment"], ["A=b"], "list"),
    (["api", "image"], "nginx:latest", "unknown image"),
    (["api", "deploy"], {}, "memory limit"),
    (["api", "secrets"], [{"source": "jwt_public", "target": "/etc/jwt.pem"}], "outside"),
    (["api", "secrets"], ["jwt_public"], "short secret"),
    (["tool-gateway", "volumes", 0, "read_only"], False, "read-only bind"),
    (["api", "tmpfs"], ["/tmp:size=64m,exec"], "tmpfs option"),  # noqa: S108
    (["orchestrator", "depends_on", "agents", "condition"], "service_started", "service_healthy"),
    (["redis-like"], {"image": "redis"}, "neither an app"),
])  # fmt: skip
def test_anything_unexpected_in_compose_is_an_error(
    path: list[Any], value: Any, match: str
) -> None:
    cfg = _mutated(path, value)
    with pytest.raises(km.ComposeError, match=match):
        km.build(cfg, FACTS)


def test_the_issuer_is_an_identifier_not_a_call() -> None:
    env = resolved("switches on")["services"]["api"]["environment"]
    assert env["AEOI_OIDC_ISSUER"].startswith("http://localhost")  # would be "unknown target"
    assert "AEOI_OIDC_ISSUER" in km.NOT_CALLS


# ---------------------------------------------------------------- dev host ports (kind)


def test_dev_host_ports_match_the_kind_cluster_port_mappings() -> None:
    kind = yaml.safe_load((ROOT / "infrastructure/kubernetes/kind/cluster.yaml").read_text())
    (node,) = kind["nodes"]
    maps = {m["hostPort"]: m["containerPort"] for m in node["extraPortMappings"]}
    assert all(m["listenAddress"] == "127.0.0.1" for m in node["extraPortMappings"])
    hosts = {o["spec"]["ports"][0]["port"]: o["spec"]["ports"][0]["nodePort"]
             for n, o in by_kind(objects("switches off"), "Service").items() if n.endswith("-host")}  # fmt: skip
    assert maps == hosts  # each smoke port -> the NodePort of that app, nothing else open
    assert sorted(maps) == kd.HOST_PORTS == sorted(cc.service_ports()[a] for a in ALL_APPS)
    assert kind["name"] == kd.CLUSTER
    assert kind["networking"]["podSubnet"] == "10.244.0.0/16"


def test_only_the_dev_host_services_are_nodeports() -> None:
    for name, svc in by_kind(objects("switches off"), "Service").items():
        is_host = name.endswith("-host")
        assert (svc["spec"].get("type") == "NodePort") == is_host
        assert svc["spec"]["selector"] == {km.NAME: name.removesuffix("-host")}


def test_rendered_files_replace_earlier_ones(tmp_path: Path) -> None:
    (tmp_path / "99-stale.yaml").write_text("kind: ConfigMap\n")
    paths = km.write(km.build(resolved("switches off"), FACTS), tmp_path)
    assert sorted(p.name for p in tmp_path.glob("*.yaml")) == sorted(p.name for p in paths)
    assert paths[0].name == "00-namespace.yaml"  # applied first
    docs = [d for p in paths for d in yaml.safe_load_all(p.read_text())]
    assert len(docs) == len(objects("switches off"))
    assert all(re.fullmatch(r"[a-z0-9-]+", d["metadata"]["name"]) for d in docs)


def test_host_traffic_is_admitted_only_by_the_dev_file_and_only_on_app_ports() -> None:
    files = km.build(resolved("switches off"), FACTS)
    text = {name: json.dumps(objs) for name, objs in files.items()}
    assert [n for n, t in text.items() if "172.18.0.1/32" in t] == ["40-dev-host-ports.yaml"]
    pol = next(o for o in files["40-dev-host-ports.yaml"] if o["kind"] == "NetworkPolicy")
    assert pol["metadata"]["name"] == "dev-host-ingress"
    assert pol["spec"]["policyTypes"] == ["Ingress"]  # never widens egress
    (rule,) = pol["spec"]["ingress"]
    assert rule["from"] == [{"ipBlock": {"cidr": "172.18.0.1/32"}}]
    assert sorted(p["port"] for p in rule["ports"]) == kd.HOST_PORTS
    assert pol["spec"]["podSelector"]["matchExpressions"][0]["values"] == sorted(ALL_APPS)


def test_a_rebuilt_image_changes_the_pod_template() -> None:
    facts = km.Facts.from_json({**FACTS.__dict__, "image_ids": {"aeoi-py:dev": "sha256:abc"}})
    deps = by_kind([o for v in km.build(resolved("switches off"), facts).values() for o in v],
                   "Deployment")  # fmt: skip
    meta = deps["api"]["spec"]["template"]["metadata"]
    assert meta["annotations"] == {"aeoi.io/image-id": "sha256:abc"}
    assert "annotations" not in deps["service-catalog"]["spec"]["template"]["metadata"]


def test_rendered_yaml_has_no_anchors(tmp_path: Path) -> None:
    for p in km.write(km.build(resolved("switches on"), FACTS), tmp_path):
        assert "&id" not in p.read_text() and "*id" not in p.read_text(), p.name


@pytest.mark.parametrize("profiles", [
    ("apps",), ("apps", "apps-catalog"), ("apps", "kafka"), ("apps", "apps-catalog", "kafka"),
])  # fmt: skip
def test_every_profile_set_k8s_apply_can_pass_builds(profiles: tuple[str, ...]) -> None:
    """k8s_deploy adds apps-catalog and kafka from .env, exactly like make apps-up. The kafka
    profile brings redis along: it is infra, not an error (found on the Mac, 22a fix1)."""
    cfg = resolved("switches on", profiles=profiles)
    km.build(cfg, FACTS)
    others = {n for n, s in cfg["services"].items() if s.get("pull_policy") != "never"}
    assert others <= km.INFRA


def test_redis_gets_no_rule_until_an_app_calls_it() -> None:
    objs = objects("switches on", profiles=("apps", "apps-catalog", "kafka"))
    assert all(port != 6379 for app in ALL_APPS for _, port in egress_ip(objs, app))


@pytest.mark.parametrize("state", STATES)
def test_every_app_reaches_the_collector_only_when_telemetry_is_switched_on(state: str) -> None:
    """Phase 23a (ADR-035): one egress rule per app, compose subnet on 4318, from the env."""
    objs = objects(state)
    on = bool(cc.ENV_STATES[state].get("AEOI_OTEL_ENDPOINT"))
    for app in ALL_APPS:
        assert (("172.19.0.0/16", 4318) in egress_ip(objs, app)) == on, app
    if on:
        g = km.graph(resolved(state))
        assert "otel-collector:4318" in kd.targets(g)  # the Mac netpol matrix checks it too
