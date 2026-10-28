"""Phase 29b (ADR-044): an alert opens an incident. Triage is CODE - a rule table, never a model.

Rules (each one is written into the IncidentCreated event, so a reader sees WHY):
  severity   label `severity`: critical -> SEV1, error|high -> SEV2, warning -> SEV3,
             info -> SEV4, missing/unknown -> SEV3 (said so). A catalog TIER-1 service is
             raised one level (max SEV1).
  service    label `service` (else `service_key`, else `job`); must be a valid service key -
             otherwise the alert is REJECTED (an incident with no service cannot be
             investigated: the orchestrator refuses it).
  affected   [service] ONLY. The catalog's dependents are the blast radius and go into the
             description - NOT into affected_services: the orchestrator investigates the
             first 3 affected services, so dependents there would send the agents to the
             wrong services.
  title      annotation `summary`, else the alertname + service.
  detected   the alert's startsAt.
  dedupe     one incident per (fingerprint, startsAt): the same alert re-sent, or a replayed
             request inside the signature window, returns the first result. `resolved`
             alerts never close anything (a human decides that) - they are counted only.

Catalog down / not configured -> the label severity and [service], and the event says so.
An alert is never lost because a dependency is down.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

from aeoi_incident.catalog import CatalogEntry
from aeoi_models.api.alerts import AlertIn
from aeoi_models.api.incidents import IncidentCreate, Severity
from aeoi_security.auth import Principal

SERVICE_KEY = re.compile(r"^[a-z0-9][a-z0-9-]{1,99}$")
LABEL_SEVERITY = {
    "critical": Severity.SEV1,
    "error": Severity.SEV2,
    "high": Severity.SEV2,
    "warning": Severity.SEV3,
    "info": Severity.SEV4,
}
ORDER = [Severity.SEV1, Severity.SEV2, Severity.SEV3, Severity.SEV4]
DEPENDENTS_IN_DESCRIPTION = 20
# Alertmanager re-sends a firing alert every repeat_interval (default 4 h) with the SAME
# fingerprint + startsAt: the dedupe must outlive any realistic firing period.
DEDUPE_TTL = timedelta(days=30)
ALERTS_PRINCIPAL = Principal(
    subject="service:alerts",
    user_id=UUID(int=0),
    email="",
    name="Alertmanager webhook",
    roles=frozenset(),
    groups=frozenset(),
)


@dataclass(frozen=True)
class Triage:
    incident: IncidentCreate
    rule: str
    blast_radius: list[str]
    catalog: str  # where tier/dependents came from: "catalog" or why not


class AlertRejectedError(Exception):
    pass


def service_of(alert: AlertIn) -> str:
    for label in ("service", "service_key", "job"):
        value = alert.labels.get(label, "").strip()
        if value:
            if not SERVICE_KEY.match(value):
                raise AlertRejectedError(f"label {label}={value[:60]!r} is not a service key")
            return value
    raise AlertRejectedError("no service label (service, service_key or job)")


def dedupe_key(alert: AlertIn) -> str:
    return f"alert:{alert.fingerprint}:{int(alert.startsAt.timestamp())}"


def triage(alert: AlertIn, entry: CatalogEntry | None, catalog_note: str) -> Triage:
    service = service_of(alert)
    raw = alert.labels.get("severity", "").strip().lower()
    base = LABEL_SEVERITY.get(raw)
    parts = [f"label severity={raw or '(none)'} -> {(base or Severity.SEV3).value}"]
    if base is None:
        parts[0] += " (unknown label: default)"
        base = Severity.SEV3
    severity = base
    if entry is not None and entry.tier == 1 and base is not Severity.SEV1:
        severity = ORDER[ORDER.index(base) - 1]
        parts.append(f"tier 1 -> raised to {severity.value}")
    elif entry is None:
        parts.append(f"tier unknown ({catalog_note})")
    else:
        parts.append(f"tier {entry.tier} -> unchanged")
    blast = sorted(entry.dependents)[:DEPENDENTS_IN_DESCRIPTION] if entry else []
    name = alert.labels.get("alertname", "Alert").strip()[:100] or "Alert"
    title = (alert.annotations.get("summary", "").strip() or f"{name} on {service}")[:300]
    if len(title) < 5:
        title = f"{name} on {service}"[:300]
    lines = [
        f"Opened by alert {name} (fingerprint {alert.fingerprint}).",
        f"Triage: {'; '.join(parts)}.",
    ]
    if desc := alert.annotations.get("description", "").strip():
        lines.append(f"Alert description: {desc[:2000]}")
    if entry is not None:
        lines.append(
            f"Blast radius (catalog dependents of {service}): "
            + (", ".join(blast) if blast else "none")
            + (" ..." if len(entry.dependents) > len(blast) else "")
        )
    return Triage(
        incident=IncidentCreate(
            title=title,
            description="\n".join(lines),
            severity=severity,
            affected_services=[service],
            detected_at=alert.startsAt,
        ),
        rule="; ".join(parts),
        blast_radius=blast,
        catalog=catalog_note,
    )
