"""The parity checker itself (scripts/phase20_parity.py): it crashed on its first real run
(an async generator inside sum()), so it is tested here - it must report "same" for equal
sources and count every difference otherwise."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

from aeoi_tools import schemas as s
from aeoi_tools.adapters.catalog import CatalogAdapter

ROOT = Path(__file__).resolve().parents[2]
SERVICES = [
    {"key": "a-db", "name": "A Db", "team": "t1", "type": "database", "tier": 1,
     "language": "n/a", "repository": "r/a", "depends_on": []},
    {"key": "b-api", "name": "B Api", "team": "t1", "type": "api", "tier": 2,
     "language": "go", "repository": "r/b", "depends_on": ["a-db"]},
]  # fmt: skip


def _parity() -> ModuleType:
    spec = importlib.util.spec_from_file_location("parity", ROOT / "scripts/phase20_parity.py")
    assert spec and spec.loader
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class Shifted:
    """A source that differs from the file: every tier off by one."""

    def __init__(self, inner: CatalogAdapter) -> None:
        self.inner = inner

    async def query(self, q: s.CatalogIn) -> s.CatalogOut:
        out = await self.inner.query(q)
        return s.CatalogOut(items=[i.model_copy(update={"tier": i.tier + 1}) for i in out.items],
                            truncated=out.truncated)  # fmt: skip


async def test_equal_sources_report_same_and_count_items(tmp_path: Path) -> None:
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps({"seed": 1, "services": SERVICES}))
    p = _parity()
    file = CatalogAdapter(path)
    lines, failed = await p.compare(file, CatalogAdapter(path), p.queries_for(SERVICES))
    assert failed == 0
    assert all(line.endswith(": same") for line in lines), lines
    assert lines[0].startswith("service_key       3 queries,     2 items")  # 2 keys + "no-such-svc"


async def test_every_difference_is_counted_and_shown(tmp_path: Path) -> None:
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps({"seed": 1, "services": SERVICES}))
    p = _parity()
    file = CatalogAdapter(path)
    lines, failed = await p.compare(file, Shifted(file), p.queries_for(SERVICES))
    assert failed > 0 and any("MISMATCH" in line for line in lines)
    assert any("json:" in line and "java:" in line for line in lines)
