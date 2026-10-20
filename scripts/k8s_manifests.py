"""Kubernetes manifests GENERATED from docker-compose.yml (Phase 22a, ADR-034).

    uv run python scripts/k8s_manifests.py <compose config json> <facts json> <out dir>

Input 1 is `docker compose config --format json` (compose has already read .env), so every
container gets EXACTLY the command, port, environment and secret files it gets in Phase 21 -
one source of truth, nothing re-typed. Input 2 is what only the running cluster knows (the
compose subnet, the host gateway, the node addresses), read at deploy time by k8s_deploy.py.

This file adds only what is Kubernetes-specific:
- probes (startup / readiness / liveness), measured memory requests, a hardened pod;
- an initContainer for every compose `depends_on` on another APP (Kubernetes has none);
- NetworkPolicies: default deny, then EXACTLY the calls the configuration makes (the URL graph
  the Phase 21 contract test checks) - nothing else is reachable;
- dev only: NodePort services on 127.0.0.1, so the existing smokes run unchanged.
Anything in compose this file does not understand is an ERROR, never silently dropped.
Pure: no IO except `main`. Secret CONTENT is never read: Secrets are created from the files by
k8s_deploy.py (kubectl reads them).
"""

from __future__ import annotations

import ipaddress
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

NAMESPACE = "ai-engineering-platform"
UID = 10001  # USER in both Dockerfiles (ADR-033)
NAME = "app.kubernetes.io/name"
PART_OF = {"app.kubernetes.io/part-of": "aeoi"}
SECRETS_DIR = "/app/secrets"
PRESTOP_S = 5  # endpoints are removed BEFORE the process gets SIGTERM (zero-downtime rollout)
GRACE_S = 30
WAIT_TIMEOUT_S = 600

# Memory REQUESTS from the Phase 21 measurement (ADR-033: peaks 81-125 MiB Python, 173 MiB the
# catalog JVM). Limits come from compose itself. CPU requests are scheduling hints, NOT measured.
IMAGES: dict[str, dict[str, Any]] = {
    "aeoi-py": {
        "memory_request": "128Mi",
        "cpu_request": "100m",
        "live": "/health/live",
        "ready": "/health/ready",
        "startup_failures": 30,  # x 2 s = 60 s (the image HEALTHCHECK start period)
    },
    "aeoi-catalog": {
        "memory_request": "192Mi",
        "cpu_request": "250m",
        "live": "/actuator/health/liveness",
        "ready": "/actuator/health/readiness",
        "startup_failures": 90,  # x 2 s = 180 s (JVM: 39-47 s on 1 CPU, Phase 21)
    },
}
# compose services outside the cluster (ADR-034: infra stays in compose = "managed services")
# (redis comes with the kafka profile; no app calls it until 22b, ADR-031, so it gets no rule)
# otel-collector (Phase 23a, ADR-035): every app exports OTLP there when .env switches it on
INFRA = {"postgres", "kafka", "keycloak", "redis", "otel-collector"}
HOST_NAMES = {"host.docker.internal"}  # Docker Desktop's name for the Mac (native Ollama)
# Kafka clients cannot be read from the environment (every app gets the bootstrap address from
# one anchor): the services whose code imports aiokafka or aeoi_messaging (a test checks)
KAFKA_CLIENTS = {"incident-service", "audit"}
# Internet egress, port 443 only, private ranges excepted: the hosted LLM providers
INTERNET_HTTPS = {"llm-gateway"}
PRIVATE = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16",
           "100.64.0.0/10", "127.0.0.0/8"]  # fmt: skip
# variables that hold an address which is NOT called (anything else unknown is an error)
NOT_CALLS = {
    "AEOI_OIDC_ISSUER": "the token `iss` (an identifier); the keys come from AEOI_OIDC_JWKS_URL",
}
# the 9 app ports on 127.0.0.1 (dev only): container port 80xx -> NodePort 308xx
NODEPORT_OFFSET = 22800

