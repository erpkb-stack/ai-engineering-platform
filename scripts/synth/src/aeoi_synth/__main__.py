"""CLI: python -m aeoi_synth {summary|load} [--seed N] [--small]"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime

from aeoi_db.config import find_repo_root, libpq_dsn
from aeoi_synth.generate import Scale, generate
from aeoi_synth.load import load, write_catalog


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="aeoi_synth", description=__doc__)
    p.add_argument("command", choices=("summary", "load"))
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--small", action="store_true", help="small dataset for quick tests")
    args = p.parse_args(argv)

    started = datetime.now(UTC)
    ds = generate(args.seed, Scale.small() if args.small else Scale())
    counts = ds.counts()
    if args.command == "summary":
        print(
            json.dumps(
                {"seed": args.seed, "fingerprint": ds.fingerprint(), "counts": counts}, indent=1
            )
        )
        return 0

    loaded = load(ds, libpq_dsn())
    out = find_repo_root() / "data" / "generated"
    catalog = write_catalog(ds, out)
    manifest = {
        "seed": args.seed,
        "scale": "small" if args.small else "default",
        "counts": counts,
        "fingerprint": ds.fingerprint(),
        "loaded_at": started.isoformat(),
        "catalog_file": str(catalog.relative_to(find_repo_root())),
    }
    (out / "MANIFEST.json").write_text(json.dumps(manifest, indent=1, sort_keys=True))
    width = max(map(len, loaded))
    for table, n in loaded.items():
        print(f"  {table:<{width}}  {n:>8,}")
    print(f"  {'catalog.services (json)':<{width}}  {len(ds.catalog):>8,}")
    print(
        f"seed={args.seed} fingerprint={manifest['fingerprint']} took={(datetime.now(UTC) - started).seconds}s"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
