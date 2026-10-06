"""Heuristic prompt-injection detector for INGESTED content (documents, tool outputs).

This is a tripwire, not a guarantee. Retrieved text is always wrapped as untrusted data
(untrusted.py) regardless of this check. Here we only decide whether a document is so
obviously hostile that it should be quarantined and never retrieved at all.
False positives are cheap (a human releases the document); false negatives are expected,
which is why the wrapping and tool-level authorization exist (defence in depth).
"""

from __future__ import annotations

import re

_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # Each pattern targets text ADDRESSED TO A MODEL. Ordinary runbook language ("ignore the
    # warning", "the proxy acts as a gateway", "run the replay tool", "rate limit first")
    # must not match - tests/... check negatives on the whole seeded corpus.
    (
        "override-instructions",
        re.compile(
            r"\b(ignore|disregard|forget)\b[^.\n]{0,40}\b(previous|prior|above|all|your|earlier)\b"
            r"[^.\n]{0,20}\b(instructions?|rules|prompts?|guidelines)\b",
            re.I,
        ),
    ),
    (
        "role-hijack",
        re.compile(
            r"\b(you are now|from now on,? you|act as (an?|the) (ai|assistant|system|admin\w*))\b",
            re.I,
        ),
    ),
    (
        "fake-role-tag",
        re.compile(
            r"(^|\n|>)\s*(system|assistant)\s*:\s*(ignore|disregard|forget|you|reveal|send|call|approve)",
            re.I,
        ),
    ),
    (
        "reveal-prompt",
        re.compile(r"\b(reveal|print|show|repeat)\b[^.\n]{0,30}\bsystem prompt\b", re.I),
    ),
    (
        "exfiltration",
        re.compile(
            r"\b(send|post|upload|exfiltrate|export|forward)\b[^.\n]{0,40}\b(incidents?|data|"
            r"credentials?|secrets?|tokens?|passwords?|logs?|everything|all)\b[^.\n]{0,30}"
            r"\bto\s+https?://",
            re.I,
        ),
    ),
    (
        "tool-coercion",
        re.compile(
            r"\b(call|invoke|execute)\b[^.\n]{0,30}\b(restart_service|delete_\w+|rollback_\w+|"
            r"approve_\w+)\b|\b(call|invoke)\b\s+(the\s+)?\w+\s+tool\s+on\s+every\b",
            re.I,
        ),
    ),
    (
        "ranking-manipulation",
        re.compile(
            r"\b(rank|rate|score)\s+(it|this|me|this (document|passage|result))\b[^.\n]{0,20}"
            r"\b(first|highest|top)\b|\bmost relevant (answer|result|document) to every\b",
            re.I,
        ),
    ),
)


def detect_injection(text: str) -> list[str]:
    """Names of the patterns that matched (empty list = no tripwire hit)."""
    return [name for name, pattern in _PATTERNS if pattern.search(text)]
