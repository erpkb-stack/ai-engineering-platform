"""Phase 13: the similar-incident eval's metric maths and dataset hygiene (pure, no IO)."""

from __future__ import annotations

import re

from aeoi_db.config import find_repo_root
from aeoi_rag.history_eval import load, score, summarise, text_for


def test_score_first_hit_and_precision() -> None:
    s = score(["a", "x", "a", "a", "y", "a"], "a")
    assert s == {"hit@1": 1.0, "hit@5": 1.0, "mrr@10": 1.0, "precision@5": 0.6}
    s = score(["x", "y", "a"], "a")
    assert s["hit@1"] == 0.0 and s["hit@5"] == 1.0 and abs(s["mrr@10"] - 1 / 3) < 1e-9
    assert score(["x"] * 6 + ["a"], "a")["hit@5"] == 0.0
    assert score([], "a") == {"hit@1": 0.0, "hit@5": 0.0, "mrr@10": 0.0, "precision@5": 0.0}


def test_summary_counts_leaks_and_empty_results() -> None:
    per = [
        {**score(["a"], "a"), "leaks": 0, "returned": 1},
        {**score([], "a"), "leaks": 2, "returned": 0},
    ]
    s = summarise(per)
    assert s["n"] == 2 and s["hit@1"]["mean"] == 0.5
    assert s["leakage_violations"] == 2 and s["empty"] == 1


def test_dataset_queries_do_not_copy_the_corpus() -> None:
    """The point of the eval: a query must not contain the category name, the error code
    (only the +code variant adds it) or a 4-word run from its category's template."""
    from aeoi_synth import vocab

    qs = load(find_repo_root() / "data" / "eval" / "history_queries_v1.jsonl")
    assert len(qs) == 36 and {q.category for q in qs} == set(vocab.ROOT_CAUSES)
    for q in qs:
        title, symptom, code, cause, fix = vocab.ROOT_CAUSES[q.category]
        text = q.query.lower()
        assert q.category.replace("_", " ") not in text, q.id
        assert code.lower() not in text and q.error_code == code, q.id
        corpus = " ".join((title, symptom, cause, fix)).lower()
        words = re.findall(r"[a-z0-9]+", corpus)
        grams = {" ".join(words[i : i + 4]) for i in range(len(words) - 3)}
        qw = re.findall(r"[a-z0-9]+", text)
        copied = {" ".join(qw[i : i + 4]) for i in range(len(qw) - 3)} & grams
        assert not copied, (q.id, copied)
        assert text_for(q, "symptom+code").endswith(code)
