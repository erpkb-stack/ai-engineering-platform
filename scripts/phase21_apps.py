"""Phase 21 (ADR-033): checks before `make apps-up`, and memory peaks for the real limits.

    uv run python scripts/phase21_apps.py profiles <compose config json>   # extra --profile flags
    uv run python scripts/phase21_apps.py check <compose config json> [--running a,b]
    uv run python scripts/phase21_apps.py fix-perms <compose config json>
    uv run python scripts/phase21_apps.py peaks [--seconds 600] [--every 2]

profiles  - the profiles YOUR .env switches need, read from `docker compose config` (compose has
            already interpolated and unquoted .env there; make reads .env differently - review #8).
check     - before `up`: every image exists locally, every secret file and bind folder exists,
            the container user (10001) can read the secret files, and no host process holds an
            app port. Prints the fix for each. Secret CONTENT is never read (stat only).
fix-perms - the explicit fix for the read check: secrets/ stays 700 (only you can enter it), the
            files the containers mount become 644 (compose secrets outside swarm are plain bind
            mounts: the file's own mode decides whether uid 10001 can read it).
peaks     - samples `docker stats`, prints the HIGHEST memory per container (run the smokes
            meanwhile, with AEOI_APP_MEM_LIMIT=2g so the limit does not cut the peak).
"""

from __future__ import annotations

import argparse
import json
import re
import socket
import stat
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter, ValidationError

FIX = {
    "jwt_public.pem": "make setup",
    "delegation_public.pem": "make delegation-keys",
    "delegation_private.pem": "make delegation-keys",
    "catalog_svc_password.txt": "make catalog-import, then make db-users",
    "anthropic_api_key.txt": (
        "create it yourself - containers read the key from this file only, never from an env "
        "var (visible in docker inspect). No key (LLM_ROUTING=routing.local.yaml)? an empty "
        "file is enough: touch secrets/anthropic_api_key.txt"
    ),
    "orchestrator_service_token.txt": "make agents-tokens",
    "agents_service_token.txt": "make agents-tokens",
    "action_executor_token.txt": "make agents-tokens",
    "rag_service_token.txt": "make rag-token",
}
UNITS = {"b": 1, "kib": 1024, "mib": 1024**2, "gib": 1024**3, "kb": 1000, "mb": 1000**2,
         "gb": 1000**3}  # fmt: skip
_BOOL = TypeAdapter(bool)


def fix_for(name: str) -> str:
    if name.endswith("_svc_password.txt"):
        return "make db-users"
    if name.startswith("tools_") and name.endswith("_token.txt"):
        return "make tools-tokens"
    return FIX.get(name, "see docs/phases/phase-21.md")


def _env(config: dict[str, Any], service: str) -> dict[str, str]:
    raw = ((config.get("services") or {}).get(service) or {}).get("environment") or {}
    return {k: "" if v is None else str(v) for k, v in raw.items()}


def _true(value: str) -> bool:
    """The same reading as the service's pydantic setting (True, 1, yes ... all count)."""
    try:
        return bool(_BOOL.validate_python(value.strip() or "false"))
    except ValidationError:
        return False


def profiles(config: dict[str, Any]) -> list[str]:
    """Extra profiles for the switches as the CONTAINERS will see them."""
    out = []
    if _env(config, "tool-gateway").get("AEOI_TOOLS_CATALOG_URL", "").strip():
        out.append("apps-catalog")
    transport = _env(config, "incident-service").get("AEOI_INCIDENT_EVENT_TRANSPORT", "")
    if transport.strip().lower() == "kafka" or _true(
        _env(config, "audit").get("AEOI_AUDIT_CONSUMER_ENABLED", "")
    ):
        out.append("kafka")
    return out


def mounted_secrets(config: dict[str, Any]) -> dict[str, list[str]]:
    """{secret file on the host: [services mounting it]} for the services in this config."""
    files = {name: spec.get("file", "") for name, spec in (config.get("secrets") or {}).items()}
    used: dict[str, list[str]] = {}
    for svc, spec in (config.get("services") or {}).items():
        for ref in spec.get("secrets") or []:
            source = ref["source"] if isinstance(ref, dict) else ref
            used.setdefault(files[source], []).append(svc)
    return used


def bind_sources(config: dict[str, Any]) -> dict[str, list[str]]:
    used: dict[str, list[str]] = {}
    for svc, spec in (config.get("services") or {}).items():
        for v in spec.get("volumes") or []:
            if isinstance(v, dict) and v.get("type") == "bind":
                used.setdefault(v["source"], []).append(svc)
    return used


def published_ports(config: dict[str, Any]) -> dict[int, str]:
    out: dict[int, str] = {}
    for svc, spec in (config.get("services") or {}).items():
        for p in spec.get("ports") or []:
            if isinstance(p, dict) and p.get("published"):
                out[int(p["published"])] = svc
    return out


