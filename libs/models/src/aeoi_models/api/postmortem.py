"""Phase 12 contract: incident timeline + postmortem (ADR-022).

Who writes what:
- CODE builds the timeline (observations of the evidence agents + incident lifecycle), every
  number in the postmortem header (time to detect, duration) and the root-cause section (the
  stored top hypothesis, verbatim, with its band and status).
- The MODEL (owner decision: Sonnet, `reasoning` route, fallback off) writes the prose
  sections and proposed action items. Every sentence cites observation refs; code drops a
  sentence that cites nothing valid or that contains a number found nowhere in its input.
- incident-service assembles the document at READ time from its own record (status,
  resolution, human timeline events) + the stored generated parts, and redacts each model
  sentence whose evidence the reader may not read.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from aeoi_models.api.history import SimilarIncident
from aeoi_models.api.hypotheses import ObsRef

# incident.incident_events.source values (migration 0003 check constraint)
TimelineSource = Literal["DEPLOY", "CONFIG", "ALERT", "LOG", "METRIC", "TRACE", "AGENT", "SYSTEM"]
SectionName = Literal["summary", "what_happened", "why", "detection", "went_well", "went_poorly"]
SECTION_TITLES: dict[str, str] = {
    "summary": "Summary",
    "what_happened": "What happened",
    "why": "Why it happened",
    "detection": "Detection",
    "went_well": "What went well",
    "went_poorly": "What went poorly",
}
ActionCategory = Literal["prevent", "detect", "mitigate", "process"]
EvidenceKey = str  # "LOG-...", "METRIC-..." (kind prefix enforced by EvidenceRef upstream)


class TimelinePoint(BaseModel):
    """One timestamped thing, written by CODE (an observation of an evidence agent, or an
    incident lifecycle fact). `ref` is the observation ref the postmortem cites (None for a
    lifecycle point)."""

    model_config = ConfigDict(extra="forbid")

    ref: ObsRef | None = None
    at: AwareDatetime  # naive times would shift the dedupe key by the server's offset
    observed_by: str = Field(default="postmortem", pattern=r"^[a-z][a-z0-9_]{1,63}$")
    source: TimelineSource
    event_type: str = Field(pattern=r"^[A-Z][A-Z_]{2,79}$")
    service_key: str | None = Field(default=None, max_length=120)
    subject: str = Field(max_length=120)
    statement: str = Field(min_length=3, max_length=500)
    evidence_keys: list[EvidenceKey] = Field(default_factory=list, max_length=4)
    sustained: bool = False  # effects: a sustained shift anchors "the first symptom" (ADR-021)


class TimelineBatch(BaseModel):
    """POST /v1/incidents/{id}/timeline (scope timeline:write). Idempotent: a point is
    stored once per (incident, source, service, subject, instant), whichever run found it.
    incident-service sets `source` from the STORED kind of the cited evidence (review)."""

    model_config = ConfigDict(extra="forbid")

    investigation_id: UUID
    agent: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    points: list[TimelinePoint] = Field(min_length=1, max_length=60)


class PmSentence(BaseModel):
    """One model-written sentence. `refs` = observation refs it cites; `evidence_keys` =
    what code mapped them to (used to redact it for a reader without that evidence).
    Stored sentences always cite (incident-service refuses empty lists); a REDACTED sentence
    in a GET response has both lists emptied, so the ids do not leak either (review)."""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=3, max_length=400)
    refs: list[ObsRef] = Field(default_factory=list, max_length=4)
    evidence_keys: list[EvidenceKey] = Field(default_factory=list, max_length=16)


class PmSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: SectionName
    sentences: list[PmSentence] = Field(default_factory=list, max_length=8)


class ActionItem(BaseModel):
    """A PROPOSED follow-up (a recommendation, not an action): no owner, no due date, nothing
    executes it. Cites the observations that motivate it."""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=5, max_length=300)
    category: ActionCategory
    refs: list[ObsRef] = Field(default_factory=list, max_length=4)
    evidence_keys: list[EvidenceKey] = Field(default_factory=list, max_length=16)


class NoChange(BaseModel):
    """Negative evidence ("requests_per_sec stayed within its baseline"): code-written, but it
    IS evidence (numbers from metrics), so it is redacted like a sentence (review: it leaked
    METRIC data to MANAGER while the METRIC rows were hidden)."""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=3, max_length=300)
    evidence_keys: list[EvidenceKey] = Field(default_factory=list, max_length=4)


RootCauseState = Literal[
    "validated",  # the top hypothesis of this investigation is shown
    "none_survived",  # reasoning ran; no hypothesis survived validation
    "reasoning_failed",  # the hypothesis step failed: the cause is UNKNOWN, not refuted
    "reasoning_off",  # reasoning disabled for this run
    "not_stored",  # hypotheses were validated but their POST failed
]


class PostmortemDraft(BaseModel):
    """Output of the postmortem agent (AgentRunResult.postmortem)."""

    model_config = ConfigDict(extra="forbid")

    timeline: list[TimelinePoint] = Field(default_factory=list, max_length=60)
    no_change: list[NoChange] = Field(default_factory=list, max_length=12)
    # the observation of the top hypothesis' cause (a deploy), chosen by CODE: incident-service
    # times the trigger from it, exactly (review: substring matching picked DEPLOY-48 for 4821)
    trigger_ref: ObsRef | None = None
    sections: list[PmSection] = Field(default_factory=list, max_length=6)
    action_items: list[ActionItem] = Field(default_factory=list, max_length=8)
    written_by: Literal["llm", "code"] = "code"
    sentences_dropped: int = 0  # uncited / unknown refs
    numbers_rejected: int = 0  # sentences dropped for a number not found in the input
    model: str | None = None
    prompt_version: int | None = None


class PostmortemIn(BaseModel):
    """POST /v1/incidents/{id}/postmortems (scope postmortems:write). Idempotent per
    (incident, investigation). The timeline is posted separately (TimelineBatch)."""

    model_config = ConfigDict(extra="forbid")

    investigation_id: UUID
    generated_at: AwareDatetime
    written_by: Literal["llm", "code"]
    model: str | None = Field(default=None, max_length=120)
    prompt_version: int | None = None
    root_cause_state: RootCauseState = "validated"
    # THIS investigation's points: the header numbers come from them, so they agree with the
    # root cause and the narrative even when another run saw an earlier symptom (review)
    points: list[TimelinePoint] = Field(default_factory=list, max_length=60)
    trigger_ref: ObsRef | None = None
    no_change: list[NoChange] = Field(default_factory=list, max_length=12)
    sections: list[PmSection] = Field(default_factory=list, max_length=6)
    action_items: list[ActionItem] = Field(default_factory=list, max_length=8)
    sentences_dropped: int = Field(default=0, ge=0)
    numbers_rejected: int = Field(default=0, ge=0)
    # Phase 13 (ADR-023): similar past incidents (shared with every engineer), code-rendered
    similar: list[SimilarIncident] = Field(default_factory=list, max_length=10)


class PostmortemGeneratedPayload(BaseModel):
    """Event PostmortemGenerated (topic incident.lifecycle, via the outbox)."""

    incident_id: UUID
    investigation_id: UUID
    written_by: Literal["llm", "code"]


class PostmortemStored(BaseModel):
    received: bool
    inserted: bool


class RootCause(BaseModel):
    """Code-assembled from the stored top hypothesis of the postmortem's investigation."""

    statement: str
    confidence: str
    status: str
    confirmed_by_human: bool = False  # Phase 16 (approval/feedback) sets this; never the model
    explanation: str | None = None  # model text (ranker), redacted like the hypotheses API
    refinement: str | None = None  # model text (critic), redacted like the hypotheses API
    history_prior: str | None = None  # Phase 13: code text, a base rate - never evidence


