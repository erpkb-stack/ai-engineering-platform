"""kind cluster for the apps (Phase 22a, ADR-034). Make targets call this; read them first.

    uv run python scripts/k8s_deploy.py up | apply | render | status | netpol-test | rollout-demo | down

up           kind cluster `aeoi` (ports 8000-8010 on 127.0.0.1), joined to the compose network
apply        compose config -> manifests (k8s_manifests.py) -> server dry-run -> Secrets and
             ConfigMaps from the files -> apply -> wait until every Deployment is available
render       only write the manifests to .k8s/manifests (read them; nothing is applied)
status       objects, pod restarts, node memory
netpol-test  EVERY app pod tries EVERY target; the result must equal the graph the policies were
             made from (allowed = open, forbidden = not open), plus a probe pod for ingress
rollout-demo restart api, then incident-service, under continuous traffic: 0 failed requests
down         delete the cluster (the compose infra keeps running)

Every kubectl call names the context `kind-aeoi`: your current kubectl context (maybe a real
cluster) is never used. Commands are argument LISTS, never a shell string (zsh lesson, Phase 22a).
Secret files are never opened here: kubectl reads them; the Secret object it builds passes
through this process in memory only (never printed, never written, no last-applied annotation).
"""

from __future__ import annotations

import argparse
import importlib.util
import ipaddress
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CLUSTER = "aeoi"
CONTEXT = f"kind-{CLUSTER}"
NODE = f"{CLUSTER}-control-plane"
COMPOSE_NETWORK = "aeoi_default"
KIND_CONFIG = ROOT / "infrastructure" / "kubernetes" / "kind" / "cluster.yaml"
WORK = ROOT / ".k8s"  # gitignored: the resolved compose config, facts, rendered manifests
HOST_PORTS = [8000, 8001, 8002, 8003, 8004, 8005, 8006, 8008, 8010]  # = cluster.yaml (tested)
ROLLOUT_S = 300
ROLLOUT_TIMEOUT = f"{ROLLOUT_S}s"
CONFIGMAP_MAX = 900 * 1024  # ConfigMaps are capped near 1 MiB
# `compose ps` lists only services of the profiles it is given (the Makefile's ALL_PROFILES)
ALL_PROFILES = [
    x for p in ("infra", "kafka", "tools", "apps", "apps-catalog") for x in ("--profile", p)
]


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    if spec is None or spec.loader is None:
        raise ImportError(name)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


km = _load("k8s_manifests")
apps21 = _load("phase21_apps")


class DeployError(RuntimeError):
    """A precondition failed; the message says what to run."""


@dataclass
class Runner:
    """subprocess with argument lists only. Tests replace `run`."""

    log: list[list[str]] = field(default_factory=list)

    def run(self, args: Sequence[str], *, stdin: str | None = None, check: bool = True,
            quiet: bool = False, live: bool = False) -> subprocess.CompletedProcess[str]:  # fmt: skip
        """live: the output goes straight to your terminal (slow steps); quiet: an error never
        prints the output (it may be a Secret)."""
        self.log.append(list(args))
        r = subprocess.run(  # noqa: S603 - argument list, no shell
            list(args), input=stdin, capture_output=not live, text=True, check=False, cwd=ROOT
        )
        if check and r.returncode != 0:
            detail = "" if quiet or live else (r.stderr or r.stdout).strip()[-800:]
            raise DeployError(f"failed: {' '.join(args[:4])} ...\n{detail}")
        return r

    def kubectl(self, *args: str, **kw: Any) -> subprocess.CompletedProcess[str]:
        return self.run(["kubectl", "--context", CONTEXT, *args], **kw)

    def kns(self, *args: str, **kw: Any) -> subprocess.CompletedProcess[str]:
        return self.kubectl("-n", km.NAMESPACE, *args, **kw)


# ---------------------------------------------------------------- facts


