"""Phase 25c (ADR-037): eval result FILES -> the `eval` schema, so one dashboard can show them.

    make eval-import [FILES="data/eval/results/rag-....json ..."]   (default: every file there)

The files stay the system of record (committed, reviewed, each with its environment and git
commit); the schema is a COPY for reading. Rules:
- a known kind only, each with a declared mapping (rag, history, risk-scan, investigations);
  anything else is refused and named - never guessed;
- a run that measured missing data is refused: rag/history without `is_baseline` (fake
  embeddings), a rag mode with `valid: false`, an investigation run with invalid cases;
- never imported: `rag-sweep-*` (tuning, not a run), `calibration-*` (derived from a run),
  `agent-log-compare-*` (the file itself says "an anecdote, not an eval. Do not quote");
- rag files written before the per-mode `valid` check (Phase 7) are refused too: one of them
  (RERANK=1) is a known-invalid run whose "hybrid" was keyword-only;
- `summary` holds NUMBERS only (obs_reader may read it, migration 0028); text stays in the file;
- idempotent: the run id is the file's own run id (investigations, written by the runner) or a
  uuid5 of the file's SHA-256, inserted ON CONFLICT DO NOTHING. A changed file = a new run.
- `seed` = 0 when the file does not record one (eval_runs.seed is NOT NULL; 0 = "not recorded").
Why an importer and not each eval writing the schema: a service never writes another
service's schema (the rag service does not own `eval`).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

import psycopg
from psycopg.types.json import Jsonb

from aeoi_common.ids import uuid7
from aeoi_db.config import libpq_dsn

ROOT = Path(__file__).resolve().parents[1]
NEVER = ("rag-sweep-", "calibration-", "agent-log-compare-")
METRICS = ("top1", "in_top3", "confident_wrong", "decoy_above", "rule_out_ok", "passed",
           "validated_wrong", "proposal_correct", "proposal_wrong", "proposal_missed",
           "wrong_high")  # fmt: skip


class RefusedError(ValueError):
    """This file is not imported; the message says why."""


def _version(dataset_file: str) -> int:
    m = re.search(r"_v(\d+)\.jsonl$", dataset_file)
    if not m:
        raise RefusedError(f"no dataset version in {dataset_file!r}")
    return int(m.group(1))


def numbers_only(x: Any, where: str = "summary") -> None:
    """`summary` is readable by obs_reader (0028): numbers, lists and dicts of numbers only."""
    if isinstance(x, dict):
        for k, v in x.items():
            numbers_only(v, f"{where}.{k}")
    elif isinstance(x, list):
        for i, v in enumerate(x):
            numbers_only(v, f"{where}[{i}]")
    elif not isinstance(x, int | float):  # bool is an int; None and text are refused
        raise RefusedError(f"{where} = {x!r} is not a number")


def _ratio(text: str) -> dict[str, int]:
    """The runner's "11/11" -> {"k": 11, "n": 11}."""
    k, n = text.split("/")
    return {"k": int(k), "n": int(n)}


def map_rag(d: dict[str, Any]) -> dict[str, Any]:
    env = d["environment"]
    if not env.get("is_baseline"):
        raise RefusedError("not a baseline run (fake embeddings measure plumbing, not retrieval)")
    if any("valid" not in r for r in d["results"].values()):
        # e.g. rag-2026-10-06T043554 (RERANK=1): every rerank failed and "hybrid" was keyword
        raise RefusedError("written before the validity check (Phase 7): degraded modes unprovable")
    bad = [m for m, r in d["results"].items() if r["valid"] is not True]
    if bad:
        raise RefusedError(f"mode(s) {', '.join(bad)} marked valid: false")
    keep = ("n", "recall@1", "recall@5", "recall@5_ci95", "recall@10", "mrr@10", "mrr@10_ci95")
    summary = {
        mode: {
            **{k: r["overall"][k] for k in keep if k in r["overall"]},
            "leakage_violations": len(r["leakage"]["violations"]),
            "quarantine_violations": len(r["quarantine"]["violations"]),
            "degraded_queries": r["degraded_queries"],
        }
        for mode, r in d["results"].items()
    }
    return {
        "dataset": ("rag-retrieval", _version(env["dataset"])),
        "started_at": env["timestamp"],
        "git_sha": env.get("git_commit"),
        "config": {"environment": env, "settings": d.get("settings")},
        "summary": summary,
    }


def map_history(d: dict[str, Any]) -> dict[str, Any]:
    env = d["environment"]
    if not env.get("is_baseline"):
        raise RefusedError("not a baseline run (fake embeddings measure plumbing, not similarity)")
    summary = {}
    for mode, r in d["results"].items():
        s = r["summary"]
        summary[mode] = {
            "n": s["n"],
            **{m: s[m]["mean"] for m in ("hit@1", "hit@5", "mrr@10", "precision@5")},
            "hit@5_ci95": s["hit@5"]["ci95"],
            "leakage_violations": s["leakage_violations"],
        }
    return {
        "dataset": ("history-similarity", _version(env["dataset"])),
        "started_at": env["timestamp"],
        "git_sha": env.get("git_commit"),
        "config": {"environment": env},
        "summary": summary,
    }


