"""Phase 12 use cases (ADR-022): agent-observed timeline points and postmortems.

incident-service never calls a model. It stores what the orchestrator posts (timeline points
written by CODE, postmortem sections written by the model and checked by code) and ASSEMBLES
the postmortem document at read time from its own record: status, resolution time and human
timeline events are always current, and every number is computed here, not by a model.

Visibility (ADR-020 evidence by kind): a timeline row whose source is an evidence kind needs
that kind's permission; a model sentence is redacted unless the reader may read EVERY piece
of evidence it cites. Code conclusions (hypothesis statements) are shown to every reader, as
in GET /hypotheses.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, true
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from aeoi_common.errors import ValidationFailedError
from aeoi_common.ids import uuid7
from aeoi_db.models.incident import Evidence, Incident, IncidentEvent, Postmortem
from aeoi_incident.events import stage_event
from aeoi_incident.service import REDACTED_TEXT, _event, hypotheses, load
from aeoi_models.api.history import Claim, SimilarIncident
from aeoi_models.api.postmortem import (
    SECTION_TITLES,
    ActionItem,
    NoChange,
    PmSection,
    PmSentence,
    PmTimelineRow,
    PostmortemDoc,
    PostmortemGeneratedPayload,
    PostmortemIn,
    RootCause,
    TimelineBatch,
    TimelinePoint,
)
from aeoi_models.events import EventType
from aeoi_security.auth import Principal
from aeoi_security.rbac import EVIDENCE_KIND_PERMS

# timeline sources that ARE evidence kinds: reading the row needs that kind's permission
KIND_SOURCES = ("DEPLOY", "CONFIG", "ALERT", "LOG", "METRIC", "TRACE")
OBSERVED = "OBSERVED_"  # event_type prefix of agent-observed points (OBSERVED_EFFECT, ...)


def dedupe_key(source: str, service: str | None, subject: str, at: datetime) -> str:
    """UTC, always: the same instant with another offset (-05:00 from a DB session) must give
    the same key, or a second run duplicates the timeline (found by the e2e test). The service
    is part of it: p95 on two services at the same minute are two points (review)."""
    return f"{source}|{service or '-'}|{subject}|{at.astimezone(UTC).isoformat()}"[:200]


async def _kinds(session: AsyncSession, incident_id: UUID, keys: set[str]) -> dict[str, str]:
    if not keys:
        return {}
    rows = await session.execute(
        select(Evidence.evidence_key, Evidence.kind).where(
            Evidence.incident_id == incident_id, Evidence.evidence_key.in_(keys)
        )
    )
    return {r.evidence_key: r.kind for r in rows.all()}


async def _require_stored(session: AsyncSession, incident_id: UUID, keys: set[str]) -> None:
    """A point or a sentence may cite only evidence stored for THIS incident (as hypotheses)."""
    missing = sorted(keys - set(await _kinds(session, incident_id, keys)))
    if missing:
        raise ValidationFailedError(
            f"cites evidence not stored for this incident: {', '.join(missing[:5])}"
        )


# ------------------------------------------------------------------ timeline
async def add_timeline(
    session: AsyncSession, ref: UUID | int, batch: TimelineBatch, who: Principal
) -> tuple[int, int]:
    """Idempotent per (incident, source|service|subject|instant): a resent batch or a second
    investigation that observed the same thing inserts nothing.

    `source` decides who may read the row, so it is NOT taken from the caller (review): it is
    the STORED kind of the first cited evidence. A point whose evidence needs more than one
    permission, or whose first kind is not a timeline source (COMMIT, DOC, ...), is refused;
    a point without evidence may only be SYSTEM/AGENT (lifecycle)."""
    incident = await load(session, ref)
    keys = {k for p in batch.points for k in p.evidence_keys}
    await _require_stored(session, incident.id, keys)
    kinds = await _kinds(session, incident.id, keys)
    rows = []
    for p in batch.points:
        own = [kinds[k] for k in p.evidence_keys]
        if own:
            # one READ PERMISSION per point (DEPLOY + CONFIG are both deploys:read): the row's
            # source then decides visibility exactly as reading that evidence would
            perms = {EVIDENCE_KIND_PERMS.get(k) for k in own}
            if len(perms) != 1 or None in perms or own[0] not in KIND_SOURCES:
                raise ValidationFailedError(
                    f"timeline point {p.subject!r} cites evidence kinds {sorted(set(own))}: "
                    "they must need one permission and start with a timeline source kind"
                )
            source = own[0]
        elif p.source in ("SYSTEM", "AGENT"):
            source = p.source
        else:
            raise ValidationFailedError(f"timeline point {p.subject!r} cites no evidence")
        rows.append({
            "id": uuid7(), "incident_id": incident.id, "occurred_at": p.at, "source": source,
            "event_type": p.event_type, "summary": p.statement, "actor": f"agent:{p.observed_by}",
            "evidence_key": p.evidence_keys[0] if p.evidence_keys else None,
            "payload": {
                "investigation_id": str(batch.investigation_id), "evidence_keys": p.evidence_keys,
                "subject": p.subject, "service_key": p.service_key, "ref": p.ref,
                "sustained": p.sustained, "via": who.actor,
            },
            "dedupe_key": dedupe_key(source, p.service_key, p.subject, p.at),
        })  # fmt: skip
    stmt = (
        insert(IncidentEvent)
        .values(rows)
        .on_conflict_do_nothing(
            index_elements=["incident_id", "dedupe_key"],
            index_where=IncidentEvent.dedupe_key.is_not(None),
        )
        .returning(IncidentEvent.id)
    )
    inserted = list((await session.scalars(stmt)).all())
    return len(rows), len(inserted)


def _readable_filter(readable: list[str]) -> Any:
    hidden = [s for s in KIND_SOURCES if s not in readable]
    return IncidentEvent.source.not_in(hidden) if hidden else true()


async def timeline_for(
    session: AsyncSession, ref: UUID | int, readable: list[str]
) -> tuple[list[IncidentEvent], int]:
    """Rows the reader may see (filtered IN SQL) + how many were hidden."""
    incident = await load(session, ref)
    base = select(IncidentEvent).where(IncidentEvent.incident_id == incident.id)
    shown = list(
        (
            await session.scalars(
                base.where(_readable_filter(readable))
                .order_by(IncidentEvent.occurred_at, IncidentEvent.id)
                .limit(1000)
            )
        ).all()
    )
    hidden = await session.scalar(
        select(func.count())
        .select_from(IncidentEvent)
        .where(IncidentEvent.incident_id == incident.id, ~_readable_filter(readable))
    )
    return shown, int(hidden or 0)


# ------------------------------------------------------------------ postmortems
def _cited(pm: PostmortemIn) -> set[str]:
    return (
        {k for s in pm.sections for x in s.sentences for k in x.evidence_keys}
        | {k for a in pm.action_items for k in a.evidence_keys}
        | {k for n in pm.no_change for k in n.evidence_keys}
        | {k for p in pm.points for k in p.evidence_keys}
        | {x.evidence_id for x in pm.similar}  # Phase 13: pointers must be stored first
    )


async def add_postmortem(
    session: AsyncSession, ref: UUID | int, pm: PostmortemIn, who: Principal
) -> bool:
    """Idempotent per (incident, investigation). Every model sentence and action item must
    cite, and every cited evidence key must be stored for this incident: else 422, nothing
    is written (redaction depends on those keys)."""
    incident = await load(session, ref)
    uncited = [x.text[:60] for s in pm.sections for x in s.sentences if not x.evidence_keys]
    uncited += [a.text[:60] for a in pm.action_items if not a.evidence_keys]
    if uncited:
        raise ValidationFailedError(f"uncited model text: {uncited[:3]}")
    await _require_stored(session, incident.id, _cited(pm))
    content = pm.model_dump(
        mode="json", include={"no_change", "sections", "action_items", "sentences_dropped",
                              "numbers_rejected", "root_cause_state", "points", "trigger_ref",
                              "similar"},
    )  # fmt: skip
    stmt = (
        insert(Postmortem)
        .values(
            id=uuid7(), incident_id=incident.id, investigation_id=pm.investigation_id,
            written_by=pm.written_by, model=pm.model, prompt_version=pm.prompt_version,
            generated_at=pm.generated_at, content=content, created_by=who.actor,
        )
        .on_conflict_do_nothing(index_elements=["incident_id", "investigation_id"])
        .returning(Postmortem.id)
    )  # fmt: skip
    inserted = (await session.scalars(stmt)).one_or_none() is not None
    if inserted:
        actor = f"agent:postmortem ({pm.written_by})"
        session.add(_event(
            incident, source="AGENT", event_type="PostmortemGenerated", actor=actor,
            summary=f"postmortem generated ({'model' if pm.written_by == 'llm' else 'code only'})",
            payload={"investigation_id": str(pm.investigation_id), "via": who.actor},
        ))  # fmt: skip
        stage_event(
            session, event_type=EventType.POSTMORTEM_GENERATED, actor=actor,
            incident_id=incident.id,
            payload=PostmortemGeneratedPayload(
                incident_id=incident.id, investigation_id=pm.investigation_id,
                written_by=pm.written_by,
            ),
        )  # fmt: skip
        await session.flush()
    return inserted


# a claim's VALUE comes from THIS incident's evidence: "same error code (ERR_X)" is log data,
# a symptom is metric data, deploy timing is deploy data (review: MANAGER read them)
CLAIM_KINDS = {"error_code": "LOG", "symptom": "METRIC", "deploy_before": "DEPLOY"}


def _similar_for(similar: list[SimilarIncident], readable: list[str]) -> list[SimilarIncident]:
    if "INCIDENT" not in readable:
        return []
    return [
        x.model_copy(update={"claims": [
            c for c in x.claims if CLAIM_KINDS.get(c.field, "INCIDENT") in readable
        ]})
        for x in similar
    ]  # fmt: skip


def _minutes(a: datetime | None, b: datetime | None) -> float | None:
    return round((b - a).total_seconds() / 60, 1) if a and b else None


def _first_symptom(points: list[TimelinePoint]) -> datetime | None:
    """THIS investigation's first SUSTAINED effect (as in ADR-021); a log-only first effect if
    none is. From the postmortem's own points, not the incident's whole timeline: another
    run with a wider window must not change this report's numbers (review)."""
    effects = [p for p in points if p.event_type == f"{OBSERVED}EFFECT"]
    pool = [p for p in effects if p.sustained] or effects
    return min((p.at for p in pool), default=None)


