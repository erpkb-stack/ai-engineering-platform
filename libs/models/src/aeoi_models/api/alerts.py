"""Phase 29b (ADR-044): the alert webhook. The request is the Prometheus Alertmanager webhook
(version 4) - what a real Alertmanager sends, so no adapter sits in between. Unknown fields
are IGNORED (Alertmanager adds fields between versions; refusing them would drop alerts);
the fields we read are validated and bounded.

Alert text (summary, description, labels) comes from outside: it is untrusted, like a title a
user types. It is stored as data and reaches a model only through the usual wrapping."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints

from aeoi_models.api.incidents import Severity

Fingerprint = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9]{1,64}$")]
MAX_ALERTS = 20


def _bounded(d: dict[str, str]) -> dict[str, str]:
    if len(d) > 64:
        raise ValueError("at most 64 labels/annotations")
    for k, v in d.items():
        if len(k) > 100 or len(v) > 2000:
            raise ValueError(f"label/annotation {k[:40]!r} too long")
    return d


Labels = Annotated[dict[str, str], AfterValidator(_bounded)]


class AlertIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: Literal["firing", "resolved"]
    labels: Labels
    annotations: Labels = Field(default_factory=dict)
    startsAt: datetime  # noqa: N815 - Alertmanager's field name
    fingerprint: Fingerprint


class AlertBatch(BaseModel):
    """Alertmanager webhook payload (v4). One request = one alert group."""

    model_config = ConfigDict(extra="ignore")

    version: str = "4"
    status: Literal["firing", "resolved"]
    alerts: list[AlertIn] = Field(min_length=1, max_length=MAX_ALERTS)


AlertOutcome = Literal["created", "duplicate", "ignored_resolved", "rejected"]


class AlertResult(BaseModel):
    fingerprint: str
    outcome: AlertOutcome
    incident_key: str | None = None
    severity: Severity | None = None
    reason: str = Field(description="the triage rule applied, or why nothing was created")


class AlertsAccepted(BaseModel):
    """200 for every valid, signed batch - per-alert outcomes inside (a 4xx/5xx would make
    Alertmanager retry the WHOLE group, re-sending alerts that were already handled)."""

    received: int
    results: list[AlertResult]
