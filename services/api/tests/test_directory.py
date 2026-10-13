"""Phase 17a (ADR-027, review H1): an IdP token must agree with OUR directory at the edge."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import pytest

from aeoi_api.directory import DirectoryCheck
from aeoi_common.errors import ForbiddenError, UnauthorizedError, UpstreamUnavailableError
from aeoi_security.auth import Principal
from aeoi_security.rbac import permissions_for


@dataclass
class FakeUser:
    external_subject: str
    is_active: bool = True


@dataclass
class FakeDir:
    users: dict[UUID, FakeUser] = field(default_factory=dict)
    roles: dict[UUID, list[str]] = field(default_factory=dict)
    loads: int = 0

    def __call__(self) -> Any:
        directory = self

        class S:
            async def get(self, _model: Any, uid: UUID) -> FakeUser | None:
                directory.loads += 1
                return directory.users.get(uid)

            async def scalars(self, stmt: Any) -> Any:
                uid = stmt.whereclause.right.value

                class R:
                    def all(self) -> list[str]:
                        return directory.roles.get(uid, [])

                return R()

        @asynccontextmanager
        async def cm() -> Any:
            yield S()

        return cm()


def idp_user(uid: UUID, *roles: str, sub: str = "oidc|lena", idp: bool = True) -> Principal:
    r = frozenset(roles)
    return Principal(subject=sub, user_id=uid, email="", name="", roles=r, groups=frozenset(),
                     permissions=permissions_for(r), idp=idp)  # fmt: skip


async def test_the_directory_decides_for_idp_tokens() -> None:
    uid = uuid4()
    d = FakeDir()
    check = DirectoryCheck(d, ttl_s=0)  # type: ignore[arg-type]
    with pytest.raises(UnauthorizedError):  # not in the directory
        await check.check(idp_user(uid, "INCIDENT_COMMANDER"))
    d.users[uid] = FakeUser("oidc|lena")
    d.roles[uid] = ["INCIDENT_COMMANDER"]
    await check.check(idp_user(uid, "INCIDENT_COMMANDER"))  # agrees
    with pytest.raises(UnauthorizedError):  # same uid, another subject: not the same person
        await check.check(idp_user(uid, "INCIDENT_COMMANDER", sub="oidc|someone.else"))
    d.users[uid].is_active = False
    with pytest.raises(UnauthorizedError):  # deactivated: out within the TTL
        await check.check(idp_user(uid, "INCIDENT_COMMANDER"))
    d.users[uid].is_active = True
    d.roles[uid] = ["SRE"]  # demoted in the directory, realm still says IC
    with pytest.raises(ForbiddenError, match="INCIDENT_COMMANDER"):
        await check.check(idp_user(uid, "INCIDENT_COMMANDER"))
    d.roles[uid] = ["SRE", "INCIDENT_COMMANDER"]  # promoted, realm not yet: fewer rights, fine
    await check.check(idp_user(uid, "INCIDENT_COMMANDER"))


async def test_cached_for_the_ttl_and_static_tokens_untouched() -> None:
    uid = uuid4()
    d = FakeDir(users={uid: FakeUser("oidc|lena")}, roles={uid: ["SRE"]})
    check = DirectoryCheck(d, ttl_s=60)  # type: ignore[arg-type]
    for _ in range(5):
        await check.check(idp_user(uid, "SRE"))
    assert d.loads == 1
    await check.check(idp_user(uuid4(), "ADMIN", idp=False))  # minted FROM the directory
    assert d.loads == 1


async def test_no_directory_means_no_idp_users() -> None:
    with pytest.raises(UpstreamUnavailableError):
        await DirectoryCheck(None).check(idp_user(uuid4(), "SRE"))
    await DirectoryCheck(None).check(idp_user(uuid4(), "SRE", idp=False))  # static: untouched


async def test_every_api_route_asks_the_directory_before_forwarding(tmp_path: Any) -> None:
    """Wiring (mutation found it untested at unit level): BOTH dependencies - guard(perm) and
    authenticated() - call the directory, and a refusal stops the request before the service."""
    import httpx

    from aeoi_api.config import Settings
    from aeoi_api.main import build_app
    from aeoi_security.testing import generate_keypair, token_for

    keys = generate_keypair()
    pub = tmp_path / "pub.pem"
    pub.write_text(keys.public_pem)
    calls: list[httpx.Request] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"items": [], "next_cursor": None})

    app = build_app(Settings(jwt_public_key_file=pub, environment="test"),  # type: ignore[arg-type]
                    transports={"incident-service": httpx.MockTransport(upstream)})  # fmt: skip
    asked: list[Principal] = []

    class Refuse:
        async def check(self, principal: Principal) -> None:
            asked.append(principal)
            raise UnauthorizedError("Not an active user in the directory.")

    app.state.directory = Refuse()
    h = {"Authorization": f"Bearer {token_for(keys, 'SRE')}"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://a") as c:
        assert (await c.get("/api/v1/incidents", headers=h)).status_code == 401  # guard(perm)
        assert (await c.get("/api/v1/me", headers=h)).status_code == 401  # authenticated()
    assert len(asked) == 2
    assert calls == []
