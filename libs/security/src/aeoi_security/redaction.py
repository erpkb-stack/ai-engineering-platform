"""Redact secrets from logs, traces and audit payloads.

Two layers: (1) sensitive KEY names -> value replaced; (2) known secret PATTERNS
in free text. This is not a PII detector (that comes in Phase 6/26).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

REDACTED = "[REDACTED]"

_SENSITIVE_KEYS = re.compile(
    r"(pass(word)?|secret|token|api[_-]?key|authorization|cookie|session|private[_-]?key|dsn)",
    re.IGNORECASE,
)

_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"sk-ant-[A-Za-z0-9_-]{10,}"),  # Anthropic
    re.compile(r"sk-[A-Za-z0-9]{20,}"),  # OpenAI-style
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),  # GitHub tokens
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),  # AWS access key id
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]{10,}"),
    re.compile(r"eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}"),  # JWT
    re.compile(r"(?i)(postgres(ql)?|redis|amqp)(\+\w+)?://[^:\s/]+:[^@\s]+@"),  # creds in URL
)


# `password=hunter2` / `api_key: sk1234` inside free text (log lines, config dumps). The value
# must contain a digit, which keeps prose like "token: bucket" untouched. Key is kept so the
# reader still learns WHAT leaked; only the value goes.
_KV_SECRET = re.compile(
    r"(?i)\b(pass(?:word|wd)?|pwd|secret|client[_-]?secret|api[_-]?key|access[_-]?key|token)"
    r"(\s*[=:]\s*)(?=[^\s,;'\"&]*\d)[^\s,;'\"&]{4,}"
)


def is_sensitive_key(key: str) -> bool:
    """True for names like `password`, `api_key`, `db.token` whose VALUE must never be shown."""
    return bool(_SENSITIVE_KEYS.search(key))


def redact_text(text: str) -> str:
    return redact_text_counted(text)[0]


def redact_text_counted(text: str) -> tuple[str, int]:
    """Like redact_text, plus how many secrets were removed (for metrics/audit, never content)."""
    out, total = text, 0
    for pattern in _SECRET_PATTERNS:
        out, n = pattern.subn(REDACTED, out)
        total += n
    out, n = _KV_SECRET.subn(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", out)
    return out, total + n


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        return redact_mapping(value)
    if isinstance(value, list | tuple):
        return type(value)(_redact_value(v) for v in value)
    return value


def redact_mapping(data: Mapping[str, Any]) -> dict[str, Any]:
    """Return a new dict with sensitive keys masked and secret patterns removed (recursive)."""
    result: dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(key, str) and _SENSITIVE_KEYS.search(key) and value is not None:
            result[key] = REDACTED
        else:
            result[key] = _redact_value(value)
    return result
