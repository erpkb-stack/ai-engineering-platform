#!/usr/bin/env bash
# Phase 11 smoke: product path -> 4 evidence agents -> hypotheses (code candidates, model rank)
# -> critic -> deterministic validation -> incident.hypotheses. On the planted demo
# (checkout-api, DEPLOY-4821) the deploy must be the top, HIGH, validated hypothesis and a
# traffic surge must be ruled out. Needs the Phase 10 services, Claude Haiku on `fast` (ranker)
# and Claude Sonnet on `reasoning` (critic, never a fallback). macOS bash 3.2 OK.
set -u
API="${AEOI_API_URL:-http://localhost:8000}"
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
scopes=$(python3 -c "import base64,json; p=open('secrets/orchestrator_service_token.txt').read().split('.')[1]; p+='='*(-len(p)%4); print(json.loads(base64.urlsafe_b64decode(p))['scope'])")
case "$scopes" in *hypotheses:write*) ok "orchestrator token has hypotheses:write";;
  *) bad "orchestrator token lacks hypotheses:write - run: make agents-tokens, then restart run-orch"; exit 1;; esac

SRE="$(make -s token ROLE=SRE 2>/dev/null)"
code=$(curl -s -o "$TMP/inc.json" -w '%{http_code}' -X POST "$API/api/v1/incidents" \
  -H "Authorization: Bearer $SRE" -H "Content-Type: application/json" -H "Idempotency-Key: smoke11-$(date +%s)" \
  -d '{"title":"Checkout 5xx and DB pool timeouts after deploy (phase 11 smoke)","severity":"SEV2","affected_services":["checkout-api"],"detected_at":"2026-10-02T09:50:00Z"}')
[ "$code" = "201" ] || { bad "create incident: $code $(head -c 300 "$TMP/inc.json")"; exit 1; }
KEY=$(json "d['key']" < "$TMP/inc.json"); ok "demo incident $KEY"
code=$(curl -s -o "$TMP/start.json" -w '%{http_code}' -X POST "$API/api/v1/incidents/$KEY/investigate" \
  -H "Authorization: Bearer $SRE" -H "Idempotency-Key: smoke11-inv-$(date +%s)")
[ "$code" = "202" ] || { bad "investigate: $code $(head -c 300 "$TMP/start.json")"; exit 1; }
INV=$(json "d['investigation_id']" < "$TMP/start.json"); ok "202 investigation $INV"

echo "  waiting: evidence agents, then ranking + critic (Haiku: seconds; llama fallback: minutes)..."
status=RUNNING; i=0
while [ "$status" = "RUNNING" ] && [ $i -lt 160 ]; do
  sleep 3; i=$((i+1))
  curl -s "$API/api/v1/investigations/$INV" -H "Authorization: Bearer $SRE" > "$TMP/inv.json"
  status=$(json "d['status']" < "$TMP/inv.json" 2>/dev/null || echo "?")
done
json "', '.join(t['agent']+'='+t['status']+' '+str(t['model'] or '-') for t in d['tasks'])" < "$TMP/inv.json" | sed 's/^/    tasks: /'
[ "$status" = "COMPLETE" ] && ok "investigation COMPLETE (evidence + hypotheses + critic)" \
  || bad "investigation $status: $(json "d.get('error')" < "$TMP/inv.json" 2>/dev/null)"
json "d['plan'].get('hypotheses')" < "$TMP/inv.json" | sed 's/^/    validation: /'
passed=$(json "(d['plan'].get('hypotheses') or {}).get('passed')" < "$TMP/inv.json")
[ "$passed" = "True" ] && ok "deterministic validation passed (citations stored, timing, critic answered)" || bad "validation did not pass"
rmodel=$(json "[t['model'] or '-' for t in d['tasks'] if t['agent']=='hypothesis'][0]" < "$TMP/inv.json" 2>/dev/null)
cmodel=$(json "[t['model'] or '-' for t in d['tasks'] if t['agent']=='critic'][0]" < "$TMP/inv.json" 2>/dev/null)
echo "    ranker=$rmodel critic=$cmodel"
if [ -z "$cmodel" ] || [ "$cmodel" = "-" ]; then bad "critic did not run (no model answered) - see the critic error above"
elif [ "$cmodel" != "$rmodel" ]; then ok "critic ($cmodel) is a different model from the ranker ($rmodel)"
else bad "critic not independent: ranker and critic are both $cmodel"; fi
case "$cmodel" in *sonnet*) ok "critic is Sonnet (reasoning route, fallback off)";;
  *llama*) bad "critic ran on llama - the reasoning route must never fall back";;
  *) warn "critic model $cmodel (expected claude-sonnet-*)";; esac