def _redact(items: list[Any], kinds: dict[str, str], readable: list[str]) -> tuple[list[Any], int]:
    """Shown only if the reader may read EVERY piece of evidence it cites. A redacted item
    keeps no refs and no evidence ids either (review: the ids leaked)."""
    out, n = [], 0
    for it in items:
        if it.evidence_keys and all(kinds.get(k) in readable for k in it.evidence_keys):
            out.append(it)
        else:
            upd: dict[str, Any] = {"text": REDACTED_TEXT, "evidence_keys": []}
            if "refs" in type(it).model_fields:
                upd["refs"] = []
            out.append(it.model_copy(update=upd))
            n += 1
    return out, n


async def postmortem_doc(
    session: AsyncSession, ref: UUID | int, readable: list[str],
    investigation_id: UUID | None = None,
) -> PostmortemDoc | None:  # fmt: skip
    incident: Incident = await load(session, ref)
    q = select(Postmortem).where(Postmortem.incident_id == incident.id)
    if investigation_id is not None:
        q = q.where(Postmortem.investigation_id == investigation_id)
    row = (await session.scalars(q.order_by(Postmortem.created_at.desc()).limit(1))).one_or_none()
    if row is None:
        return None
    stored = PostmortemIn.model_validate({
        **row.content, "investigation_id": row.investigation_id, "generated_at": row.generated_at,
        "written_by": row.written_by, "model": row.model, "prompt_version": row.prompt_version,
    })  # fmt: skip
    kinds = await _kinds(session, incident.id, _cited(stored))
    sections: list[PmSection] = []
    redacted = 0
    for sec in stored.sections:
        sentences, n = _redact(sec.sentences, kinds, readable)
        redacted += n
        sections.append(PmSection(name=sec.name, sentences=sentences))
    actions, n = _redact(stored.action_items, kinds, readable)
    redacted += n
    no_change, n = _redact(stored.no_change, kinds, readable)
    redacted += n

    hyps = await hypotheses(session, incident.id, row.investigation_id, readable)
    live = [h for h in hyps if h.status != "REJECTED"]
    top = min(live, key=lambda h: h.rank) if live else None
    root = None
    if top is not None:
        d = top.detail or {}
        root = RootCause(
            statement=top.statement, confidence=top.confidence, status=top.status,
            confirmed_by_human=top.status == "CONFIRMED",
            explanation=str(d["explanation"]) if d.get("explanation") else None,
            refinement=str(d["refinement"]) if d.get("refinement") else None,
            history_prior=str(d["history_prior"]) if d.get("history_prior") else None,
        )  # fmt: skip

    shown, hidden = await timeline_for(session, incident.id, readable)
    # numbers: from THIS investigation's points; the trigger is the observation code chose
    first = _first_symptom(stored.points)
    trigger = next((p.at for p in stored.points if stored.trigger_ref
                    and p.ref == stored.trigger_ref), None)  # fmt: skip
    state = stored.root_cause_state
    if state == "validated" and root is None:  # stored "validated" but nothing readable
        state = "not_stored"
    return PostmortemDoc(
        incident_key=incident.key, title=incident.title, severity=incident.severity,
        status=incident.status, services=list(incident.affected_services or []),
        detected_at=incident.detected_at.astimezone(UTC), resolved_at=_utc(incident.resolved_at),
        investigation_id=row.investigation_id, generated_at=row.generated_at.astimezone(UTC),
        written_by=row.written_by, model=row.model,
        first_symptom_at=_utc(first), trigger_at=_utc(trigger),
        minutes_trigger_to_symptom=_minutes(trigger, first),
        minutes_to_detect=_minutes(first, incident.detected_at),
        minutes_to_resolve=_minutes(first, incident.resolved_at),
        root_cause=root, root_cause_state=state,
        ruled_out=[h.statement for h in hyps if h.status == "REJECTED"],
        no_change=no_change,
        timeline=[PmTimelineRow(at=e.occurred_at.astimezone(UTC), source=e.source, summary=e.summary,
                                actor=e.actor, evidence_key=e.evidence_key) for e in shown],
        hidden_timeline_rows=hidden, sections=sections, action_items=actions,
        redacted_sentences=redacted, sentences_dropped=stored.sentences_dropped,
        numbers_rejected=stored.numbers_rejected,
        # past incidents shared with every engineer, as INCIDENT evidence pointers (ADR-023)
        similar=_similar_for(stored.similar, readable),
    )  # fmt: skip