# compose keys and what happens to them; any OTHER key fails the generation
HANDLED = {
    "command", "environment", "image", "ports", "secrets", "volumes", "tmpfs", "depends_on",
    "deploy", "read_only", "cap_drop", "security_opt", "pull_policy",
}  # fmt: skip
IGNORED = {
    "profiles": "which services exist is decided before (k8s_deploy.py, like make apps-up)",
    "restart": "a Deployment always restarts",
    "logging": "kubelet rotates container logs",
    "init": "PID 1 is uvicorn / the JVM, both handle SIGTERM",
    "networks": "one cluster network; NetworkPolicies decide",
    "entrypoint": "must be null: the image entrypoint is kept (checked)",
    "extra_hosts": "pods resolve host.docker.internal through Docker's DNS (measured)",
    "healthcheck": "the image HEALTHCHECK is Docker-only; probes below",
}

_URL = re.compile(r"\bhttps?://([a-z0-9.-]+):(\d+)")
_HOSTPORT = re.compile(r"^([a-z0-9.-]+):(\d+)$")


class ComposeError(ValueError):
    """compose holds something this generator would have to guess about."""


@dataclass(frozen=True)
class Facts:
    """What only the running cluster and Docker know (read by k8s_deploy.py, never typed)."""

    compose_subnets: list[str]  # aeoi_default: postgres, kafka, keycloak live here
    host_gateway: str  # host.docker.internal as the node resolves it (native Ollama)
    node_sources: list[str]  # EVERY IPv4 address of the node: the kubelet probes come from one
    dev_clients: list[str]  # the `kind` network gateway: the Mac's 127.0.0.1:80xx arrives from it
    image_ids: dict[str, str] = field(default_factory=dict)  # image -> ID (a rebuild rolls pods)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Facts:
        facts = cls(
            compose_subnets=[str(ipaddress.ip_network(s)) for s in data["compose_subnets"]],
            host_gateway=str(ipaddress.ip_address(data["host_gateway"])),
            node_sources=[str(ipaddress.ip_address(a)) for a in data["node_sources"]],
            dev_clients=[str(ipaddress.ip_address(a)) for a in data["dev_clients"]],
            image_ids={str(k): str(v) for k, v in (data.get("image_ids") or {}).items()},
        )
        if not facts.compose_subnets or not facts.node_sources or not facts.dev_clients:
            raise ComposeError("facts: no compose subnet, node address or dev client address")
        return facts


@dataclass
class App:
    name: str
    image: str
    kind: str  # key of IMAGES
    port: int
    spec: dict[str, Any]


@dataclass
class Graph:
    """Who may call whom, from the configuration only. The policies AND the Mac matrix
    (k8s_deploy.py netpol-test) use it: the matrix proves the cluster ENFORCES this graph; that
    the graph itself is right is proven by the unit tests, which take their truth from the
    Settings classes (tests/unit/test_k8s_manifests.py, expected_edges)."""

    apps: dict[str, int]  # name -> port
    calls: dict[str, set[str]] = field(default_factory=dict)  # app -> apps it calls
    infra: dict[str, dict[str, int]] = field(default_factory=dict)  # app -> {infra: port}
    host: dict[str, set[int]] = field(default_factory=dict)  # app -> host ports
    internet: set[str] = field(default_factory=set)

    def callers(self, target: str) -> list[str]:
        return sorted(a for a, t in self.calls.items() if target in t)


# ---------------------------------------------------------------- reading compose


def image_kind(image: str) -> str:
    repo = image.rsplit("/", 1)[-1].split(":", 1)[0]
    if repo not in IMAGES:
        raise ComposeError(f"unknown image {image!r}: add its probes to IMAGES")
    return repo


def apps_of(config: dict[str, Any]) -> dict[str, App]:
    """The services built here (pull_policy never); everything else is infra."""
    out = {}
    for name, spec in sorted((config.get("services") or {}).items()):
        if spec.get("pull_policy") != "never":
            if name not in INFRA:
                raise ComposeError(f"{name}: neither an app (pull_policy never) nor known infra")
            continue
        unknown = set(spec) - HANDLED - set(IGNORED)
        if unknown:
            raise ComposeError(
                f"{name}: compose keys the generator does not handle: {sorted(unknown)}"
            )
        if spec.get("entrypoint") not in (None, []):
            raise ComposeError(f"{name}: entrypoint overrides are not translated")
        targets = [port_target(p) for p in spec.get("ports") or []]
        if len(targets) != 1:
            raise ComposeError(f"{name}: exactly one port expected, got {targets}")
        env = environment(spec)
        if "AEOI_SERVICE_PORT" in env and int(env["AEOI_SERVICE_PORT"]) != targets[0]:
            raise ComposeError(f"{name}: AEOI_SERVICE_PORT != the container port")
        out[name] = App(name, spec["image"], image_kind(spec["image"]), targets[0], spec)
    if not out:
        raise ComposeError("no app services in this compose config")
    return out


