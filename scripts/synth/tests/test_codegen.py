"""Phase 14 (ADR-024): the synthetic repositories are real, consistent and deterministic."""

from __future__ import annotations

from functools import cache
from pathlib import Path

import aeoi_codeparse as cp

from aeoi_synth.codegen import IMPACT_MESSAGE, REPO_ACL_FILE, write_repos
from aeoi_synth.generate import Dataset, Scale, generate


@cache
def ds() -> Dataset:
    return generate(42, Scale.small())


def test_every_commit_edits_a_real_file_and_its_diff_applies_in_order() -> None:
    d = ds()
    diffs = {x["sha"]: x for x in d.tables["devdata.commit_diffs"]}
    head = {p: c for files in d.code.repos.values() for p, c in files.items()}
    last: dict[str, str] = {}
    for c in sorted(d.tables["devdata.commits"], key=lambda r: (r["committed_at"], r["sha"])):
        x = diffs[c["sha"]]
        assert c["files_changed"] == [x["path"]] and x["path"] in head
        assert c["additions"] == x["additions"] and x["patch"].startswith(f"--- a/{x['path']}")
        last[x["path"]] = x["content_after"]
    # the last change of every file IS the file at HEAD: diffs and repositories agree
    assert all(head[p] == content for p, content in last.items())


def test_pull_requests_list_the_files_of_their_commit() -> None:
    d = ds()
    commits = {c["sha"]: c for c in d.tables["devdata.commits"]}
    for pr in d.tables["devdata.pull_requests"]:
        assert pr["changed_files"] == commits[pr["merge_commit_sha"]]["files_changed"]


def test_generated_code_parses_without_errors_in_all_three_languages() -> None:
    langs = set()
    for files in ds().code.repos.values():
        for path, text in files.items():
            lang = cp.language_of(path)
            assert lang is not None, path
            langs.add(lang)
            assert cp.parse(lang, text).errors == 0, path
    assert langs == {"java", "python", "go"}


def test_rules_find_exactly_the_planted_changes() -> None:
    """Code-only rules (ADR-024) over EVERY commit: the planted N+1 and the planted field
    rename are found, nothing else is a loop query or a removed field."""
    d = ds()
    msgs = {c["sha"]: c["message"] for c in d.tables["devdata.commits"]}
    found: dict[str, list[str]] = {}
    for x in d.tables["devdata.commit_diffs"]:
        lang = cp.language_of(x["path"])
        assert lang is not None
        for f in cp.analyze_diff(lang, x["path"], x["content_after"], x["patch"]):
            found.setdefault(f.rule, []).append(msgs[x["sha"]])
    assert found["query_in_loop"] == ["Batch order lookup: query open orders per cart item (#812)"]
    assert found["field_removed"] == [IMPACT_MESSAGE]
    assert set(found["limit_changed"]) <= {"Tune timeouts", "Improve retries"}
    assert "remote_call_in_loop" not in found


def test_codepack_is_deterministic_and_every_repo_has_an_acl(tmp_path: Path) -> None:
    a = write_repos(ds().code, tmp_path / "a")
    b = write_repos(generate(42, Scale.small()).code, tmp_path / "b")
    assert a == b and a["repositories"] >= 30
    for repo in ds().code.repos:
        assert (tmp_path / "a" / repo / REPO_ACL_FILE).is_file()
