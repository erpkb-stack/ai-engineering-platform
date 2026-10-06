from __future__ import annotations

import hashlib
import json
from pathlib import Path

from aeoi_db.config import find_repo_root
from aeoi_synth.docpack import build_pack, write_pack


def test_pack_is_deterministic(tmp_path: Path) -> None:
    a = write_pack(tmp_path / "a" / "sample-documents")
    b = write_pack(tmp_path / "b" / "sample-documents")
    assert a == b


def test_committed_pack_matches_generator(tmp_path: Path) -> None:
    """The committed corpus is what the eval numbers refer to: it must equal the generator output."""
    write_pack(tmp_path / "sample-documents")
    committed = find_repo_root() / "data" / "sample-documents"
    fresh = json.loads((tmp_path / "sample-documents" / "manifest.json").read_text())
    for e in fresh["documents"]:
        data = (committed / e["path"]).read_bytes()
        assert hashlib.sha256(data).hexdigest() == e["sha256"], e["path"]
    q_fresh = (tmp_path / "eval" / "rag_queries_v1.jsonl").read_text()
    assert (find_repo_root() / "data" / "eval" / "rag_queries_v1.jsonl").read_text() == q_fresh


def test_every_query_target_exists_and_acl_is_explicit() -> None:
    docs, queries = build_pack()
    uris = {d.source_uri for d in docs}
    assert all(d.allowed_groups for d in docs)
    for q in queries:
        assert set(q.relevant) <= uris and set(q.forbidden) <= uris
        assert q.kind in {"keyword", "paraphrase", "leakage", "adversarial"}
    kinds = [q.kind for q in queries]
    assert kinds.count("leakage") == 4 and kinds.count("adversarial") == 4
