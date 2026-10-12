# ADR-025: Advisory release risk by code rules: a band over cited factors, coverage reported apart, and a way back that says when a flag flip would NOT help

Status: Proposed
Date: 2026-10-12
Refines: ADR-024 (diff rules, change impact), ADR-019 (tokens), ADR-021 (rubric bands); architecture.md FR-8 ("release risk analysis for a PR/RC - advisory, never blocks deploys")

## Context
Phase 14 explains a deploy AFTER an incident. FR-8 asks the same question BEFORE: is this PR or
release candidate risky? The first Phase 14 run on the Mac added a second reason: the critic
and the postmortem writer proposed "set order_batching back to false" as a mitigation, but the
per-item query in the diff is not behind that flag - the advice would not have helped.
Constraints:
1. No ground truth: synthetic deploys are random and NOT linked to the 600 past incidents.
   Only two changes are known-bad (PR #812, PR #377). Any accuracy number would be invented.
2. Advisory only: nothing may read a report to gate a deploy.
3. A report quotes code: readable with `code:read` (+ `deploys:read`), never by MANAGER.

Owner decisions (2026-10-12): target = **PR or commit range** (a deploy key is also accepted, to
ask "would it have been flagged?"); **code only**; score = **rubric band + labelled cases, no
accuracy claim**; **stored report + API**. Gate: PR #15 merged (9433be9), Phase 14 verified.

## Decision
```
api POST /api/v1/release-risk (code:read) ─► orchestrator POST /v1/release-risk (code:read + deploys:read)
   ─► agents release_risk (caller's OWN token as on-behalf-of, synchronous, 10 s deadline per call)
        target: PR -> merge commit | range: service + base + head | deploy -> previous successful deploy..it
        compare_commits ─► Phase 14 rules (code.agent.scan_commit: one place decides what a finding is)
        query_service_catalog (tier, dependents) · find_references (removed field, shared code only)
        rubric.assess (pure): factors -> band, findings_band, coverage, way back
   ─► orchestrator.release_assessments (one row per request, migration 0023) ─► 201 + report
```
### A. Factors (each a code template citing the evidence it came from)
| Factor | Severity |
|---|---|
| `code_mechanism`: a query / remote call added inside a loop | HIGH on tier 1 or ≥ 5 dependents or UNKNOWN blast radius; else MEDIUM |
| `breaking_change`: a field removed/renamed that other services still read | HIGH; context when nobody else reads it |
| `not_checked`: anything not checked (no base, too long, diff not fitting, > 10 commits, > 20 files, references capped, deadline, catalog down) | MEDIUM (raises to MEDIUM, never to HIGH) |
| `limit_change`, `blast_radius`, `config_change`, `nothing_shipped` | context (never move the band) |
Band: HIGH = a HIGH finding or two MEDIUM findings; MEDIUM = one MEDIUM finding or something
not checked; LOW = context only. A report also carries `findings_band` (risk FOUND in what was
checked) and `coverage` (full | partial), so "we found a problem" and "we could not see" are
never the same word.

### B. The way back (code decides, from the diff and the deploy)
- the risky call sits in the THEN branch of ONE positive flag check (`flags.isEnabled("x")`,
  `FEATURE_X`) → `flag_off`: switching it off takes it out without a deploy;
- otherwise → `redeploy` (deploy target), "not shipping" (range), "reverting PR #n" (PR);
- a flag the release CHANGED that no changed source file reads (identifiers and string literals,
  never comments, never substrings) → `flag_not_read`: "switching it back does NOT undo the code
  change" - the Phase 14 lesson, now computed;
- a deploy that shipped no new code but changed a flag → `flag_off`: the flag IS the change.

### C. Synchronous, bounded, recorded
Code only and a handful of tool calls: synchronous (api 15 s ⟩ orchestrator 12 s ⟩ agent 10 s,
each tool call bounded by what is left). This breaks the api rule "long-running work is 202"
on purpose: the work is bounded and short; a 202 + polling would add a status resource for
seconds of work. The caller's own token goes to the agent as on-behalf-of (never stored, like
an investigation start); a token delegated to an investigation is refused (ADR-019).
A request that reached the agent is stored, FAILED included (with the reason); agents
unreachable = 503 and nothing stored. Idempotency-Key per requester: a retry returns the first
report (200), the same key for another target is 409, another user's key never matches.

