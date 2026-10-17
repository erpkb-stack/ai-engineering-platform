"""Wait until other services are READY (Phase 22a, ADR-034): a Kubernetes initContainer.

    python /app/infrastructure/docker/wait_for.py [--timeout 600] URL [URL ...]

Compose had `depends_on: {condition: service_healthy}`; Kubernetes has no such thing. The
orchestrator resumes RUNNING investigations at start, so api, incident-service and agents must
be READY before it starts (a resumed node calls them at once). Exit 0 when EVERY url answers
HTTP 200, 1 at the deadline. No proxy, like healthcheck.py.
"""

from __future__ import annotations

import argparse
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable

Probe = Callable[[str], bool]


def ready(url: str) -> bool:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # never a proxy
    try:
        with opener.open(url, timeout=3) as r:
            return bool(r.status == 200)
    except (urllib.error.URLError, OSError):  # 503 (not ready) is an HTTPError, a URLError
        return False


def wait(
    urls: list[str],
    *,
    timeout_s: float,
    every_s: float = 2.0,
    probe: Probe = ready,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> list[str]:
    """The urls still NOT ready at the deadline (empty = all ready)."""
    pending = list(dict.fromkeys(urls))
    deadline = clock() + timeout_s
    while True:
        pending = [u for u in pending if not probe(u)]
        if not pending or clock() >= deadline:
            return pending
        print(f"waiting for {', '.join(pending)}", file=sys.stderr)
        sleep(every_s)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--timeout", type=float, default=600)
    ap.add_argument("urls", nargs="+")
    args = ap.parse_args(argv)
    bad = [u for u in args.urls if not u.startswith("http://")]
    if bad:
        print(f"wait_for: only http:// urls: {bad}", file=sys.stderr)
        return 2
    pending = wait(args.urls, timeout_s=args.timeout)
    if pending:
        print(
            f"wait_for: NOT ready after {args.timeout:.0f} s: {', '.join(pending)}", file=sys.stderr
        )
        return 1
    print("wait_for: all ready", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
