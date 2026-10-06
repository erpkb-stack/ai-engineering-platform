"""Parse md / txt / html / json / code / pdf into titled sections of plain text.

Each parser keeps STRUCTURE (headings, symbols, pages) because chunk headers built from it
improve both keyword and vector retrieval. Text a reader can't see (HTML display:none,
<script>) is not indexed, but it IS returned as `hidden_text` so the injection detector
can scan it: hidden text is the classic indirect prompt-injection carrier.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import PurePosixPath
from typing import Any

SOURCES = ("markdown", "pdf", "txt", "html", "json", "code")
CODE_EXTENSIONS = {
    ".py": "python",
    ".java": "java",
    ".go": "go",
    ".ts": "typescript",
    ".js": "javascript",
    ".sql": "sql",
    ".kt": "kotlin",
}


class ParseError(ValueError):
    """The file cannot be parsed into text (corrupt, encrypted, unsupported)."""


@dataclass
class Section:
    heading: str
    text: str
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class Parsed:
    title: str | None
    sections: list[Section]
    hidden_text: str = ""

    @property
    def full_text(self) -> str:
        return "\n\n".join(s.text for s in self.sections if s.text.strip())


def detect_source(filename: str) -> str:
    ext = PurePosixPath(filename).suffix.lower()
    if ext in (".md", ".markdown"):
        return "markdown"
    if ext in (".html", ".htm"):
        return "html"
    if ext == ".json":
        return "json"
    if ext == ".pdf":
        return "pdf"
    if ext == ".txt":
        return "txt"
    if ext in CODE_EXTENSIONS:
        return "code"
    raise ParseError(f"unsupported file type '{ext}'")


def parse(data: bytes, source: str, filename: str, *, max_pdf_pages: int = 300) -> Parsed:
    if source == "pdf":
        return _pdf(data, max_pdf_pages)
    text = _decode(data)
    if source == "markdown":
        return _markdown(text)
    if source == "txt":
        return Parsed(None, [Section("", text.strip())])
    if source == "html":
        return _html(text)
    if source == "json":
        return _json(text)
    if source == "code":
        return _code(text, CODE_EXTENSIONS.get(PurePosixPath(filename).suffix.lower(), "text"))
    raise ParseError(f"unsupported source '{source}'")


def _decode(data: bytes) -> str:
    if b"\x00" in data[:4096]:
        raise ParseError("binary content in a text format")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1")


# --------------------------------------------------------------------------- markdown
_MD_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")


def _markdown(text: str) -> Parsed:
    title: str | None = None
    sections: list[Section] = []
    heading, in_fence = "", False
    buf: list[str] = []
    for line in text.splitlines():
        if line.strip().startswith("```"):
            in_fence = not in_fence
        m = None if in_fence else _MD_HEADING.match(line)
        if m:
            if buf and "".join(buf).strip():
                sections.append(Section(heading, "\n".join(buf).strip()))
            buf = []
            level, heading = len(m.group(1)), m.group(2).strip()
            if level == 1 and title is None:
                title = heading
            continue
        buf.append(line)
    if buf and "".join(buf).strip():
        sections.append(Section(heading, "\n".join(buf).strip()))
    return Parsed(title, sections)


# --------------------------------------------------------------------------- html
_HIDDEN_STYLE = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden", re.I)
_SKIP_TAGS = {"script", "style", "noscript", "template", "nav", "footer", "head"}
_HEADINGS = {"h1", "h2", "h3"}
_BLOCK = {"p", "li", "div", "tr", "br", "section", "article", "h1", "h2", "h3", "h4", "pre"}


class _HtmlText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title: str | None = None
        self.sections: list[Section] = []
        self.hidden: list[str] = []
        self._heading = ""
        self._buf: list[str] = []
        self._stack: list[tuple[str, bool, bool]] = []  # (tag, skipped, hidden)
        self._in_title = False
        self._head_buf: list[str] | None = None

    def _skipping(self) -> tuple[bool, bool]:
        skipped = any(s for _, s, _ in self._stack)
        hidden = any(h for _, _, h in self._stack)
        return skipped, hidden

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("br", "img", "hr", "meta", "link", "input"):
            if tag == "br":
                self._buf.append("\n")
            return
        style = dict(attrs).get("style") or ""
        hidden = bool(_HIDDEN_STYLE.search(style)) or any(k == "hidden" for k, _ in attrs)
        self._stack.append((tag, tag in _SKIP_TAGS, hidden))
        if tag == "title":
            self._in_title = True
        if tag in _HEADINGS and not any(self._skipping()):
            self._flush()
            self._head_buf = []
        elif tag in _BLOCK:
            self._buf.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        if tag in _HEADINGS and self._head_buf is not None:
            self._heading = " ".join("".join(self._head_buf).split())
            if tag == "h1" and not self.title:
                self.title = self._heading
            self._head_buf = None
        # pop to the matching tag (tolerates unclosed children)
        for i in range(len(self._stack) - 1, -1, -1):
            if self._stack[i][0] == tag:
                del self._stack[i:]
                break
        if tag in _BLOCK:
            self._buf.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title = self.title or data.strip() or None
            return
        skipped, hidden = self._skipping()
        if hidden:
            self.hidden.append(data)
            return
        if skipped:
            if self._stack and self._stack[-1][0] == "script":
                self.hidden.append(data)
            return
        if self._head_buf is not None:
            self._head_buf.append(data)
        else:
            self._buf.append(data)

    def _flush(self) -> None:
        text = re.sub(r"[ \t]+", " ", "".join(self._buf))
        text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
        if text:
            self.sections.append(Section(self._heading, text))
        self._buf = []

    def close(self) -> None:
        super().close()
        self._flush()


def _html(text: str) -> Parsed:
    p = _HtmlText()
    p.feed(text)
    p.close()
    return Parsed(
        p.title, p.sections, hidden_text="\n".join(h.strip() for h in p.hidden if h.strip())
    )


# --------------------------------------------------------------------------- json
def _flatten(value: Any, prefix: str, out: list[str], depth: int = 0) -> None:
    if depth > 20:
        out.append(f"{prefix}: [nested too deep]")
        return
    if isinstance(value, dict):
        for k, v in value.items():
            _flatten(v, f"{prefix}.{k}" if prefix else str(k), out, depth + 1)
    elif isinstance(value, list):
        for i, v in enumerate(value):
            _flatten(v, f"{prefix}[{i}]", out, depth + 1)
    else:
        out.append(f"{prefix}: {json.dumps(value) if not isinstance(value, str) else value}")


def _json(text: str) -> Parsed:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ParseError(f"invalid JSON: {exc.msg} at line {exc.lineno}") from exc
    lines: list[str] = []
    _flatten(data, "", lines)
    # "a.b.maxPoolSize: 40" - the key path makes config values findable by name
    return Parsed(None, [Section("", "\n".join(lines))])


# --------------------------------------------------------------------------- code
_SYMBOL = {
    "python": re.compile(r"^(?:async\s+def|def|class)\s+([A-Za-z_]\w*)"),
    "java": re.compile(
        r"^\s{0,4}(?:public|private|protected|final|abstract|static|\s)*\s*(?:class|interface|record|enum)\s+([A-Za-z_]\w*)"
    ),
    "kotlin": re.compile(r"^(?:class|object|interface|fun)\s+([A-Za-z_]\w*)"),
    "go": re.compile(r"^func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)"),
    "typescript": re.compile(
        r"^(?:export\s+)?(?:async\s+)?(?:function|class|interface)\s+([A-Za-z_]\w*)"
    ),
    "javascript": re.compile(r"^(?:export\s+)?(?:async\s+)?(?:function|class)\s+([A-Za-z_]\w*)"),
}


def _code(text: str, language: str) -> Parsed:
    """Split at top-level symbols (regex, not a real parser: tree-sitter comes in Phase 14).

    Leading comments/docstrings before the first symbol become a 'module' section, so a
    file-level docstring stays searchable.
    """
    pattern = _SYMBOL.get(language)
    sections: list[Section] = []
    name = "module"
    buf: list[str] = []
    for line in text.splitlines():
        m = pattern.match(line) if pattern else None
        if m and buf and "".join(buf).strip():
            sections.append(
                Section(name, "\n".join(buf).rstrip(), {"language": language, "symbol": name})
            )
            buf = []
        if m:
            name = m.group(1)
        buf.append(line)
    if buf and "".join(buf).strip():
        sections.append(
            Section(name, "\n".join(buf).rstrip(), {"language": language, "symbol": name})
        )
    return Parsed(None, sections)


# --------------------------------------------------------------------------- pdf
def _pdf(data: bytes, max_pages: int) -> Parsed:
    import pypdfium2 as pdfium  # type: ignore[import-untyped]  # binary wheel; imported lazily so other formats never need it

    try:
        doc = pdfium.PdfDocument(data)
    except pdfium.PdfiumError as exc:
        raise ParseError(f"cannot open PDF: {exc}") from exc
    try:
        if len(doc) > max_pages:
            raise ParseError(f"PDF has {len(doc)} pages; limit is {max_pages}")
        sections = []
        for i in range(len(doc)):
            page = doc[i]
            text = page.get_textpage().get_text_range().replace("\r\n", "\n").replace("\r", "\n")
            if text.strip():
                sections.append(Section(f"page {i + 1}", text.strip(), {"page": i + 1}))
        title = (doc.get_metadata_dict().get("Title") or "").strip() or None
    finally:
        doc.close()
    if not sections:
        raise ParseError("PDF has no extractable text (scanned image? OCR is out of scope)")
    return Parsed(title, sections)
