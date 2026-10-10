#!/usr/bin/env bash
# Phase 12 smoke: product path -> evidence agents -> hypotheses -> critic -> validate -> report
# (code timeline + Sonnet narrative, checked by code) -> GET /postmortem, assembled at read
# time. On the planted demo (checkout-api, DEPLOY-4821 at 09:42, p95 latency from 09:49,
# detected 09:50) the numbers are known, so they are checked exactly. Prints the markdown at
# the end: READ IT - passing checks say nothing about whether the prose is good. bash 3.2 OK.
set -u
API="${AEOI_API_URL:-http://localhost:8000}"
pass=0; fail=0; note=0
ok()   { echo "✔ $1"; pass=$((pass+1)); }
bad()  { echo "✘ $1"; fail=$((fail+1)); }
warn() { echo "! $1"; note=$((note+1)); }
json() { python3 -c "import sys,json; d=json.load(sys.stdin); print($1)"; }
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

for pair in "api 8000" "incident 8001" "orchestrator 8002" "agents 8003" "rag 8004" "llm-gateway 8005" "tool-gateway 8006" "audit 8008"; do
  set -- $pair
  code=$(curl -s -o /dev/null -w '%{http_code}' "localhost:$2/health/live")
  [ "$code" = "200" ] && ok "$1 up" || { bad "$1 not running on :$2"; exit 1; }
done
scopes=$(python3 -c "import base64,json; p=open('secrets/orchestrator_service_token.txt').read().split('.')[1]; p+='='*(-len(p)%4); print(json.loads(base64.urlsafe_b64decode(p))['scope'])")
case "$scopes" in *postmortems:write*timeline:write*|*timeline:write*postmortems:write*) ok "orchestrator token has timeline:write + postmortems:write";;
  *) bad "orchestrator token lacks timeline:write/postmortems:write - run: make agents-tokens, then restart run-orch"; exit 1;; esac

SRE="$(make -s token ROLE=SRE 2>/dev/null)"
code=$(curl -s -o "$TMP/inc.json" -w '%{http_code}' -X POST "$API/api/v1/incidents" \
  -H "Authorization: Bearer $SRE" -H "Content-Type: application/json" -H "Idempotency-Key: smoke12-$(date +%s)" \
  -d '{"title":"Checkout 5xx and DB pool timeouts after deploy (phase 12 smoke)","severity":"SEV2","affected_services":["checkout-api"],"detected_at":"2026-10-02T09:50:00Z"}')
[ "$code" = "201" ] || { bad "create incident: $code $(head -c 300 "$TMP/inc.json")"; exit 1; }
KEY=$(json "d['key']" < "$TMP/inc.json"); ok "demo incident $KEY"
code=$(curl -s -o "$TMP/start.json" -w '%{http_code}' -X POST "$API/api/v1/incidents/$KEY/investigate" \
  -H "Authorization: Bearer $SRE" -H "Idempotency-Key: smoke12-inv-$(date +%s)")
[ "$code" = "202" ] || { bad "investigate: $code $(head -c 300 "$TMP/start.json")"; exit 1; }
INV=$(json "d['investigation_id']" < "$TMP/start.json"); ok "202 investigation $INV"

echo "  waiting: evidence agents, ranking, critic, then the postmortem writer (Sonnet: ~30-60 s)..."
status=RUNNING; i=0
while [ "$status" = "RUNNING" ] && [ $i -lt 200 ]; do
  sleep 3; i=$((i+1))
  curl -s "$API/api/v1/investigations/$INV" -H "Authorization: Bearer $SRE" > "$TMP/inv.json"
  status=$(json "d['status']" < "$TMP/inv.json" 2>/dev/null || echo "?")
done
json "', '.join(t['agent']+'='+t['status']+' x'+str(t['attempt'])+' '+str(t['model'] or '-') for t in d['tasks'])" < "$TMP/inv.json" | sed 's/^/    tasks: /'
[ "$status" = "COMPLETE" ] && ok "investigation COMPLETE (evidence + hypotheses + critic + postmortem)" \
  || bad "investigation $status: $(json "d.get('error')" < "$TMP/inv.json" 2>/dev/null)"
json "d['plan'].get('postmortem')" < "$TMP/inv.json" | sed 's/^/    report: /'
wmodel=$(json "[t['model'] or '-' for t in d['tasks'] if t['agent']=='postmortem'][0]" < "$TMP/inv.json" 2>/dev/null)
case "$wmodel" in *sonnet*) ok "writer is Sonnet ($wmodel, reasoning route, fallback off)";;
  -|"") bad "the postmortem writer did not run (no model answered) - see the error above";;
  *) bad "writer ran on $wmodel (expected claude-sonnet-*; never a fallback)";; esac
