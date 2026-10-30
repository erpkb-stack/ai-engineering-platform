# Phase 30 - Architecture review (ADR-048)

Status: **30a BUILT 2026-10-30 (sandbox; needs the Mac run)** - the review, the owner's decisions,
the fixes for the two leaks + the audit/Kafka defects, the docs that showed components that do
not run. Owner (2026-10-30): "Start Phase 30"; scope = **lean review** (what runs, cited against
the measurements of Phases 25-29; no new features); reviews = **independent subagents**;
audit = **keep, fix defects**; Kafka = **keep, safe defaults**; code fixes = **the two leaks**
(+ the audit/Kafka fixes those two answers chose).

## 0. Gate
Phase 29 closed: gate passed (4 real-model demo passes in a row, 041438-170026), the close
package (PR "Phase 29 closed") applied by the owner before 30a is merged.

## 1. Objective and business reason
Find what is unnecessary, wrong or unsafe in what RUNS, decide ADR-011's merge question, and
make the architecture document true. KPI (architecture.md §4): trust - an incident tool whose
audit trail silently stops, or whose postmortem fires a link beacon, is not usable; and the
interview story must survive "show me the 11 services" (9 run).

## 2. Method
Two subagents with the repo's own definitions (`.claude/agents/architecture-critic.md`,
`security-reviewer.md`), read-only, each told to mark every finding FACT/CONFIRMED (file:line)
or HYPOTHESIS/SUSPECTED (what would prove it). I re-checked the top findings in the code; the
two that needed the real system were checked on the Mac (read-only queries, below).