# ------------------------------------------------------------------ markdown
def _utc(d: datetime | None) -> datetime | None:
    """The DB session hands back its own offset (-05:00 in the sandbox): normalise, or a "Z"
    lands on a local time (live-run bug, smoke 12)."""
    return d.astimezone(UTC) if d else None


def _t(d: datetime | None) -> str:
    u = _utc(d)
    return u.strftime("%Y-%m-%d %H:%M:%SZ") if u else "-"


# only what makes a link, an image or HTML: "[", "]", "<", ">" (and "\\" so an escape cannot
# be undone). Escaping "_" and "(" made order_batching read as order\\_batching in a
# terminal (Mac run) and protects nothing: a link needs "[" first.
MD_ESCAPE = str.maketrans({c: f"\\{c}" for c in "\\[]<>"})


def _md(text: str) -> str:
    """Model text is data: escaped so no renderer turns it into a link, image or HTML (the
    agent already drops such sentences; this is the second layer)."""
    return text.translate(MD_ESCAPE)


def _sentence(s: PmSentence | ActionItem | NoChange) -> str:
    if s.text == REDACTED_TEXT or isinstance(s, NoChange):  # code text: shown as written
        return s.text
    return f"{_md(s.text)} [{', '.join(s.refs)}]"


def _gap(m: float | None, what: str) -> str:
    if m is None:
        return ""
    if m < 0:  # e.g. an alert fired before the first SUSTAINED metric shift (review)
        return f" ({-m:g} min BEFORE the first sustained symptom)"
    return f" ({m:g} min {what})"


