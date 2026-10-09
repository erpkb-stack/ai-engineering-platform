"""rag on-behalf-of path (ADR-020): the tool-gateway's service token (`rag:obo`) + the user's
token, or an investigation's DELEGATED token, in X-On-Behalf-Of. The group filter must use the
on-behalf-of USER's groups - never the service's (it has none) - with the real retriever, the
real doc pack and the real permission SQL."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from aeoi_rag.config import Settings
from aeoi_rag.main import build_app as build_rag
from aeoi_security.testing import (
    KeyPair,
    delegated_token_for,
    generate_keypair,
    service_token_for,
    token_for,
)

from .conftest import Pack, llm_client, make_llm_app

pytestmark = pytest.mark.integration
RESTRICTED = "docpack://restricted/secrets-rotation-procedure.md"  # security-team only


@pytest.fixture(scope="module")
def sts_keys() -> KeyPair:
    return generate_keypair()  # delegation keys: a separate pair (ADR-019)


@pytest.fixture
async def obo_rag(
    rag_settings: Settings, keys: KeyPair, pubfile: Path, pack: Pack, sts_keys: KeyPair,
    tmp_path: Path,
) -> AsyncIterator[httpx.AsyncClient]:  # fmt: skip
    dpub = tmp_path / "delegation_public.pem"
    dpub.write_text(sts_keys.public_pem)
    client = llm_client(keys, make_llm_app(pubfile))
    app: FastAPI = build_rag(
        rag_settings.model_copy(update={"delegation_public_key_file": dpub}), llm_client=client
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://rag"
    ) as c:
        yield c
    await client.aclose()
    await app.state.engine.dispose()


def gateway(keys: KeyPair, scope: str = "rag:obo") -> str:
    return "Bearer " + service_token_for(keys, "tool-gateway", scope)


async def uris(rag: httpx.AsyncClient, headers: dict[str, str], q: str) -> list[str]:
    r = await rag.post("/v1/search", json={"query": q, "k": 10}, headers=headers)
    assert r.status_code == 200, r.text
    return [h["source_uri"] for h in r.json()["results"]]


async def test_obo_uses_the_users_groups_not_the_services(
    obo_rag: httpx.AsyncClient, keys: KeyPair
) -> None:
    q = "secrets rotation procedure"
    outsider = {"Authorization": gateway(keys),
                "X-On-Behalf-Of": "Bearer " + token_for(keys, "ENGINEER", groups=("eng-all",))}  # fmt: skip
    member = {"Authorization": gateway(keys),
              "X-On-Behalf-Of": "Bearer " + token_for(keys, "ENGINEER", groups=("security-team",))}  # fmt: skip
    assert RESTRICTED not in await uris(obo_rag, outsider, q)
    assert RESTRICTED in await uris(obo_rag, member, q)
    # same answer as the user searching directly: OBO adds nothing, removes nothing
    direct = {"Authorization": "Bearer " + token_for(keys, "ENGINEER", groups=("eng-all",))}
    assert await uris(obo_rag, outsider, q) == await uris(obo_rag, direct, q)


async def test_delegated_token_in_the_obo_slot_carries_the_directory_groups(
    obo_rag: httpx.AsyncClient, keys: KeyPair, sts_keys: KeyPair
) -> None:
    q = "secrets rotation procedure"

    def deleg(*groups: str) -> dict[str, str]:
        tok = delegated_token_for(sts_keys, "SRE", investigation_id=uuid4(), groups=groups)
        return {"Authorization": gateway(keys), "X-On-Behalf-Of": f"Bearer {tok}"}

    assert RESTRICTED not in await uris(obo_rag, deleg("eng-all"), q)
    assert RESTRICTED in await uris(obo_rag, deleg("security-team"), q)


@pytest.mark.parametrize(
    ("case", "status"),
    [
        ("service alone", 403),  # no confused deputy: the gateway never searches as itself
        ("service without rag:obo", 403),
        ("delegated as the bearer", 401),  # ADR-019: never a primary bearer
        ("obo with a service token", 403),
        ("obo garbage", 403),
        ("obo user without docs:read", 403),  # ADMIN: separation of duties
    ],
)
async def test_obo_refusals(
    obo_rag: httpx.AsyncClient, keys: KeyPair, sts_keys: KeyPair, case: str, status: int
) -> None:
    user = "Bearer " + token_for(keys, "ENGINEER")
    headers = {
        "service alone": {"Authorization": gateway(keys)},
        "service without rag:obo": {"Authorization": gateway(keys, "audit:write"),
                                    "X-On-Behalf-Of": user},
        "delegated as the bearer": {"Authorization": "Bearer " + delegated_token_for(
            sts_keys, "SRE", investigation_id=uuid4())},
        "obo with a service token": {"Authorization": gateway(keys),
                                     "X-On-Behalf-Of": gateway(keys)},
        "obo garbage": {"Authorization": gateway(keys), "X-On-Behalf-Of": "Bearer x.y.z"},
        "obo user without docs:read": {"Authorization": gateway(keys),
                                       "X-On-Behalf-Of": "Bearer " + token_for(keys, "ADMIN")},
    }[case]  # fmt: skip
    r = await obo_rag.post("/v1/search", json={"query": "pool"}, headers=headers)
    assert r.status_code == status, (case, r.text)


async def test_without_the_delegation_key_rag_refuses_delegated_obo(
    rag: httpx.AsyncClient, keys: KeyPair, sts_keys: KeyPair
) -> None:
    """The default fixture app has no delegation key file: delegated tokens are refused."""
    tok = delegated_token_for(sts_keys, "SRE", investigation_id=uuid4())
    r = await rag.post("/v1/search", json={"query": "pool"},
                       headers={"Authorization": gateway(keys), "X-On-Behalf-Of": f"Bearer {tok}"})  # fmt: skip
    assert r.status_code == 403


async def test_delegated_tokens_cannot_open_whole_documents(
    obo_rag: httpx.AsyncClient, keys: KeyPair, sts_keys: KeyPair, owner_conn: Any
) -> None:
    """Least privilege (review finding): the investigation flow searches; it never opens a
    whole document. A user token in the OBO slot still can."""
    (doc_id,) = owner_conn.execute(
        "SELECT id FROM rag.documents WHERE source_uri = %s", (RESTRICTED,)
    ).fetchone()
    tok = delegated_token_for(sts_keys, "SRE", investigation_id=uuid4(), groups=("security-team",))
    r = await obo_rag.get(f"/v1/documents/{doc_id}",
                          headers={"Authorization": gateway(keys), "X-On-Behalf-Of": f"Bearer {tok}"})  # fmt: skip
    assert r.status_code == 403
    user = token_for(keys, "ENGINEER", groups=("security-team",))
    r = await obo_rag.get(f"/v1/documents/{doc_id}",
                          headers={"Authorization": gateway(keys), "X-On-Behalf-Of": f"Bearer {user}"})  # fmt: skip
    assert r.status_code == 200
