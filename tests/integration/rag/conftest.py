"""RAG integration fixtures: real Postgres (least-privilege rag login), the REAL llm-gateway
app in-process with fake providers (hashed bag-of-words embeddings), the doc pack ingested once.
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import psycopg
import pytest
from fastapi import FastAPI
from pydantic import SecretStr

from aeoi_db.config import database_url, libpq_dsn
from aeoi_db.users import ensure_login
from aeoi_llm.config import Settings as LlmSettings
from aeoi_llm.main import build_app as build_llm
from aeoi_llm_client import HttpLLMClient
from aeoi_rag.config import Settings
from aeoi_rag.db import make_engine, make_sessionmaker
from aeoi_rag.ingest import Ingestor, IngestReport
from aeoi_rag.main import build_app as build_rag
from aeoi_rag.main import make_embedder
from aeoi_security.testing import KeyPair, generate_keypair, service_token_for, token_for
from aeoi_synth.docpack import DocSpec, build_pack, write_pack

LOGIN = "rag_svc_test"
CONFIG = Path(__file__).resolve().parents[3] / "services" / "llm-gateway" / "config"


@dataclass
class Pack:
    root: Path
    docs: dict[str, DocSpec]  # source_uri -> spec
    report: IngestReport


@pytest.fixture(scope="session")
def keys() -> KeyPair:
    return generate_keypair()


@pytest.fixture(scope="session")
def pubfile(tmp_path_factory: pytest.TempPathFactory, keys: KeyPair) -> Path:
    path = tmp_path_factory.mktemp("ragkeys") / "jwt_public.pem"
    path.write_text(keys.public_pem)
    return path


@pytest.fixture(scope="session")
def rag_db_url(migrated_db: str) -> str:
    password = secrets.token_hex(16)
    with psycopg.connect(libpq_dsn(database=migrated_db), autocommit=True) as conn:
        ensure_login(conn, LOGIN, "svc_rag", password)
    return (
        database_url(database=migrated_db)
        .set(username=LOGIN, password=password)
        .render_as_string(hide_password=False)
    )


@pytest.fixture(scope="session")
def rag_settings(rag_db_url: str, pubfile: Path) -> Settings:
    return Settings(
        db_url_override=SecretStr(rag_db_url),
        jwt_public_key_file=pubfile,
        environment="test",
        log_json=False,
    )  # type: ignore[call-arg]


def make_llm_app(pubfile: Path) -> FastAPI:
    return build_llm(
        LlmSettings(
            routing_file=CONFIG / "routing.test.yaml",
            jwt_public_key_file=pubfile,
            record_usage=False,
            cache_enabled=False,
            environment="test",
            log_json=False,
        )
    )  # type: ignore[call-arg]


def llm_client(keys: KeyPair, app: FastAPI) -> HttpLLMClient:
    async def token() -> str:
        return service_token_for(keys, "rag", "llm:invoke")

    return HttpLLMClient("http://llm", token, transport=httpx.ASGITransport(app=app))


@pytest.fixture(scope="session")
def pack(
    tmp_path_factory: pytest.TempPathFactory, rag_settings: Settings, keys: KeyPair, pubfile: Path
) -> Pack:
    root = tmp_path_factory.mktemp("pack") / "sample-documents"
    write_pack(root)
    docs, _ = build_pack()

    async def run() -> IngestReport:
        engine = make_engine(rag_settings.sqlalchemy_url(), 2)
        client = llm_client(keys, make_llm_app(pubfile))
        try:
            ing = Ingestor(
                make_sessionmaker(engine), make_embedder(rag_settings, client), rag_settings
            )
            report = await ing.ingest_pack(root)
            report.embedded_chunks = await ing.embed_pending()
            return report
        finally:
            await client.aclose()
            await engine.dispose()

    return Pack(root, {d.source_uri: d for d in docs}, asyncio.run(run()))


@pytest.fixture
async def rag_app(
    rag_settings: Settings, keys: KeyPair, pubfile: Path, pack: Pack
) -> AsyncIterator[FastAPI]:
    client = llm_client(keys, make_llm_app(pubfile))
    app = build_rag(rag_settings, llm_client=client)
    yield app
    await client.aclose()
    await app.state.engine.dispose()


@pytest.fixture
async def rag(rag_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=rag_app), base_url="http://rag"
    ) as c:
        yield c


def user(keys: KeyPair, *groups: str, role: str = "ENGINEER") -> dict[str, str]:
    return {"Authorization": f"Bearer {token_for(keys, role, groups=tuple(groups))}"}


@pytest.fixture
def owner_conn(migrated_db: str) -> Iterator[psycopg.Connection]:
    with psycopg.connect(libpq_dsn(database=migrated_db), autocommit=True) as conn:
        yield conn
