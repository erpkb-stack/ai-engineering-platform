#!/usr/bin/env bash
# Phase 13 smoke (ADR-023): hybrid similar-incident search (rag, ACL in SQL) -> the
# historical_incident agent (as the user, via the tool gateway) -> only incidents shared with
# every engineer kept, compared by Haiku (claims checked by code), stored as INCIDENT pointers
# -> history priors on the hypotheses (tie-break only) -> postmortem section.
# Planted demo: checkout-api, DEPLOY-4821, a connection-pool exhaustion. The seed has ~90
# past pool-exhaustion incidents with the SAME text (one template per category), 5 of them on
# checkout-api - INC-2911 (planted) is one of those 5 twins, so its key is a note, not a check.
# Prints the similar-incident section at the end: READ IT. bash 3.2 OK.
set -u
API="${AEOI_API_URL:-http://localhost:8000}"
RAG="${AEOI_RAG_URL:-http://localhost:8004}"
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

# ---- 1. retrieval: vectors exist, ACL holds in the hybrid query -----------------------------
OUT="$(make -s token ROLE=ENGINEER WITHOUT=security-team 2>/dev/null)"
SEC="$(make -s token ROLE=ADMIN 2>/dev/null)"  # ADMIN is always in security-team (synth)
q='{"query":"TLS handshake failures, internal certificate expired","k":10,"mode":"vector"}'
code=$(curl -s -o "$TMP/v.json" -w '%{http_code}' -X POST "$RAG/v1/incidents/search" \
  -H "Authorization: Bearer $OUT" -H "Content-Type: application/json" -d "$q")
n=$(json "len(d)" < "$TMP/v.json" 2>/dev/null || echo 0)
[ "$code" = "200" ] && [ "${n:-0}" -gt 0 ] && ok "vector branch answers ($n hits)" \
  || { bad "vector search: $code, $n hits - run: make embed-history (needs run-llm)"; exit 1; }
q='{"query":"TLS handshake failures, internal certificate expired","k":20,"mode":"hybrid"}'
curl -s -X POST "$RAG/v1/incidents/search" -H "Authorization: Bearer $OUT" \
  -H "Content-Type: application/json" -d "$q" > "$TMP/out.json"
curl -s -X POST "$RAG/v1/incidents/search" -H "Authorization: Bearer $SEC" \
  -H "Content-Type: application/json" -d "$q" > "$TMP/sec.json"
json "(all(h['open_to_engineering'] for h in d), {h['retrieval'] for h in d} == {'hybrid'})" < "$TMP/out.json" \
  | grep -q "(True, True)" && ok "outsider: hybrid hits, every one shared with all engineers (ACL in SQL)" \
  || bad "outsider sees a restricted incident or retrieval is not hybrid: $(json "[(h['incident_key'], h['open_to_engineering'], h['retrieval']) for h in d][:5]" < "$TMP/out.json")"
r=$(json "sum(1 for h in d if not h['open_to_engineering'])" < "$TMP/sec.json")
[ "${r:-0}" -gt 0 ] && ok "security-team member (ADMIN) also sees $r restricted certificate incident(s)" \
  || warn "security-team member (ADMIN) saw no restricted incident (seed has ~half of certificate_expiry restricted)"

# ---- 2. the investigation ------------------------------------------------------------------
SRE="$(make -s token ROLE=SRE 2>/dev/null)"
code=$(curl -s -o "$TMP/inc.json" -w '%{http_code}' -X POST "$API/api/v1/incidents" \
  -H "Authorization: Bearer $SRE" -H "Content-Type: application/json" -H "Idempotency-Key: smoke13-$(date +%s)" \
  -d '{"title":"Checkout 5xx and DB pool timeouts after deploy (phase 13 smoke)","severity":"SEV2","affected_services":["checkout-api"],"detected_at":"2026-10-02T09:50:00Z"}')
[ "$code" = "201" ] || { bad "create incident: $code $(head -c 300 "$TMP/inc.json")"; exit 1; }
KEY=$(json "d['key']" < "$TMP/inc.json"); ok "demo incident $KEY"
code=$(curl -s -o "$TMP/start.json" -w '%{http_code}' -X POST "$API/api/v1/incidents/$KEY/investigate" \
  -H "Authorization: Bearer $SRE" -H "Idempotency-Key: smoke13-inv-$(date +%s)")
[ "$code" = "202" ] || { bad "investigate: $code $(head -c 300 "$TMP/start.json")"; exit 1; }
INV=$(json "d['investigation_id']" < "$TMP/start.json"); ok "202 investigation $INV"
echo "  waiting: evidence agents, history (Haiku), ranking, critic, postmortem (Sonnet)..."
status=RUNNING; i=0
while [ "$status" = "RUNNING" ] && [ $i -lt 200 ]; do
  sleep 3; i=$((i+1))
  curl -s "$API/api/v1/investigations/$INV" -H "Authorization: Bearer $SRE" > "$TMP/inv.json"
  status=$(json "d['status']" < "$TMP/inv.json" 2>/dev/null || echo "?")
done
json "', '.join(t['agent']+'='+t['status']+' '+str(t['model'] or '-') for t in d['tasks'])" < "$TMP/inv.json" | sed 's/^/    tasks: /'
[ "$status" = "COMPLETE" ] && ok "investigation COMPLETE (history included)" \
  || bad "investigation $status: $(json "d.get('error')" < "$TMP/inv.json" 2>/dev/null)"
