#!/usr/bin/env bash
# Phase 10 smoke: the PRODUCT path with four agents in parallel (log_analysis, metrics,
# deployment, knowledge) on the planted demo (checkout-api, DEPLOY-4821, pool exhaustion).
# Checks facts the planted data makes true, evidence per kind, the knowledge pointer rule,
# per-kind evidence visibility (MANAGER) and the delegation record.
# Needs: make dev, run-llm, run-rag, run-tools, run-audit, run-agents, run-orch;
#        make tools-tokens (rag:obo) and make rag-ingest done once. macOS bash 3.2 OK.
set -u
API="${AEOI_API_URL:-http://localhost:8000}"
INC_URL="${AEOI_INCIDENT_URL:-http://localhost:8001}"
pass=0; fail=0; note=0
ok()   { echo "✔ $1"; pass=$((pass+1)); }
bad()  { echo "✘ $1"; fail=$((fail+1)); }
warn() { echo "! $1"; note=$((note+1)); }
json() { python3 -c "import sys,json; d=json.load(sys.stdin); print($1)"; }
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

for pair in "api 8000" "incident 8001" "orchestrator 8002" "agents 8003" "rag 8004" "llm-gateway 8005" "tool-gateway 8006"; do
  set -- $pair
  code=$(curl -s -o /dev/null -w '%{http_code}' "localhost:$2/health/live")
  [ "$code" = "200" ] && ok "$1 up" || { bad "$1 not running on :$2"; exit 1; }
done
[ -s secrets/tools_rag_token.txt ] && ok "tool-gateway rag token present" \
  || { bad "no secrets/tools_rag_token.txt - run: make tools-tokens (rag:obo); restart run-tools"; exit 1; }

SRE="$(make -s token ROLE=SRE 2>/dev/null)"
code=$(curl -s -o "$TMP/inc.json" -w '%{http_code}' -X POST "$API/api/v1/incidents" \
  -H "Authorization: Bearer $SRE" -H "Content-Type: application/json" -H "Idempotency-Key: smoke10-$(date +%s)" \
  -d '{"title":"Checkout 5xx and DB pool timeouts after deploy (phase 10 smoke)","severity":"SEV2","affected_services":["checkout-api"],"detected_at":"2026-10-02T09:50:00Z"}')
[ "$code" = "201" ] || { bad "create incident: $code $(head -c 300 "$TMP/inc.json")"; exit 1; }
KEY=$(json "d['key']" < "$TMP/inc.json"); INC_ID=$(json "d['id']" < "$TMP/inc.json"); ok "demo incident $KEY"

code=$(curl -s -o "$TMP/start.json" -w '%{http_code}' -X POST "$API/api/v1/incidents/$KEY/investigate" \
  -H "Authorization: Bearer $SRE" -H "Idempotency-Key: smoke10-inv-$(date +%s)")
[ "$code" = "202" ] || { bad "investigate: $code $(head -c 300 "$TMP/start.json")"; exit 1; }
INV=$(json "d['investigation_id']" < "$TMP/start.json"); ok "202 investigation $INV"

echo "  waiting for 4 agents (the log agent's local CPU model sets the pace: ~1-2 min)..."
status=RUNNING; i=0
while [ "$status" = "RUNNING" ] && [ $i -lt 120 ]; do
  sleep 3; i=$((i+1))
  curl -s "$API/api/v1/investigations/$INV" -H "Authorization: Bearer $SRE" > "$TMP/inv.json"
  status=$(json "d['status']" < "$TMP/inv.json" 2>/dev/null || echo "?")
done
json "', '.join(t['agent']+'='+t['status']+' x'+str(t['attempt']) for t in d['tasks'])" < "$TMP/inv.json" | sed 's/^/    tasks: /'
evid_ok=$(json "all(t['status']=='SUCCEEDED' for t in d['tasks'] if t['agent'] not in ('hypothesis','critic'))" < "$TMP/inv.json" 2>/dev/null)
if [ "$status" = "COMPLETE" ]; then ok "investigation COMPLETE (all 4 agents + reasoning)"
elif [ "$status" = "PARTIAL" ] && [ "$evid_ok" = "True" ]; then ok "all 4 evidence agents SUCCEEDED"; warn "reasoning: $(json "d.get('error')" < "$TMP/inv.json")"
else bad "investigation $status: $(json "d.get('error')" < "$TMP/inv.json" 2>/dev/null)"; fi
n=$(json "sum(1 for t in d['tasks'] if t['agent'] not in ('hypothesis','critic'))" < "$TMP/inv.json"); [ "$n" = "4" ] && ok "4 evidence tasks planned" || bad "$n evidence tasks, expected 4"