def port_target(p: Any) -> int:
    """The CONTAINER port: {"target": 8000} (compose config) or "127.0.0.1:8000:8000" (file)."""
    if isinstance(p, dict):
        return int(p["target"])
    return int(str(p).rsplit(":", 1)[-1].split("/")[0])


def environment(spec: dict[str, Any]) -> dict[str, str]:
    raw = spec.get("environment") or {}
    if isinstance(raw, list):
        raise ComposeError("environment as a list is not expected in `compose config` output")
    return {k: "" if v is None else str(v) for k, v in raw.items()}


def addresses(value: str) -> list[tuple[str, int]]:
    """host:port pairs a value points at: URLs, or a JSON list of "host:port" (the allow-list)."""
    found = [(h, int(p)) for h, p in _URL.findall(value)]
    if value.startswith("["):
        try:
            items = json.loads(value)
        except json.JSONDecodeError:
            items = []
        for item in items if isinstance(items, list) else []:
            m = _HOSTPORT.match(str(item))
            if m:
                found.append((m[1], int(m[2])))
    return found


def mem_bytes(value: Any) -> int:
    """compose config gives bytes ("268435456"); the raw file gives "256M" (binary units)."""
    s = str(value).strip().lower()
    m = re.fullmatch(r"(\d+)\s*([kmg]?)i?b?", s)
    if not m:
        raise ComposeError(f"unreadable memory value {value!r}")
    return int(m[1]) * {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3}[m[2]]


def k8s_memory(value: Any) -> str:
    """Kubernetes "256M" is 10^6 bytes; compose "256M" is MiB - always write Mi."""
    n = mem_bytes(value)
    if n % 1024**2:
        raise ComposeError(f"memory {value!r} is not a whole number of MiB")
    return f"{n // 1024**2}Mi"


def graph(config: dict[str, Any], apps: dict[str, App] | None = None) -> Graph:
    apps = apps or apps_of(config)
    g = Graph(apps={a.name: a.port for a in apps.values()})
    for app in apps.values():
        env = environment(app.spec)
        for var, value in sorted(env.items()):
            if var in NOT_CALLS:
                continue
            for host, port in addresses(value):
                if host in apps:
                    if port != apps[host].port:
                        raise ComposeError(
                            f"{app.name}: {var} -> {host}:{port}, but {host} listens on {apps[host].port}"
                        )
                    if host != app.name:
                        g.calls.setdefault(app.name, set()).add(host)
                elif host in INFRA:
                    g.infra.setdefault(app.name, {})[host] = port
                elif host in HOST_NAMES:
                    g.host.setdefault(app.name, set()).add(port)
                elif host in config.get("services", {}):
                    raise ComposeError(
                        f"{app.name}: {var} -> {host}, a compose service not in this cluster"
                    )
                elif host == "service-catalog":
                    continue  # the Java switch is off: its service is not deployed, never called
                else:
                    raise ComposeError(
                        f"{app.name}: {var} points at {host}:{port} - unknown target"
                    )
        # the database: every service that holds a DB login (one *_svc_password file per login)
        if any(Path(t).name.endswith("_svc_password.txt") for t in secret_targets(app)):
            g.infra.setdefault(app.name, {})["postgres"] = int(env.get("AEOI_PG_PORT", "5432"))
        if app.name in KAFKA_CLIENTS:
            bootstrap = env.get("AEOI_KAFKA_BOOTSTRAP_SERVERS", "")
            for item in filter(None, bootstrap.split(",")):
                m = _HOSTPORT.match(item.strip())
                if not m or m[1] not in INFRA:
                    raise ComposeError(f"{app.name}: Kafka bootstrap {item!r} is not compose infra")
                g.infra.setdefault(app.name, {})[m[1]] = int(m[2])
        if app.name in INTERNET_HTTPS:
            g.internet.add(app.name)
    return g


