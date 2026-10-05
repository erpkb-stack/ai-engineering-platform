from collections import Counter
from functools import cache

import pytest

from aeoi_synth.generate import (
    DEMO_DB,
    DEMO_DECOY,
    DEMO_DEPLOY_KEY,
    DEMO_SERVICE,
    SIMILAR_HISTORY,
    Dataset,
    Scale,
    T,
    generate,
)


@cache
def full() -> Dataset:
    return generate(42)


@cache
def small(seed: int = 42) -> Dataset:
    return generate(seed, Scale.small())


def test_deterministic_same_seed() -> None:
    assert small(42).fingerprint() == generate(42, Scale.small()).fingerprint()


def test_different_seed_differs() -> None:
    assert small(42).fingerprint() != small(7).fingerprint()


def test_spec_minimum_volumes() -> None:
    c = full().counts()
    assert c["catalog.services"] >= 100
    assert c["rag.historical_incidents"] >= 500
    assert c["rag.documents"] >= 1000
    for table in (
        "devdata.log_events",
        "devdata.metric_points",
        "devdata.deployments",
        "devdata.commits",
        "devdata.pull_requests",
        "rag.runbooks",
    ):
        assert c[table] > 0, table


def test_catalog_dependencies_resolve_and_are_acyclic() -> None:
    services = {s["key"]: s for s in full().catalog}
    for s in services.values():
        assert set(s["depends_on"]) <= services.keys(), s["key"]
    state: dict[str, int] = {}  # 1 = visiting, 2 = done

    def visit(key: str) -> None:
        assert state.get(key) != 1, f"cycle through {key}"
        if state.get(key) == 2:
            return
        state[key] = 1
        for dep in services[key]["depends_on"]:
            visit(dep)
        state[key] = 2

    for key in services:
        visit(key)


@pytest.mark.parametrize(
    ("table", "key"),
    [
        ("devdata.deployments", "deploy_key"),
        ("devdata.commits", "sha"),
        ("rag.historical_incidents", "incident_key"),
        ("identity.users", "email"),
        ("rag.documents", "source_uri"),
    ],
)
def test_unique_business_keys(table: str, key: str) -> None:
    values = [r[key] for r in full().tables[table]]
    dupes = [v for v, n in Counter(values).items() if n > 1]
    assert not dupes, dupes[:5]


def test_referential_integrity() -> None:
    t = full().tables
    shas = {c["sha"] for c in t["devdata.commits"]}
    assert all(d["commit_sha"] in shas for d in t["devdata.deployments"])
    assert all(p["merge_commit_sha"] in shas for p in t["devdata.pull_requests"])
    doc_ids = {d["id"] for d in t["rag.documents"]}
    assert all(r["document_id"] in doc_ids for r in t["rag.runbooks"])
    user_ids = {u["id"] for u in t["identity.users"]}
    assert all(r["user_id"] in user_ids for r in t["identity.user_roles"])
    groups = {g["name"] for g in t["identity.groups"]}
    assert all(r["group_name"] in groups for r in t["identity.user_groups"])
    service_keys = {s["key"] for s in full().catalog}
    assert all(d["service_key"] in service_keys for d in t["devdata.deployments"])
    assert all(m["service_key"] in service_keys for m in t["devdata.metric_points"])


def test_every_document_has_allowed_groups() -> None:
    assert all(d["allowed_groups"] for d in full().tables["rag.documents"])


class TestPlantedDemo:
    def test_deploy_and_commit(self) -> None:
        t = full().tables
        deploy = next(d for d in t["devdata.deployments"] if d["deploy_key"] == DEMO_DEPLOY_KEY)
        assert deploy["service_key"] == DEMO_SERVICE
        assert deploy["config_diff"]["feature_flags.order_batching"] == [False, True]
        commit = next(c for c in t["devdata.commits"] if c["sha"] == deploy["commit_sha"])
        assert any("OrderRepository" in f for f in commit["files_changed"])

    def _first_change(self, svc: str, metric: str, threshold: float) -> object:
        points = sorted(
            (
                m
                for m in full().tables["devdata.metric_points"]
                if m["service_key"] == svc and m["metric"] == metric and m["ts"] >= T(hour=9)
            ),
            key=lambda m: m["ts"],
        )
        return next(m["ts"] for m in points if m["value"] > threshold)

    def test_signal_order_supports_the_critic(self) -> None:
        """App latency rises BEFORE errors, and errors BEFORE DB latency (critic evidence)."""
        app_latency = self._first_change(DEMO_SERVICE, "p95_latency_ms", 300)
        errors = self._first_change(DEMO_SERVICE, "http_5xx_per_min", 52)
        db_latency = self._first_change(DEMO_DB, "db_query_latency_ms", 13.6)
        pool = self._first_change(DEMO_SERVICE, "db_pool_utilization", 0.95)
        assert app_latency < errors < db_latency
        assert pool <= errors

    def test_pool_timeout_logs_after_deploy(self) -> None:
        logs = [
            log
            for log in full().tables["devdata.log_events"]
            if log["error_code"] == "ERR_POOL_TIMEOUT"
        ]
        assert len(logs) > 50
        assert min(log["ts"] for log in logs) > T(hour=9, minute=44)

    def test_decoy_present(self) -> None:
        assert any(
            log["service_key"] == DEMO_DECOY and "restarted" in log["message"]
            for log in full().tables["devdata.log_events"]
        )

    def test_similar_history(self) -> None:
        keys = {h["incident_key"] for h in full().tables["rag.historical_incidents"]}
        assert set(SIMILAR_HISTORY) <= keys

    def test_restricted_and_adversarial_docs(self) -> None:
        docs = full().tables["rag.documents"]
        restricted = [d for d in docs if d["title"] == "Internal Security Architecture"]
        assert restricted and "eng-all" not in restricted[0]["allowed_groups"]
        assert any("ignore previous instructions" in d["content"] for d in docs)

    def test_runbook_db_012(self) -> None:
        rb = next(r for r in full().tables["rag.runbooks"] if r["runbook_key"] == "RUNBOOK-DB-012")
        assert rb["version"] == "8.4"
        assert len(rb["steps"]) == 5
