"""Phase 25a (ADR-037): investigation scenario pack v1 - incidents with a PLANTED cause.

The main dataset has ONE investigable scenario (the demo). An accuracy number over one case
means nothing, so this pack plants 16 more. Pure function of the seed: no IO, no clock.

Every scenario gets its own service and its own day AFTER the demo day: the main dataset has
no deploys, logs or metrics there, so nothing in the pack overlaps the demo or another case.

The ground truth (`expected`) is written down HERE, from what was planted - never from what
the system answers. It goes to eval.eval_cases only; no agent or tool can read that schema.
Three kinds of case:
- detectable: the cause is a signal the system models (a deploy, a saturation metric, a
  traffic change). Scored: is the top hypothesis that cause?
- detectable with a decoy: a later deploy, another service's deploy, a saturation that is a
  CONSEQUENCE. Scored the same, plus: did the decoy outrank the cause?
- undetectable: the cause leaves no signal the system models (certificate, DNS, disk, a
  downstream service). The right answer is "no confident cause". Scored: was a HIGH
  hypothesis claimed anyway (a confident wrong answer - the worst error)?
Scenarios I wrote measure how well the system finds causes I planted: an UPPER bound, not
real-world accuracy.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import NAMESPACE_URL, uuid5

PACK_NAME = "investigations"
PACK_VERSION = 1
FIRST_DAY = datetime(2026, 10, 6, 14, 0, tzinfo=UTC)  # the demo day is 2026-10-02
BEFORE = timedelta(minutes=200)  # covers window (60 min) + metrics baseline (120 min) + slack
AFTER = timedelta(minutes=30)  # the orchestrator's window ends 30 min after detection
BASE = {
    "http_5xx_per_min": 40.0,
    "p95_latency_ms": 180.0,
    "requests_per_sec": 250.0,
    "cpu_util": 0.45,
    "db_pool_utilization": 0.55,
    "thread_pool_utilization": 0.40,
}
CAP = {"cpu_util": 0.99, "db_pool_utilization": 1.0, "thread_pool_utilization": 1.0}


@dataclass
class Scenario:
    key: str
    category: str
    service: str
    services: list[str]
    detected_at: datetime
    title: str
    expected: dict[str, Any]
    deployments: list[dict[str, Any]] = field(default_factory=list)
    logs: list[dict[str, Any]] = field(default_factory=list)
    metrics: list[dict[str, Any]] = field(default_factory=list)

    def case_input(self) -> dict[str, Any]:
        """What the runner sends - exactly what an on-call engineer would type."""
        return {
            "title": self.title,
            "severity": "SEV2",
            "affected_services": self.services,
            "detected_at": self.detected_at.isoformat(),
        }


@dataclass(frozen=True)
class Spec:
    """One scenario, in minutes relative to detection (negative = before)."""

    key: str
    category: str
    title: str
    detectable: bool
    culprit: tuple[str, str] | None  # (cause_kind, subject); subject "#n" = the n-th deploy
    deploys: tuple[tuple[int, dict[str, Any], int], ...] = ()  # (minute, config_diff, service#)
    shifts: tuple[tuple[str, int, float], ...] = ()  # (metric, minute, factor) on service 0
    errors: tuple[tuple[str, str, int], ...] = ()  # (error_code, message, minute) on service 0
    other_shifts: tuple[tuple[str, int, float], ...] = ()  # on service 1
    decoys: tuple[str, ...] = ()  # subjects that must NOT be the top hypothesis
    rule_out: tuple[str, ...] = ()  # subjects that must end up REJECTED
    two_services: bool = False


SYMPTOMS = (("http_5xx_per_min", -10, 2.5), ("p95_latency_ms", -11, 2.4))
POOL_LOG = ("ERR_POOL_TIMEOUT", "Timed out acquiring a DB connection from the pool (waited=5000ms)")

SPECS: tuple[Spec, ...] = (
    Spec(
        "S01",
        "connection_pool_exhaustion",
        "{svc} 5xx and DB pool timeouts after deploy",
        True,
        ("change", "#0"),
        deploys=((-17, {}, 0),),
        shifts=(*SYMPTOMS, ("db_pool_utilization", -12, 1.8)),
        errors=((*POOL_LOG, -10),),
    ),
    Spec(
        "S02",
        "bad_config_change",
        "{svc} errors after configuration change",
        True,
        ("change", "#0"),
        deploys=((-16, {"http.client.timeout_ms": [2000, 0]}, 0),),
        shifts=SYMPTOMS[:1],
        errors=(("ERR_CONFIG_INVALID", "Rejected request: client timeout is 0", -10),),
    ),
    Spec(
        "S03",
        "feature_flag_regression",
        "{svc} 5xx spike after release",
        True,
        ("change", "#0"),
        deploys=((-15, {"feature_flags.new_pricing_path": [False, True]}, 0),),
        shifts=SYMPTOMS,
        errors=(("ERR_UNHANDLED_EXCEPTION", "NullPointerException in PricingPath.apply", -10),),
    ),
    Spec(
        "S04",
        "connection_pool_exhaustion",
        "{svc} 5xx after deploy (a later hotfix too)",
        True,
        ("change", "#0"),
        deploys=((-18, {}, 0), (-3, {}, 0)),
        shifts=(*SYMPTOMS, ("db_pool_utilization", -12, 1.8)),
        errors=((*POOL_LOG, -10),),
        decoys=("#1",),
        rule_out=("#1",),
    ),
    Spec(
        "S05",
        "bad_config_change",
        "{svc} errors; two services deployed this morning",
        True,
        ("change", "#0"),
        deploys=((-16, {"cache.ttl_s": [300, 0]}, 0), (-24, {}, 1)),
        shifts=SYMPTOMS[:1],
        errors=(
            ("ERR_CACHE_DISABLED", "Cache bypassed: ttl=0, every read hits the database", -10),
        ),
        decoys=("#1",),
        two_services=True,
    ),
    Spec(
        "S06",
        "connection_pool_exhaustion",
        "{svc} slow and failing, no recent deploy",
        True,
        ("saturation", "db_pool_utilization"),
        shifts=(("db_pool_utilization", -18, 1.8), *SYMPTOMS),
        errors=((*POOL_LOG, -10),),
        rule_out=("requests_per_sec",),
    ),
    Spec(
        "S07",
        "thread_pool_saturation",
        "{svc} latency spike under normal load",
        True,
        ("saturation", "thread_pool_utilization"),
        shifts=(("thread_pool_utilization", -18, 2.3), SYMPTOMS[1]),
        errors=(("ERR_EXECUTOR_REJECTED", "Task rejected: request executor queue full", -9),),
        rule_out=("requests_per_sec",),
    ),
    Spec(
        "S08",
        "cpu_saturation",
        "{svc} latency up, CPU pegged",
        True,
        ("saturation", "cpu_util"),
        shifts=(("cpu_util", -18, 2.0), SYMPTOMS[1]),
        rule_out=("requests_per_sec",),
    ),
    Spec(
        "S09",
        "traffic_surge",
        "{svc} errors during a traffic spike",
        True,
        ("traffic", "requests_per_sec"),
        shifts=(("requests_per_sec", -18, 3.0), *SYMPTOMS),
        errors=(("ERR_RATE_LIMITED", "Upstream rate limit reached", -9),),
    ),
    Spec(
        "S10",
        "connection_pool_exhaustion",
        "{svc} 5xx after deploy; pool full later",
        True,
        ("change", "#0"),
        deploys=((-17, {}, 0),),
        shifts=(*SYMPTOMS, ("db_pool_utilization", -4, 1.8)),
        errors=((*POOL_LOG, -10),),
        decoys=("db_pool_utilization",),
        rule_out=("db_pool_utilization",),
    ),
    Spec(
        "S11",
        "certificate_expiry",
        "{svc} TLS handshake failures",
        False,
        None,
        shifts=SYMPTOMS[:1],
        errors=(("ERR_TLS_CERT_EXPIRED", "TLS handshake failed: certificate has expired", -10),),
        rule_out=("requests_per_sec",),
    ),
    Spec(
        "S12",
        "dns_failure",
        "{svc} cannot reach dependencies",
        False,
        None,
        shifts=SYMPTOMS[:1],
        errors=(("ERR_DNS_RESOLUTION", "DNS resolution failed for payments.internal", -10),),
    ),
    Spec(
        "S13",
        "disk_full",
        "{svc} write errors",
        False,
        None,
        shifts=SYMPTOMS[:1],
        errors=(("ERR_DISK_FULL", "No space left on device while writing the spool", -10),),
    ),
    Spec(
        "S14",
        "certificate_expiry",
        "{svc} TLS failures after a log-level change",
        False,
        None,
        deploys=((-16, {"logging.level": ["INFO", "DEBUG"]}, 0),),
        shifts=SYMPTOMS[:1],
        errors=(("ERR_TLS_CERT_EXPIRED", "TLS handshake failed: certificate has expired", -10),),
        decoys=("#0",),
    ),
    Spec(
        "S15",
        "memory_leak",
        "{svc} pods restarting (OOM)",
        True,
        ("change", "#0"),
        deploys=((-170, {}, 0),),
        shifts=SYMPTOMS,
        errors=(("ERR_OUT_OF_MEMORY", "Container killed: OOMKilled (limit 512Mi)", -10),),
    ),
    Spec(
        "S16",
        "dependency_timeout",
        "{svc} degraded; downstream slow",
        False,
        None,
        shifts=SYMPTOMS[:1],
        errors=(("ERR_UPSTREAM_TIMEOUT", "Timed out calling the downstream service", -10),),
        other_shifts=(("p95_latency_ms", -16, 3.0),),
        two_services=True,
    ),
)
SERVICES = (
    "orders-api", "payments-api", "inventory-api", "cart-api", "catalog-api", "search-api",
    "pricing-api", "shipping-api", "returns-api", "notifications-api", "identity-api",
    "accounts-api", "billing-api", "ledger-api", "fraud-api", "reviews-api",
)  # fmt: skip
PARTNERS = ("media-api", "tax-api")  # second services of S05 / S16, nowhere else


def _metric_rows(
    rnd: random.Random, svc: str, d: datetime, shifts: tuple[tuple[str, int, float], ...]
) -> list[dict[str, Any]]:
    rows = []
    t = d - BEFORE
    while t <= d + AFTER:
        for name, base in BASE.items():
            v = base * (1 + rnd.uniform(-0.05, 0.05))
            for metric, minute, factor in shifts:
                if metric == name and t >= d + timedelta(minutes=minute):
                    v = min(v * factor, CAP.get(name, v * factor))
            rows.append({"service_key": svc, "metric": name, "ts": t, "value": round(v, 4)})
        t += timedelta(minutes=1)
    return rows


def _log_rows(
    rnd: random.Random, svc: str, d: datetime, errors: tuple[tuple[str, str, int], ...]
) -> list[dict[str, Any]]:
    rows = []
    t = d - BEFORE
    while True:  # quiet background: INFO only, so error codes come from the plant
        t += timedelta(seconds=rnd.randint(200, 520))
        if t >= d + AFTER:  # never past the range: `make eval-seed` deletes by it (test)
            break
        rows.append(_log(svc, t, "INFO", "Request batch processed", None))
    for code, message, minute in errors:
        t = d + timedelta(minutes=minute)
        while t < d + AFTER:
            rows.append(_log(svc, t, "ERROR", message, code))
            t += timedelta(seconds=rnd.randint(6, 15))
    return sorted(rows, key=lambda r: r["ts"])


def _log(svc: str, ts: datetime, level: str, msg: str, code: str | None) -> dict[str, Any]:
    tid = uuid5(NAMESPACE_URL, f"aeoi:eval:trace:{svc}:{ts.isoformat()}").hex
    return {"ts": ts, "service_key": svc, "level": level, "message": msg, "error_code": code,
            "trace_id": tid, "attributes": {}}  # fmt: skip


def _resolve(subject: str, deploy_keys: list[str]) -> str:
    return deploy_keys[int(subject[1:])] if subject.startswith("#") else subject


def build(seed: int = 42) -> list[Scenario]:
    out = []
    for i, s in enumerate(SPECS):
        rnd = random.Random(f"{seed}:{s.key}")
        d = FIRST_DAY + timedelta(days=i)
        svc = SERVICES[i]
        services = [svc, PARTNERS[0 if s.key == "S05" else 1]] if s.two_services else [svc]
        keys = [f"DEPLOY-{9100 + 10 * i + n}" for n in range(len(s.deploys))]
        deps = [
            {
                "id": uuid5(NAMESPACE_URL, f"aeoi:eval:deploy:{k}"),
                "deploy_key": k,
                "service_key": services[who],
                "version": f"{(d + timedelta(minutes=minute)):%Y.%m.%d}.{n + 1}",
                "environment": "production",
                "status": "SUCCEEDED",
                "commit_sha": None,  # the loader takes the service's newest real commit
                "deployed_by": "cd-bot",
                "config_diff": diff,
                "started_at": d + timedelta(minutes=minute),
                "finished_at": d + timedelta(minutes=minute + 4),
            }
            for n, (k, (minute, diff, who)) in enumerate(zip(keys, s.deploys, strict=True))
        ]
        metrics = _metric_rows(rnd, svc, d, s.shifts)
        logs = _log_rows(rnd, svc, d, s.errors)
        if s.two_services:
            metrics += _metric_rows(rnd, services[1], d, s.other_shifts)
            logs += _log_rows(rnd, services[1], d, ())
        culprit = (
            {"kind": s.culprit[0], "subject": _resolve(s.culprit[1], keys)} if s.culprit else None
        )
        out.append(
            Scenario(
                key=f"{PACK_NAME}-v{PACK_VERSION}-{s.key}",
                category=s.category,
                service=svc,
                services=services,
                detected_at=d,
                title=s.title.format(svc=svc),
                expected={
                    "detectable": s.detectable,
                    "category": s.category,
                    "culprit": culprit,
                    "decoys": [_resolve(x, keys) for x in s.decoys],
                    "rule_out": [_resolve(x, keys) for x in s.rule_out],
                },
                deployments=deps,
                logs=logs,
                metrics=metrics,
            )
        )
    return out


def pack_range() -> tuple[datetime, datetime]:
    """Every row of the pack has its time in here, and the main dataset has nothing here."""
    return FIRST_DAY - BEFORE, FIRST_DAY + timedelta(days=len(SPECS) - 1) + AFTER
