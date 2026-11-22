"""Phase 32a: ONE catalog file for the Java import (it deletes and replaces the whole catalog):
the generated Northwind file + AEOI's own services, merged by the same rule as the JSON adapter
(a key in two files is refused). Usage: python scripts/catalog_merge.py OUT IN [IN ...]"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from aeoi_tools.adapters.catalog import load_services


def main(argv: list[str]) -> int:
    out, *ins = (Path(a) for a in argv)
    if not ins:
        print(__doc__, file=sys.stderr)
        return 2
    first = json.loads(ins[0].read_text())
    merged = {"seed": first.get("seed"), "services": list(load_services(*ins).values())}
    out.write_text(json.dumps(merged))
    print(f"catalog: {len(merged['services'])} services from {len(ins)} file(s) -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
