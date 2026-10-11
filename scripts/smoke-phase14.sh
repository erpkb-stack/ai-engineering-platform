#!/usr/bin/env bash
# Phase 14 smoke (ADR-024): code index (tree-sitter, ACL in SQL) -> "who uses X" -> the
# code_intelligence agent (code only, as the user, via the tool gateway) -> the deploy's diff
# -> a query added inside a loop (PR #812) -> COMMIT evidence -> one more source for the
# deploy hypothesis (band rule unchanged). Needs: make codepack, make code-index, re-seeded db.
# Prints the finding and its hunk at the end: READ IT. bash 3.2 OK.
set -u
API="${AEOI_API_URL:-http://localhost:8000}"
RAG="${AEOI_RAG_URL:-http://localhost:8004}"
pass=0; fail=0
ok()   { echo "✔ $1"; pass=$((pass+1)); }
bad()  { echo "✘ $1"; fail=$((fail+1)); }
json() { python3 -c "import sys,json; d=json.load(sys.stdin); print($1)"; }
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

for pair in "api 8000" "incident 8001" "orchestrator 8002" "agents 8003" "rag 8004" "llm-gateway 8005" "tool-gateway 8006" "audit 8008"; do
  set -- $pair
  code=$(curl -s -o /dev/null -w '%{http_code}' "localhost:$2/health/live")
  [ "$code" = "200" ] && ok "$1 up" || { bad "$1 not running on :$2"; exit 1; }
done

# ---- 1. the code index: change impact, ACL in SQL -------------------------------------------
ENG="$(make -s token ROLE=ENGINEER 2>/dev/null)"
MGR="$(make -s token ROLE=MANAGER 2>/dev/null)"
q='{"symbol":"ShipmentResponse.address","limit":200}'
code=$(curl -s -o "$TMP/refs.json" -w '%{http_code}' -X POST "$RAG/v1/code/references" \
  -H "Authorization: Bearer $ENG" -H "Content-Type: application/json" -d "$q")
svcs=$(json "' '.join(sorted({h['service_key'] for h in d} - {'shipping-api'}))" < "$TMP/refs.json" 2>/dev/null)
[ "$code" = "200" ] && [ -n "$svcs" ] \
  || { bad "find references: $code, no hits - run: make codepack && make code-index"; exit 1; }
[ "$svcs" = "media-api notifications-api recommendations-api reviews-api" ] \
  && ok "ShipmentResponse.address is read by exactly the 4 planted services: $svcs" \
  || bad "impact services: $svcs (expected media-api notifications-api recommendations-api reviews-api)"
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$RAG/v1/code/references" \
  -H "Authorization: Bearer $MGR" -H "Content-Type: application/json" -d "$q")
[ "$code" = "403" ] && ok "MANAGER cannot read code (403)" || bad "MANAGER code references: $code (expected 403)"

# ---- 2. the investigation ------------------------------------------------------------------
SRE="$(make -s token ROLE=SRE 2>/dev/null)"
code=$(curl -s -o "$TMP/inc.json" -w '%{http_code}' -X POST "$API/api/v1/incidents" \
  -H "Authorization: Bearer $SRE" -H "Content-Type: application/json" -H "Idempotency-Key: smoke14-$(date +%s)" \
  -d '{"title":"Checkout 5xx and DB pool timeouts after deploy (phase 14 smoke)","severity":"SEV2","affected_services":["checkout-api"],"detected_at":"2026-10-02T09:50:00Z"}')
[ "$code" = "201" ] || { bad "create incident: $code $(head -c 300 "$TMP/inc.json")"; exit 1; }
KEY=$(json "d['key']" < "$TMP/inc.json"); ok "demo incident $KEY"
code=$(curl -s -o "$TMP/start.json" -w '%{http_code}' -X POST "$API/api/v1/incidents/$KEY/investigate" \
  -H "Authorization: Bearer $SRE" -H "Idempotency-Key: smoke14-inv-$(date +%s)")
[ "$code" = "202" ] || { bad "investigate: $code $(head -c 300 "$TMP/start.json")"; exit 1; }
INV=$(json "d['investigation_id']" < "$TMP/start.json"); ok "202 investigation $INV"
echo "  waiting: evidence agents, history, code (no model), ranking, critic, postmortem..."
status=RUNNING; i=0
while [ "$status" = "RUNNING" ] && [ $i -lt 200 ]; do
  sleep 3; i=$((i+1))
  curl -s "$API/api/v1/investigations/$INV" -H "Authorization: Bearer $SRE" > "$TMP/inv.json"
  status=$(json "d['status']" < "$TMP/inv.json" 2>/dev/null || echo "?")