def compose_config(r: Runner) -> dict[str, Any]:
    """`docker compose config` with the profiles YOUR .env switches need (like make apps-up)."""
    os.environ.setdefault("AEOI_IMAGE_TAG", os.environ.get("IMAGE_TAG", "dev"))
    base = json.loads(
        r.run(["docker", "compose", "--profile", "apps", "config", "--format", "json"]).stdout
    )
    extra = [x for p in apps21.profiles(base) for x in ("--profile", p)]
    cfg: dict[str, Any] = json.loads(
        r.run(
            ["docker", "compose", "--profile", "apps", *extra, "config", "--format", "json"]
        ).stdout
    )
    return cfg


def pod_gateway(pod_cidr: str) -> str:
    """The first address of the node's pod range (the node side of the pod links in kindnet)."""
    return str(next(ipaddress.ip_network(pod_cidr).hosts()))


def ipv4_gateway(ipam: list[dict[str, Any]]) -> list[str]:
    """IPv4 gateways of a docker network (Docker Desktop may omit Gateway: then the first host)."""
    out = []
    for c in ipam:
        sub = c.get("Subnet", "")
        if not sub or ":" in sub:
            continue
        out.append(c.get("Gateway") or str(next(ipaddress.ip_network(sub).hosts())))
    return out


def facts_from(
    network: list[dict[str, Any]],
    host_lines: str,
    nodes: dict[str, Any],
    node_addrs: str,
    kind_network: list[dict[str, Any]],
    image_ids: dict[str, str],
) -> dict[str, Any]:
    subnets = [c["Subnet"] for c in network if ":" not in c.get("Subnet", ":")]  # IPv4 only
    host = next((ln.split()[0] for ln in host_lines.splitlines() if ln.strip()), "")
    # kubelet probes leave the node from one of ITS addresses (which one is a kindnet detail):
    # all of them, from `ip -4 -o addr` and the API, plus the pod-range gateway
    sources: list[str] = []
    for line in node_addrs.splitlines():
        parts = line.split()
        if "inet" in parts:
            addr = parts[parts.index("inet") + 1].split("/")[0]
            if not ipaddress.ip_address(addr).is_loopback:
                sources.append(addr)
    for n in nodes.get("items", []):
        sources += [a["address"] for a in n["status"]["addresses"] if a["type"] == "InternalIP"]
        if n["spec"].get("podCIDR"):
            sources.append(pod_gateway(n["spec"]["podCIDR"]))
    # the Mac's 127.0.0.1:80xx arrives from a Docker network GATEWAY: the `kind` one on a fresh
    # node, the compose one after a Docker restart (proven on the Mac 2026-10-20: adding only
    # 172.19.0.1/32 turned a timeout into 200). Both are host-side /32s, never a container.
    kind_gw = ipv4_gateway(kind_network)
    dev = sorted(set(kind_gw) | set(ipv4_gateway(network)))
    if not subnets:
        raise DeployError(f"no IPv4 subnet on {COMPOSE_NETWORK}")
    if not host:
        raise DeployError("host.docker.internal does not resolve on the kind node")
    if not kind_gw:
        raise DeployError("no IPv4 gateway on the docker network `kind`")
    return {"compose_subnets": subnets, "host_gateway": host,
            "node_sources": sorted(set(sources)), "dev_clients": dev,
            "image_ids": image_ids}  # fmt: skip


def read_facts(r: Runner, config: dict[str, Any]) -> dict[str, Any]:
    def ipam(network: str) -> list[dict[str, Any]]:
        out = r.run(["docker", "network", "inspect", network, "--format", "{{json .IPAM.Config}}"])
        return list(json.loads(out.stdout) or [])

    host = r.run(["docker", "exec", NODE, "getent", "ahostsv4", "host.docker.internal"]).stdout
    addrs = r.run(["docker", "exec", NODE, "ip", "-4", "-o", "addr", "show"]).stdout
    nodes = json.loads(r.kubectl("get", "nodes", "-o", "json").stdout)
    ids = {
        image: r.run(["docker", "image", "inspect", "--format", "{{.Id}}", image]).stdout.strip()
        for image in images(config)
    }
    return facts_from(ipam(COMPOSE_NETWORK), host, nodes, addrs, ipam("kind"), ids)