## Selectivity and labelled cases (`make risk-scan`, offline, the same rubric code)
Over every synthetic deploy (1,784, seed 42, sandbox):
| | count |
|---|---|
| HIGH (both planted changes, nothing else) | 2 |
| MEDIUM, ONLY because part could not be checked (first deploy in the 60-day window: no base) | 102 |
| LOW | 1,680 |
| if a limit change counted as MEDIUM (mutation) | 182 HIGH + 546 MEDIUM = **40.8 % flagged** |
6 hand-labelled cases (`data/eval/release_risk_cases_v1.jsonl`): both planted changes, a routine
deploy, a limit-only deploy, a redeploy, a first deploy - all as labelled. **What this is and is
not:** the labels are the author's, by construction from the generator; they lock behaviour and
catch regressions (the limit-change mutation fails 3 checks). They are not accuracy. The only
risky code in the synthetic history is planted, so "2 of 1,784" says the rule is quiet on
template code, not that it would be quiet on real code.

## Review (independent, read-only subagent) - 2 high, 6 medium, 6 low
The first version of this phase was an unreviewed draft (an interrupted earlier attempt);
reviewed in full before the owner saw it. Fixed with tests (each mutation-checked):
H1 a capped `find_references` (50 references inside the service itself) read as "nobody else
uses it" → LOW; H2 `!flags.isEnabled(x)`, `flag || legacy`, `i.isFlagged()`, `featureCount > 0`
were taken as guards → the advice was INVERTED; M3 flag "read" by substring (`Enabled` in every
isEnabled, a YAML file, a TODO comment); M4 a config-only redeploy dropped its config change and
said a flag flip "does NOT undo" a change that was only the flag; M5 a swapped range was a quiet
LOW; M6 the deadline was checked once → a slow tool ended in 503 + nothing stored; M8 "two
MEDIUM" counted "not checked" and duplicate hits; plus the gateway's silent 20-files cut and
output cap now count as not checked, cited evidence is kept first under the cap, `checked` lists
only what ran, a CHECK violation is no longer swallowed as an idempotency race, PR evidence.
**Not fixed (documented):** M7 - `compare_commits` has no repository ACL (Phase 14 gap), so
commit hunks from a repo restricted in rag's ACL would land in a shared report; latent (every
synthetic repo is `eng-all`); a real code host must enforce it in the tool.

## First Mac run (2026-10-12) and fix1
`make risk-smoke` 19/19 and `make risk-scan` identical to the sandbox (2 HIGH, 102 MEDIUM, 1,680
LOW; 6/6 labelled). The DEPLOY-4821 way back read: "only a redeploy of 0742889fcf63 takes it
out" + "switching order_batching back does NOT undo the code change".
`make code-smoke` (regression) failed once: the CRITIC (Sonnet) wrote a `missing` sentence over
the schema's maxLength 200; the gateway's one repair was too long again; the whole critique was
lost (PARTIAL, named - the failure handling worked). Not a Phase 15 change, but a real defect:
since Phase 14 the critic explains a code mechanism and its sentences grew. fix1: every model
free-text field (critic, ranker, postmortem) is bounded in the schema at 1,200 characters
(runaway only) and cut in code to the stored size at a word, with "…" (`common.clip`). Same
lesson as Phase 13's `max_tokens`: a size limit must cost characters, never the answer.
After fix1 on the Mac: `make code-smoke` 21/21, `make hypo-smoke` ✔, `make pm-smoke` 22/22,
integration 228 passed + 1 skipped (6 min 43 s). One green run after a model-dependent failure
proves little on its own; the schema test is what keeps the defect from coming back.

## Alternatives
| Option | Why not |
|---|---|
| A model judges risk / writes the summary | owner chose code only; nothing a model adds here is checkable |
| Backtest on planted deploy→incident links | measures our own generator (circular) |
| Weighted 0-100 score | weights are guesses with no data to fit them |
| Limit changes as risk | 40.8 % of releases flagged: a warning everyone learns to ignore |
| "Not checked" as LOW | unknown is not safe; but it never reaches HIGH alone |
| 202 + polling | a status resource for seconds of bounded, code-only work |
| A new release service | the orchestrator already runs agents AS THE USER and records results |

## Tradeoffs
- `make risk-scan` rebuilds the tool view from the generated data (same functions, not the same
  IO path); the agent's IO path is covered by the e2e tests.
- Two concurrent requests with the same key both run the agent (both audited); one row is kept.
- Flag detection is by name; a flag read into a local variable first is not seen → the advice is
  a redeploy (the safe side).

## Prototype vs Production vs Enterprise-scale
[P] synthetic repos, simple-name rules, one rubric. [Prod] run on every PR from the code host
(webhook), post the report as a PR comment (still advisory), measure with real outcomes (did
HIGH PRs cause incidents? Phase 25), repository ACL in `compare_commits`. [Ent] per-team rubric
weights fitted on outcome data, deploy-pipeline annotation (never a gate without an ADR).