def secret_targets(app: App) -> list[str]:
    return [ref["target"] if isinstance(ref, dict) else f"/run/secrets/{ref}"
            for ref in app.spec.get("secrets") or []]  # fmt: skip


# ---------------------------------------------------------------- data plan (for k8s_deploy)


def data_plan(config: dict[str, Any], apps: dict[str, App] | None = None) -> list[dict[str, Any]]:
    """The Secrets and ConfigMaps each pod mounts, with their HOST files. k8s_deploy.py creates
    them with kubectl from the files; nothing here opens a file."""
    apps = apps or apps_of(config)
    files = {n: s.get("file", "") for n, s in (config.get("secrets") or {}).items()}
    plan = []
    for app in apps.values():
        refs = app.spec.get("secrets") or []
        if refs:
            items = {}
            for ref in refs:
                if not isinstance(ref, dict):
                    raise ComposeError(
                        f"{app.name}: short secret syntax (target /run/secrets) not expected"
                    )
                target = Path(ref["target"])
                if str(target.parent) != SECRETS_DIR:
                    raise ComposeError(f"{app.name}: secret target {target} outside {SECRETS_DIR}")
                if target.name in items:
                    raise ComposeError(f"{app.name}: two secrets mounted as {target.name}")
                if not files.get(ref["source"]):
                    raise ComposeError(f"{app.name}: secret {ref['source']} has no file")
                items[target.name] = files[ref["source"]]
            plan.append({"kind": "Secret", "name": f"{app.name}-files", "app": app.name,
                         "mount": SECRETS_DIR, "files": items})  # fmt: skip
        for i, vol in enumerate(app.spec.get("volumes") or []):
            if not (isinstance(vol, dict) and vol.get("type") == "bind" and vol.get("read_only")):
                raise ComposeError(f"{app.name}: only read-only bind mounts are translated: {vol}")
            plan.append({"kind": "ConfigMap", "name": f"{app.name}-data{i}", "app": app.name,
                         "mount": vol["target"], "directory": vol["source"]})  # fmt: skip
    return plan


# ---------------------------------------------------------------- objects


def labels(name: str) -> dict[str, str]:
    return {NAME: name, **PART_OF}


def meta(name: str, **extra: Any) -> dict[str, Any]:
    return {"name": name, "namespace": NAMESPACE, "labels": labels(name), **extra}


def namespace() -> dict[str, Any]:
    # Pod Security Admission "restricted": the API SERVER refuses a pod that is root, keeps a
    # capability, allows privilege escalation or has no seccomp profile - not only our tests
    pss = {f"pod-security.kubernetes.io/{m}": "restricted" for m in ("enforce", "warn", "audit")}
    return {"apiVersion": "v1", "kind": "Namespace",
            "metadata": {"name": NAMESPACE, "labels": {**PART_OF, **pss}}}  # fmt: skip


def container_security() -> dict[str, Any]:
    return {
        "allowPrivilegeEscalation": False,
        "readOnlyRootFilesystem": True,
        "capabilities": {"drop": ["ALL"]},
        "runAsNonRoot": True,
    }


def pod_security() -> dict[str, Any]:
    return {
        "runAsNonRoot": True,
        "runAsUser": UID,
        "runAsGroup": UID,
        "fsGroup": UID,  # mounted Secret files are group 10001, mode 0440: no 644 needed
        "seccompProfile": {"type": "RuntimeDefault"},
    }


def tmpfs_volumes(app: App) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    vols, mounts = [], []
    for i, entry in enumerate(app.spec.get("tmpfs") or []):
        path, _, opts = str(entry).partition(":")
        size = "64Mi"  # compose without a size (the catalog's /tmp): the Python one's 64m
        for opt in filter(None, opts.split(",")):
            key, _, val = opt.partition("=")
            if key != "size":
                raise ComposeError(f"{app.name}: tmpfs option {opt!r} not translated")
            size = k8s_memory(val)
        vols.append({"name": f"tmp{i}", "emptyDir": {"medium": "Memory", "sizeLimit": size}})
        mounts.append({"name": f"tmp{i}", "mountPath": path})
    return vols, mounts


