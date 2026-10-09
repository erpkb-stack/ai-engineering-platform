"""Post the evidence of every SUCCEEDED task of an investigation from its recorded batches.

Used when the graph cannot reach collect_evidence itself: the investigation deadline hit
while a sibling was still running (review finding, Phase 10: three finished agents' evidence
was thrown away because one agent was slow), and by the `repost-evidence` CLI.
Idempotent: incident-service dedupes per evidence key.
"""

from __future__ import annotations

from uuid import UUID

from aeoi_models.api.agents import EvidenceItem
from aeoi_orchestrator.clients import CallError, IncidentClient
from aeoi_orchestrator.store import Store


async def post_recorded_evidence(
    store: Store, incidents: IncidentClient, investigation_id: UUID, incident_id: UUID, label: str
) -> tuple[int, int, str | None]:
    """-> (succeeded tasks, rows inserted, error). error = some batch was not stored."""
    succeeded = inserted = 0
    errors: list[str] = []
    for t in await store.tasks(investigation_id):
        if t["status"] != "SUCCEEDED":
            continue
        succeeded += 1
        items = [EvidenceItem.model_validate(e) for e in await store.evidence_of(t["task_id"])]
        if not items:
            continue
        try:
            inserted += await incidents.post_evidence(
                str(incident_id), investigation_id, t["agent"], items, f"{t['agent']}: {label}"
            )
        except CallError as exc:
            errors.append(f"{t['agent']}: evidence was not stored: {exc}")
    return succeeded, inserted, "; ".join(errors) or None
