"""rag settings. Retrieval knobs live here so the eval can compare them explicitly."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import SettingsConfigDict

from aeoi_common.settings import BaseServiceSettings
from aeoi_db.config import find_repo_root, service_database_url


def _secret(name: str) -> Path:
    return find_repo_root() / "secrets" / name


class Settings(BaseServiceSettings):
    model_config = SettingsConfigDict(env_prefix="AEOI_RAG_", extra="ignore", frozen=True)

    service_name: str = "rag"
    port: int = 8004
    db_user: str = "rag_svc"
    db_password_file: Path = Field(default_factory=lambda: _secret("rag_svc_password.txt"))
    db_url_override: SecretStr | None = None
    db_pool_size: int = 5

    jwt_public_key_file: Path = Field(default_factory=lambda: _secret("jwt_public.pem"))
    jwt_issuer: str = "aeoi-dev-issuer"
    jwt_audience: str = "aeoi-api"

    # LLM gateway (embeddings + optional rerank). The service token is minted by
    # `make rag-token` (dev). Prod: OAuth2 client credentials / workload identity.
    llm_gateway_url: str = "http://localhost:8005"
    llm_token_file: Path = Field(default_factory=lambda: _secret("rag_service_token.txt"))
    embed_route: str = "embed"
    # nomic-embed-text was trained with task prefixes; leaving them out costs quality.
    document_prefix: str = "search_document: "
    query_prefix: str = "search_query: "
    embed_batch_size: int = 32

    # chunking (~4 chars per token is a rough estimate; no tokenizer dependency)
    chunk_target_tokens: int = 350
    chunk_overlap_tokens: int = 50
    max_document_bytes: int = 10 * 1024 * 1024
    max_pdf_pages: int = 300

    # retrieval
    vector_candidates: int = 40
    keyword_candidates: int = 40
    # Chosen by `make rag-sweep` on the owner's Mac (nomic-embed-text): best on the dev split,
    # confirmed on the held-out test split (MRR@10 0.799 vs 0.598 for plain RRF k=60). ADR-016.
    rrf_k: int = 1
    # Weighted RRF when the query contains an identifier. 1.0 = plain RRF. Tune ONLY from a
    # real-embedding eval run (AEOI_RAG_KEYWORD_WEIGHT_IDENTIFIERS=2 make rag-eval), never from fakes.
    keyword_weight_identifiers: float = 2.0
    vector_weight_no_identifiers: float = 4.0
    max_chunks_per_document: int = 2
    hnsw_ef_search: int = 100

    # rerank (LLM, listwise graded relevance). Off by default until the eval shows a gain.
    rerank_default: bool = False
    rerank_route: str = "fast"
    rerank_route_restricted: str = "local"  # RESTRICTED text never goes to a hosted model
    rerank_candidates: int = 10
    rerank_snippet_chars: int = 400
    rerank_timeout_s: float = 25.0

    def sqlalchemy_url(self) -> str:
        if self.db_url_override is not None:
            return self.db_url_override.get_secret_value()
        return service_database_url(
            user=self.db_user, password_file=self.db_password_file
        ).render_as_string(hide_password=False)