## 3. Findings and what happened to each
| # | finding (source) | checked | disposition |
|---|---|---|---|
| 1 | audit partitions created ONCE (0011); last = 2026-12 on the Mac; from 2027-01 rows go to the default partition and the next creation FAILS (critic) | code + Mac: `y2026m09..m12` | **fixed** (0030 + the service keeps 3 months ahead) |
| 2 | approvals reach audit only over Kafka; transport + consumer OFF by default (critic) | code; Mac `.env` has both on, 81 approval rows | **fixed as a default** (compose/kind on; host runs off, said in `.env.example`) |
| 3 | postmortem markdown: title, root-cause statement, ruled-out list, timeline written raw; T-EXF-2 claimed "escaped" (security #1) | code `postmortem.py:440,460` | **fixed** + test |
| 4 | model text hidden only when the hypothesis' OWN edges are hidden; the models saw everything; a zero-edge hypothesis was never hidden (security #3) | code `service.py:518`; MANAGER reads INCIDENT/DOC only | **fixed** (ADR-045 every-kind rule for all model text) + test |
| 5 | Kafka rows always outcome SUCCESS (critic) | code `consumer.py:42` | **fixed** (failed action = FAILURE) + test |
| 6 | knowledge-service, evaluation: CLAUDE.md only; architecture.md draws them, Kafka flows that do not exist, Redis as shared state (critic, ~18 lines) | `find services/<x>` | **docs fixed**; stubs removed (`git rm`) |
| 7 | 3 Kafka topics with no producer/consumer; `incident.processed_events` never written (critic) | grep | **removed** (topics not created; table dropped in 0030) |
| 8 | webhook rate limit charged before the signature: unsigned junk starves the real sender; bucket map grows per address (security #2) | reviewer traced | **open** (ADR-048 §5, threat model T-AUTH-2) |
| 9 | warm-up does not fail closed if its zero-signature token is accepted (security #5) | code | open |
| 10 | demo harness can be pointed at a non-local host with minted IC tokens (security #4) | code | open (dev-only) |
| 11 | defang misses `Www.` and bare domains (security #8) | code | open (T-EXF-3) |
| 12 | outbox tables never purged; orchestrator resume has no lease (2 replicas double-run) (critic) | grep / code | open (1 replica is a requirement, ADR-034) |
| 13 | alert retries create duplicate incidents (critic, HYPOTHESIS) | **refuted**: one incident per (fingerprint, startsAt); every demo run's re-send is `duplicate` | no change |
| - | some line numbers in the critic's audit-consumer findings were wrong (`consumer.py` has 124 lines) | - | the substance checked, citations corrected here |

Good, one line each (critic): the outbox, idempotent consumer, latched readiness and chaos rules
written before the runs are sound and measured.

## 4. Changes (30a)
- `libs/db/alembic/versions/0030_audit_partitions_kept_by_the_service.py`: `ensure_partitions` v2
  (SECURITY DEFINER, pinned search_path, skip-with-WARNING, EXECUTE only svc_audit, 3 months
  ahead), drop `incident.processed_events`; model removed.
- `services/audit`: `keep_partitions` at start + every 6 h (`AEOI_AUDIT_PARTITION_*`);
  `consumer.outcome_of` (ActionExecuted FAILED -> FAILURE).
- `services/incident-service`: every untrusted markdown field escaped; model text (hypotheses
  + postmortem sentences/action items) needs every evidence kind; negative evidence (code text)
  keeps the per-citation rule.
- `docker-compose.yml`: kafka transport + audit consumer ON by default; `.env.example`;
  `create-topics.sh` + `verify-infra.sh`: only `incident.lifecycle` (+ DLQ).
- Docs: architecture.md (as-built §7/8/9/11, topics, schema, indexes, tree), ADR-048, threat
  model (T-EXF-2/3 extended, T-AUD-1 new), data-model, CLAUDE.md, this file, interview notes.
- Tests: `tests/integration/services/test_audit_partitions.py` (3), `test_model_text_redaction.py`,
  `tests/unit/test_audit_outcome.py`, `test_postmortem_render.py::test_every_untrusted_field_is_escaped_in_the_markdown`;
  `test_k8s_deploy.py` follows the new default. Each new test FAILED on the old code (checked).

## 5. How to verify on the Mac
```
make db-upgrade && make db-check
uv run python - <<'EOF'
import psycopg
from aeoi_db.config import libpq_dsn
c = psycopg.connect(libpq_dsn())
print([r[0] for r in c.execute("select c.relname from pg_inherits i join pg_class c on c.oid = i.inhrelid where i.inhparent = 'audit.audit_events'::regclass order by 1")])
EOF
make images && make demo-fresh && kubectl --context kind-aeoi -n ai-engineering-platform logs deploy/audit | grep audit_partitions
make demo; make demo-clean
```
Expected: partitions up to `y2027m01` (3 months ahead); an `audit_partitions_ensured` log line;
`make demo` PASS (the demo reads hypotheses as SRE/IC - every kind - so nothing it checks is
redacted). `make verify-infra`: 2 topics, `incident.lifecycle has 3 partitions`.

## 6. Failure scenarios
- The keeper cannot reach the DB: a warning, the service keeps serving, the next check (6 h)
  tries again; 3 months of headroom.
- A month's rows already in the default partition: that month is skipped with a WARNING (rows
  stay); the months after it are created. Moving those rows is a manual job (the table is
  append-only).
- Kafka down in a deployed system: the outbox keeps the events (relay retries), audit readiness
  says degraded; `make k8s-apply` refuses to deploy without compose kafka.
- A MANAGER reads a postmortem: the model's sentences are redacted (counted); code-written
  timing, statements and negative evidence stay.

## 7. Production considerations
Kafka mTLS + ACLs (only incident-service may produce to `incident.lifecycle`); audit to WORM
storage; an alert on "next month's partition missing" and on outbox age; verify-then-charge
rate limiting for a public webhook; a resume lease before any replica > 1.

## 8. Verify
- [ ] 30a on the Mac: `make db-upgrade` + `make db-check` clean; partitions to 2027-01; the keeper's log line in the audit pod.
- [ ] CI green on the PR (incl. the 4 new integration tests).
- [ ] `make demo` PASS on the new images (one run is enough here: the gate was Phase 29).
- [ ] The stubs removed (`git rm -r services/knowledge-service services/evaluation`).

## 9. Interview questions (30-second answers in `docs/interview/phase-30-review.md`)
1. You reviewed your own architecture - how do you avoid grading your own homework?
2. What was the most serious thing the review found?
3. You had 11 services. Was that right?
4. Why keep Kafka for one consumer?
5. What did the reviewers get wrong?
6. What did you leave open, and why is that acceptable?
