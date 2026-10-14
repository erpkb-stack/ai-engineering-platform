"""Repo-wide test isolation: a developer's switches in .env must never change what a test runs.

The Makefile exports .env into every command, `make test-integration` included (17b-fix1 was
the same class of bug). Variables listed here select DEV wiring, not behaviour under test: each
test gets the default unless it sets one itself (monkeypatch.setenv after this fixture).
"""

from __future__ import annotations

import os

import pytest

DEV_ONLY_SWITCHES = (
    # Phase 20 (ADR-032): the gateway's catalog source. Tests use the JSON default; the Java
    # path is tested with fakes (unit) and by `make catalog-smoke` (real service).
    "AEOI_TOOLS_CATALOG_URL",
)


# at import, before ANY fixture: session-scoped fixtures run before function-scoped ones
for _name in DEV_ONLY_SWITCHES:
    os.environ.pop(_name, None)


@pytest.fixture(autouse=True)
def _no_dev_switches(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in DEV_ONLY_SWITCHES:
        monkeypatch.delenv(name, raising=False)
