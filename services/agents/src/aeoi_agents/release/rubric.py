"""Release risk rubric (ADR-025): pure functions, no IO. The band is a RULE over factors, never
a model's number, and a signal that could not be checked counts against the release (unknown is
not safe). Also used by `make risk-scan` (release/scan.py) over the whole synthetic history."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

import aeoi_codeparse as cp

from aeoi_models.api.release import RiskBand, RiskFactor, RollbackStep, Severity

BLAST_DEPENDENTS = 5  # a service this many others depend on is high blast radius
MECHANISM_RULES = ("query_in_loop", "remote_call_in_loop")


def band_of(factors: Iterable[RiskFactor]) -> RiskBand:
    """HIGH: any HIGH factor, or two MEDIUM FINDINGS. MEDIUM: one MEDIUM finding, or a part
    that could not be checked. LOW: only context. "Not checked" raises the band to MEDIUM and
    no further (review: a tier-3 loop query + an unchecked file became HIGH)."""
    fs = list(factors)
    found = [f.severity for f in fs if f.kind != "not_checked"]
    if "HIGH" in found or found.count("MEDIUM") >= 2:
        return "HIGH"
    if "MEDIUM" in found or any(f.kind == "not_checked" for f in fs):
        return "MEDIUM"
    return "LOW"


def findings_band(factors: Iterable[RiskFactor]) -> RiskBand:
    """The band of what WAS checked: every factor except "not checked"."""
    return band_of(f for f in factors if f.kind != "not_checked")


def mechanism_severity(tier: int | None, dependents: int | None) -> Severity:
    """A loop query on a tier-1 or widely used service is HIGH; elsewhere MEDIUM. An UNKNOWN
    blast radius (catalog not reachable) is treated as high: unknown is not safe."""
    if tier is None or dependents is None:
        return "HIGH"
    return "HIGH" if tier == 1 or dependents >= BLAST_DEPENDENTS else "MEDIUM"


def flag_keys(config_keys: Sequence[str]) -> list[str]:
    """The changed config keys that are feature flags (by name: [P])."""
    return [k for k in config_keys if "flag" in k.lower() or "feature" in k.lower()]


def flags_read(flag: str, contents: Iterable[tuple[str, str]]) -> bool:
    """Does any changed SOURCE file use this flag - as an identifier or a string literal, never
    in a comment, never by substring (review: `Enabled` matched every isEnabled, a YAML file
    and a TODO comment counted as reading it)? `contents` = (path, file after the change)."""
    names = cp.flag_variants(flag)
    for path, text in contents:
        lang = cp.language_of(path)
        if lang is not None and text and names & cp.names_and_strings(lang, text):
            return True
    return False


def rollback_plan(
    risky: Sequence[tuple[str, str | None]],
    changed_flags: Sequence[str],
    changed_contents: Sequence[tuple[str, str]],
    *,
    undo: str,
    code_shipped: bool = True,
) -> list[RollbackStep]:
    """How to take the risky part back out. `risky` = (what, flag guard or None) per risky
    finding; `undo` = how this target is undone without a flag (a redeploy of the previous
    commit, or reverting the PR). The Phase 14 lesson: a flag the release CHANGED but the
    changed code does not READ cannot undo the code change - say so explicitly."""
    steps: list[RollbackStep] = []
    for what, guard in risky:
        if guard:
            steps.append(RollbackStep(kind="flag_off", statement=(
                f"{what} runs only when `{guard}` is true: switching that flag off takes it out "
                "without a deploy.")[:400]))  # fmt: skip
        else:
            steps.append(RollbackStep(kind="redeploy", statement=(
                f"{what} is not behind any feature flag: only {undo} takes it out.")[:400]))  # fmt: skip
    for flag in changed_flags:
        if not code_shipped:  # review: a config-only deploy - the flag IS the change
            steps.append(RollbackStep(kind="flag_off", statement=(
                f"{flag} changed and no new code shipped: switching it back undoes this "
                "deploy's change.")[:400]))  # fmt: skip
        elif flags_read(flag, changed_contents):
            steps.append(RollbackStep(kind="flag_off", statement=(
                f"{flag} changed in this release and is read by the changed code: switching it "
                "back changes that code's behaviour.")[:400]))  # fmt: skip
        else:
            steps.append(RollbackStep(kind="flag_not_read", statement=(
                f"{flag} changed in this release but the changed code does not read it: "
                "switching it back does NOT undo the code change.")[:400]))  # fmt: skip
    if not steps:
        steps.append(RollbackStep(kind="none", statement="Nothing in this change needs a "
                                  "specific way back: no risky finding, no flag changed."))  # fmt: skip
    return steps


@dataclass
class Hit:
    """One diff-rule finding, with what the IO side found out about it."""

    rule: str
    detail: str
    symbol: str
    where: str  # "x() in priceCart (CartService.java:18)" - for the rollback text
    guard: str | None
    evidence_id: str
    file: str = ""
    consumers: list[str] | None = None  # field_removed: other services reading it (None = unknown)
    consumer_evidence: list[str] = field(default_factory=list)


def assess(
    hits: Sequence[Hit],
    *,
    service_key: str,
    tier: int | None,
    dependents: Sequence[str] | None,
    catalog_evidence: str | None,
    config_keys: Sequence[str],
    deploy_evidence: str | None,
    not_checked: Sequence[str],
    contents: Sequence[tuple[str, str]],
    undo: str,
) -> tuple[list[RiskFactor], RiskBand, list[RollbackStep]]:
    """The whole rule, pure: findings + blast radius + config -> factors, band, way back.
    Shared by the agent and `make risk-scan`, so the scan measures the rule that runs."""
    factors: list[RiskFactor] = []
    risky: list[tuple[str, str | None]] = []
    seen: set[tuple[str, str, str]] = set()  # one loop finding per (rule, call, place)
    if dependents is not None and tier is not None:
        factors.append(RiskFactor(kind="blast_radius", severity="context", statement=(
            f"{service_key} is tier {tier} with {len(dependents)} dependent service(s)"
            + (f" ({', '.join(dependents[:6])}{', ...' if len(dependents) > 6 else ''})"
               if dependents else "") + "."),
            evidence_ids=[catalog_evidence] if catalog_evidence else []))  # fmt: skip
    n_dep = len(dependents) if dependents is not None else None
    for h in hits:
        if h.rule in MECHANISM_RULES:
            if (h.rule, h.symbol, h.where) in seen:
                continue  # the same call twice in one method is one mechanism, not two
            seen.add((h.rule, h.symbol, h.where))
            factors.append(RiskFactor(kind="code_mechanism",
                severity=mechanism_severity(tier, n_dep), statement=(
                h.detail + (f" Behind flag `{h.guard}`." if h.guard else "")
                + (" Unknown blast radius: rated as high." if tier is None else
                   f" On a tier-{tier} service with {n_dep} dependent(s).")),
                evidence_ids=[h.evidence_id]))  # fmt: skip
            risky.append((h.where, h.guard))
        elif h.rule == "limit_changed":
            factors.append(RiskFactor(kind="limit_change", severity="context",
                                      statement=f"{h.detail} ({h.file}).",
                                      evidence_ids=[h.evidence_id]))  # fmt: skip
        elif h.rule == "field_removed" and h.consumers is not None:
            ids = [h.evidence_id, *h.consumer_evidence][:10]
            if h.consumers:
                factors.append(RiskFactor(kind="breaking_change", severity="HIGH", statement=(
                    f"{h.detail}. {len(h.consumers)} other service(s) still read it and will "
                    f"break: {', '.join(h.consumers[:8])}.")[:500], evidence_ids=ids))  # fmt: skip
                risky.append((f"the removal of {h.symbol}", None))
            else:
                factors.append(RiskFactor(kind="breaking_change", severity="context", statement=(
                    f"{h.detail}. No other service in code every engineer may read uses it."),
                    evidence_ids=ids))  # fmt: skip
    if config_keys:
        factors.append(RiskFactor(kind="config_change", severity="context",
                                  statement=f"Config changed with this deploy: {', '.join(config_keys)}.",
                                  evidence_ids=[deploy_evidence] if deploy_evidence else []))  # fmt: skip
    if not_checked:
        factors.append(RiskFactor(kind="not_checked", severity="MEDIUM", statement=(
            f"{len(not_checked)} part(s) of this release could not be checked - unknown is not "
            f"safe: {'; '.join(not_checked[:4])}")[:500]))  # fmt: skip
    factors = factors[:40]
    return factors, band_of(factors), rollback_plan(risky, flag_keys(config_keys), contents,
                                                    undo=undo)[:20]  # fmt: skip