done
json "', '.join(t['agent']+'='+t['status']+' '+str(t['model'] or '-') for t in d['tasks'])" < "$TMP/inv.json" | sed 's/^/    tasks: /'
[ "$status" = "COMPLETE" ] && ok "investigation COMPLETE (code included)" \
  || bad "investigation $status: $(json "d.get('error')" < "$TMP/inv.json" 2>/dev/null)"
json "d['plan'].get('code')" < "$TMP/inv.json" | sed 's/^/    code: /'
cm=$(json "[(t['status'], t['model'] or '-') for t in d['tasks'] if t['agent']=='code_intelligence'][:1]" < "$TMP/inv.json" 2>/dev/null)
[ "$cm" = "[('SUCCEEDED', '-')]" ] && ok "code_intelligence SUCCEEDED with no model (code only)" \
  || bad "code_intelligence task: ${cm:-none} - is AEOI_ORCH_CODE on and are the agents/tools tokens current?"
json "(d['plan'].get('code') or {}).get('findings_by_rule')" < "$TMP/inv.json" | grep -q "'query_in_loop': 1" \
  && ok "one finding: a query added inside a loop (details in the evidence below)" \
  || bad "no query_in_loop finding - run: make db-seed (commit diffs), then retry"

# ---- 3. what was stored ----------------------------------------------------------------------
curl -s "$API/api/v1/incidents/$KEY/evidence" -H "Authorization: Bearer $SRE" > "$TMP/ev.json"
json "[e for e in d if e['kind']=='COMMIT' and 'PR #812' in e['excerpt'] and 'findOpenOrders(item.getSku())' in e['excerpt']][:1] != []" < "$TMP/ev.json" | grep -q True \
  && ok "COMMIT evidence: PR #812 + the hunk with the call in the loop" \
  || bad "COMMIT evidence: $(json "[(e['kind'], e['excerpt'][:80]) for e in d if e['kind']=='COMMIT']" < "$TMP/ev.json")"
curl -s "$API/api/v1/incidents/$KEY/hypotheses" -H "Authorization: Bearer $SRE" > "$TMP/h.json"
json "[(h['confidence'], any(x['kind']=='COMMIT' and x['stance']=='SUPPORTS' for x in h['edges'])) for h in d if 'DEPLOY-4821' in h['statement']][:1]" < "$TMP/h.json" \
  | grep -q "('HIGH', True)" && ok "deploy hypothesis: band HIGH (unchanged) + supported by the COMMIT" \
  || bad "deploy hypothesis: $(json "[(h['key'], h['confidence'], [x['kind'] for x in h['edges']]) for h in d][:3]" < "$TMP/h.json")"
json "[h['key'] for h in d if 'DEPLOY-4821' not in h['statement'] and any(x['kind']=='COMMIT' for x in h['edges'])]" < "$TMP/h.json" | grep -q "^\[\]$" \
  && ok "no other hypothesis cites the commit" || bad "COMMIT evidence leaked into another hypothesis"
curl -s "$API/api/v1/investigations/$INV" -H "Authorization: Bearer $MGR" > "$TMP/minv.json"
grep -q "findOpenOrders\|CartService" "$TMP/minv.json" && bad "MANAGER investigation (plan/error) names code" \
  || ok "MANAGER investigation shows counts only (no symbol, no path)"
curl -s "$API/api/v1/incidents/$KEY/evidence" -H "Authorization: Bearer $MGR" > "$TMP/mev.json"
grep -q "findOpenOrders" "$TMP/mev.json" && bad "MANAGER evidence shows the code" || ok "MANAGER evidence hides the code"
curl -s "$API/api/v1/investigations/$INV/trace" -H "Authorization: Bearer $MGR" > "$TMP/mt.json"
json "(lambda r: bool(r) and all(r))([e['redacted'] for e in d['executions'] if e['agent']=='code_intelligence'])" < "$TMP/mt.json" | grep -q True \
  && ok "MANAGER cannot read the code trace (diffs)" \
  || bad "MANAGER code trace: $(json "[(e['agent'], e['redacted']) for e in d['executions']]" < "$TMP/mt.json")"

echo; echo "------------------------------------------------------------ the finding (read it)"
json "next((e['excerpt'] for e in d if e['kind']=='COMMIT'), '-')" < "$TMP/ev.json"
echo "passed=$pass failed=$fail   incident=$KEY investigation=$INV"
[ "$fail" = "0" ]