def check_hardening(app: App) -> None:
    s = app.spec
    if s.get("read_only") is not True:
        raise ComposeError(f"{app.name}: compose is not read_only - fix compose first")
    if s.get("cap_drop") != ["ALL"]:
        raise ComposeError(f"{app.name}: compose does not drop ALL capabilities")
    if "no-new-privileges:true" not in (s.get("security_opt") or []):
        raise ComposeError(f"{app.name}: compose lacks no-new-privileges")


def probe(path: str, period: int, failures: int, timeout: int = 3) -> dict[str, Any]:
    return {"httpGet": {"path": path, "port": "http"}, "periodSeconds": period,
            "timeoutSeconds": timeout, "failureThreshold": failures}  # fmt: skip


def resources(app: App) -> dict[str, Any]:
    kind = IMAGES[app.kind]
    limits = ((app.spec.get("deploy") or {}).get("resources") or {}).get("limits") or {}
    if "memory" not in limits:
        raise ComposeError(f"{app.name}: no memory limit in compose")
    out: dict[str, Any] = {
        "requests": {"memory": kind["memory_request"], "cpu": kind["cpu_request"]},
        "limits": {"memory": k8s_memory(limits["memory"])},
    }
    if "cpus" in limits:  # the same CPU cap as compose (the catalog JVM needs its 2.0 to start)
        out["limits"]["cpu"] = f"{round(float(limits['cpus']) * 1000)}m"
    if mem_bytes(out["requests"]["memory"].replace("Mi", "M")) > mem_bytes(limits["memory"]):
        raise ComposeError(f"{app.name}: memory request above the compose limit")
    return out


def wait_urls(app: App, apps: dict[str, App]) -> list[str]:
    """compose depends_on on another APP -> the URLs the initContainer waits for."""
    deps = app.spec.get("depends_on") or {}
    out = []
    for dep, how in sorted(deps.items()):
        if dep in INFRA:
            continue  # compose infra: checked by k8s_deploy.py before apply
        if dep not in apps:
            raise ComposeError(f"{app.name}: depends_on {dep}, which is not deployed")
        if (how or {}).get("condition") != "service_healthy":
            raise ComposeError(f"{app.name}: depends_on {dep} without service_healthy")
        d = apps[dep]
        out.append(f"http://{dep}:{d.port}{IMAGES[d.kind]['ready']}")
    return out


def template_meta(app: App, image_id: str | None) -> dict[str, Any]:
    """The image ID as an annotation: the tag stays `:dev` with imagePullPolicy Never, so without
    it a rebuilt image would change nothing and the old code would keep running."""
    out: dict[str, Any] = {"labels": labels(app.name)}
    if image_id:
        out["annotations"] = {"aeoi.io/image-id": image_id}
    return out


