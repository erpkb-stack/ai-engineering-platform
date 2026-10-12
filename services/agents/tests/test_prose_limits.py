"""Model free text: the JSON schema bounds only RUNAWAY output; code cuts to the stored size.

Regression (Mac, 2026-10-12, make code-smoke): the critic wrote a `missing` sentence longer
than the schema's maxLength 200, the gateway's one repair was too long again, and the WHOLE
critique was lost (run PARTIAL). One long sentence must cost characters, not the critique."""

from __future__ import annotations

from typing import Any

import jsonschema
import pytest

from aeoi_agents.common import PROSE_MAX, clip
from aeoi_agents.postmortem.agent import SCHEMA as PM_SCHEMA
from aeoi_agents.reasoning.critic import SCHEMA as CRITIC_SCHEMA
from aeoi_agents.reasoning.hypothesis import SCHEMA as RANK_SCHEMA

# the sentence the Mac run lost (start of it, as the gateway reported), made long enough
LONG = ("Latency rose at 09:49, before pool use at 09:50, and the deploy came first. Pool "
        "saturation looks like an intermediate step: the per-item query held connections "
        "longer, so pool use climbed after the latency shift, not before it.")  # fmt: skip


def critique(missing: str) -> dict[str, Any]:
    return {"reviews": [{"id": "h1", "verdict": "supported", "contradicting": [],
                         "missing": missing}],
            "alternatives": [], "no_alternative_reason": "x"}  # fmt: skip


def test_a_long_sentence_no_longer_rejects_the_whole_critique() -> None:
    assert len(LONG) > 200  # the old limit
    jsonschema.validate(critique(LONG), CRITIC_SCHEMA)


def test_runaway_text_is_still_rejected() -> None:
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(critique("x" * (PROSE_MAX + 1)), CRITIC_SCHEMA)


def test_no_model_prose_field_has_a_tight_limit() -> None:
    """Every free-text maxLength in a model schema is the runaway bound, not a style limit."""

    def limits(node: Any) -> list[int]:
        if isinstance(node, dict):
            own = [node["maxLength"]] if node.get("type") == "string" and "maxLength" in node \
                and "pattern" not in node and node["maxLength"] > 20 else []  # fmt: skip
            return own + [x for v in node.values() for x in limits(v)]
        if isinstance(node, list):
            return [x for v in node for x in limits(v)]
        return []

    for schema in (CRITIC_SCHEMA, RANK_SCHEMA, PM_SCHEMA):
        assert limits(schema) and set(limits(schema)) == {PROSE_MAX}


def test_clip_cuts_at_a_word_and_says_so() -> None:
    out = clip(LONG, 120)
    assert len(out) <= 120 and out.endswith("…") and LONG.startswith(out[:-1])
    assert not out[:-1].endswith(" ") and out[-2] != " "
    assert clip("  short \n text ", 50) == "short text"
    assert len(clip("x" * 500, 100)) == 100  # one long word: hard cut, still bounded
