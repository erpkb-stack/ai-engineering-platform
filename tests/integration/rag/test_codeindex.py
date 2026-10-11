"""Phase 14 (ADR-024): the code index (tree-sitter at HEAD) and "who uses X", ACL in the SQL.

The repositories are the deterministic codepack (small scale), written to a temp dir. These
tests prove the index plumbing and the permission filter, not the precision of the rules
(that is scripts/synth/tests/test_codegen.py, over every commit).
"""

from __future__ import annotations

import json
import shutil
from functools import cache
from pathlib import Path

import httpx
import psycopg
import pytest
from fastapi import FastAPI

from aeoi_rag import codeindex
from aeoi_security.testing import KeyPair
from aeoi_synth.codegen import REPO_ACL_FILE, CodePack, write_repos
from aeoi_synth.generate import Scale, generate

from .conftest import user

pytestmark = pytest.mark.integration
SYMBOL = "ShipmentResponse.address"
READERS = {"notifications-api", "reviews-api", "media-api", "recommendations-api"}


@cache
def codepack() -> CodePack:
    pack: CodePack = generate(42, Scale.small()).code
    return pack


def repo_of(service: str) -> str:
    return next(r for r, files in codepack().repos.items()
                if any(p.startswith(f"services/{service}/") for p in files))  # fmt: skip


@pytest.fixture
def repos(tmp_path: Path) -> Path:
    root = tmp_path / "sample-repositories"
    write_repos(codepack(), root)
    return root


@pytest.fixture
async def indexed(rag_app: FastAPI, repos: Path, owner_conn: psycopg.Connection) -> Path:
    owner_conn.execute("TRUNCATE rag.code_files CASCADE")
    rep = await codeindex.index_repos(rag_app.state.sessionmaker, repos)
    assert rep.repositories == len(codepack().repos) and rep.parse_errors == 0
    assert rep.files_indexed > 0 and rep.refs > 0 and not rep.skipped_repos
    return repos


async def test_impact_of_the_planted_rename_is_exactly_the_four_readers(
    rag_app: FastAPI, indexed: Path
) -> None:
    hits = await codeindex.references(rag_app.state.sessionmaker, SYMBOL, ["eng-all"])
    services = {h["service_key"] for h in hits} - {"shipping-api"}
    assert services == READERS
    assert {h["name"] for h in hits} <= {"address", "getAddress", "Address", "ShipmentResponse"}


async def test_exclude_repository_drops_the_owners_own_repo(
    rag_app: FastAPI, indexed: Path
) -> None:
    own = repo_of("shipping-api")
    hits = await codeindex.references(
        rag_app.state.sessionmaker, SYMBOL, ["eng-all"], exclude_repository=own
    )
    assert hits and all(h["repository"] != own for h in hits)


async def test_no_groups_means_nothing_and_a_wrong_group_means_nothing(
    rag_app: FastAPI, indexed: Path
) -> None:
    s = rag_app.state.sessionmaker
    assert await codeindex.references(s, SYMBOL, []) == []
    assert await codeindex.references(s, SYMBOL, ["finance-only"]) == []


def _plant(root: Path, repo: str, groups: list[str], service: str, source: str) -> None:
    d = root / "northwind" / repo
    (d / "services" / service).mkdir(parents=True)
    (d / REPO_ACL_FILE).write_text(json.dumps({"allowed_groups": groups}))
    (d / "services" / service / "x.go").write_text(source)


LEAK = """package vault

func Leak(r ShipmentResponse, repo OrderRepository) string {
\trepo.FindOpenOrders("x")
\treturn r.Address
}
"""


