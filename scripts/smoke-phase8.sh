#!/usr/bin/env bash
# Phase 8 smoke: Log Analysis agent end to end on the seeded demo (checkout-api, DEPLOY-4821).
# Needs running: make dev (api+incident), make run-llm, make run-tools, make run-audit, make run-agents,
# and (Phase 9) make run-orch: `agent-run` now goes through the orchestrator service.
# macOS bash 3.2 compatible. Exit 1 on any failure. ROUTE=local|fast picks the model route.
set -u
API="${AEOI_API_URL:-http://localhost:8000}"
pass=0; fail=0; note=0
ok()   { echo "✔ $1"; pass=$((pass+1)); }
bad()  { echo "✘ $1"; fail=$((fail+1)); }
warn() { echo "! $1"; note=$((note+1)); }
json() { python3 -c "import sys,json; d=json.load(sys.stdin); print($1)"; }
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

for pair in "api 8000" "incident 8001" "orchestrator 8002" "agents 8003" "llm-gateway 8005" "tool-gateway 8006"; do
  set -- $pair
  code=$(curl -s -o /dev/null -w '%{http_code}' "localhost:$2/health/live")
  [ "$code" = "200" ] && ok "$1 up" || { bad "$1 not running on :$2"; exit 1; }
done
[ -s secrets/orchestrator_service_token.txt ] && [ -s secrets/agents_service_token.txt ] \
  && ok "service tokens present" || { bad "missing tokens - run: make agents-tokens"; exit 1; }

SRE="$(make -s token ROLE=SRE 2>/dev/null)"
code=$(curl -s -o "$TMP/inc.json" -w '%{http_code}' -X POST "$API/api/v1/incidents" \
  -H "Authorization: Bearer $SRE" -H "Content-Type: application/json" -H "Idempotency-Key: smoke8-$(date +%s)" \
  -d '{"title":"Checkout 5xx after deploy (phase 8 smoke)","severity":"SEV2","affected_services":["checkout-api"],"detected_at":"2026-10-02T09:50:00Z"}')
[ "$code" = "201" ] || { bad "create incident: $code $(head -c 300 "$TMP/inc.json")"; exit 1; }
KEY=$(json "d['key']" < "$TMP/inc.json"); ok "demo incident $KEY (detected 2026-10-02 09:50Z)"

echo "  running the Log Analysis agent (local CPU model can take ~1 min)..."
if make -s agent-run INCIDENT="$KEY" ${ROUTE:+ROUTE=$ROUTE} > "$TMP/run.txt" 2>&1; then
  ok "agent run COMPLETE"; sed 's/^/    /' "$TMP/run.txt" | head -40
else bad "agent run failed:"; sed 's/^/    /' "$TMP/run.txt" | head -40; fi
grep -q "ERR_POOL_TIMEOUT" "$TMP/run.txt" && ok "facts name the demo error code (ERR_POOL_TIMEOUT)" || bad "no ERR_POOL_TIMEOUT fact"
grep -q "DEGRADED" "$TMP/run.txt" && warn "run was degraded (see line above) - facts are still code-made"
grep -q "\[llm\]" "$TMP/run.txt" && ok "at least one cluster labelled by the model" || warn "no model labels (rule labels only)"

code=$(curl -s -o "$TMP/ev.json" -w '%{http_code}' "$API/api/v1/incidents/$KEY/evidence" -H "Authorization: Bearer $SRE")
n=$(json "sum(1 for e in d if e['evidence_key'].startswith('LOG-'))" < "$TMP/ev.json" 2>/dev/null || echo 0)
[ "$code" = "200" ] && [ "$n" -gt 0 ] && ok "$n LOG evidence rows stored for $KEY (cited by the facts)" || bad "evidence: $code n=$n"
curl -s "$API/api/v1/incidents/$KEY/timeline" -H "Authorization: Bearer $SRE" > "$TMP/tl.json"
json "[e['event_type'] for e in d]" < "$TMP/tl.json" | grep -q EvidenceRetrieved && ok "timeline shows EvidenceRetrieved by agent:log_analysis" || bad "no EvidenceRetrieved on the timeline"

INV=$(grep -o "investigation=[0-9a-f-]*" "$TMP/run.txt" | head -1 | cut -d= -f2)
if [ -n "$INV" ]; then
  uv run --quiet python - "$INV" > "$TMP/trace.txt" 2>&1 <<'PY'
import sys, psycopg
from aeoi_db.config import libpq_dsn
with psycopg.connect(libpq_dsn()) as c:
    row = c.execute("""SELECT e.status, e.model, e.prompt_id, e.prompt_version, e.input_tokens,
        e.output_tokens, e.cost_usd, e.latency_ms, (SELECT count(*) FROM orchestrator.messages m
        WHERE m.execution_id = e.id) FROM orchestrator.agent_executions e JOIN orchestrator.tasks t
        ON t.id = e.task_id WHERE t.investigation_id = %s""", (sys.argv[1],)).fetchone()
    print(row)
PY
  grep -q "SUCCEEDED" "$TMP/trace.txt" && ok "trace row: $(cat "$TMP/trace.txt")" || bad "trace row missing: $(cat "$TMP/trace.txt")"
fi

# Phase 9: a MANAGER is refused BEFORE any agent runs (no investigations:run); the agent-level
# "missing_permission -> FAILED, no facts" case is an integration test now.
if make -s agent-run INCIDENT="$KEY" ROLE=MANAGER > "$TMP/mgr.txt" 2>&1; then bad "MANAGER run should be refused"
else grep -q "investigations:run" "$TMP/mgr.txt" && ok "MANAGER refused before any agent runs (no investigations:run)" || bad "MANAGER: $(head -c 200 "$TMP/mgr.txt")"; fi

echo; echo "passed=$pass failed=$fail notes=$note   incident=$KEY"
[ "$fail" = "0" ]