wb=$(json "(d['plan'].get('postmortem') or {}).get('written_by')" < "$TMP/inv.json")
[ "$wb" = "llm" ] && ok "narrative written by the model and kept by code" || bad "narrative written_by=$wb"
nr=$(json "(d['plan'].get('postmortem') or {}).get('numbers_rejected')" < "$TMP/inv.json")
sd=$(json "(d['plan'].get('postmortem') or {}).get('sentences_dropped')" < "$TMP/inv.json")
[ "$nr" = "0" ] && [ "$sd" = "0" ] && ok "code dropped no sentence (0 uncited, 0 unverifiable numbers)" \
  || warn "code dropped $sd uncited and $nr unverifiable-number sentence(s): read the trace - model behaviour"

curl -s "$API/api/v1/incidents/$KEY/postmortem" -H "Authorization: Bearer $SRE" > "$TMP/pm.json"
rc=$(json "(d.get('root_cause') or {}).get('statement','')" < "$TMP/pm.json")
case "$rc" in *DEPLOY-4821*) ok "root cause = the stored top hypothesis (DEPLOY-4821), by code";;
  *) bad "root cause: ${rc:-none}";; esac
json "(d['root_cause']['confidence'], d['root_cause']['status'], d['root_cause']['confirmed_by_human'])" < "$TMP/pm.json" \
  | grep -q "('HIGH', 'VALIDATED', False)" && ok "root cause HIGH / VALIDATED / not confirmed by a human" \
  || bad "root cause band/status: $(json "d.get('root_cause')" < "$TMP/pm.json" | head -c 200)"
json "(d['trigger_at'], d['first_symptom_at'], d['minutes_trigger_to_symptom'], d['minutes_to_detect'])" < "$TMP/pm.json" \
  | grep -q "('2026-10-02T09:42:00Z', '2026-10-02T09:49:00Z', 7.0, 1.0)" \
  && ok "numbers by code: trigger 09:42, first symptom 09:49, +7 min, detected +1 min" \
  || bad "numbers: $(json "(d['trigger_at'], d['first_symptom_at'], d['minutes_trigger_to_symptom'], d['minutes_to_detect'])" < "$TMP/pm.json")"
src=$(json "' '.join(sorted({r['source'] for r in d['timeline']}))" < "$TMP/pm.json")
case " $src " in *" DEPLOY "*" METRIC "*) ok "timeline merges agent observations + incident events ($src)";;
  *) bad "timeline sources: $src";; esac
uncited=$(json "sum(1 for s in d['sections'] for x in s['sentences'] if not x['refs'])" < "$TMP/pm.json")
n=$(json "sum(len(s['sentences']) for s in d['sections'])" < "$TMP/pm.json")
[ "${n:-0}" -gt 0 ] && [ "$uncited" = "0" ] && ok "$n model sentence(s), every one cites observations" \
  || bad "sentences=$n uncited=$uncited"

curl -s "$API/api/v1/incidents/$KEY/postmortem?format=markdown" -H "Authorization: Bearer $SRE" > "$TMP/pm.md"
grep -q "not yet confirmed by a human" "$TMP/pm.md" && ok "markdown says the root cause is not confirmed by a human" \
  || bad "markdown does not state the root cause is unconfirmed"

MGR="$(make -s token ROLE=MANAGER 2>/dev/null)"
curl -s "$API/api/v1/incidents/$KEY/postmortem" -H "Authorization: Bearer $MGR" > "$TMP/mpm.json"
json "(d['redacted_sentences'] == $n + len(d['action_items']) + len(d['no_change']), d['hidden_timeline_rows'] > 0, not {r['source'] for r in d['timeline']} & {'LOG','METRIC','DEPLOY','CONFIG'})" < "$TMP/mpm.json" \
  | grep -q "(True, True, True)" && ok "MANAGER: root cause yes; model sentences and evidence rows hidden" \
  || bad "MANAGER view: $(json "(d['redacted_sentences'], d['hidden_timeline_rows'])" < "$TMP/mpm.json")"

echo; echo "------------------------------------------------------------ the postmortem (read it)"
cat "$TMP/pm.md"
echo "passed=$pass failed=$fail notes=$note   incident=$KEY investigation=$INV"
[ "$fail" = "0" ]
