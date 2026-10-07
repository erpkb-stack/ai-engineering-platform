"""Versioned prompts: services/agents/prompts/<agent>/v<N>.md with a small front matter.

The file in git is the source of truth. The sha256 goes into every trace, so "which exact
prompt produced this finding?" has an answer even if someone edits a file without bumping
the version (the sha changes; a test also fails if v1's sha changes - see test_prompts).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"


@dataclass(frozen=True)
class Prompt:
    prompt_id: str
    version: int
    route: str
    text: str
    sha256: str
    path: str


def load_prompt(agent: str, version: int, root: Path = PROMPTS_DIR) -> Prompt:
    path = root / agent / f"v{version}.md"
    raw = path.read_text(encoding="utf-8")
    if not raw.startswith("---\n"):
        raise ValueError(f"{path}: missing front matter")
    head, _, body = raw[4:].partition("\n---\n")
    meta = dict(line.split(":", 1) for line in head.splitlines() if ":" in line)
    meta = {k.strip(): v.strip() for k, v in meta.items()}
    if int(meta["version"]) != version:
        raise ValueError(f"{path}: front matter version {meta['version']} != file v{version}")
    return Prompt(
        prompt_id=meta["prompt_id"],
        version=version,
        route=meta.get("route", "fast"),
        text=body.strip(),
        sha256=hashlib.sha256(raw.encode()).hexdigest(),
        path=str(path.relative_to(root.parent)),
    )
