"""Database-enforced business rules. Each test proves a rule holds even if app code is wrong."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import psycopg
import pytest
from psycopg import errors
from psycopg.types.json import Jsonb

from aeoi_common.ids import uuid7

pytestmark = pytest.mark.integration
SHA = "a" * 64


def make_incident(db: psycopg.Connection) -> tuple[str, int]:
    row = db.execute(
        "INSERT INTO incident.incidents (id, title, severity, detected_at, created_by) "
        "VALUES (%s, 'HTTP 500 spike', 'SEV2', now(), 'user:test') RETURNING id, number",
        (uuid7(),),
    ).fetchone()
    assert row
    return str(row[0]), int(row[1])


def make_approval(db: psycopg.Connection, incident_id: str, **over: object) -> None:
    cols = {
        "id": uuid7(),
        "incident_id": incident_id,
        "action": "rollback_deployment",
        "action_args": Jsonb({"deploy": "DEPLOY-4821"}),
        "args_sha256": SHA,
        "recommendation": Jsonb({}),
        "evidence_keys": ["DEPLOY-4821"],
        "requested_by": "agent:report",
        "expires_at": datetime.now(UTC) + timedelta(minutes=30),
    } | over
    db.execute(
        f"INSERT INTO incident.approvals ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))})",
        list(cols.values()),
    )


class TestIncidents:
    def test_human_key_from_sequence(self, db: psycopg.Connection) -> None:
        _, number = make_incident(db)
        assert number >= 10000

    def test_resolved_requires_timestamp(self, db: psycopg.Connection) -> None:
        incident_id, _ = make_incident(db)
        with pytest.raises(errors.CheckViolation, match="resolved_has_timestamp"):
            db.execute(
                "UPDATE incident.incidents SET status = 'RESOLVED' WHERE id = %s", (incident_id,)
            )

    def test_updated_at_trigger(self, db: psycopg.Connection) -> None:
        incident_id, _ = make_incident(db)
        db.execute(
            "UPDATE incident.incidents SET updated_at = '2000-01-01' WHERE id = %s", (incident_id,)
        )
        row = db.execute(
            "SELECT updated_at > '2020-01-01' FROM incident.incidents WHERE id = %s", (incident_id,)
        ).fetchone()
        assert row and row[0] is True


class TestApprovals:
    def test_decision_needs_reason_and_decider(self, db: psycopg.Connection) -> None:
        incident_id, _ = make_incident(db)
        with pytest.raises(errors.CheckViolation):
            make_approval(
                db,
                incident_id,
                decision="APPROVED",
                decided_at=datetime.now(UTC),
                decided_by=uuid4(),
                reason="",
            )

    def test_only_approved_can_be_consumed(self, db: psycopg.Connection) -> None:
        incident_id, _ = make_incident(db)
        with pytest.raises(errors.CheckViolation, match="only_approved_consumed"):
            make_approval(
                db,
                incident_id,
                decision="REJECTED",
                decided_at=datetime.now(UTC),
                decided_by=uuid4(),
                reason="evidence too weak",
                consumed_at=datetime.now(UTC),
            )

    def test_one_pending_request_per_action(self, db: psycopg.Connection) -> None:
        incident_id, _ = make_incident(db)
        make_approval(db, incident_id)
        with pytest.raises(errors.UniqueViolation):
            make_approval(db, incident_id)

    def test_args_hash_must_be_sha256(self, db: psycopg.Connection) -> None:
        incident_id, _ = make_incident(db)
        with pytest.raises(errors.CheckViolation, match="args_sha256_hex"):
            make_approval(db, incident_id, args_sha256="not-a-hash")


class TestToolCalls:
    def test_consequential_call_needs_approval(self, db: psycopg.Connection) -> None:
        with pytest.raises(errors.CheckViolation, match="consequential_needs_approval"):
            db.execute(
                "INSERT INTO tools.tool_calls (id, on_behalf_of, tool_name, side_effect, input, status) "
                "VALUES (%s, %s, 'restart_service', 'CONSEQUENTIAL', '{}', 'OK')",
                (uuid7(), uuid4()),
            )

    def test_denied_consequential_call_is_recordable(self, db: psycopg.Connection) -> None:
        db.execute(
            "INSERT INTO tools.tool_calls (id, on_behalf_of, tool_name, side_effect, input, status, "
            "denial_reason) VALUES (%s, %s, 'restart_service', 'CONSEQUENTIAL', '{}', 'DENIED', "
            "'no approval')",
            (uuid7(), uuid4()),
        )


class TestAudit:
    def _insert(self, db: psycopg.Connection) -> str:
        audit_id = uuid7()
        db.execute(
            "INSERT INTO audit.audit_events (id, actor, action, resource_type, outcome) "
            "VALUES (%s, 'user:u1', 'approval.decide', 'approval', 'SUCCESS')",
            (audit_id,),
        )
        return str(audit_id)

    def test_append_only_update(self, db: psycopg.Connection) -> None:
        audit_id = self._insert(db)
        with pytest.raises(errors.InsufficientPrivilege, match="append-only"):
            db.execute(
                "UPDATE audit.audit_events SET outcome = 'DENIED' WHERE id = %s", (audit_id,)
            )

    def test_append_only_delete(self, db: psycopg.Connection) -> None:
        audit_id = self._insert(db)
        with pytest.raises(errors.InsufficientPrivilege, match="append-only"):
            db.execute("DELETE FROM audit.audit_events WHERE id = %s", (audit_id,))

    def test_actor_format(self, db: psycopg.Connection) -> None:
        with pytest.raises(errors.CheckViolation, match="actor_format"):
            db.execute(
                "INSERT INTO audit.audit_events (id, actor, action, resource_type, outcome) "
                "VALUES (%s, 'bob', 'x', 'y', 'SUCCESS')",
                (uuid7(),),
            )


class TestPermissionAwareRetrieval:
    """ADR-006: the permission filter is in SQL, and chunk ACLs cannot drift from documents."""

    def _doc(self, db: psycopg.Connection, title: str, groups: list[str], text: str) -> str:
        doc_id = uuid7()
        db.execute(
            "INSERT INTO rag.documents (id, source, source_uri, title, department, owner, version, "
            "content, content_sha256, allowed_groups) VALUES (%s, 'markdown', %s, %s, 'eng', 'team', "
            "'1', %s, %s, %s)",
            (doc_id, f"docs://t/{doc_id}", title, text, SHA, groups),
        )
        db.execute(
            "INSERT INTO rag.document_chunks (id, document_id, chunk_index, content, token_count, "
            "allowed_groups) VALUES (%s, %s, 0, %s, 10, '{forged-group}')",
            (uuid7(), doc_id, text),
        )
        return str(doc_id)

    def test_chunk_acl_comes_from_document_not_from_caller(self, db: psycopg.Connection) -> None:
        self._doc(db, "Security design", ["security-team"], "kms key rotation")
        row = db.execute(
            "SELECT allowed_groups FROM rag.document_chunks WHERE content = 'kms key rotation'"
        ).fetchone()
        assert row and row[0] == ["security-team"]  # the forged group was overwritten

    def test_acl_change_propagates_to_chunks(self, db: psycopg.Connection) -> None:
        doc_id = self._doc(db, "Design", ["eng-all"], "pool sizing guide")
        db.execute(
            "UPDATE rag.documents SET allowed_groups = '{architecture-team}' WHERE id = %s",
            (doc_id,),
        )
        row = db.execute(
            "SELECT allowed_groups FROM rag.document_chunks WHERE document_id = %s", (doc_id,)
        ).fetchone()
        assert row and row[0] == ["architecture-team"]

    def test_engineer_query_never_returns_restricted_chunk(self, db: psycopg.Connection) -> None:
        self._doc(
            db,
            "Internal Security Architecture",
            ["security-team", "architecture-team"],
            "network segmentation secrets rotation design",
        )
        self._doc(db, "Public runbook", ["eng-all"], "segmentation basics for engineers")
        rows = db.execute(
            "SELECT content FROM rag.document_chunks "
            "WHERE tsv @@ plainto_tsquery('english', 'segmentation') AND allowed_groups && %s",
            (["eng-all"],),
        ).fetchall()
        assert [r[0] for r in rows] == ["segmentation basics for engineers"]

    def test_document_without_groups_is_rejected(self, db: psycopg.Connection) -> None:
        with pytest.raises(errors.CheckViolation, match="has_allowed_groups"):
            self._doc(db, "Orphan", [], "no acl")


class TestOrchestrator:
    def test_one_running_investigation_per_incident(self, db: psycopg.Connection) -> None:
        incident = uuid4()
        sql = (
            "INSERT INTO orchestrator.investigations (id, incident_id, requested_by, budget_usd) "
            "VALUES (%s, %s, 'user:u1', 1.0)"
        )
        db.execute(sql, (uuid7(), incident))
        with pytest.raises(errors.UniqueViolation):
            db.execute(sql, (uuid7(), incident))