def deployment(
    app: App, apps: dict[str, App], plan: list[dict[str, Any]], image_id: str | None = None
) -> dict[str, Any]:
    check_hardening(app)
    kind = IMAGES[app.kind]
    env = environment(app.spec)
    vols, mounts = tmpfs_volumes(app)
    for item in (p for p in plan if p["app"] == app.name):
        vname = item["name"]
        if item["kind"] == "Secret":
            src = {"secret": {"secretName": vname, "defaultMode": 0o440}}
        else:
            src = {"configMap": {"name": vname, "defaultMode": 0o444}}
        vols.append({"name": vname, **src})
        mounts.append({"name": vname, "mountPath": item["mount"], "readOnly": True})
    container: dict[str, Any] = {
        "name": "app",
        "image": app.image,
        "imagePullPolicy": "Never",  # loaded with `kind load`; never pulled from a registry
        "ports": [{"name": "http", "containerPort": app.port, "protocol": "TCP"}],
        "env": [{"name": k, "value": v} for k, v in sorted(env.items())],
        "resources": resources(app),
        "securityContext": container_security(),
        "startupProbe": probe(kind["live"], 2, kind["startup_failures"], timeout=2),
        # ready = can serve now (deps checked); live = the process only (no deps: a DB outage
        # must not restart every pod - libs/web/aeoi_web/health.py)
        "readinessProbe": probe(kind["ready"], 5, 2),
        "livenessProbe": probe(kind["live"], 10, 3),
        "lifecycle": {"preStop": {"sleep": {"seconds": PRESTOP_S}}},
        "volumeMounts": mounts,
    }
    if app.spec.get("command"):
        container["args"] = [str(a) for a in app.spec["command"]]  # compose command = k8s args
    pod: dict[str, Any] = {
        "automountServiceAccountToken": False,  # no pod talks to the Kubernetes API
        "enableServiceLinks": False,  # no API_PORT=tcp://... env vars next to our settings
        "terminationGracePeriodSeconds": GRACE_S,
        "securityContext": pod_security(),
        "containers": [container],
        "volumes": vols,
    }
    urls = wait_urls(app, apps)
    if urls:
        if app.kind != "aeoi-py":
            raise ComposeError(f"{app.name}: wait_for.py is only in the aeoi-py image")
        pod["initContainers"] = [{
            "name": "wait-for",
            "image": app.image,
            "imagePullPolicy": "Never",
            "command": ["python", "/app/infrastructure/docker/wait_for.py",
                        "--timeout", str(WAIT_TIMEOUT_S), *urls],
            "resources": {"requests": {"memory": "32Mi", "cpu": "10m"},
                          "limits": {"memory": "64Mi"}},
            "securityContext": container_security(),
        }]  # fmt: skip
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": meta(app.name),
        "spec": {
            "replicas": 1,  # 22a; replicas are 22b
            "revisionHistoryLimit": 3,
            "selector": {"matchLabels": {NAME: app.name}},
            # zero-downtime with ONE replica: the new pod must be READY before the old stops
            "strategy": {
                "type": "RollingUpdate",
                "rollingUpdate": {"maxSurge": 1, "maxUnavailable": 0},
            },
            "template": {"metadata": template_meta(app, image_id), "spec": pod},
        },
    }


def service(app: App) -> dict[str, Any]:
    # the SAME name as in compose: every *_URL from Phase 21 works unchanged
    return {"apiVersion": "v1", "kind": "Service", "metadata": meta(app.name),
            "spec": {"selector": {NAME: app.name},
                     "ports": [{"name": "http", "port": app.port, "targetPort": "http"}]}}  # fmt: skip


def node_port(port: int) -> int:
    n = port + NODEPORT_OFFSET
    if not 30000 <= n <= 32767:
        raise ComposeError(f"port {port} has no NodePort in 30000-32767")
    return n


def host_service(app: App) -> dict[str, Any]:
    """DEV ONLY: 127.0.0.1:<port> on the Mac -> this pod (kind extraPortMappings -> NodePort).
    In production only api is exposed, behind an ingress."""
    return {"apiVersion": "v1", "kind": "Service", "metadata": meta(f"{app.name}-host"),
            "spec": {"type": "NodePort", "selector": {NAME: app.name},
                     "ports": [{"name": "http", "port": app.port, "targetPort": "http",
                                "nodePort": node_port(app.port)}]}}  # fmt: skip


def cidr(addr: str) -> str:
    return str(ipaddress.ip_network(addr))  # a bare address -> /32


