"""Generate the Python models of the service-catalog contract (ADR-032, contract-first).

    uv run python scripts/catalog_contract.py           # write the models
    uv run python scripts/catalog_contract.py --check   # exit 1 if the committed file is stale

ONE function makes the file, used by `make catalog-contract` and by the drift test, so the
test can never check something the target does not produce.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "services" / "service-catalog" / "openapi.yaml"
TARGET = (
    ROOT / "services" / "tool-gateway" / "src" / "aeoi_tools" / "adapters" / "catalog_contract.py"
)
HEADER = (
    '"""GENERATED from services/service-catalog/openapi.yaml by `make catalog-contract` - do not '
    'edit.\n\nThe drift test (test_catalog_contract.py) fails when this file is stale."""'
)


def _bin(name: str) -> str:
    return str(Path(sys.executable).parent / name)  # the venv's tools, never a global one


def generate(spec: Path = SPEC) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "catalog_contract.py"
        subprocess.run(  # noqa: S603 - fixed argv, venv binaries, repo paths
            [_bin("datamodel-codegen"), "--input", str(spec), "--input-file-type", "openapi",
             "--output", str(out), "--output-model-type", "pydantic_v2.BaseModel",
             "--target-python-version", "3.12", "--use-standard-collections",
             "--use-union-operator", "--field-constraints", "--disable-timestamp",
             "--use-double-quotes", "--formatters", "black", "isort",
             "--custom-file-header", HEADER],
            check=True, capture_output=True, text=True,
        )  # fmt: skip
        # the repo's formatter decides the final bytes (black/isort only make it parseable)
        for cmd in (["check", "--quiet", "--fix", "--select", "I,UP"], ["format", "--quiet"]):
            subprocess.run([_bin("ruff"), *cmd, str(out)], check=True,  # noqa: S603 - fixed argv
                           capture_output=True, text=True)  # fmt: skip
        return out.read_text()


def main(argv: list[str]) -> int:
    fresh = generate()
    if "--check" in argv:
        if not TARGET.exists() or TARGET.read_text() != fresh:
            print(f"{TARGET.relative_to(ROOT)} is stale: run `make catalog-contract`",
                  file=sys.stderr)  # fmt: skip
            return 1
        return 0
    TARGET.write_text(fresh)
    print(f"wrote {TARGET.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
