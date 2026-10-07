"""Output sanitisation: every tool result passes here before anyone sees it.

1 item cap (spec.max_items) and per-string cap (long log lines / PR bodies)
2 secret redaction + PII scrub on EVERY string (keys like `token` are masked whole)
3 injection tripwire: flag (not drop) strings that look like instructions to a model.
  Dropping would hide evidence: a malicious commit message IS a finding for the Critic.
4 evidence ids: one per item, derived from the tool_call id -> any claim can be traced back
  to the exact call (and its audit row) that produced it.
5 byte cap (spec.max_output_bytes): drop items from the end until it fits; mark truncated.
The result is labelled `untrusted: true`. Prompt builders MUST wrap it with
`aeoi_security.untrusted.wrap_untrusted` (Phase 8); the gateway never builds prompts.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from aeoi_security.injection import detect_injection
from aeoi_security.pii import scrub_pii
from aeoi_security.redaction import REDACTED, is_sensitive_key, redact_text_counted

MAX_STRING_CHARS = 4_000
TRUNC_MARK = " [...truncated by tool-gateway]"


@dataclass
class SanitizeReport:
    secrets_redacted: int = 0
    pii_redacted: dict[str, int] = field(default_factory=dict)
    injection_flags: list[dict[str, Any]] = field(default_factory=list)
    strings_truncated: int = 0
    items_dropped: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "secrets_redacted": self.secrets_redacted,
            "pii_redacted": dict(sorted(self.pii_redacted.items())),
            "injection_flags": self.injection_flags[:50],
            "strings_truncated": self.strings_truncated,
            "items_dropped": self.items_dropped,
        }


def evidence_id(kind: str, call_id: UUID, index: int) -> str:
    """LOG-<call hex>-<n>: kind-prefixed (findings.EvidenceRef rule), traceable to the call."""
    return f"{kind}-{call_id.hex}-{index}"


def _clean_str(value: str, report: SanitizeReport, where: str, ev: str | None) -> str:
    # NUL: Postgres JSONB rejects \u0000, so one NUL would make the RECORD fail (-> no audit row).
    value = value.replace("\x00", "")
    # Redact BEFORE truncating: a cut through the middle of a token would leave a prefix that
    # no pattern matches. The pre-cap only bounds regex CPU on pathological inputs.
    value, n = redact_text_counted(value[: MAX_STRING_CHARS * 16])
    report.secrets_redacted += n
    scrubbed = scrub_pii(value)
    for label, count in scrubbed.counts.items():
        report.pii_redacted[label] = report.pii_redacted.get(label, 0) + count
    text = scrubbed.text
    hits = detect_injection(text)
    if hits:
        report.injection_flags.append({"evidence_id": ev, "field": where, "patterns": hits})
    if len(text) > MAX_STRING_CHARS:
        text = text[:MAX_STRING_CHARS] + TRUNC_MARK
        report.strings_truncated += 1
    return text


def _clean(value: Any, report: SanitizeReport, where: str, ev: str | None) -> Any:
    if isinstance(value, str):
        return _clean_str(value, report, where, ev)
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            key = str(k).replace("\x00", "")[:200]
            if is_sensitive_key(key) and v is not None and not isinstance(v, bool | int):
                out[key] = REDACTED
                report.secrets_redacted += 1
            else:
                out[key] = _clean(v, report, f"{where}.{key}", ev)
        return out
    if isinstance(value, list):
        return [_clean(v, report, f"{where}[]", ev) for v in value]
    return value


def sanitize(
    data: dict[str, Any],
    *,
    call_id: UUID,
    max_items: int,
    max_bytes: int,
    evidence_kind: str = "DOC",
    item_kind: Callable[[dict[str, Any]], str] | None = None,
) -> tuple[dict[str, Any], list[str], SanitizeReport]:
    """`data` is the validated output model dumped in JSON mode. Returns (clean data,
    evidence ids, report). Never raises on content: worst case is an empty, truncated result."""
    report = SanitizeReport()
    items: list[Any] = list(data.get("items", []))
    if len(items) > max_items:
        report.items_dropped += len(items) - max_items
        items = items[:max_items]
        data["truncated"] = True
    clean_items = []
    for i, item in enumerate(items):
        kind = item_kind(item) if item_kind and isinstance(item, dict) else evidence_kind
        ev = evidence_id(kind, call_id, i)
        cleaned = _clean(item, report, "items", ev)
        if isinstance(cleaned, dict):
            cleaned["evidence_id"] = ev
        clean_items.append(cleaned)
    rest = {
        k: _clean(v, report, k, None) for k, v in data.items() if k not in ("items", "truncated")
    }
    out = {**rest, "items": clean_items, "truncated": bool(data.get("truncated", False))}
    while clean_items and len(json.dumps(out, default=str).encode()) > max_bytes:
        clean_items.pop()
        report.items_dropped += 1
        out["truncated"] = True
    return out, [it["evidence_id"] for it in clean_items if isinstance(it, dict)], report


def summarize_input(args: Any, limit_bytes: int = 8_192) -> dict[str, Any]:
    """What we store in tool_calls.input: redacted, and bounded even for an oversized attack
    payload (we keep its size and top-level keys, not its content)."""
    if not isinstance(args, dict):
        return {"_invalid_type": type(args).__name__}
    size = len(json.dumps(args, default=str).encode())
    if size > limit_bytes:  # measured BEFORE cleaning: the raw payload is what was sent
        keys = sorted(str(k).replace("\x00", "")[:80] for k in args)[:50]
        return {"_truncated": True, "size_bytes": size, "keys": keys}
    return _clean(args, SanitizeReport(), "input", None)  # type: ignore[no-any-return]