def port_busy(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex((host, port)) == 0


def image_exists(image: str) -> bool:
    return (
        subprocess.run(  # noqa: S603 - fixed argument list, no shell
            ["docker", "image", "inspect", image],  # noqa: S607
            capture_output=True, check=False,
        ).returncode
        == 0
    )  # fmt: skip


def check(
    config: dict[str, Any],
    *,
    busy: Callable[[int], bool] = port_busy,
    has_image: Callable[[str], bool] = image_exists,
    running: set[str] | None = None,
) -> list[str]:
    """Problems that would make `up` fail or a container unready. Never reads a secret."""
    problems = []
    for image in sorted({s["image"] for s in (config.get("services") or {}).values()
                         if s.get("pull_policy") == "never"}):  # fmt: skip
        if not has_image(image):
            problems.append(f"no local image {image}: run make images")
    unreadable = []
    for path, services in sorted(mounted_secrets(config).items()):
        p, name = Path(path), Path(path).name
        if not p.is_file():  # existence only: the content is never read
            problems.append(f"missing secrets/{name} (for {', '.join(services)}): {fix_for(name)}")
        elif not p.stat().st_mode & stat.S_IROTH:
            unreadable.append(name)
    if unreadable:
        problems.append(
            f"{len(unreadable)} secret file(s) only YOU can read ({', '.join(unreadable[:3])}"
            f"{', ...' if len(unreadable) > 3 else ''}): the containers run as uid 10001. "
            "Run make apps-secrets-perms (files 644, secrets/ stays 700)"
        )
    for path, services in sorted(bind_sources(config).items()):
        if not Path(path).is_dir():
            problems.append(f"missing {path} (for {', '.join(services)}): make db-seed")
    running = running or set()
    for port, svc in sorted(published_ports(config).items()):
        if svc not in running and busy(port):
            problems.append(
                f"port {port} ({svc}) is used by a process outside this compose project - "
                f"for an app port usually a host `make run-*` (e.g. make stop-tools)"
            )
    return problems


def fix_perms(config: dict[str, Any]) -> list[str]:
    """Only the mounted files; the folder is (re)set to 700 so nobody else can enter it."""
    done = []
    files = [Path(p) for p in mounted_secrets(config) if Path(p).is_file()]
    for folder in {f.parent for f in files}:
        folder.chmod(0o700)
    for f in files:
        f.chmod(0o644)
        done.append(f.name)
    return sorted(done)


def parse_mem(usage: str) -> int:
    """'123.4MiB / 512MiB' -> bytes used (the part before the slash)."""
    m = re.match(r"\s*([\d.]+)\s*([KMG]?i?B)", usage, re.IGNORECASE)
    if not m:
        raise ValueError(f"unreadable memory usage: {usage!r}")
    return int(float(m.group(1)) * UNITS[m.group(2).lower()])


def sample() -> dict[str, int]:
    out = subprocess.run(
        ["docker", "stats", "--no-stream", "--format", "{{.Name}}\t{{.MemUsage}}"],  # noqa: S607
        check=True, capture_output=True, text=True,
    ).stdout  # fmt: skip
    return {
        name: parse_mem(mem)
        for name, mem in (ln.split("\t", 1) for ln in out.splitlines() if "\t" in ln)
    }


def report(peaks: dict[str, int], samples: int) -> str:
    lines = [f"{'container':<34} {'peak MiB':>9}   ({samples} samples)"]
    lines += [f"{n:<34} {b / 1024**2:>9.0f}" for n, b in sorted(peaks.items())]
    lines.append(f"{'TOTAL of peaks':<34} {sum(peaks.values()) / 1024**2:>9.0f}")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("profiles", "check", "fix-perms"):
        c = sub.add_parser(name)
        c.add_argument("config_json")
        if name == "check":
            c.add_argument("--running", default="", help="comma list of running app services")
    p = sub.add_parser("peaks")
    p.add_argument("--seconds", type=float, default=600)
    p.add_argument("--every", type=float, default=2)
    args = ap.parse_args(argv)
    if args.cmd == "peaks":
        peaks: dict[str, int] = {}
        n, end = 0, time.monotonic() + args.seconds
        try:
            while time.monotonic() < end:
                for name, used in sample().items():
                    peaks[name] = max(peaks.get(name, 0), used)
                n += 1
                time.sleep(args.every)
        except KeyboardInterrupt:
            pass
        print(report(peaks, n))
        return 0
    config = json.loads(Path(args.config_json).read_text())
    if args.cmd == "profiles":
        print(" ".join(f"--profile {p}" for p in profiles(config)))
        return 0
    if args.cmd == "fix-perms":
        done = fix_perms(config)
        print(f"secrets/ is 700; {len(done)} mounted file(s) now 644: {', '.join(done)}")
        return 0
    problems = check(config, running={s for s in args.running.split(",") if s})
    for line in problems:
        print(f"  - {line}")
    if problems:
        print(f"apps-up: {len(problems)} problem(s), nothing started")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