curl -s "$API/api/v1/investigations/$INV/trace" -H "Authorization: Bearer $SRE" > "$TMP/trace.json"
facts() { json "' || '.join(f['statement'] for e in d['executions'] if e['agent']=='$1' for f in e['output'].get('facts', []))" < "$TMP/trace.json" 2>/dev/null; }
facts log_analysis | grep -q ERR_POOL_TIMEOUT && ok "log: ERR_POOL_TIMEOUT fact" || bad "log: no ERR_POOL_TIMEOUT fact"
# planted: p95 latency moves at 09:49, 5xx at 09:51, pool utilisation at 09:50 (demo-scenario.md)
facts metrics | grep -q "p95_latency_ms: sustained shift up from 2026-10-02 09:49" \
  && ok "metrics: p95 latency shift from 09:49Z" || bad "metrics: no p95 shift at 09:49Z ($(facts metrics | head -c 300))"
facts metrics | grep -q "db_pool_utilization: sustained shift up from 2026-10-02 09:50" \
  && ok "metrics: pool utilisation shift from 09:50Z" || bad "metrics: no pool shift at 09:50Z"
facts metrics | grep -q "requests_per_sec: stayed within its baseline" \
  && ok "metrics: traffic stayed within baseline (rules out a surge)" || warn "metrics: requests_per_sec not reported steady"
facts deployment | grep -q "DEPLOY-4821.*feature_flags.order_batching false -> true.*started 8 min before the incident was detected (finished 6 min before)" \
  && ok "deployment: DEPLOY-4821, order_batching false -> true, started 8 min before detection" \
  || bad "deployment: DEPLOY-4821 fact missing ($(facts deployment | head -c 300))"
facts deployment | grep -q "db.pool.max_size" && bad "deployment: touched-not-changed key reported as a change" \
  || ok "deployment: db.pool.max_size [20,20] is not a change"
refs=$(json "sum(len(e['output'].get('references', [])) for e in d['executions'] if e['agent']=='knowledge')" < "$TMP/trace.json")
[ "${refs:-0}" -gt 0 ] && ok "knowledge: $refs runbook/doc pointer(s)" || warn "knowledge: no references (rag-ingest done? embeddings up?)"

curl -s "$INC_URL/v1/incidents/$INC_ID/evidence" -H "Authorization: Bearer $SRE" > "$TMP/ev.json"
kinds=$(json "' '.join(sorted({e['kind'] for e in d}))" < "$TMP/ev.json")
for k in LOG METRIC DEPLOY CONFIG; do
  case " $kinds " in *" $k "*) ok "evidence kind $k stored";; *) bad "no $k evidence (kinds: $kinds)";; esac
done
texty=$(json "sum(1 for e in d if e['kind'] in ('RUNBOOK','DOC') and not e['excerpt'].startswith('rag document '))" < "$TMP/ev.json")
[ "$texty" = "0" ] && ok "knowledge evidence = pointers only (no doc text on the incident)" \
  || bad "$texty knowledge evidence row(s) hold text, not pointers"

MGR="$(make -s token ROLE=MANAGER 2>/dev/null)"
curl -s "$INC_URL/v1/incidents/$INC_ID/evidence" -H "Authorization: Bearer $MGR" > "$TMP/mev.json"
mk=$(json "' '.join(sorted({e['kind'] for e in d}))" < "$TMP/mev.json")
case " $mk " in *" LOG "*|*" METRIC "*|*" DEPLOY "*) bad "MANAGER sees raw evidence kinds: $mk";;
  *) ok "MANAGER sees no LOG/METRIC/DEPLOY evidence (kinds: ${mk:-none})";; esac

uv run --quiet python - "$INV" > "$TMP/deleg.txt" 2>&1 <<'PY'
import sys, psycopg
from aeoi_db.config import libpq_dsn
inv = sys.argv[1]
with psycopg.connect(libpq_dsn()) as c:
    g = c.execute("SELECT id, revoked_reason FROM identity.delegation_grants WHERE investigation_id=%s", (inv,)).fetchall()
    agents = sorted(r[0] for r in c.execute("SELECT DISTINCT agent_name FROM tools.tool_calls WHERE investigation_id=%s", (inv,)))
    print(f"grants={len(g)} revoked={g[0][1] if g else None!r} agents_with_tool_calls={','.join(agents)}")
PY
grep -qE "grants=1 revoked='investigation (COMPLETE|PARTIAL)' agents_with_tool_calls=deployment,knowledge,log_analysis,metrics" "$TMP/deleg.txt" \
  && ok "one grant for 4 agents, revoked: $(cat "$TMP/deleg.txt")" || bad "delegation/tool calls: $(cat "$TMP/deleg.txt")"

echo; echo "passed=$pass failed=$fail notes=$note   incident=$KEY investigation=$INV"
[ "$fail" = "0" ]