class PmTimelineRow(BaseModel):
    at: datetime
    source: str
    summary: str
    actor: str
    evidence_key: str | None = None


class PostmortemDoc(BaseModel):
    """GET /v1/incidents/{id}/postmortem. Assembled by code at read time."""

    incident_key: str
    title: str
    severity: str
    status: str
    services: list[str]
    detected_at: datetime
    resolved_at: datetime | None
    investigation_id: UUID
    generated_at: datetime
    written_by: str
    model: str | None
    # numbers: CODE only, from the timeline and the incident record
    first_symptom_at: datetime | None = None
    trigger_at: datetime | None = None
    minutes_trigger_to_symptom: float | None = None
    minutes_to_detect: float | None = None
    minutes_to_resolve: float | None = None
    root_cause: RootCause | None = None
    root_cause_state: RootCauseState = "validated"
    ruled_out: list[str] = Field(default_factory=list)
    no_change: list[NoChange] = Field(default_factory=list)
    timeline: list[PmTimelineRow] = Field(default_factory=list)
    hidden_timeline_rows: int = 0
    sections: list[PmSection] = Field(default_factory=list)
    action_items: list[ActionItem] = Field(default_factory=list)
    redacted_sentences: int = 0
    sentences_dropped: int = 0
    numbers_rejected: int = 0
    similar: list[SimilarIncident] = Field(default_factory=list)  # Phase 13 (ADR-023)
