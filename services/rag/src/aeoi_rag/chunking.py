"""Structure-aware chunking with contextual headers.

- Never crosses a section boundary (a chunk about "Mitigation" must not start in "Symptoms").
- Packs paragraphs up to ~target tokens; splits oversized paragraphs on lines, then words.
- Overlap: the tail of the previous chunk (same section) is repeated, so a fact split across a
  boundary still appears whole in one chunk.
- Every chunk starts with "Title > Heading": the vector and the keyword index both see what
  the chunk is ABOUT, not only its words (a cheap version of "contextual retrieval", without
  an LLM call per chunk). Whether it helps HERE is a hypothesis until an ablation measures it.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

from aeoi_rag.parsing import Section

CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    return max(1, math.ceil(len(text) / CHARS_PER_TOKEN))


@dataclass
class Chunk:
    index: int
    content: str  # header + body: what gets embedded and keyword-indexed
    token_count: int
    metadata: dict[str, Any] = field(default_factory=dict)


def _pieces(text: str, max_chars: int) -> list[str]:
    """Paragraphs; oversized ones split on lines, then on words."""
    out: list[str] = []
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        if len(para) <= max_chars:
            out.append(para)
            continue
        line_buf = ""
        for line in para.splitlines():
            if len(line) > max_chars:  # one enormous line (minified JSON, logs)
                if line_buf:
                    out.append(line_buf)
                    line_buf = ""
                cur = ""
                for w in line.split(" "):
                    # a single "word" longer than a chunk is cut into slices - never dropped
                    parts = [w[i : i + max_chars] for i in range(0, len(w), max_chars)] or [""]
                    for part in parts:
                        if cur and len(cur) + len(part) + 1 > max_chars:
                            out.append(cur)
                            cur = ""
                        cur = f"{cur} {part}" if cur else part
                if cur:
                    out.append(cur)
                continue
            if len(line_buf) + len(line) + 1 > max_chars and line_buf:
                out.append(line_buf)
                line_buf = ""
            line_buf = f"{line_buf}\n{line}" if line_buf else line
        if line_buf:
            out.append(line_buf)
    return out


def chunk_sections(
    title: str, sections: list[Section], *, target_tokens: int = 350, overlap_tokens: int = 50
) -> list[Chunk]:
    if target_tokens <= overlap_tokens:
        raise ValueError("target_tokens must be larger than overlap_tokens")
    max_chars = target_tokens * CHARS_PER_TOKEN
    overlap_chars = overlap_tokens * CHARS_PER_TOKEN
    chunks: list[Chunk] = []

    def emit(heading: str, body: str, meta: dict[str, Any]) -> None:
        header = f"{title} > {heading}" if heading else title
        content = f"{header}\n\n{body}".strip()
        chunks.append(
            Chunk(len(chunks), content, estimate_tokens(content), {**meta, "heading": heading})
        )

    for section in sections:
        # pieces leave room for the overlap tail, so tail + piece never exceeds the target
        pieces = _pieces(section.text, max_chars - overlap_chars)
        buf: list[str] = []
        size = 0
        for piece in pieces:
            if buf and size + len(piece) + 2 > max_chars:
                emit(section.heading, "\n\n".join(buf), section.meta)
                tail = "\n\n".join(buf)[-overlap_chars:] if overlap_chars else ""
                # start the overlap at a word boundary
                tail = tail[tail.find(" ") + 1 :] if " " in tail else tail
                buf, size = ([tail] if tail else []), len(tail)
            buf.append(piece)
            size += len(piece) + 2
        if buf:
            emit(section.heading, "\n\n".join(buf), section.meta)
    return chunks
