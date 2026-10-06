"""PII scrubbing before (a) embedding, (b) sending to a hosted LLM, (c) writing traces.

Pattern-based, deliberately conservative. It catches the formats that appear in operational
documents (emails, phone numbers, card numbers, US SSNs). It does NOT catch names or free-text
addresses - that needs an NER model (Phase 26). Say so when asked; don't oversell it.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
# +1 (512) 555-0199 / 512-555-0199 / +44 20 7946 0958
_PHONE = re.compile(
    r"(?<![\w-])(?:\+\d{1,3}[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}(?![\w-])"  # NANP
    r"|(?<![\w-])\+\d{1,3}[\s.-]\d{2,4}[\s.-]\d{3,4}[\s.-]\d{3,4}(?![\w-])"  # international, needs "+"
)
_CARD = re.compile(r"\b(?:\d[ -]?){13,19}\b")
_SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")


def _luhn_ok(digits: str) -> bool:
    total, parity = 0, len(digits) % 2
    for i, ch in enumerate(digits):
        d = int(ch)
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


@dataclass
class ScrubResult:
    text: str
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(self.counts.values())


def scrub_pii(text: str) -> ScrubResult:
    counts: dict[str, int] = {}

    def sub(
        pattern: re.Pattern[str], label: str, s: str, check: Callable[[str], bool] | None = None
    ) -> str:
        def repl(m: re.Match[str]) -> str:
            raw = m.group(0)
            if check is not None and not check(raw):
                return raw
            counts[label] = counts.get(label, 0) + 1
            return f"[{label}]"

        return pattern.sub(repl, s)

    out = sub(_EMAIL, "EMAIL", text)
    out = sub(_CARD, "CARD", out, lambda r: _luhn_ok(re.sub(r"\D", "", r)))
    out = sub(_SSN, "SSN", out)
    out = sub(_PHONE, "PHONE", out, lambda r: len(re.sub(r"\D", "", r)) >= 10)
    return ScrubResult(out, counts)
