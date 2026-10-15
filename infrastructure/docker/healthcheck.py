"""Container healthcheck (Phase 21, ADR-033): the service's READINESS, not just "the port is open".

    python /app/infrastructure/docker/healthcheck.py

The port comes from $AEOI_SERVICE_PORT, so the ONE image can declare a HEALTHCHECK that works
for every service (compose sets the variable). Exit 0 only on HTTP 200 from
http://127.0.0.1:<port>/health/ready, else 1 (Docker reserves 2). No curl in the image, and no
proxy: Docker Desktop may copy the host's proxy settings into containers.
"""

from __future__ import annotations

import os
import sys
import urllib.error
import urllib.request

PATH = "/health/ready"


def target(env: dict[str, str]) -> str | None:
    """The URL to probe, or None without a usable port."""
    port = env.get("AEOI_SERVICE_PORT", "")
    return f"http://127.0.0.1:{int(port)}{PATH}" if port.isdigit() else None


def main() -> int:
    url = target(dict(os.environ))
    if url is None:
        print("healthcheck: AEOI_SERVICE_PORT is not set", file=sys.stderr)
        return 1
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # never a proxy
    try:
        with opener.open(url, timeout=3) as r:
            return 0 if r.status == 200 else 1
    except (urllib.error.URLError, OSError) as exc:  # HTTPError (503 not ready) is a URLError
        print(f"not ready: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