ROOT_STATE_TEXT = {
    "none_survived": "No hypothesis survived validation: the evidence did not support any "
    "candidate cause. Root cause unknown.",
    "reasoning_failed": "The hypothesis step FAILED for this investigation: the root cause was "
    "not analysed (unknown, not refuted). See the investigation's error.",
    "reasoning_off": "Hypotheses were not generated for this investigation (reasoning off).",
    "not_stored": "Hypotheses were validated but could not be stored or read: see the "
    "investigation. The narrative below may name a cause this page cannot show.",
}


def _claim(c: Claim) -> str:
    return f"{c.field.replace('_', ' ')}" + (f" ({c.value})" if c.value else "")


def render_markdown(doc: PostmortemDoc) -> str:
    """Plain code template. Model text is marked; every number above the line is code's."""
    rc = doc.root_cause
    lines = [
        f"# Postmortem {doc.incident_key}: {doc.title}",
        "",
        f"Severity {doc.severity} · status {doc.status} · services {', '.join(doc.services)}",
        f"Generated {_t(doc.generated_at)} from investigation {doc.investigation_id} · "
        f"narrative written by {'model ' + (doc.model or '?') if doc.written_by == 'llm' else 'code only (model unavailable)'}",
        "",
        "## Impact and timing (computed by code)",
        f"- First sustained symptom: {_t(doc.first_symptom_at)}",
        f"- Trigger (top hypothesis' change) started: {_t(doc.trigger_at)}"
        + _gap(doc.minutes_trigger_to_symptom, "before the first symptom"),
        f"- Detected: {_t(doc.detected_at)}" + _gap(doc.minutes_to_detect, "after the first symptom"),
        f"- Resolved: {_t(doc.resolved_at)}"
        + (_gap(doc.minutes_to_resolve, "after the first symptom")
           if doc.resolved_at else " (not resolved when this was read)"),
        "",
        "## Most likely root cause (top hypothesis)",
    ]  # fmt: skip
    if rc is None:
        lines.append(ROOT_STATE_TEXT.get(doc.root_cause_state, ROOT_STATE_TEXT["none_survived"]))
    else:
        lines += [
            rc.statement,
            "",
            f"Confidence {rc.confidence} (evidence rubric) · status {rc.status} · "
            + ("confirmed by a human." if rc.confirmed_by_human
               else "**not yet confirmed by a human** (Phase 16)."),
        ]  # fmt: skip
        if rc.refinement:
            lines += ["", f"Mechanism proposed by the critic (model text): {_md(rc.refinement)}"]
        if rc.history_prior:
            lines += [
                "",
                f"History (a base rate, not evidence for this incident): {rc.history_prior}",
            ]
    if doc.ruled_out:
        lines += ["", "Ruled out by code:"] + [f"- {s}" for s in doc.ruled_out]
    if doc.no_change:
        lines += ["", "Did not change (negative evidence):"] + [
            f"- {_sentence(x)}" for x in doc.no_change
        ]
    for sec in doc.sections:
        if sec.sentences:
            lines += ["", f"## {SECTION_TITLES[sec.name]} (model text, cites observations)"]
            lines += [_sentence(s) for s in sec.sentences]
    if doc.similar:
        lines += [
            "",
            "## Similar past incidents (shared with all engineers; claims checked by code)",
        ]
        for x in doc.similar:
            same = [_claim(c) for c in x.claims if c.relation == "same"]
            diff = [_claim(c) for c in x.claims if c.relation == "different"]
            lines.append(
                f"- {x.incident_key} ({x.root_cause_category}, {_t(x.occurred_at)[:10]}, "
                f"{', '.join(x.service_keys)}): same {', '.join(same) or '-'}; "
                f"different {', '.join(diff) or '-'}"
                + (" [this service's history]" if x.scope == "service" else "")
                + (" (compared by code)" if x.compared_by == "code" else "")
            )
        lines.append("Similar is not the same: a past root cause is not evidence for this one.")
    if doc.action_items:
        lines += ["", "## Proposed action items (model text; no owner, nothing executes them)"]
        lines += [f"- [{a.category}] {_sentence(a)}" for a in doc.action_items]
    lines += ["", "## Timeline", "", "| time (UTC) | source | what | by |", "|---|---|---|---|"]
    lines += [f"| {_t(r.at)} | {r.source} | {r.summary.replace('|', '/')} | {r.actor} |"
              for r in doc.timeline]  # fmt: skip
    if doc.hidden_timeline_rows:
        lines.append(f"\n{doc.hidden_timeline_rows} timeline row(s) hidden: you lack the permission"
                     " for their evidence kind.")  # fmt: skip
    if doc.redacted_sentences:
        lines.append(f"{doc.redacted_sentences} model sentence(s) redacted for the same reason.")
    if doc.sentences_dropped or doc.numbers_rejected:
        lines.append(f"Code dropped {doc.sentences_dropped} uncited and {doc.numbers_rejected} "
                     "unverifiable-number sentence(s) from the model's draft.")  # fmt: skip
    return "\n".join(lines) + "\n"