json "d['plan'].get('history')" < "$TMP/inv.json" | sed 's/^/    history: /'
hm=$(json "[t['model'] or '-' for t in d['tasks'] if t['agent']=='historical_incident'][0]" < "$TMP/inv.json" 2>/dev/null)
case "$hm" in *haiku*) ok "history compared by Haiku ($hm)";; *) bad "history model: ${hm:-none} (expected claude-haiku-*, fast route)";; esac
json "(d['plan'].get('history') or {}).get('retrieval')" < "$TMP/inv.json" | grep -q hybrid \
  && ok "the agent's search was hybrid" || bad "the agent's search was not hybrid (vectors missing?)"
json "'INC-2911' in ((d['plan'].get('history') or {}).get('similar') or [])" < "$TMP/inv.json" | grep -q True \
  && ok "the planted INC-2911 is among the similar incidents" \
  || warn "INC-2911 not among similar (one of 5 identical checkout-api twins; see the checks below)"
curl -s "$API/api/v1/investigations/$INV/trace" -H "Authorization: Bearer $SRE" > "$TMP/tr.json"
HQ=$(json "next((n[len('query (code): '):] for e in d['executions'] if e['agent']=='historical_incident' for n in (e['output'] or {}).get('notes', []) if n.startswith('query (code): ')), '')" < "$TMP/tr.json" 2>/dev/null)
echo "    agent query: $HQ"
# int(... or 0): a skipped/failed history has no counts (set -u would abort on "None": review)
rej=$(json "int(((d['plan'].get('history') or {}).get('claims_rejected')) or 0)" < "$TMP/inv.json" 2>/dev/null || echo 0)
kept=$(json "int(((d['plan'].get('history') or {}).get('claims_kept')) or 0)" < "$TMP/inv.json" 2>/dev/null || echo 0)
if [ "$kept" = "0" ] && [ "$rej" = "0" ]; then bad "no Haiku claim kept or rejected (history skipped or failed?)"
elif [ "$rej" = "0" ]; then ok "Haiku made $kept claim(s); code rejected none"
else warn "code rejected $rej of $((kept+rej)) Haiku claims (model behaviour - this is the Phase 25 measurement)"; fi

# ---- 3. what was stored ----------------------------------------------------------------------
curl -s "$API/api/v1/incidents/$KEY/evidence" -H "Authorization: Bearer $SRE" > "$TMP/ev.json"
json "(len([e for e in d if e['kind']=='INCIDENT']), all(e['excerpt'].startswith('past incident ') for e in d if e['kind']=='INCIDENT'))" < "$TMP/ev.json" \
  | python3 -c "import sys; n,ptr=eval(sys.stdin.read()); print('OK' if n>0 and ptr else f'BAD {n} {ptr}')" | grep -q OK \
  && ok "similar incidents stored as INCIDENT pointers (key + category, no text)" \
  || bad "INCIDENT evidence: $(head -c 300 "$TMP/ev.json")"
curl -s "$API/api/v1/incidents/$KEY/hypotheses" -H "Authorization: Bearer $SRE" > "$TMP/h.json"
json "[(h['confidence'], bool(h['detail'].get('history_prior'))) for h in d if 'DEPLOY-4821' in h['statement']][:1]" < "$TMP/h.json" \
  | grep -q "('HIGH', True)" && ok "deploy hypothesis: band HIGH (unchanged) + history prior shown" \
  || bad "deploy hypothesis band/prior: $(json "[(h['key'], h['confidence'], h['detail'].get('history_prior')) for h in d][:3]" < "$TMP/h.json")"
curl -s "$API/api/v1/incidents/$KEY/postmortem?format=markdown" -H "Authorization: Bearer $SRE" > "$TMP/pm.md"
grep -q "## Similar past incidents" "$TMP/pm.md" && ok "postmortem has the similar-incident section" \
  || bad "postmortem has no similar-incident section"
cats=$(grep -E "^- INC-[0-9]+ \(" "$TMP/pm.md" | sed -E 's/^- INC-[0-9]+ \(([a-z_]+),.*/\1/' | sort -u | tr '\n' ' ')
[ "$cats" = "connection_pool_exhaustion " ] && ok "every similar incident is a connection-pool exhaustion (the right category)" \
  || bad "similar categories: ${cats:-none}"
grep -E "^- INC-[0-9]+ \(" "$TMP/pm.md" | grep -q ", checkout-api)" \
  && ok "at least one similar incident is on checkout-api (the service vote breaks ties between twins)" \
  || bad "no similar incident on checkout-api - run: make history-explain Q=\"$HQ\" SERVICE=checkout-api"
MGR="$(make -s token ROLE=MANAGER 2>/dev/null)"
curl -s "$API/api/v1/investigations/$INV/trace" -H "Authorization: Bearer $MGR" > "$TMP/mt.json"
json "(lambda r: bool(r) and all(r))([e['redacted'] for e in d['executions'] if e['agent']=='historical_incident'])" < "$TMP/mt.json" | grep -q True \
  && ok "MANAGER cannot read the history trace (observations + past incident text)" \
  || bad "MANAGER history trace: $(json "[(e['agent'], e['redacted']) for e in d['executions']]" < "$TMP/mt.json")"

echo; echo "------------------------------------------------------------ similar incidents + prior (read them)"
grep -E "^History|^## Similar|^- INC-|^Similar is not" "$TMP/pm.md"
echo "passed=$pass failed=$fail notes=$note   incident=$KEY investigation=$INV"
[ "$fail" = "0" ]
