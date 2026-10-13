"""Write the api's OpenAPI contract to frontend/openapi.json (Phase 17b).

The console's TypeScript types are generated FROM this file (`make openapi`), never typed by
hand. `tests/unit/test_openapi_contract.py` fails when the api changes and this file was not
regenerated, so the UI can never drift silently from the api.

  uv run python scripts/export_openapi.py [--check]
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

from aeoi_db.config import find_repo_root

OUT = Path("frontend") / "openapi.json"


def spec() -> dict[str, Any]:
    from aeoi_api.config import Settings
    from aeoi_api.main import build_app
    from aeoi_security.testing import generate_keypair

    with tempfile.TemporaryDirectory() as d:  # the contract does not depend on any key
        pub = Path(d) / "pub.pem"
        pub.write_text(generate_keypair().public_pem)
        app = build_app(Settings(jwt_public_key_file=pub, environment="test"))  # type: ignore[arg-type]
        full = app.openapi()
    # the browser talks to /api/v1 only: the token exchange (/internal) and health stay out
    full["paths"] = {p: v for p, v in full["paths"].items() if p.startswith("/api/v1/")
                     and p != "/api/v1/health"}  # fmt: skip
    return full


def render() -> str:
    return json.dumps(spec(), indent=2, sort_keys=True) + "\n"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--check", action="store_true", help="exit 1 if the file is out of date")
    args = p.parse_args(argv)
    out = find_repo_root() / OUT
    text = render()
    if args.check:
        if not out.exists() or out.read_text() != text:
            print(f"{OUT} is out of date: run `make openapi`", file=sys.stderr)
            return 1
        return 0
    out.write_text(text)
    print(f"wrote {OUT} ({len(spec()['paths'])} paths)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