@pytest.mark.parametrize("symbol", [SYMBOL, "FindOpenOrders"])
async def test_a_restricted_file_never_leaks_through_the_api(
    rag: httpx.AsyncClient, rag_app: FastAPI, indexed: Path, keys: KeyPair, symbol: str
) -> None:
    """A security-team-only repository that uses the symbol: visible to security-team, never in
    the SQL result for eng-all. A plain name (FindOpenOrders) has no type sub-query, so the
    main ACL predicate is the only thing standing between eng-all and the row."""
    _plant(indexed, "zz-vault", ["security-team"], "vault-api", LEAK)
    await codeindex.index_repos(rag_app.state.sessionmaker, indexed)
    body = {"symbol": symbol, "limit": 200}
    eng = await rag.post("/v1/code/references", json=body, headers=user(keys, "eng-all"))
    sec = await rag.post("/v1/code/references", json=body, headers=user(keys, "security-team"))
    assert eng.status_code == 200 and sec.status_code == 200
    assert "vault-api" not in {h["service_key"] for h in eng.json()}
    assert {h["service_key"] for h in sec.json()} == {"vault-api"}
    assert all(h["open_to_engineering"] is False for h in sec.json())  # never stored on an incident
    assert eng.json() == [] or all(h["open_to_engineering"] for h in eng.json())


async def test_a_field_with_the_same_name_on_another_type_is_not_impact(
    rag_app: FastAPI, indexed: Path
) -> None:
    """Simple names are a heuristic ([P]); the type filter is what keeps Customer.Address out."""
    decoy = "package crm\n\ntype Customer struct {\n\tAddress string\n}\n\n" \
            "func Where(c Customer) string {\n\treturn c.Address\n}\n"  # fmt: skip
    _plant(indexed, "zz-crm", ["eng-all"], "crm-api", decoy)
    await codeindex.index_repos(rag_app.state.sessionmaker, indexed)
    hits = await codeindex.references(rag_app.state.sessionmaker, SYMBOL, ["eng-all"])
    assert "crm-api" not in {h["service_key"] for h in hits}
    plain = await codeindex.references(rag_app.state.sessionmaker, "Address", ["eng-all"])
    assert "crm-api" in {h["service_key"] for h in plain}  # the decoy IS indexed


async def test_a_manager_cannot_read_code(rag: httpx.AsyncClient, keys: KeyPair) -> None:
    r = await rag.post("/v1/code/references", json={"symbol": SYMBOL},
                       headers=user(keys, "eng-all", role="MANAGER"))  # fmt: skip
    assert r.status_code == 403


async def test_reindex_skips_unchanged_files_and_drops_deleted_ones(
    rag_app: FastAPI, indexed: Path, owner_conn: psycopg.Connection
) -> None:
    s = rag_app.state.sessionmaker
    again = await codeindex.index_repos(s, indexed)
    assert again.files_indexed == 0 and again.files_unchanged > 0
    reader = repo_of("media-api")
    victim = next(p for p in codepack().repos[reader] if p.startswith("services/media-api/"))
    (indexed / reader / victim).unlink()
    gone = await codeindex.index_repos(s, indexed)
    assert gone.files_deleted == 1 and gone.files_indexed == 0
    n = owner_conn.execute(
        "SELECT count(*) FROM rag.code_files WHERE repository = %s AND path = %s",
        (reader, victim),
    ).fetchone()
    assert n == (0,)
    # symbols/refs went with the file (ON DELETE CASCADE): no orphan rows
    orphans = owner_conn.execute(
        "SELECT count(*) FROM rag.code_refs r LEFT JOIN rag.code_files f ON f.id = r.file_id "
        "WHERE f.id IS NULL"
    ).fetchone()
    assert orphans == (0,)


async def test_a_repository_without_an_acl_file_is_not_indexed(
    rag_app: FastAPI, repos: Path, owner_conn: psycopg.Connection
) -> None:
    owner_conn.execute("TRUNCATE rag.code_files CASCADE")
    victim = repo_of("shipping-api")
    (repos / victim / REPO_ACL_FILE).unlink()
    rep = await codeindex.index_repos(rag_app.state.sessionmaker, repos)
    assert rep.skipped_repos == [victim]
    n = owner_conn.execute(
        "SELECT count(*) FROM rag.code_files WHERE repository = %s", (victim,)
    ).fetchone()
    assert n == (0,)
    shutil.rmtree(repos)