curl -s "$API/api/v1/incidents/$KEY/hypotheses" -H "Authorization: Bearer $SRE" > "$TMP/h.json"
json "'\n'.join('    #%s [%s] %s %s' % (h['rank'], h['confidence'], h['status'], h['statement'])
  + (('\n        critic disputed (not counted): %s' % ', '.join(h['detail']['critic_disputed'])) if h['detail'].get('critic_disputed') else '')
  + (('\n        refinement (critic): %s' % h['detail']['refinement']) if h['detail'].get('refinement') else '') for h in d)" < "$TMP/h.json"
top=$(json "[h for h in d if h['status']!='REJECTED'][0]['statement']" < "$TMP/h.json" 2>/dev/null)
case "$top" in *DEPLOY-4821*) ok "top hypothesis: the deploy DEPLOY-4821";; *) warn "top hypothesis is not the deploy: $top (model ranking - read the explanation)";; esac
band=$(json "[h for h in d if 'DEPLOY-4821' in h['statement']][0]['confidence']" < "$TMP/h.json" 2>/dev/null)
[ "$band" = "HIGH" ] && ok "deploy hypothesis is HIGH by the rubric (3 independent sources, nothing against)" || bad "deploy band: $band"
# the critic should file "the flag flip in DEPLOY-4821" as a refinement of the deploy, not a rival
# (prompt v2 `refines`); a model behaviour, so a note - the band check above is the guarantee
restated=$(json "sum(1 for h in d if h['rank'] > 1 and h['status'] == 'PROPOSED' and 'DEPLOY-4821' in h['statement'] and not h['statement'].startswith('Deploy '))" < "$TMP/h.json" 2>/dev/null)
[ "${restated:-0}" = "0" ] && ok "no critic alternative restates the deploy as a rival" \
  || warn "$restated critic alternative(s) restate DEPLOY-4821 as a rival (did it use refines?)"
json "[h for h in d if 'thread pool' in h['statement']][0]['confidence']" < "$TMP/h.json" 2>/dev/null | grep -q LOW \
  && ok "thread-pool alternative is LOW (it started after the symptoms)" || bad "thread-pool hypothesis band is not LOW"
json "[h['statement'] for h in d if h['status']=='REJECTED']" < "$TMP/h.json" | grep -q "traffic surge" \
  && ok "traffic surge ruled out (requests_per_sec steady)" || bad "traffic surge not ruled out"
unc=$(json "sum(1 for h in d if h['detail'].get('explanation') and not h['detail'].get('explanation_refs'))" < "$TMP/h.json")
[ "$unc" = "0" ] && ok "every stored explanation cites observations" || bad "$unc explanation(s) without citations"
edges=$(json "sum(len(h['edges']) for h in d)" < "$TMP/h.json")
[ "${edges:-0}" -gt 0 ] && ok "$edges evidence edges (SUPPORTS/CONTRADICTS) on the incident" || bad "no evidence edges"
json "[h['statement'] for h in d]" < "$TMP/h.json" | grep -qE " 0[0-8]:[0-9]{2}" \
  && bad "a statement shows a pre-09:00 time (local time labelled Z?)" || ok "statement times are UTC"

MGR="$(make -s token ROLE=MANAGER 2>/dev/null)"
curl -s "$API/api/v1/incidents/$KEY/hypotheses" -H "Authorization: Bearer $MGR" > "$TMP/mh.json"
mshown=$(json "sum(len(h['edges']) for h in d)" < "$TMP/mh.json"); mhid=$(json "sum(h['hidden_edges'] for h in d)" < "$TMP/mh.json")
[ "$mshown" = "0" ] && [ "${mhid:-0}" -gt 0 ] && ok "MANAGER sees the conclusions, not the raw evidence ($mhid edges hidden)" \
  || bad "MANAGER edges shown=$mshown hidden=$mhid"
curl -s "$API/api/v1/investigations/$INV/trace" -H "Authorization: Bearer $MGR" > "$TMP/mt.json"
json "all(e['redacted'] for e in d['executions'] if e['agent'] in ('hypothesis','critic'))" < "$TMP/mt.json" | grep -q True \
  && ok "MANAGER trace: hypothesis/critic prompts redacted" || bad "MANAGER can read reasoning prompts"

echo; echo "passed=$pass failed=$fail notes=$note   incident=$KEY investigation=$INV"
[ "$fail" = "0" ]
