"""Embeddings through the LLM gateway (never a model SDK here - AGENTS.md rule 6)."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from aeoi_db.base import EMBEDDING_DIM
from aeoi_llm_client import LLMClient


class EmbeddingError(RuntimeError):
    pass


class Embedder(Protocol):
    @property
    def model(self) -> str | None: ...
    async def embed_documents(self, texts: list[str]) -> list[list[float]]: ...
    async def embed_query(self, text: str) -> list[float]: ...


class FileTokenProvider:
    """Reads the service token from a file on every call (cheap) so a rotated token is
    picked up without a restart. The file is created by `make rag-token`."""

    def __init__(self, path: Path) -> None:
        self.path = path

    async def __call__(self) -> str:
        try:
            token = self.path.read_text().strip()
        except FileNotFoundError as exc:
            raise EmbeddingError(
                f"service token file {self.path} missing - run `make rag-token`"
            ) from exc
        if not token:
            raise EmbeddingError(f"service token file {self.path} is empty")
        return token


class GatewayEmbedder:
    def __init__(
        self,
        client: LLMClient,
        *,
        route: str,
        document_prefix: str,
        query_prefix: str,
        batch_size: int = 32,
    ) -> None:
        self._client = client
        self._route = route
        self._doc_prefix = document_prefix
        self._query_prefix = query_prefix
        self._batch = batch_size
        self._model: str | None = None

    @property
    def model(self) -> str | None:
        return self._model

    async def _embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), self._batch):
            resp = await self._client.embed(texts[i : i + self._batch], route=self._route)
            if resp.dimensions != EMBEDDING_DIM:
                # the column is vector(768); a different model needs a new column + backfill
                raise EmbeddingError(
                    f"model {resp.model} returns {resp.dimensions}-d vectors; schema expects {EMBEDDING_DIM}"
                )
            if self._model is not None and resp.model != self._model:
                raise EmbeddingError(
                    f"embedding model changed mid-run: {self._model} -> {resp.model}"
                )
            self._model = resp.model
            out.extend(resp.vectors)
        return out

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return await self._embed([self._doc_prefix + t for t in texts])

    async def embed_query(self, text: str) -> list[float]:
        return (await self._embed([self._query_prefix + text]))[0]
