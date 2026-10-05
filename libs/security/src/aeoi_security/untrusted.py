"""Wrap retrieved/tool text as untrusted DATA before it reaches a prompt.

This is channel separation, not a complete prompt-injection defence. The main
control is capability limits (agents cannot act without approval). What this
guarantees: untrusted text cannot close the wrapper early and pretend to be
instructions, and the source is always labelled.
"""

from __future__ import annotations

import html
import re

UNTRUSTED_TAG = "untrusted_data"
_ATTR_SAFE = re.compile(r"[^A-Za-z0-9_.:/-]")
_MAX_CHARS_DEFAULT = 20_000


def _safe_attr(value: str) -> str:
    return _ATTR_SAFE.sub("_", value)[:120]


def wrap_untrusted(
    text: str,
    *,
    source: str,
    item_id: str,
    max_chars: int = _MAX_CHARS_DEFAULT,
) -> str:
    """Return text escaped and wrapped in <untrusted_data> with labelled source/id.

    - `<`, `>` and `&` are escaped, so the content cannot open or close tags.
    - Content longer than `max_chars` is truncated with a visible marker.
    """
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    truncated = len(text) > max_chars
    body = html.escape(text[:max_chars], quote=False)
    if truncated:
        body += "\n[...truncated by AEOI...]"
    return (
        f'<{UNTRUSTED_TAG} source="{_safe_attr(source)}" id="{_safe_attr(item_id)}">\n'
        f"{body}\n"
        f"</{UNTRUSTED_TAG}>"
    )
