"""Phase 12 (ADR-022): the postmortem markdown is a CODE template. Pure function, no IO."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from uuid import uuid4

from aeoi_incident.postmortem import render_markdown
from aeoi_incident.service import REDACTED_TEXT
from aeoi_models.api.postmortem import (
    PmSection,
    PmSentence,
    PmTimelineRow,
    PostmortemDoc,
    RootCause,
)

CST = timezone(timedelta(hours=-5))  # what the DB session hands back in the sandbox


def doc(**kw: object) -> PostmortemDoc:
    at = datetime(2026, 10, 2, 9, 42, tzinfo=UTC).astimezone(CST)
    base: dict[str, object] = {
        "incident_key": "INC-1", "title": "t", "severity": "SEV2", "status": "INVESTIGATING",
        "services": ["checkout-api"], "detected_at": at + timedelta(minutes=8),
        "resolved_at": None, "investigation_id": uuid4(), "generated_at": at,
        "written_by": "llm", "model": "claude-sonnet-5-5", "first_symptom_at": at,
        "root_cause": RootCause(statement="Deploy X may have caused it", confidence="HIGH",
                                status="VALIDATED"),
        "timeline": [PmTimelineRow(at=at, source="DEPLOY", summary="a | b", actor="agent:d")],
        "sections": [PmSection(name="summary", sentences=[
            PmSentence(text="Deploy X most likely did it.", refs=["o1"],
                       evidence_keys=["DEPLOY-1"]),
            PmSentence(text=REDACTED_TEXT, refs=["o2"], evidence_keys=["LOG-1"]),
        ])],
    }  # fmt: skip
    base.update(kw)
    return PostmortemDoc.model_validate(base)


def test_times_are_utc_with_z_whatever_offset_the_db_returned() -> None:
    md = render_markdown(doc())
    assert "2026-10-02 09:42:00Z" in md and "04:42" not in md


def test_unconfirmed_root_cause_is_said_and_model_text_is_marked() -> None:
    md = render_markdown(doc())
    assert "**not yet confirmed by a human**" in md
    assert "(model text, cites observations)" in md
    assert "Deploy X most likely did it. [o1]" in md
    assert f"{REDACTED_TEXT}\n" in md and "[o2]" not in md  # a redacted sentence shows no refs
    assert "| a / b |" in md  # a pipe in data cannot break the table


def test_confirmed_and_missing_root_cause_say_why() -> None:
    """Review: "no hypothesis survived" was printed when reasoning had FAILED - a false claim
    that every cause was refuted."""
    rc = RootCause(statement="s", confidence="HIGH", status="CONFIRMED", confirmed_by_human=True)
    assert "confirmed by a human." in render_markdown(doc(root_cause=rc))
    none = render_markdown(doc(root_cause=None, root_cause_state="none_survived"))
    assert "No hypothesis survived validation" in none
    failed = render_markdown(doc(root_cause=None, root_cause_state="reasoning_failed"))
    assert "FAILED" in failed and "not refuted" in failed and "survived" not in failed


def test_model_markup_is_escaped_and_a_negative_gap_is_said_plainly() -> None:
    s = PmSentence(text="see ![x](http://a) now", refs=["o1"], evidence_keys=["DEPLOY-1"])
    md = render_markdown(doc(sections=[PmSection(name="summary", sentences=[s])],
                             minutes_to_detect=-4.0))  # fmt: skip
    assert "![x](" not in md and "!\\[x\\](" in md  # no image: "[" is escaped
    assert "4 min BEFORE the first sustained symptom" in md and "(-4 min" not in md


def test_escaping_does_not_mangle_ordinary_words() -> None:
    """Mac run: order_batching read as order\\_batching and (ERR) as \\(ERR\\) in a terminal."""
    s = PmSentence(text="order_batching held DB connections (ERR_POOL_TIMEOUT) longer",
                   refs=["o1"], evidence_keys=["DEPLOY-1"])  # fmt: skip
    md = render_markdown(doc(sections=[PmSection(name="summary", sentences=[s])]))
    assert "order_batching held DB connections (ERR_POOL_TIMEOUT) longer [o1]" in md


def test_first_symptom_is_this_runs_first_sustained_effect() -> None:
    """Review: the header used EVERY run's points, so a wider second window moved the numbers
    away from the root cause and narrative on the same page."""
    from aeoi_incident.postmortem import _first_symptom
    from aeoi_models.api.postmortem import TimelinePoint

    t = datetime(2026, 10, 2, 9, 0, tzinfo=UTC)

    def pt(minute: int, et: str, sustained: bool) -> TimelinePoint:
        return TimelinePoint(at=t + timedelta(minutes=minute), source="METRIC", event_type=et,
                             subject="m", statement="s s s", sustained=sustained)  # fmt: skip

    pts = [pt(40, "OBSERVED_EFFECT", False), pt(49, "OBSERVED_EFFECT", True),
           pt(30, "OBSERVED_CHANGE", False)]  # fmt: skip
    assert _first_symptom(pts) == t + timedelta(minutes=49)  # a blip never anchors
    assert _first_symptom(pts[:1]) == t + timedelta(minutes=40)  # log-only: best available
    assert _first_symptom([]) is None


def test_similar_claims_are_filtered_by_the_kind_their_value_comes_from() -> None:
    """Phase 13 review: "same error code (ERR_X)" is LOG data, a symptom METRIC data, deploy
    timing DEPLOY data - a reader without those permissions sees only the service claim."""
    from aeoi_incident.postmortem import _similar_for
    from aeoi_models.api.history import Claim, SimilarIncident

    x = SimilarIncident(
        incident_key="INC-1", rank=1, retrieval="hybrid", root_cause_category="c",
        occurred_at="2025-01-01T00:00:00+00:00", evidence_id=f"INCIDENT-{'a' * 32}-0",  # type: ignore[arg-type]
        claims=[Claim(field="service", relation="same", value="checkout-api"),
                Claim(field="error_code", relation="same", value="ERR_X"),
                Claim(field="symptom", relation="same", value="latency: both"),
                Claim(field="deploy_before", relation="same", value="both")],
    )  # fmt: skip
    assert [c.field for c in _similar_for([x], ["INCIDENT"])[0].claims] == ["service"]
    every = ["INCIDENT", "LOG", "METRIC", "DEPLOY"]
    assert len(_similar_for([x], every)[0].claims) == 4
    assert _similar_for([x], ["LOG", "METRIC"]) == []  # INCIDENT not readable: no section
