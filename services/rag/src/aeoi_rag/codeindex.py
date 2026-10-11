"""Phase 14 (ADR-024): index repositories at HEAD (tree-sitter) and answer "who uses X".

Index: every source file of every repository under `root/<org>/<repo>/` that has an explicit
ACL file (`.aeoi-repo.json`, `allowed_groups`). A repository without one is NOT indexed
(services/rag/CLAUDE.md: never index a file without an explicit ACL). Re-running is cheap:
unchanged files (same sha256) are skipped; files gone from disk are deleted from the index.

References: by SIMPLE name (aeoi_codeparse), filtered by the caller's groups IN the SQL.
"ShipmentResponse.address" means: uses of address / getAddress / Address in services that
also use the type ShipmentResponse somewhere - a heuristic, not type resolution ([P]).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aeoi_codeparse as cp
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aeoi_rag.history import OPEN_GROUP

ACL_FILE = ".aeoi-repo.json"
MAX_FILE_BYTES = 512_000


@dataclass
class IndexReport:
    repositories: int = 0
    files_indexed: int = 0
    files_unchanged: int = 0
    files_deleted: int = 0
    symbols: int = 0
    refs: int = 0
    parse_errors: int = 0
    skipped_repos: list[str] = field(default_factory=list)  # no ACL file


def _repos(root: Path) -> list[Path]:
    return sorted(p for p in root.glob("*/*") if p.is_dir())


def _service_key(path: str) -> str | None:
    parts = path.split("/")
    return parts[1] if len(parts) > 2 and parts[0] == "services" else None


async def index_repos(sessions: async_sessionmaker[AsyncSession], root: Path) -> IndexReport:
    rep = IndexReport()
    for repo_dir in _repos(root):
        repo = f"{repo_dir.parent.name}/{repo_dir.name}"
        acl_path = repo_dir / ACL_FILE
        if not acl_path.is_file():
            rep.skipped_repos.append(repo)
            continue
        acl = json.loads(acl_path.read_text())
        groups = sorted({str(g) for g in acl.get("allowed_groups") or []})
        if not groups:
            rep.skipped_repos.append(repo)
            continue
        rep.repositories += 1
        seen: set[str] = set()
        for f in sorted(repo_dir.rglob("*")):
            rel = f.relative_to(repo_dir).as_posix()
            lang = cp.language_of(rel)
            if lang is None or not f.is_file() or f.stat().st_size > MAX_FILE_BYTES:
                continue
            seen.add(rel)
            source = f.read_text(encoding="utf-8", errors="replace")
            sha = hashlib.sha256(source.encode()).hexdigest()
            async with sessions() as s, s.begin():
                row = (
                    await s.execute(
                        text("SELECT id, content_sha256, allowed_groups FROM rag.code_files "
                             "WHERE repository = :r AND path = :p"),
                        {"r": repo, "p": rel},
                    )
                ).first()  # fmt: skip
                if (
                    row is not None
                    and row.content_sha256 == sha
                    and sorted(row.allowed_groups) == groups
                ):
                    rep.files_unchanged += 1
                    continue
                parsed = cp.parse(lang, source)
                if row is not None:
                    await s.execute(
                        text("DELETE FROM rag.code_files WHERE id = :id"), {"id": row.id}
                    )
                file_id = (
                    await s.execute(
                        text(
                            "INSERT INTO rag.code_files (id, repository, path, service_key, language,"
                            " content_sha256, allowed_groups) VALUES (gen_random_uuid(), :r, :p, :svc,"
                            " :lang, :sha, CAST(:g AS varchar[])) RETURNING id"
                        ),
                        {"r": repo, "p": rel, "svc": _service_key(rel), "lang": lang, "sha": sha,
                         "g": groups},
                    )
                ).scalar_one()  # fmt: skip
                if parsed.symbols:
                    await s.execute(
                        text("INSERT INTO rag.code_symbols (id, file_id, kind, name, container, "
                             "start_line, end_line) VALUES (gen_random_uuid(), :f, :k, :n, :c, :a, :b)"),
                        [{"f": file_id, "k": x.kind, "n": x.name[:200], "c": (x.container or "")[:200] or None,
                          "a": x.start_line, "b": x.end_line} for x in parsed.symbols],
                    )  # fmt: skip
                if parsed.refs:
                    await s.execute(
                        text("INSERT INTO rag.code_refs (id, file_id, kind, name, line, enclosing) "
                             "VALUES (gen_random_uuid(), :f, :k, :n, :l, :e)"),
                        [{"f": file_id, "k": x.kind, "n": x.name[:200], "l": x.line,
                          "e": (x.enclosing or "")[:200] or None} for x in parsed.refs],
                    )  # fmt: skip
                rep.files_indexed += 1
                rep.symbols += len(parsed.symbols)
                rep.refs += len(parsed.refs)
                rep.parse_errors += parsed.errors
        async with sessions() as s, s.begin():
            gone = (
                await s.execute(
                    text("DELETE FROM rag.code_files WHERE repository = :r AND NOT (path = ANY(:keep)) "
                         "RETURNING id"),
                    {"r": repo, "keep": sorted(seen)},
                )
            ).all()  # fmt: skip
            rep.files_deleted += len(gone)
    return rep


_REFS_SQL = """
SELECT r.kind, r.name, r.line, r.enclosing, f.repository, f.path, f.service_key, f.allowed_groups
FROM rag.code_refs r
JOIN rag.code_files f ON f.id = r.file_id
WHERE r.name = ANY(:names)
  AND f.allowed_groups && CAST(:groups AS varchar[])
  AND (CAST(:repo AS text) IS NULL OR f.repository <> CAST(:repo AS text))
  AND (CAST(:type AS text) IS NULL OR EXISTS (
        SELECT 1 FROM rag.code_refs t JOIN rag.code_files tf ON tf.id = t.file_id
        WHERE t.name = CAST(:type AS text) AND tf.service_key = f.service_key
          AND tf.repository = f.repository
          AND tf.allowed_groups && CAST(:groups AS varchar[])))
ORDER BY f.repository, f.path, r.line
LIMIT :limit
"""


def split_symbol(symbol: str) -> tuple[str | None, str]:
    """'ShipmentResponse.address' -> ('ShipmentResponse', 'address'); 'findOpenOrders' -> (None, ...)."""
    if "." in symbol:
        owner, member = symbol.rsplit(".", 1)
        return owner.rsplit(".", 1)[-1] or None, member
    return None, symbol


async def references(
    sessions: async_sessionmaker[AsyncSession],
    symbol: str,
    groups: Sequence[str],
    *,
    exclude_repository: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """`groups` MUST come from a verified token. No groups -> nothing."""
    if not groups:
        return []
    owner, member = split_symbol(symbol)
    names = sorted(cp.accessor_names(member)) if owner else [member]
    async with sessions() as s:
        rows = (
            await s.execute(
                text(_REFS_SQL),
                {"names": names, "groups": sorted(set(groups)), "repo": exclude_repository,
                 "type": owner, "limit": limit},
            )
        ).mappings().all()  # fmt: skip
    caller = set(groups)
    out = []
    for r in rows:
        if not caller.intersection(r["allowed_groups"]):  # defence in depth, as in search.py
            raise RuntimeError("permission filter violated - refusing to return results")
        row = {k: v for k, v in r.items() if k != "allowed_groups"}
        # stored on an incident (visible per ROLE) only when every engineer may read the file
        row["open_to_engineering"] = OPEN_GROUP in r["allowed_groups"]
        out.append(row)
    return out