def policies(g: Graph, facts: Facts) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = [
        {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
         "metadata": meta("default-deny"),
         "spec": {"podSelector": {}, "policyTypes": ["Ingress", "Egress"]}},
        {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
         "metadata": meta("allow-dns"),
         "spec": {"podSelector": {}, "policyTypes": ["Egress"],
                  "egress": [{"to": [{"namespaceSelector": {"matchLabels": {
                      "kubernetes.io/metadata.name": "kube-system"}},
                                      "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}}}],
                              "ports": [{"protocol": "UDP", "port": 53},
                                        {"protocol": "TCP", "port": 53}]}]}},
    ]  # fmt: skip
    for name, port in sorted(g.apps.items()):
        tcp = [{"protocol": "TCP", "port": port}]
        node = [{"ipBlock": {"cidr": cidr(a)}} for a in facts.node_sources]
        ingress = [{"from": node, "ports": tcp}]  # kubelet probes come from the node itself
        callers = g.callers(name)
        if callers:
            ingress.append({"from": [{"podSelector": {"matchLabels": {NAME: c}}} for c in callers],
                            "ports": tcp})  # fmt: skip
        egress = []
        for target in sorted(g.calls.get(name, set())):  # new dicts each time: no YAML anchors
            egress.append({"to": [{"podSelector": {"matchLabels": {NAME: target}}}],
                           "ports": [{"protocol": "TCP", "port": g.apps[target]}]})  # fmt: skip
        for _infra, iport in sorted(g.infra.get(name, {}).items()):
            # compose containers are not pods: their subnet (read at deploy time) + the port
            egress.append({"to": [{"ipBlock": {"cidr": c}} for c in facts.compose_subnets],
                           "ports": [{"protocol": "TCP", "port": iport}]})  # fmt: skip
        for hport in sorted(g.host.get(name, set())):
            egress.append({"to": [{"ipBlock": {"cidr": cidr(facts.host_gateway)}}],
                           "ports": [{"protocol": "TCP", "port": hport}]})  # fmt: skip
        if name in g.internet:
            egress.append({"to": [{"ipBlock": {"cidr": "0.0.0.0/0", "except": PRIVATE}}],
                           "ports": [{"protocol": "TCP", "port": 443}]})  # fmt: skip
        spec: dict[str, Any] = {"podSelector": {"matchLabels": {NAME: name}},
                                "policyTypes": ["Ingress", "Egress"], "ingress": ingress}  # fmt: skip
        if egress:
            spec["egress"] = egress
        out.append({"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
                    "metadata": meta(f"app-{name}"), "spec": spec})  # fmt: skip
    return out


def dev_host_ingress(g: Graph, facts: Facts) -> dict[str, Any]:
    """DEV ONLY: the Mac's 127.0.0.1:80xx reaches the pods from the `kind` network gateway
    (Docker Desktop's port forwarding). Only the app ports; a pod listens on its own one only."""
    return {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
            "metadata": meta("dev-host-ingress"),
            "spec": {"podSelector": {"matchExpressions": [
                         {"key": NAME, "operator": "In", "values": sorted(g.apps)}]},
                     "policyTypes": ["Ingress"],
                     "ingress": [{"from": [{"ipBlock": {"cidr": cidr(a)}} for a in facts.dev_clients],
                                  "ports": [{"protocol": "TCP", "port": p}
                                            for p in sorted(set(g.apps.values()))]}]}}  # fmt: skip


def build(config: dict[str, Any], facts: Facts) -> dict[str, list[dict[str, Any]]]:
    """file name -> objects. Applied in file-name order (namespace first)."""
    apps = apps_of(config)
    g = graph(config, apps)
    plan = data_plan(config, apps)
    return {
        "00-namespace.yaml": [namespace()],
        "10-network-policies.yaml": policies(g, facts),
        "20-services.yaml": [service(a) for a in apps.values()],
        "30-deployments.yaml": [
            deployment(a, apps, plan, facts.image_ids.get(a.image)) for a in apps.values()
        ],
        "40-dev-host-ports.yaml": [
            *(host_service(a) for a in apps.values()),
            dev_host_ingress(g, facts),
        ],
    }


class _Plain(yaml.SafeDumper):
    """No &id001 anchors: shared dicts are written out in full (readable, diffable)."""

    def ignore_aliases(self, data: Any) -> bool:
        return True


def write(files: dict[str, list[dict[str, Any]]], out: Path) -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("*.yaml"):
        old.unlink()  # a file from an earlier render must not be applied again
    written = []
    for name, objs in files.items():
        p = out / name
        p.write_text(
            "# GENERATED by scripts/k8s_manifests.py from docker-compose.yml - do not edit\n"
            + yaml.dump_all(objs, Dumper=_Plain, sort_keys=False, default_flow_style=False)
        )
        written.append(p)
    return written


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__.split("\n\n")[1], file=sys.stderr)
        return 2
    config = json.loads(Path(argv[0]).read_text())
    facts = Facts.from_json(json.loads(Path(argv[1]).read_text()))
    try:
        files = build(config, facts)
    except ComposeError as exc:
        print(f"k8s_manifests: {exc}", file=sys.stderr)
        return 1
    for p in write(files, Path(argv[2])):
        print(p)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
