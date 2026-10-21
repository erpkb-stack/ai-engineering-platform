"""Phase 21 (ADR-033): docker-compose.yml is checked against the CODE, not against itself.

Each app container is compared with its service's Settings class:
- every outbound `*_url` setting is set, to a compose service name and THAT service's port
  (a `localhost` left behind is the container calling itself);
- it mounts EXACTLY the secret files its settings read (least privilege, both directions);
- the tool-gateway egress allow-list equals the hosts of its HTTP-backed tools (both directions);
- the hardening every container must have (non-root, read-only, no capabilities, loopback ports).
No Docker needed: the YAML is parsed and `${...}` is interpolated here, under both .env states.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import re
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.parse import urlsplit

import pytest
import yaml

from aeoi_llm.config import base_url_env, load_routing

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
DOCKER = ROOT / "infrastructure" / "docker"

# compose service -> (settings module, the Settings env prefix it must use)
APPS = {
    "api": "aeoi_api.config",
    "incident-service": "aeoi_incident.config",
    "orchestrator": "aeoi_orchestrator.config",
    "agents": "aeoi_agents.config",
    "rag": "aeoi_rag.config",
    "llm-gateway": "aeoi_llm.config",
    "tool-gateway": "aeoi_tools.config",
    "audit": "aeoi_audit.config",
}
# *_url settings that are NOT a call to another app service (each with the reason)
NOT_SERVICE_CALLS = {
    "database_url",  # unused base field: services build the DSN from AEOI_PG_HOST + their login
    "redis_url",  # unused until Phase 22 (ADR-031)
    "oidc_jwks_url",  # Keycloak, checked on its own below
    "catalog_url",  # the Java switch, checked with the allow-list below
}
ENV_STATES = {
    "switches off": {},
    "switches on": {
        "AEOI_TOOLS_CATALOG_URL": "http://localhost:8010",
        "AEOI_OIDC_ISSUER": "http://localhost:8090/realms/aeoi",
        "AEOI_INCIDENT_EVENT_TRANSPORT": "kafka",
        "AEOI_AUDIT_CONSUMER_ENABLED": "true",
        "AEOI_OTEL_ENDPOINT": "http://localhost:4318",  # Phase 23a
    },
}
_VAR = re.compile(r"\$\{(?P<name>[A-Z0-9_]+)(?:(?P<op>:-|:\+|-)(?P<arg>[^}]*))?\}")


def interpolate(value: str, env: dict[str, str]) -> str:
    """The subset of compose interpolation this file uses: ${X}, ${X:-default}, ${X:+alt}."""

    def one(m: re.Match[str]) -> str:
        have = env.get(m["name"], "")
        if m["op"] == ":+":
            return m["arg"] if have else ""
        if m["op"] in (":-", "-"):
            return have or m["arg"]
        return have

    return _VAR.sub(one, value)


def env_of(service: str, env: dict[str, str]) -> dict[str, str]:
    raw = COMPOSE["services"][service].get("environment") or {}
    return {k: interpolate(str(v), env) for k, v in raw.items()}


def settings_class(service: str) -> Any:
    return importlib.import_module(APPS[service]).Settings


def env_name(cls: Any, field: str) -> str:
    info = cls.model_fields[field]
    return info.alias or (cls.model_config.get("env_prefix", "") + field).upper()


def service_ports() -> dict[str, int]:
    """compose name -> the port the process listens on INSIDE its container."""
    out = {s: int(COMPOSE["services"][s]["environment"]["AEOI_SERVICE_PORT"]) for s in APPS}
    out["service-catalog"] = 8010
    out["keycloak"] = 8080  # kc.sh --http-port=8080
    return out


def hostport(url: str) -> str:
    p = urlsplit(url)
    return f"{p.hostname}:{p.port}"


# ---------------------------------------------------------------- addresses


@pytest.mark.parametrize("service", sorted(APPS))
@pytest.mark.parametrize("state", sorted(ENV_STATES))
def test_every_call_to_another_service_uses_its_compose_name_and_port(
    service: str, state: str
) -> None:
    cls, env = settings_class(service), env_of(service, ENV_STATES[state])
    ports = service_ports()
    urls = [f for f in cls.model_fields if f.endswith("_url") and f not in NOT_SERVICE_CALLS]
    assert urls or service in ("incident-service", "audit", "llm-gateway"), service
    for field in urls:
        name = env_name(cls, field)
        assert name in env, f"{service}: {name} is not set - the container would call itself"
        p = urlsplit(env[name])
        assert p.hostname in ports, f"{service}: {name}={env[name]} is not a compose service"
        assert p.port == ports[p.hostname], f"{service}: {name} uses the wrong port"


def _environ_addresses() -> dict[str, str]:
    """AEOI_* variables read with os.environ (NOT a Settings field) whose default is localhost,
    in code the services run (CLIs excluded: they run on the host)."""
    found: dict[str, str] = {}
    pattern = re.compile(r'os\.environ\.get\(\s*"(AEOI_[A-Z_]+)"\s*,\s*"?([^")]*)')
    for path in [*ROOT.glob("libs/*/src/**/*.py"), *ROOT.glob("services/*/src/**/*.py")]:
        if path.name in ("__main__.py", "devtoken.py", "users.py"):
            continue
        for name, default in pattern.findall(path.read_text()):
            if "localhost" in default or name in ("AEOI_PG_HOST", "AEOI_PG_PORT"):
                found[name] = str(path.relative_to(ROOT))
    return found


def test_addresses_read_outside_settings_are_set_too() -> None:
    """aeoi_db builds every DSN from AEOI_PG_HOST/PORT via os.environ: a missing one means each
    container dials ITSELF on localhost:5433 (review #3) - and no Settings test would see it."""
    names = _environ_addresses()
    assert {"AEOI_PG_HOST", "AEOI_PG_PORT"} <= set(names)
    pg_port = "5432"  # the port inside the postgres container (published: 5433)
    for service in APPS:
        env = env_of(service, {})
        for name, where in names.items():
            assert name in env, f"{service}: {name} (read in {where}) is not set"
        assert env["AEOI_PG_HOST"] == "postgres"
        assert env["AEOI_PG_PORT"] == pg_port


def test_no_app_container_is_told_to_call_localhost() -> None:
    for state, env in ENV_STATES.items():
        for service in [*APPS, "service-catalog"]:
            for k, v in env_of(service, env).items():
                if k in ("AEOI_OIDC_ISSUER", "NO_PROXY", "no_proxy"):
                    continue  # the token's `iss` claim (a name, never called); proxy bypass
                assert not re.search(r"localhost|127\.0\.0\.1", v), (state, service, k, v)


def test_the_egress_allow_list_is_exactly_the_hosts_of_the_http_tools() -> None:
    """Both directions: a missing host breaks a tool; an extra host widens egress. The list is
    the same in both switch states: with the Java catalog off, `service-catalog:8010` stays
    listed but nothing calls it (the gateway reads the JSON file) and the name does not resolve
    (the container is not started)."""
    on = env_of("tool-gateway", ENV_STATES["switches on"])
    off = env_of("tool-gateway", ENV_STATES["switches off"])
    allowed = set(json.loads(on["AEOI_TOOLS_EGRESS_ALLOWLIST"]))
    assert allowed == {hostport(on["AEOI_TOOLS_RAG_URL"]), hostport(on["AEOI_TOOLS_CATALOG_URL"])}
    assert off["AEOI_TOOLS_CATALOG_URL"] == ""  # switch off -> the JSON file, like a host run
    assert on["AEOI_TOOLS_EGRESS_ALLOWLIST"] == off["AEOI_TOOLS_EGRESS_ALLOWLIST"]
    # the name the gateway is told matches the field it reads
    cls = settings_class("tool-gateway")
    assert env_name(cls, "egress_allowlist") == "AEOI_TOOLS_EGRESS_ALLOWLIST"


def test_user_tokens_keep_their_issuer_and_keys_come_over_the_compose_network() -> None:
    on = env_of("api", ENV_STATES["switches on"])
    iss, jwks = on["AEOI_OIDC_ISSUER"], on["AEOI_OIDC_JWKS_URL"]
    assert iss == ENV_STATES["switches on"]["AEOI_OIDC_ISSUER"]  # == the token's iss, unchanged
    assert hostport(jwks) == f"keycloak:{service_ports()['keycloak']}"
    # same realm: the keys of the issuer the tokens name, not of another realm
    assert urlsplit(jwks).path == urlsplit(iss).path + "/protocol/openid-connect/certs"
    assert "--hostname=http://localhost:${AEOI_KC_PORT:-8090}" in str(
        COMPOSE["services"]["keycloak"]["command"]
    )  # what keeps `iss` the same however Keycloak is reached
    assert env_of("api", ENV_STATES["switches off"])["AEOI_OIDC_JWKS_URL"] == ""


def test_kafka_is_reached_on_its_internal_listener() -> None:
    kafka_env = COMPOSE["services"]["kafka"]["environment"]
    for service in APPS:
        bootstrap = env_of(service, {})["AEOI_KAFKA_BOOTSTRAP_SERVERS"]
        assert f"INTERNAL://{bootstrap}" in kafka_env["KAFKA_ADVERTISED_LISTENERS"], service


def test_the_native_ollama_is_reached_through_the_host_gateway_and_stays_local() -> None:
    llm = COMPOSE["services"]["llm-gateway"]
    env = env_of("llm-gateway", {})
    name = base_url_env("ollama")
    assert name in env
    # EXACTLY this: one more line could map the name to a remote IP and send unredacted
    # prompts off the machine (review #11)
    assert llm["extra_hosts"] == ["host.docker.internal:host-gateway"]
    assert not any("extra_hosts" in COMPOSE["services"][s] for s in APPS if s != "llm-gateway")
    for policy in ("routing.yaml", "routing.local.yaml"):  # the value is accepted by the loader
        cfg = load_routing(ROOT / "services/llm-gateway/config" / policy, env={name: env[name]})
        assert cfg.providers["ollama"].hosted is False


# ---------------------------------------------------------------- secrets


def _secret_files_read_by(service: str) -> set[str]:
    cls = settings_class(service)
    s = cls()
    files = {
        v.name
        for f in cls.model_fields
        if isinstance(v := getattr(s, f), Path) and "secrets" in v.parts
    }
    if service == "llm-gateway":  # the provider key file named in the routing policy
        cfg = load_routing(ROOT / "services/llm-gateway/config/routing.yaml", env={})
        files |= {p.api_key_file for p in cfg.providers.values() if p.api_key_file}
    return files


def _mounted(service: str) -> dict[str, str]:
    files = {k: Path(v["file"]).name for k, v in COMPOSE["secrets"].items()}
    out = {}
    for ref in COMPOSE["services"][service].get("secrets") or []:
        out[files[ref["source"]]] = ref["target"]
    return out


@pytest.mark.parametrize("service", sorted(APPS))
def test_each_container_gets_exactly_the_secret_files_it_reads(service: str) -> None:
    mounted = _mounted(service)
    assert set(mounted) == _secret_files_read_by(service), service
    for name, target in mounted.items():
        assert target == f"/app/secrets/{name}", (service, name)  # where find_repo_root looks


def test_the_catalog_gets_only_its_two_files() -> None:
    assert set(_mounted("service-catalog")) == {"jwt_public.pem", "catalog_svc_password.txt"}


def test_no_secret_value_and_no_private_signing_key_where_it_does_not_belong() -> None:
    for service in [*APPS, "service-catalog"]:
        spec = COMPOSE["services"][service]
        for k, v in (spec.get("environment") or {}).items():
            secretish = re.search(r"PASSWORD|SECRET|API_KEY|TOKEN", k.removesuffix("_FILE"))
            if secretish and k.endswith("_FILE"):  # a PATH to a mounted secret, never the value
                assert str(v).startswith("/app/secrets/"), (service, k, v)
            else:
                assert not secretish, (service, k)
        mounted = set(_mounted(service))
        assert "jwt_private.pem" not in mounted, service  # only `make token` on the host signs
        if service != "api":  # the token exchange (STS) is the only signer of delegations
            assert "delegation_private.pem" not in mounted, service
        assert not any(str(v).startswith("./secrets") for v in spec.get("volumes") or []), service


# ---------------------------------------------------------------- hardening


@pytest.mark.parametrize("service", [*sorted(APPS), "service-catalog"])
def test_every_app_container_is_hardened(service: str) -> None:
    spec = COMPOSE["services"][service]
    # keys that would undo the hardening or put .env (and maybe an API key) into the container
    # env, visible in `docker inspect` (review #2)
    for key in ("user", "env_file", "privileged", "network_mode", "pid", "ipc", "cap_add",
                "devices", "volumes_from"):  # fmt: skip
        assert key not in spec, (service, key)
    assert spec["pull_policy"] == "never"  # our images are built locally, never pulled
    assert spec["read_only"] is True
    assert spec["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in spec["security_opt"]
    assert spec["deploy"]["resources"]["limits"]["memory"]  # rules: limits on every service
    for p in spec["ports"]:
        assert p.startswith("127.0.0.1:"), (service, p)  # never reachable from the LAN
    for v in spec.get("volumes") or []:
        assert isinstance(v, dict), (service, "long form: a missing folder must be an error")
        assert v["read_only"] is True, (service, v)
        assert v["bind"]["create_host_path"] is False, (service, v)


def test_calls_between_services_never_go_through_a_proxy() -> None:
    """Docker Desktop copies the host's proxy settings into containers; these calls carry user
    tokens (review #12)."""
    must = {*APPS, "service-catalog", "postgres", "kafka", "keycloak", "localhost", "127.0.0.1",
            "host.docker.internal"}  # fmt: skip
    for service in APPS:
        env = env_of(service, {})
        assert env["NO_PROXY"] == env["no_proxy"]
        assert must <= set(env["NO_PROXY"].split(",")), service


def test_the_kafka_probe_does_not_get_the_brokers_heap() -> None:
    test = " ".join(COMPOSE["services"]["kafka"]["healthcheck"]["test"])
    assert "KAFKA_HEAP_OPTS=-Xmx64m" in test  # else it inherits -Xmx512m -Xms512m (review #5)


def test_the_orchestrator_waits_for_everything_a_resumed_run_calls() -> None:
    deps = COMPOSE["services"]["orchestrator"]["depends_on"]
    for svc in ("agents", "api", "incident-service", "postgres"):
        assert deps[svc]["condition"] == "service_healthy", svc


@pytest.mark.parametrize("service", sorted(APPS))
def test_command_port_and_published_port_agree(service: str) -> None:
    spec = COMPOSE["services"][service]
    cmd = spec["command"]
    port = spec["environment"]["AEOI_SERVICE_PORT"]
    assert "healthcheck" not in spec  # the image's HEALTHCHECK, one definition only
    assert cmd[cmd.index("--port") + 1] == port
    assert cmd[cmd.index("--host") + 1] == "0.0.0.0"  # noqa: S104 - inside the container only
    assert spec["ports"] == [f"127.0.0.1:{port}:{port}"]
    package = APPS[service].split(".")[0]
    assert f"{package}.main:build_app" in cmd


# ---------------------------------------------------------------- images


def _stages(dockerfile: str) -> list[str]:
    return re.split(r"(?m)^FROM ", (DOCKER / dockerfile).read_text())[1:]


@pytest.mark.parametrize("dockerfile", ["python.Dockerfile", "catalog.Dockerfile"])
def test_images_run_as_non_root_with_a_healthcheck(dockerfile: str) -> None:
    runtime = _stages(dockerfile)[-1]
    users = re.findall(r"(?m)^USER (\S+)", runtime)
    assert users and users[-1] == "10001:10001"
    assert re.search(r"(?m)^HEALTHCHECK ", runtime)
    assert not re.search(r"(?m)^COPY .*secrets", runtime)


def test_the_python_runtime_stage_has_no_build_tools_and_the_build_is_frozen() -> None:
    builder, runtime = _stages("python.Dockerfile")
    assert "uv sync --frozen --no-dev --all-packages" in builder
    assert "uv" not in re.sub(r"(?m)^#.*$", "", runtime).replace("/app/.venv", "").split()  # no uv
    assert "pip install" not in runtime


@pytest.mark.parametrize(
    "ignore", ["python.Dockerfile.dockerignore", "catalog.Dockerfile.dockerignore"]
)
def test_build_contexts_are_allow_lists_that_never_add_secrets_back(ignore: str) -> None:
    lines = [ln.strip() for ln in (DOCKER / ignore).read_text().splitlines()]
    rules = [ln for ln in lines if ln and not ln.startswith("#")]
    assert rules[0] == "**"  # everything out first
    adds = [i for i, r in enumerate(rules) if r.startswith("!")]
    for i in adds:
        assert rules[i] not in ("!**", "!*", "!**/*"), rules[i]
        assert not re.search(r"secrets|\.env|data", rules[i]), rules[i]
    # the secret exclusions come AFTER every re-include (later rules win)
    for must in ("**/secrets", "**/.env*"):
        assert must in rules
        assert rules.index(must) > max(adds), must


def test_the_root_dockerignore_keeps_secrets_out_of_any_other_build() -> None:
    rules = (ROOT / ".dockerignore").read_text().splitlines()
    for must in ("secrets/", "**/.env*", "data/"):
        assert must in rules


# ---------------------------------------------------------------- helper scripts


def _load(path: str, name: str) -> ModuleType:
    s = importlib.util.spec_from_file_location(name, ROOT / path)
    assert s and s.loader
    m = importlib.util.module_from_spec(s)
    s.loader.exec_module(m)
    return m


def test_the_healthcheck_probes_readiness_on_loopback_only() -> None:
    hc = _load("infrastructure/docker/healthcheck.py", "healthcheck")
    assert hc.target({"AEOI_SERVICE_PORT": "8006"}) == "http://127.0.0.1:8006/health/ready"
    assert hc.target({}) is None  # no port: unhealthy (exit 1), never a silent "healthy"
    assert hc.target({"AEOI_SERVICE_PORT": "80@evil.test"}) is None


def _apps_config(tmp_path: Path) -> dict[str, Any]:
    present = tmp_path / "jwt_public.pem"
    present.write_text("x")
    present.chmod(0o644)
    private = tmp_path / "tools_audit_token.txt"
    private.write_text("x")
    private.chmod(0o600)  # what umask 077 gives
    return {
        "secrets": {"jwt": {"file": str(present)}, "aud": {"file": str(private)},
                    "tok": {"file": str(tmp_path / "tools_rag_token.txt")},
                    "pw": {"file": str(tmp_path / "rag_svc_password.txt")}},
        "services": {
            "tool-gateway": {"image": "aeoi-py:dev", "pull_policy": "never",
                             "secrets": [{"source": "jwt"}, {"source": "tok"}, {"source": "aud"}],
                             "ports": [{"published": "8006"}],
                             "volumes": [{"type": "bind", "source": str(tmp_path / "nodata")}]},
            "rag": {"image": "aeoi-py:dev", "pull_policy": "never",
                    "secrets": [{"source": "pw"}], "ports": [{"published": "8004"}]},
            "service-catalog": {"image": "aeoi-catalog:dev", "pull_policy": "never"},
        },
    }  # fmt: skip


def test_apps_check_names_the_fix_and_never_needs_the_content(tmp_path: Path) -> None:
    apps = _load("scripts/phase21_apps.py", "phase21_apps")
    config = _apps_config(tmp_path)
    problems = apps.check(config, busy=lambda port: port == 8006,
                          has_image=lambda image: image == "aeoi-py:dev", running=set())  # fmt: skip
    text = "\n".join(problems)
    assert "no local image aeoi-catalog:dev: run make images" in text  # never a Docker Hub pull
    assert "aeoi-py" not in text
    assert "tools_rag_token.txt" in text and "make tools-tokens" in text
    assert "rag_svc_password.txt" in text and "make db-users" in text
    assert "only YOU can read (tools_audit_token.txt)" in text and "apps-secrets-perms" in text
    assert "nodata" in text and "make db-seed" in text  # no silently created empty folder
    assert "port 8006" in text
    assert "jwt_public" not in text  # present and readable
    # our own running container on its port is not a clash
    again = apps.check(config, busy=lambda _p: True, has_image=lambda _i: True,
                       running={"tool-gateway", "rag"})  # fmt: skip
    assert not any("port" in p for p in again)


def test_fix_perms_opens_the_mounted_files_only_and_closes_the_folder(tmp_path: Path) -> None:
    apps = _load("scripts/phase21_apps.py", "phase21_apps")
    config = _apps_config(tmp_path)
    other = tmp_path / "jwt_private.pem"  # not mounted by any container: must stay 600
    other.write_text("x")
    other.chmod(0o600)
    tmp_path.chmod(0o755)
    assert apps.fix_perms(config) == ["jwt_public.pem", "tools_audit_token.txt"]
    assert (tmp_path / "tools_audit_token.txt").stat().st_mode & 0o777 == 0o644
    assert other.stat().st_mode & 0o777 == 0o600
    assert tmp_path.stat().st_mode & 0o777 == 0o700  # nobody else can enter the folder


@pytest.mark.parametrize(("catalog", "transport", "consumer", "expected"), [
    ("", "log", "false", []),
    ("http://service-catalog:8010", "log", "false", ["apps-catalog"]),
    ("", "kafka", "false", ["kafka"]),
    ("", "KAFKA", "", ["kafka"]),       # what the service accepts, not what make's filter sees
    ("", "log", "True", ["kafka"]),
    ("", "log", "1", ["kafka"]),
    ("   ", "log", "no", []),
])  # fmt: skip
def test_profiles_follow_the_switches_as_the_containers_see_them(
    catalog: str, transport: str, consumer: str, expected: list[str]
) -> None:
    apps = _load("scripts/phase21_apps.py", "phase21_apps")
    config = {"services": {
        "tool-gateway": {"environment": {"AEOI_TOOLS_CATALOG_URL": catalog}},
        "incident-service": {"environment": {"AEOI_INCIDENT_EVENT_TRANSPORT": transport}},
        "audit": {"environment": {"AEOI_AUDIT_CONSUMER_ENABLED": consumer}},
    }}  # fmt: skip
    assert apps.profiles(config) == expected


@pytest.mark.parametrize(("usage", "mib"), [
    ("123.4MiB / 512MiB", 123.4), ("1.5GiB / 2GiB", 1536), ("900KiB / 1GiB", 900 / 1024),
    ("0B / 0B", 0),
])  # fmt: skip
def test_peak_sampling_reads_docker_stats_units(usage: str, mib: float) -> None:
    apps = _load("scripts/phase21_apps.py", "phase21_apps")
    assert apps.parse_mem(usage) == pytest.approx(mib * 1024**2, rel=1e-3)


# Phase 23a fix (Mac, 2026-10-17): tempo-init ran `chown` inside grafana/tempo, which has no
# chown. A one-shot helper must use an image that ships the tool it execs. This is a regression
# guard (a list we keep), not proof: only `docker compose up` on the Mac proves it.
# image -> the helper tools it is KNOWN to have (seen working on the Mac)
TOOLS_IN_IMAGE = {
    "apache/kafka:3.9.1": {"/bin/bash"},  # kafka-init, since Phase 18a
    "redis:7.4-alpine": {"chown"},  # busybox; tempo-init, Phase 23a fix
}


def test_one_shot_helpers_use_an_image_that_has_their_tool() -> None:
    for name, svc in COMPOSE["services"].items():
        if svc.get("restart") != "no" or not svc.get("entrypoint"):
            continue
        tool = svc["entrypoint"][0]
        assert tool in TOOLS_IN_IMAGE.get(svc["image"], set()), (
            f"{name}: {tool} is not known to exist in {svc['image']}"
        )


def test_peaks_are_sampled_while_the_smokes_run_not_after() -> None:
    """Phase 23c: the 23a peak was taken AFTER the smokes (at rest). `peaks --during` runs the
    targets itself and samples until they end; a failed target is reported, not hidden."""
    import sys
    import time as _time

    apps = _load("scripts/phase21_apps.py", "phase21_apps_during")
    seen: list[float] = []

    def sampler() -> dict[str, int]:
        seen.append(_time.monotonic())
        return {"tempo": 100 * len(seen), "loki": 50}

    start = _time.monotonic()
    slow = [sys.executable, "-c", "import time; time.sleep(0.6)"]
    failing = [sys.executable, "-c", "raise SystemExit(3)"]
    peaks, n, codes = apps.peaks_during([slow, failing], every=0.1, sampler=sampler)
    end = _time.monotonic()
    assert codes == [0, 3]
    assert n >= 4 and len(seen) == n  # sampled repeatedly during the 0.6 s run
    assert all(start <= t <= end for t in seen)
    assert peaks == {"tempo": 100 * n, "loki": 50}  # the max, not the last


def test_a_failed_sample_is_counted_not_the_end_of_sampling(
    capsys: pytest.CaptureFixture[str],
) -> None:
    import subprocess as _sp
    import sys

    apps = _load("scripts/phase21_apps.py", "phase21_apps_missed")
    calls = [0]

    def flaky() -> dict[str, int]:
        calls[0] += 1
        if calls[0] == 2:
            raise _sp.CalledProcessError(1, ["docker", "stats"])
        return {"tempo": calls[0]}

    peaks, n, codes = apps.peaks_during(
        [[sys.executable, "-c", "import time; time.sleep(0.5)"]], every=0.1, sampler=flaky
    )
    assert codes == [0] and n == calls[0] - 1 and n >= 3
    assert peaks["tempo"] == calls[0]  # sampling went on after the failure
    assert "1 sample(s) failed" in capsys.readouterr().out
