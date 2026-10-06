"""Unit-test helpers: a fake sessionmaker that records what the service would persist."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any
from uuid import uuid4

import pytest

from aeoi_security.auth import Principal
from aeoi_security.rbac import permissions_for
from aeoi_tools.contracts import ToolContext


@dataclass
class FakeDB:
    rows: list[Any] = field(default_factory=list)
    fail: bool = False

    def __call__(self) -> FakeDB:  # sessionmaker() -> session
        return self

    async def __aenter__(self) -> FakeDB:
        return self

    async def __aexit__(
        self, t: type[BaseException] | None, e: BaseException | None, tb: TracebackType | None
    ) -> None:
        return None

    def begin(self) -> FakeDB:
        return self

    def add(self, row: Any) -> None:
        if self.fail:
            raise ConnectionError("db down")
        self.rows.append(row)

    def of(self, kind: str) -> list[Any]:
        return [r for r in self.rows if type(r).__name__ == kind]


def user(*roles: str, groups: tuple[str, ...] = ("eng-all",)) -> Principal:
    uid = uuid4()
    return Principal(
        subject=f"oidc|{uid}",
        user_id=uid,
        email="u@northwind.example",
        name="U",
        roles=frozenset(roles),
        groups=frozenset(groups),
        permissions=permissions_for(frozenset(roles)),
    )


def ctx(*roles: str, agent: str | None = None, approval: bool = False) -> ToolContext:
    return ToolContext(
        user=user(*roles),
        user_token="t",
        agent=agent,
        service="service:agents" if agent else None,
        approval_id=uuid4() if approval else None,
    )


@pytest.fixture
def fake_db() -> FakeDB:
    return FakeDB()


@pytest.fixture
def make_ctx() -> Callable[..., ToolContext]:
    return ctx