# ---------------------------------------------------------------- up / down


def port_busy(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def clusters(r: Runner) -> list[str]:
    return r.run(["kind", "get", "clusters"]).stdout.split()


def node_networks(r: Runner) -> list[str]:
    out = r.run(
        ["docker", "inspect", NODE, "--format", "{{json .NetworkSettings.Networks}}"]
    ).stdout
    return sorted(json.loads(out))


def up(r: Runner, busy: Callable[[int], bool] = port_busy) -> list[str]:
    for tool in ("kind", "kubectl", "docker"):
        if shutil.which(tool) is None:
            raise DeployError(f"{tool} is not installed (brew install {tool})")
    if r.run(["docker", "network", "inspect", COMPOSE_NETWORK], check=False).returncode:
        raise DeployError(f"no docker network {COMPOSE_NETWORK}: start the infra first (make up)")
    done = []
    if CLUSTER not in clusters(r):
        taken = [p for p in HOST_PORTS if busy(p)]
        if taken:
            raise DeployError(
                f"127.0.0.1 port(s) {taken} are in use - the cluster needs {HOST_PORTS}. Stop the "
                "compose apps (make apps-down) or host services (make stop-*) first"
            )
        r.run(["kind", "create", "cluster", "--name", CLUSTER, "--config", str(KIND_CONFIG),
               "--wait", "120s"], live=True)  # fmt: skip
        done.append(f"cluster {CLUSTER} created")
    else:
        done.append(f"cluster {CLUSTER} exists")
    if COMPOSE_NETWORK not in node_networks(r):
        # pods reach postgres/kafka/keycloak by their compose names only through this network
        r.run(["docker", "network", "connect", COMPOSE_NETWORK, NODE])
        done.append(f"node joined {COMPOSE_NETWORK}")
    return done


def down(r: Runner) -> list[str]:
    if CLUSTER not in clusters(r):
        return [f"no cluster {CLUSTER}"]
    r.run(["kind", "delete", "cluster", "--name", CLUSTER])
    return [f"cluster {CLUSTER} deleted; the app ports are free again"]


# ---------------------------------------------------------------- apply


def ps_rows(r: Runner, service: str) -> list[dict[str, Any]]:
    """`docker compose ps --format json`: one object per line (new) or one array (old)."""
    out = r.run(
        ["docker", "compose", *ALL_PROFILES, "ps", "--format", "json", service]
    ).stdout.strip()
    if out.startswith("["):
        return list(json.loads(out))
    return [json.loads(ln) for ln in out.splitlines() if ln.strip().startswith("{")]


def preflight(r: Runner, config: dict[str, Any]) -> None:
    if CLUSTER not in clusters(r):
        raise DeployError("no kind cluster: make kind-up")
    if COMPOSE_NETWORK not in node_networks(r):
        raise DeployError(f"the node is not on {COMPOSE_NETWORK}: make kind-up")
    if not any(row.get("Health") == "healthy" for row in ps_rows(r, "postgres")):
        raise DeployError("compose postgres is not healthy: make up")
    running = r.run(
        ["docker", "compose", *ALL_PROFILES, "ps", "--services", "--status", "running"]
    ).stdout.split()
    if "kafka" in apps21.profiles(config) and "kafka" not in running:
        raise DeployError(
            "your .env turns Kafka on, but compose kafka is not running: make up PROFILE=kafka"
        )
    on = sorted(set(running) & set(km.apps_of(config)))
    if on:
        raise DeployError(f"compose app containers still running ({', '.join(on)}): make apps-down")
    problems = [
        p for p in apps21.check(config, busy=lambda _p: False) if "only YOU can read" not in p
    ]  # kubectl reads them as you: no 644 needed
    if problems:
        raise DeployError("\n  - ".join(["missing pieces:", *problems]))


def data_command(item: dict[str, Any]) -> list[str]:
    """kubectl create ... --dry-run=client -o yaml: an object kubectl builds from the FILES."""
    if item["kind"] == "Secret":
        args = ["create", "secret", "generic", item["name"]]
        args += [f"--from-file={key}={path}" for key, path in sorted(item["files"].items())]
    else:
        folder = Path(item["directory"])
        files = sorted(p for p in folder.iterdir() if p.is_file()) if folder.is_dir() else []
        if not files:
            raise DeployError(f"{folder} is empty or missing (for {item['app']}): make db-seed")
        size = sum(p.stat().st_size for p in files)
        if size > CONFIGMAP_MAX:
            raise DeployError(f"{folder} is {size} bytes: too big for a ConfigMap")
        args = ["create", "configmap", item["name"], *[f"--from-file={p.name}={p}" for p in files]]
    return [*args, "-n", km.NAMESPACE, "--dry-run=client", "-o", "yaml"]


def render(r: Runner, config: dict[str, Any] | None = None) -> tuple[dict[str, Any], list[Path]]:
    if CLUSTER not in clusters(r):
        raise DeployError("no kind cluster: make kind-up (the manifests need its addresses)")
    WORK.mkdir(exist_ok=True)
    config = config or compose_config(r)
    (WORK / "compose.json").write_text(json.dumps(config))
    facts = read_facts(r, config)
    (WORK / "facts.json").write_text(json.dumps(facts, indent=2))
    files = km.build(config, km.Facts.from_json(facts))
    return config, km.write(files, WORK / "manifests")


def images(config: dict[str, Any]) -> list[str]:
    return sorted({a.image for a in km.apps_of(config).values()})


def apply(r: Runner) -> list[str]:
    config = compose_config(r)
    preflight(r, config)
    config, paths = render(r, config)
    done = []
    for image in images(config):
        # kind skips an image whose ID the node already has; a rebuilt image is loaded again
        r.run(["kind", "load", "docker-image", image, "--name", CLUSTER], live=True)
        done.append(f"loaded {image}")
    ns, rest = paths[0], paths[1:]
    r.kubectl("apply", "--server-side", "-f", str(ns))
    # the API server validates everything first (schema + Pod Security "restricted")
    for p in rest:
        dry = r.kubectl("apply", "--server-side", "--dry-run=server", "-f", str(p))
        # Pod Security is ENFORCED on pods only; for a Deployment it is a warning, and the pods
        # would then silently never start: a warning here is an error
        if "PodSecurity" in dry.stderr:
            raise DeployError(f"{p.name} violates Pod Security restricted:\n{dry.stderr.strip()}")
    for item in km.data_plan(config):
        # server-side apply: no last-applied annotation holding a copy of the secret
        obj = r.kubectl(*data_command(item), quiet=True).stdout
        r.kubectl("apply", "--server-side", "-f", "-", stdin=obj, quiet=True)
        done.append(f"{item['kind']} {item['name']}")
    for p in rest:
        r.kubectl("apply", "--server-side", "-f", str(p))
    deadline = time.monotonic() + ROLLOUT_S  # ONE budget for all: not 9 x 300 s when probes fail
    for app in km.apps_of(config):
        left = max(10, int(deadline - time.monotonic()))
        r.kns("rollout", "status", f"deployment/{app}", f"--timeout={left}s", live=True)
        done.append(f"{app} available")
    return done


def status(r: Runner) -> str:
    parts = [r.kns("get", "deployments,pods,services,networkpolicies", "-o", "wide").stdout]
    pods = json.loads(r.kns("get", "pods", "-o", "json").stdout)
    for p in pods.get("items", []):
        for c in p["status"].get("containerStatuses", []):
            last = (c.get("lastState") or {}).get("terminated") or {}
            parts.append(f"{p['metadata']['name']}: restarts={c['restartCount']}"
                         f" last={last.get('reason', '-')}")  # fmt: skip
    parts.append(r.run(["docker", "stats", "--no-stream", "--format",
                        "{{.Name}}\t{{.MemUsage}}\t{{.CPUPerc}}", NODE]).stdout)  # fmt: skip
    return "\n".join(parts)


# ---------------------------------------------------------------- netpol-test

# runs INSIDE a pod (python -c, one argument: no shell). TCP connect only, no payload.
PROBE_CODE = r"""
import errno, json, socket, sys, concurrent.futures as cf
def one(t):
    h, p = t.rsplit(":", 1)
    try:
        socket.getaddrinfo(h, int(p), socket.AF_INET)
    except OSError:
        return t, "dns"
    try:
        socket.create_connection((h, int(p)), timeout=3).close()
        return t, "open"
    except ConnectionRefusedError:
        return t, "refused"
    except (TimeoutError, socket.timeout):
        return t, "timeout"
    except OSError as e:
        # a policy may answer with ICMP "unreachable" instead of a silent drop
        if e.errno in (errno.EHOSTUNREACH, errno.ENETUNREACH):
            return t, "unreachable"
        return t, "error:" + (errno.errorcode.get(e.errno or 0) or type(e).__name__)
with cf.ThreadPoolExecutor(32) as ex:
    print(json.dumps(dict(ex.map(one, sys.argv[1:]))))
"""
INTERNET_TARGET = "api.anthropic.com:443"
HOST_NAME = "host.docker.internal"
PROBE = "netpol-probe"


def targets(g: Any) -> dict[str, str]:
    """target -> its owner key in the graph."""
    out = {f"{a}:{p}": a for a, p in g.apps.items()}
    for per in g.infra.values():
        for infra, port in per.items():
            out[f"{infra}:{port}"] = infra
    for ports in g.host.values():
        for port in ports:
            out[f"{HOST_NAME}:{port}"] = "host"
    if g.internet:
        out[INTERNET_TARGET] = "internet"
    return out


def allowed(g: Any, src: str, target: str) -> bool:
    host, port = target.rsplit(":", 1)
    if host in g.apps:
        return bool(host in g.calls.get(src, set()))
    if host in km.INFRA:
        return bool(g.infra.get(src, {}).get(host) == int(port))
    if host == HOST_NAME:
        return bool(int(port) in g.host.get(src, set()))
    return bool(target == INTERNET_TARGET and src in g.internet)


# dropped, rejected or answered "unreachable" by a policy; "dns"/"error:*" prove nothing (Mac,
# 22a fix2: external targets gave EHOSTUNREACH, pod targets a timeout)
CLOSED = {"timeout", "refused", "unreachable"}
MUST_BE_UP = "postgres:5432"  # the headline: agents -> postgres is only shown CLOSED if it is up


def judge(g: Any, results: dict[str, dict[str, str]]) -> tuple[list[str], list[str], list[str]]:
    """(failures, skipped, passed). A target counts as UP only if an allowed source opened it;
    a forbidden check against a target that is down proves nothing: skipped, never passed.
    In-cluster names always resolve, so "dns"/"error" for an app target is a failure."""
    up = {t for src, res in results.items() for t, v in res.items()
          if v == "open" and allowed(g, src, t)}  # fmt: skip
    fail: list[str] = []
    skip: list[str] = []
    ok: list[str] = []
    for src, res in sorted(results.items()):
        for t, v in sorted(res.items()):
            line = f"{src:>16} -> {t:<26} {v}"
            is_app = t.split(":")[0] in g.apps
            if t.split(":")[0] == src:
                skip.append(line + "  (its own Service)")
            elif src == PROBE:
                if v == "open":
                    fail.append(line + "  (probe: no app admits it)")
                elif v not in CLOSED:
                    fail.append(line + "  (probe: name or network broken)")
                else:
                    (ok if t in up else skip).append(line)
            elif allowed(g, src, t):
                if v == "open":
                    ok.append(line)
                elif is_app or t in up:
                    fail.append(line + "  (ALLOWED call blocked)")
                else:
                    skip.append(line + "  (allowed; target not reachable - not running?)")
            elif v == "open":
                fail.append(line + "  (FORBIDDEN but open)")
            elif v in CLOSED:
                if t in up:
                    ok.append(line)
                else:
                    skip.append(line + "  (forbidden; target never shown up, proves nothing)")
            elif is_app:
                fail.append(line + "  (name or network broken)")
            elif t in up:
                skip.append(line + "  (forbidden; unexplained error, target IS up - look)")
            else:
                skip.append(line + "  (forbidden; target down, proves nothing)")
    if results and MUST_BE_UP not in up:
        fail.append(
            f"{MUST_BE_UP} was not reached by ANY allowed pod: the deny checks prove nothing"
        )
    return fail, skip, ok


def probe_objects(image: str) -> list[dict[str, Any]]:
    """A pod that may send anywhere INSIDE the namespace; no app admits it, so every app port
    must stay closed to it: this tests the INGRESS side (the matrix tests both together)."""
    sel = {"matchLabels": {km.NAME: PROBE}}
    pol = {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
           "metadata": km.meta(PROBE),
           "spec": {"podSelector": sel, "policyTypes": ["Egress"],
                    "egress": [{"to": [{"podSelector": {}}]}]}}  # fmt: skip
    pod = {"apiVersion": "v1", "kind": "Pod", "metadata": km.meta(PROBE),
           "spec": {"automountServiceAccountToken": False, "enableServiceLinks": False,
                    "securityContext": km.pod_security(), "terminationGracePeriodSeconds": 0,
                    "containers": [{"name": "app", "image": image, "imagePullPolicy": "Never",
                                    "command": ["python", "-c", "import time; time.sleep(900)"],
                                    "resources": {"requests": {"memory": "32Mi", "cpu": "10m"},
                                                  "limits": {"memory": "64Mi"}},
                                    "securityContext": km.container_security()}]}}  # fmt: skip
    return [pol, pod]


# The Mac -> 127.0.0.1:<port> path (Phase 23c). The pod matrix never tested it, and on
# 2026-10-19 it said "0 FAILED" while every request from the Mac timed out after a Docker
# restart. Docker's port proxy ACCEPTS the TCP connection even when nothing answers behind it
# ("0 bytes received"), so only an HTTP answer counts - any status code.
_HOST_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
HOST_HINT = ("no answer from the Mac. After a Docker restart this was seen twice (phase-23.md 8b):"
             " BEFORE recreating the node run `docker inspect aeoi-control-plane` (its networks)"
             " and, in the node, `conntrack -L | grep 30800` while a curl runs (the real src=)")  # fmt: skip


def host_probe(url: str, timeout: float = 5.0) -> str:
    """'http:<status>' when the app answered; otherwise why not."""
    try:
        with _HOST_OPENER.open(url, timeout=timeout) as resp:
            return f"http:{resp.status}"
    except urllib.error.HTTPError as e:
        return f"http:{e.code}"
    except urllib.error.URLError as e:
        reason = e.reason
        if isinstance(reason, ConnectionRefusedError):
            return "refused"
        if isinstance(reason, (TimeoutError, socket.timeout)):
            return "timeout"
        return f"error:{type(reason).__name__}"
    except TimeoutError:
        return "timeout"  # the connection was accepted, then nothing came back
    except (ConnectionError, OSError) as e:
        return f"no-answer:{type(e).__name__}"  # e.g. accepted, then closed (RemoteDisconnected)


def host_urls(apps: dict[str, Any]) -> dict[str, str]:
    """app -> the liveness URL the Mac reaches through kind's port mapping."""
    return {name: f"http://127.0.0.1:{a.port}{km.IMAGES[a.kind]['live']}"
            for name, a in sorted(apps.items())}  # fmt: skip


def judge_host(results: dict[str, str]) -> tuple[list[str], list[str]]:
    """(failures, passed): every app must ANSWER the Mac."""
    fail: list[str] = []
    ok: list[str] = []
    for name, v in sorted(results.items()):
        line = f"{'mac':>16} -> {name:<26} {v}"
        (ok if v.startswith("http:") else fail).append(line if v.startswith("http:")
                                                       else f"{line}  ({HOST_HINT})")  # fmt: skip
    return fail, ok


def netpol_test(r: Runner) -> int:
    config = compose_config(r)  # the switches as they are NOW (they decide the graph)
    apps = km.apps_of(config)
    g = km.graph(config, apps)
    tlist = sorted(targets(g))
    py_image = next(a.image for a in apps.values() if a.kind == "aeoi-py")
    results: dict[str, dict[str, str]] = {}
    for src, app in sorted(apps.items()):
        if app.kind != "aeoi-py":
            continue  # the catalog image has no python; it is still a TARGET
        out = r.kns(
            "exec", f"deployment/{src}", "-c", "app", "--", "python", "-c", PROBE_CODE, *tlist
        )
        results[src] = json.loads(out.stdout.strip().splitlines()[-1])
    doc = "---\n".join(json.dumps(o) + "\n" for o in probe_objects(py_image))
    r.kubectl("apply", "--server-side", "-f", "-", stdin=doc)
    try:
        r.kns("wait", "--for=condition=Ready", f"pod/{PROBE}", "--timeout=90s")
        app_targets = [t for t in tlist if t.split(":")[0] in g.apps]
        out = r.kns("exec", PROBE, "--", "python", "-c", PROBE_CODE, *app_targets)
        results[PROBE] = json.loads(out.stdout.strip().splitlines()[-1])
    finally:
        r.kns(
            "delete", "pod,networkpolicy", PROBE, "--ignore-not-found", "--wait=false", check=False
        )
    fail, skip, ok = judge(g, results)
    host_fail, host_ok = judge_host(
        {name: host_probe(url) for name, url in host_urls(apps).items()}
    )
    fail += host_fail
    ok += host_ok
    for title, lines in (("PASS", ok), ("SKIPPED", skip), ("FAIL", fail)):
        print(f"--- {title} ({len(lines)})")
        print("\n".join(lines))
    print(f"netpol-test: {len(ok)} passed, {len(skip)} skipped, {len(fail)} FAILED")
    return 1 if fail else 0


# ---------------------------------------------------------------- rollout demo

DEMO_URL = "http://127.0.0.1:8000/api/v1/incidents?limit=1"  # api -> incident-service
DEMO_EVERY_S = 0.6  # 1.7 req/s: under the api's 120/min per-user limit (a 429 would be OUR bug)
FAST_EVERY_S = 0.05  # 20 req/s straight at each pod: a gap of 0.1 s is seen
# (name, url, needs the token, interval). The product GET goes api -> incident-service, and the
# api's proxy RETRIES a GET on a reset - so it alone could hide a drop. The two /health/live loads
# go straight to each pod through its NodePort: nothing retries them.
DEMO_LOADS = [
    ("api GET /api/v1/incidents", DEMO_URL, True, DEMO_EVERY_S),
    ("api /health/live", "http://127.0.0.1:8000/health/live", False, FAST_EVERY_S),
    ("incident-service /health/live", "http://127.0.0.1:8001/health/live", False, FAST_EVERY_S),
]


@dataclass
class Traffic:
    token: str | None
    url: str = DEMO_URL
    every_s: float = DEMO_EVERY_S
    results: list[tuple[float, str]] = field(default_factory=list)
    _stop: threading.Event = field(default_factory=threading.Event)

    def one(self) -> str:
        """A NEW connection per request (what a host client sees); inside the cluster the api
        keeps its pooled keep-alive connections to incident-service."""
        if not self.url.startswith("http://127.0.0.1:"):
            raise DeployError(f"the demo only calls the local api, not {self.url}")
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        req = urllib.request.Request(self.url, headers=headers)  # noqa: S310
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(req, timeout=5) as resp:
                return str(resp.status)
        except urllib.error.HTTPError as e:
            return str(e.code)
        except (urllib.error.URLError, OSError) as e:
            return f"error:{type(getattr(e, 'reason', e)).__name__}"

    def loop(self) -> None:
        while not self._stop.is_set():
            self.results.append((time.time(), self.one()))
            self._stop.wait(self.every_s)

    def stop(self) -> None:
        self._stop.set()


def pods_of(r: Runner, app: str) -> list[str]:
    out = r.kns("get", "pods", "-l", f"{km.NAME}={app}", "-o", "jsonpath={.items[*].metadata.name}")
    return sorted(out.stdout.split())


def summarize(results: list[tuple[float, str]]) -> tuple[int, dict[str, int]]:
    bad: dict[str, int] = {}
    for _, s in results:
        if s != "200":
            bad[s] = bad.get(s, 0) + 1
    return len(results), bad


def rollout_demo(r: Runner, token: str, apps: Sequence[str] = ("api", "incident-service")) -> int:
    loads = [(name, Traffic(token if auth else None, url, every))
             for name, url, auth, every in DEMO_LOADS]  # fmt: skip
    for name, t in loads:
        warm = [t.one() for _ in range(3)]
        if any(s != "200" for s in warm):
            print(f"rollout-demo: {name} not healthy BEFORE the demo: {warm} (make k8s-status)")
            return 2
    threads = [threading.Thread(target=t.loop, daemon=True) for _, t in loads]
    for th in threads:
        th.start()
    try:
        for app in apps:
            before = pods_of(r, app)
            t0 = time.monotonic()
            r.kns("rollout", "restart", f"deployment/{app}")
            r.kns("rollout", "status", f"deployment/{app}", f"--timeout={ROLLOUT_TIMEOUT}")
            for old in before:  # traffic keeps running until the old pod is really gone
                r.kns("wait", "--for=delete", f"pod/{old}", "--timeout=90s", check=False)
            time.sleep(3)
            after = pods_of(r, app)
            print(f"{app}: {before} -> {after} in {time.monotonic() - t0:.0f} s")
            if set(before) & set(after):
                print(f"rollout-demo: {app} was NOT replaced")
                return 2
    finally:
        for _, t in loads:
            t.stop()
        for th in threads:
            th.join(timeout=10)
    failed = 0
    for name, t in loads:
        total, bad = summarize(t.results)
        failed += sum(bad.values())
        print(f"  {name:<32} {total:>5} requests, {sum(bad.values())} failed {bad or ''}")
        for ts, s in [x for x in t.results if x[1] != "200"][:5]:
            print(f"      {time.strftime('%H:%M:%S', time.localtime(ts))} {s}")
    print(f"rollout-demo: {failed} failed requests")
    return 1 if failed else 0


# ---------------------------------------------------------------- main


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["up", "apply", "render", "status", "netpol-test",
                                    "rollout-demo", "down"])  # fmt: skip
    args = ap.parse_args(argv)
    r = Runner()
    try:
        if args.cmd == "up":
            print("\n".join(up(r)) + "\nnext: make k8s-apply")
            print(
                "note: kind made kind-aeoi your CURRENT kubectl context; our scripts never rely on it"
            )
        elif args.cmd == "down":
            print("\n".join(down(r)))
        elif args.cmd == "render":
            _, paths = render(r)
            print("\n".join(str(p.relative_to(ROOT)) for p in paths))
        elif args.cmd == "apply":
            print("\n".join(apply(r)) + "\nnext: make smoke, make k8s-netpol-test")
        elif args.cmd == "status":
            print(status(r))
        elif args.cmd == "netpol-test":
            return netpol_test(r)
        elif args.cmd == "rollout-demo":
            token = os.environ.get("TOKEN", "")
            if not token:
                raise DeployError("TOKEN is not set (the make target sets it)")
            return rollout_demo(r, token)
    except (DeployError, km.ComposeError) as exc:
        print(f"k8s: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
