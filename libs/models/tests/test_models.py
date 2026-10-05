import json
from datetime import datetime
from uuid import uuid4

import pytest
from pydantic import BaseModel, TypeAdapter, ValidationError

from aeoi_common import set_correlation_id
from aeoi_models import (
    ConfidenceBand,
    EventEnvelope,
    EventType,
    EvidenceKind,
    EvidenceRef,
    Fact,
    Finding,
    Hypothesis,
    Recommendation,
)


def ev(eid: str, kind: EvidenceKind) -> EvidenceRef:
    return EvidenceRef(evidence_id=eid, kind=kind, source_system="synthetic")


LOG = ev("LOG-18293", EvidenceKind.LOG)
METRIC = ev("METRIC-92831", EvidenceKind.METRIC)
DEPLOY = ev("DEPLOY-4821", EvidenceKind.DEPLOY)


class IncidentCreatedPayload(BaseModel):
    title: str


class TestEnvelope:
    def test_defaults_and_roundtrip(self) -> None:
        set_correlation_id("corr-42")
        incident = uuid4()
        env = EventEnvelope[IncidentCreatedPayload](
            event_type=EventType.INCIDENT_CREATED,
            incident_id=incident,
            actor="service:incident-service",
            payload=IncidentCreatedPayload(title="HTTP 500 spike"),
        )
        assert env.event_id.version == 7
        assert env.correlation_id == "corr-42"
        assert env.partition_key() == str(incident).encode()
        restored = EventEnvelope[IncidentCreatedPayload].model_validate_json(env.model_dump_json())
        assert restored == env
        set_correlation_id(None)

    def test_rejects_bad_actor(self) -> None:
        with pytest.raises(ValidationError, match="actor"):
            EventEnvelope[IncidentCreatedPayload](
                event_type=EventType.INCIDENT_CREATED,
                actor="bob",
                payload=IncidentCreatedPayload(title="x"),
            )

    def test_rejects_naive_timestamp(self) -> None:
        with pytest.raises(ValidationError, match="timezone"):
            EventEnvelope[IncidentCreatedPayload](
                event_type=EventType.INCIDENT_CREATED,
                actor="agent:log",
                occurred_at=datetime(2026, 10, 2, 10, 32),
                payload=IncidentCreatedPayload(title="x"),
            )

    def test_no_partition_key_without_incident(self) -> None:
        env = EventEnvelope[IncidentCreatedPayload](
            event_type=EventType.INCIDENT_CREATED,
            actor="user:u1",
            payload=IncidentCreatedPayload(title="x"),
        )
        assert env.partition_key() is None


class TestEvidence:
    def test_prefix_must_match_kind(self) -> None:
        with pytest.raises(ValidationError, match="must start with 'METRIC-'"):
            EvidenceRef(evidence_id="LOG-1", kind=EvidenceKind.METRIC, source_system="s")

    def test_id_format(self) -> None:
        with pytest.raises(ValidationError):
            EvidenceRef(evidence_id="log 1; drop table", kind=EvidenceKind.LOG, source_system="s")


class TestFindings:
    def test_fact_requires_evidence(self) -> None:
        with pytest.raises(ValidationError):
            Fact(statement="Error rate rose 42%", produced_by="metrics", evidence=[])

    def test_high_confidence_rejected_with_contradiction(self) -> None:
        with pytest.raises(ValidationError, match="contradicting"):
            Hypothesis(
                statement="DB connection pool exhaustion",
                produced_by="orchestrator",
                supporting=[LOG, METRIC],
                contradicting=[DEPLOY],
                confidence=ConfidenceBand.HIGH,
            )

    def test_high_confidence_needs_two_distinct_sources(self) -> None:
        with pytest.raises(ValidationError, match="at least 2"):
            Hypothesis(
                statement="DB connection pool exhaustion",
                produced_by="orchestrator",
                supporting=[LOG, LOG],
                confidence=ConfidenceBand.HIGH,
            )

    def test_medium_with_contradiction_is_fine(self) -> None:
        h = Hypothesis(
            statement="Application thread saturation",
            produced_by="critic",
            supporting=[METRIC],
            contradicting=[LOG],
            confidence=ConfidenceBand.MEDIUM,
        )
        assert h.type == "hypothesis"

    def test_consequential_recommendation_must_require_approval(self) -> None:
        with pytest.raises(ValidationError, match="approval"):
            Recommendation(
                statement="Roll back the deployment",
                produced_by="report",
                action="rollback deploy 2026.10.02.4",
                based_on=[DEPLOY],
                consequential=True,
                requires_approval=False,
            )

    def test_discriminated_union_parses_from_json(self) -> None:
        adapter: TypeAdapter[Fact | Hypothesis | Recommendation] = TypeAdapter(Finding)
        raw = json.dumps(
            {
                "type": "fact",
                "statement": "Error rate rose 11 minutes after deploy",
                "produced_by": "deployment",
                "evidence": [
                    {"evidence_id": "DEPLOY-4821", "kind": "DEPLOY", "source_system": "cd"}
                ],
            }
        )
        assert isinstance(adapter.validate_json(raw), Fact)

    def test_findings_are_immutable(self) -> None:
        fact = Fact(statement="x happened", produced_by="log", evidence=[LOG])
        with pytest.raises(ValidationError):
            fact.statement = "changed"  # type: ignore[misc]
