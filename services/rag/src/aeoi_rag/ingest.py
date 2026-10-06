"""Ingestion: parse -> scrub (secrets + PII) -> injection tripwire -> chunk -> store -> embed.

Two phases on purpose:
  1. `ingest_*`  writes documents + chunks (embedding NULL) in ONE transaction per document.
  2. `embed_pending` embeds chunks in batches and commits per batch.
A crash or a gateway outage in phase 2 loses at most one batch; re-running continues where it
stopped. Unchanged documents (same sha256) are skipped, so re-ingesting is cheap.

ACLs are never guessed: a file without a manifest entry (allowed_groups) is rejected.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aeoi_common.ids import uuid7
from aeoi_db.models.rag import Document, DocumentChunk
from aeoi_rag.chunking import chunk_sections
from aeoi_rag.config import Settings
from aeoi_rag.embedder import Embedder
from aeoi_rag.parsing import Parsed, ParseError, Section, detect_source, parse
from aeoi_security.injection import detect_injection
from aeoi_security.pii import scrub_pii
from aeoi_security.redaction import redact_text_counted

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class DocMeta:
    source_uri: str
    title: str
    department: str
    owner: str
    allowed_groups: list[str]
    sensitivity: str = "INTERNAL"
    version: str = "1"
    source: str | None = None  # detected from the filename when None


@dataclass
class DocOutcome:
    source_uri: str
    status: str  # indexed | unchanged | quarantined | rejected
    chunks: int = 0
    detail: str | None = None
    pii: int = 0
    secrets: int = 0


@dataclass
class IngestReport:
    outcomes: list[DocOutcome] = field(default_factory=list)
    embedded_chunks: int = 0
    seconds: float = 0.0

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for o in self.outcomes:
            out[o.status] = out.get(o.status, 0) + 1
        return out


def _clean(text: str) -> tuple[str, int, int]:
    redacted, n_secrets = redact_text_counted(text)
    scrubbed = scrub_pii(redacted)
    return scrubbed.text, scrubbed.total, n_secrets


class Ingestor:
    def __init__(
        self, sessions: async_sessionmaker[AsyncSession], embedder: Embedder, settings: Settings
    ) -> None:
        self._sessions = sessions
        self._embedder = embedder
        self._s = settings

    # ------------------------------------------------------------------ documents
    async def ingest_bytes(self, meta: DocMeta, data: bytes, filename: str) -> DocOutcome:
        if not meta.allowed_groups:
            return DocOutcome(
                meta.source_uri, "rejected", detail="no allowed_groups (default deny)"
            )
        if len(data) > self._s.max_document_bytes:
            return DocOutcome(
                meta.source_uri,
                "rejected",
                detail=f"larger than {self._s.max_document_bytes} bytes",
            )
        try:
            source = meta.source or detect_source(filename)
            parsed = parse(data, source, filename, max_pdf_pages=self._s.max_pdf_pages)
        except ParseError as exc:
            return DocOutcome(meta.source_uri, "rejected", detail=str(exc))

        # Scan BEFORE scrubbing (scrubbing could hide an exfiltration URL) and include text
        # a human reader would never see.
        hits = detect_injection(parsed.full_text + "\n" + parsed.hidden_text)
        cleaned_sections, pii, secrets = [], 0, 0
        for sec in parsed.sections:
            t, p, k = _clean(sec.text)
            pii, secrets = pii + p, secrets + k
            cleaned_sections.append(Section(sec.heading, t, sec.meta))
        cleaned = Parsed(parsed.title, cleaned_sections)
        content = cleaned.full_text
        sha = hashlib.sha256(content.encode()).hexdigest()
        reason = "injection:" + ",".join(hits) if hits else None
        title = meta.title or parsed.title or meta.source_uri

        async with self._sessions() as s, s.begin():
            existing = await s.scalar(
                select(Document)
                .where(Document.source_uri == meta.source_uri, Document.version == meta.version)
                .with_for_update()
            )
            if (
                existing is not None
                and existing.content_sha256 == sha
                and existing.allowed_groups == meta.allowed_groups
                and existing.quarantine_reason == reason
            ):
                return DocOutcome(meta.source_uri, "unchanged", pii=pii, secrets=secrets)
            doc = existing or Document(id=uuid7(), source_uri=meta.source_uri, version=meta.version)
            doc.source, doc.title, doc.department, doc.owner = (
                source,
                title[:400],
                meta.department,
                meta.owner,
            )
            doc.content, doc.content_sha256 = content, sha
            doc.allowed_groups, doc.sensitivity = list(meta.allowed_groups), meta.sensitivity
            doc.quarantined, doc.quarantine_reason = reason is not None, reason
            doc.indexed_at = None
            if existing is None:
                s.add(doc)
            await s.flush()
            await s.execute(delete(DocumentChunk).where(DocumentChunk.document_id == doc.id))
            if reason is not None:
                # Quarantined: stored for review, never chunked, never retrievable.
                log.warning("rag_document_quarantined", source_uri=meta.source_uri, reason=reason)
                return DocOutcome(
                    meta.source_uri, "quarantined", detail=reason, pii=pii, secrets=secrets
                )
            chunks = chunk_sections(
                title,
                cleaned.sections,
                target_tokens=self._s.chunk_target_tokens,
                overlap_tokens=self._s.chunk_overlap_tokens,
            )
            for c in chunks:
                # allowed_groups is set by the DB trigger from the document (never by us);
                # we pass the document's value only to satisfy NOT NULL before the trigger runs.
                s.add(
                    DocumentChunk(
                        id=uuid7(),
                        document_id=doc.id,
                        chunk_index=c.index,
                        content=c.content,
                        token_count=c.token_count,
                        allowed_groups=list(meta.allowed_groups),
                        metadata_={**c.metadata, "source": source},
                    )
                )
        return DocOutcome(meta.source_uri, "indexed", chunks=len(chunks), pii=pii, secrets=secrets)

    async def ingest_pack(self, root: Path) -> IngestReport:
        """Ingest a directory described by manifest.json (path, title, ACL, ... per file)."""
        started = time.perf_counter()
        manifest = json.loads(await asyncio.to_thread((root / "manifest.json").read_text))
        report = IngestReport()
        listed = set()
        for entry in manifest["documents"]:
            listed.add(entry["path"])
            data = await asyncio.to_thread((root / entry["path"]).read_bytes)
            meta = DocMeta(
                source_uri=entry["source_uri"],
                title=entry["title"],
                department=entry["department"],
                owner=entry["owner"],
                allowed_groups=list(entry["allowed_groups"]),
                sensitivity=entry.get("sensitivity", "INTERNAL"),
                version=str(entry.get("version", "1")),
                source=entry.get("source"),
            )
            report.outcomes.append(await self.ingest_bytes(meta, data, entry["path"]))
        files = await asyncio.to_thread(
            lambda: sorted(
                p
                for p in root.rglob("*")
                # dotfiles (.DS_Store, .gitkeep) are not documents
                if p.is_file() and p.name != "manifest.json" and not p.name.startswith(".")
            )
        )
        for path in files:
            rel = path.relative_to(root).as_posix()
            if rel not in listed:
                report.outcomes.append(
                    DocOutcome(f"docpack://{rel}", "rejected", detail="not in manifest (no ACL)")
                )
        report.seconds = time.perf_counter() - started
        return report

    async def chunk_db_documents(self) -> IngestReport:
        """Chunk documents that already live in rag.documents (the Phase 3 seed) and have no
        chunks yet. Their content was written by the seed loader, so we only chunk + embed."""
        started = time.perf_counter()
        report = IngestReport()
        async with self._sessions() as s:
            docs = (
                await s.scalars(
                    select(Document)
                    .where(
                        ~Document.quarantined,
                        ~select(DocumentChunk.id)
                        .where(DocumentChunk.document_id == Document.id)
                        .exists(),
                    )
                    .order_by(Document.id)
                )
            ).all()
        for doc in docs:
            hits = detect_injection(doc.content)
            async with self._sessions() as s, s.begin():
                d = await s.get(Document, doc.id, with_for_update=True)
                if d is None:
                    continue
                if hits:
                    d.quarantined, d.quarantine_reason = True, "injection:" + ",".join(hits)
                    report.outcomes.append(
                        DocOutcome(d.source_uri, "quarantined", detail=d.quarantine_reason)
                    )
                    continue
                chunks = chunk_sections(
                    d.title,
                    [Section("", d.content)],
                    target_tokens=self._s.chunk_target_tokens,
                    overlap_tokens=self._s.chunk_overlap_tokens,
                )
                for c in chunks:
                    s.add(
                        DocumentChunk(
                            id=uuid7(),
                            document_id=d.id,
                            chunk_index=c.index,
                            content=c.content,
                            token_count=c.token_count,
                            allowed_groups=list(d.allowed_groups),
                            metadata_={**c.metadata, "source": d.source},
                        )
                    )
                report.outcomes.append(DocOutcome(d.source_uri, "indexed", chunks=len(chunks)))
        report.seconds = time.perf_counter() - started
        return report

    # ------------------------------------------------------------------ embeddings
    async def embed_pending(
        self, *, limit: int | None = None, progress: Callable[[int, int], None] | None = None
    ) -> int:
        """Embed chunks with NULL embedding, batch by batch, committing each batch."""
        async with self._sessions() as s:
            total = (
                await s.scalar(
                    select(func.count())
                    .select_from(DocumentChunk)
                    .where(DocumentChunk.embedding.is_(None))
                )
                or 0
            )
        done = 0
        batch = self._s.embed_batch_size
        while limit is None or done < limit:
            async with self._sessions() as s:
                rows = (
                    await s.execute(
                        select(DocumentChunk.id, DocumentChunk.document_id, DocumentChunk.content)
                        .where(DocumentChunk.embedding.is_(None))
                        .order_by(DocumentChunk.document_id, DocumentChunk.chunk_index)
                        .limit(batch)
                    )
                ).all()
            if not rows:
                break
            vectors = await self._embedder.embed_documents([r.content for r in rows])
            model = self._embedder.model
            async with self._sessions() as s, s.begin():
                for r, v in zip(rows, vectors, strict=True):
                    await s.execute(
                        update(DocumentChunk)
                        .where(DocumentChunk.id == r.id, DocumentChunk.embedding.is_(None))
                        .values(embedding=v, embedding_model=model)
                    )
                await s.execute(
                    text(
                        "UPDATE rag.documents d SET indexed_at = now() WHERE d.id = ANY(:ids) "
                        "AND NOT EXISTS (SELECT 1 FROM rag.document_chunks c "
                        "WHERE c.document_id = d.id AND c.embedding IS NULL)"
                    ),
                    {"ids": list({r.document_id for r in rows})},
                )
            done += len(rows)
            if progress is not None:
                progress(done, total)
        return done

    async def reset_embeddings_for_other_models(self, current_model: str) -> int:
        """After switching embedding model: clear vectors from other models so they are re-embedded."""
        async with self._sessions() as s, s.begin():
            res = await s.execute(
                update(DocumentChunk)
                .where(
                    DocumentChunk.embedding_model.is_not(None),
                    DocumentChunk.embedding_model != current_model,
                )
                .values(embedding=None, embedding_model=None)
            )
            return int(res.rowcount or 0)  # type: ignore[attr-defined]

    async def stats(self) -> dict[str, Any]:
        async with self._sessions() as s:
            row = (
                await s.execute(
                    text(
                        "SELECT (SELECT count(*) FROM rag.documents) AS documents, "
                        "(SELECT count(*) FROM rag.documents WHERE quarantined) AS quarantined, "
                        "(SELECT count(*) FROM rag.document_chunks) AS chunks, "
                        "(SELECT count(*) FROM rag.document_chunks WHERE embedding IS NULL) AS pending, "
                        "(SELECT coalesce(json_object_agg(m, n), '{}') FROM (SELECT embedding_model m, count(*) n "
                        " FROM rag.document_chunks WHERE embedding_model IS NOT NULL GROUP BY 1) t) AS models"
                    )
                )
            ).one()
        return dict(row._mapping)


def new_retrieval_id() -> uuid.UUID:
    return uuid7()