def map_risk(d: dict[str, Any]) -> dict[str, Any]:
    keep = ("deploys", "bands", "findings_bands", "flagged_share", "flagged_by_code",
            "flagged_only_unchecked")  # fmt: skip
    return {
        "dataset": ("release-risk-selectivity", _version(d["labelled_cases"])),
        "started_at": d["generated_at"],
        "git_sha": None,
        "config": {"question": d["question"], "labelled_cases": d["labelled_cases"]},
        "summary": {
            **{k: d[k] for k in keep},
            "labelled_failures": len(d["labelled_failures"]),
        },
    }


def map_investigations(d: dict[str, Any]) -> dict[str, Any]:
    if d["summary"].get("invalid"):
        raise RefusedError(f"invalid cases {d['summary']['invalid']}: evidence was incomplete")
    s = d["summary"]
    ratios = ("top1", "in_top3", "confident_wrong", "validated_wrong", "undetectable_abstained",
              "decoy_above", "rule_out_ok", "passed", "wrong_high")  # fmt: skip
    summary = {
        "cases": s["cases"],
        **{k: _ratio(s[k]) for k in ratios if k in s},
        "proposals": s.get("proposals") or {},
        "cost_usd": s.get("cost_usd") or 0,
        # a rescore is the same paid run scored again: 1 marks it (two rows, one run)
        "rescored": 1 if d.get("rescored_from") else 0,
    }
    return {
        "dataset": (d["config"]["dataset"], int(d["config"]["version"])),
        "started_at": d["started_at"],
        "git_sha": None,
        "config": {**d["config"], "rescored_from": d.get("rescored_from")},
        "summary": summary,
        "run_id": None if d.get("rescored_from") else d["run_id"],
        "cases": d["cases"],
    }


def plan(path: Path) -> dict[str, Any]:
    """What importing `path` would write (pure). Raises RefusedError with the reason."""
    name = path.name
    if name.startswith(NEVER):
        raise RefusedError(
            "never imported (tuning, derived or anecdote - see the module docstring)"
        )
    d = json.loads(path.read_text())
    if name.startswith("rag-"):
        out = map_rag(d)
    elif name.startswith("history-"):
        out = map_history(d)
    elif name.startswith("risk-scan-"):
        out = map_risk(d)
    elif name.startswith("investigations-"):
        out = map_investigations(d)
    else:
        raise RefusedError("unknown kind of result file")
    numbers_only(out["summary"])
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    out["sha256"] = sha
    own = out.get("run_id")
    out["id"] = UUID(own) if own else uuid5(NAMESPACE_URL, f"aeoi:eval:import:{sha}")
    return out


def write(conn: psycopg.Connection[Any], p: dict[str, Any], source: str) -> str:
    name, version = p["dataset"]
    ds = conn.execute(
        "INSERT INTO eval.datasets (id, name, version, description) VALUES (%s, %s, %s, %s)"
        " ON CONFLICT (name, version) DO UPDATE SET name = EXCLUDED.name RETURNING id",
        (uuid5(NAMESPACE_URL, f"aeoi:eval:dataset:{name}:{version}"), name, version,
         f"imported from result files ({name} v{version})"),
    ).fetchone()  # fmt: skip
    if ds is None:  # RETURNING always yields a row; keeps mypy and ruff (S101) honest
        raise RuntimeError("eval.datasets upsert returned no row")
    started = datetime.fromisoformat(p["started_at"])
    row = conn.execute(
        "INSERT INTO eval.eval_runs (id, dataset_id, config, config_sha256, git_sha, seed,"
        " status, summary, started_at, finished_at)"
        " VALUES (%s, %s, %s, %s, %s, 0, 'SUCCEEDED', %s, %s, %s) ON CONFLICT (id) DO NOTHING"
        " RETURNING id",
        (p["id"], ds[0], Jsonb({**p["config"], "imported_from": source}), p["sha256"],
         (p["git_sha"] or None) and str(p["git_sha"])[:40], Jsonb(p["summary"]), started,
         started),
    ).fetchone()  # fmt: skip
    if row is None:
        return "already imported"
    cases = 0
    for c in p.get("cases", []):
        case = conn.execute(
            "SELECT id FROM eval.eval_cases WHERE dataset_id = %s AND case_key = %s",
            (ds[0], c["case"]),
        ).fetchone()
        if case is None:
            continue  # the pack was not seeded here: the run summary still lands
        for m in METRICS:
            v = c["score"].get(m)
            if v is not None:
                conn.execute(
                    "INSERT INTO eval.evaluations (id, run_id, case_id, metric, value)"
                    " VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                    (uuid7(), p["id"], case[0], m, float(v)),
                )
        cases += 1
    return f"imported ({cases} cases scored)" if p.get("cases") else "imported"


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="*", type=Path)
    ap.add_argument("--dir", type=Path, default=ROOT / "data/eval/results")
    args = ap.parse_args(argv)
    files = args.files or sorted(args.dir.glob("*.json"))
    imported = 0
    with psycopg.connect(libpq_dsn()) as conn:
        for f in files:
            try:
                p = plan(f)
            except RefusedError as e:
                print(f"  skip  {f.name}: {e}")
                continue
            except (KeyError, ValueError) as e:
                print(f"  skip  {f.name}: unexpected shape ({type(e).__name__}: {e})")
                continue
            what = write(conn, p, f.name)
            imported += what.startswith("imported")
            print(f"  {'ok' if what.startswith('imported') else 'same':<5} {f.name}: {what}")
        conn.commit()
    print(f"{imported} run(s) imported, {len(files)} file(s) read")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
