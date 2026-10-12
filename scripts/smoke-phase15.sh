#!/usr/bin/env bash
# Phase 15 smoke (ADR-025): ADVISORY release risk, code only, through the api gateway.
# The planted N+1 (PR #812 / DEPLOY-4821) and the planted rename (PR #377) must be HIGH with the
# right factor; a redeploy and a routine deploy must be LOW; a release candidate range works
# before anything ships; a retry is idempotent; MANAGER cannot ask. Needs: make code-index.
# Prints the DEPLOY-4821 report at the end: READ IT (the "way back" lines). bash 3.2 OK.
set -u
API="${AEOI_API_URL:-http://localhost:8000}"
pass=0; fail=0
ok()   { echo "✔ $1"; pass=$((pass+1)); }
bad()  { echo "✘ $1"; fail=$((fail+1)); }
json() { python3 -c "import sys,json; d=json.load(sys.stdin); print($1)"; }
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

for pair in "api 8000" "orchestrator 8002" "agents 8003" "rag 8004" "tool-gateway 8006" "audit 8008"; do
  set -- $pair
  code=$(curl -s -o /dev/null -w '%{http_code}' "localhost:$2/health/live")
  [ "$code" = "200" ] && ok "$1 up" || { bad "$1 not running on :$2"; exit 1; }
done
SRE="$(make -s token ROLE=SRE 2>/dev/null)"
MGR="$(make -s token ROLE=MANAGER 2>/dev/null)"
# rr NAME BODY [extra curl args]: POST, keep the body in $TMP/NAME.json, echo the status code
rr() { curl -s -o "$TMP/$1.json" -w '%{http_code}' -X POST "$API/api/v1/release-risk" \
         -H "Authorization: Bearer ${TOKEN:-$SRE}" -H "Content-Type: application/json" "${@:3}" -d "$2"; }

# ---- 1. the planted bad deploy -------------------------------------------------------------
KEY="smoke15-$(date +%s)"
code=$(rr d4821 '{"deploy_key":"DEPLOY-4821"}' -H "Idempotency-Key: $KEY")
[ "$code" = "201" ] || { bad "POST DEPLOY-4821: $code $(head -c 300 "$TMP/d4821.json")"; exit 1; }
json "(d['band'], d['status'], d['report']['coverage'], d['report']['advisory'])" < "$TMP/d4821.json" \
  | grep -q "('HIGH', 'SUCCEEDED', 'full', True)" && ok "DEPLOY-4821: HIGH, fully checked, advisory" \
  || bad "DEPLOY-4821: $(json "(d['band'], d['status'], d['report'].get('coverage'), d.get('error'))" < "$TMP/d4821.json")"
json "[f['kind'] for f in d['report']['factors'] if f['severity']=='HIGH']" < "$TMP/d4821.json" | grep -q "code_mechanism" \
  && ok "the HIGH factor is the query added in the loop (code_mechanism)" \
  || bad "no HIGH code_mechanism: $(json "[(f['kind'], f['severity']) for f in d['report']['factors']]" < "$TMP/d4821.json")"
json "[s['kind'] for s in d['report']['rollback']]" < "$TMP/d4821.json" | grep -q "\['redeploy', 'flag_not_read'\]" \
  && ok "way back: a redeploy; flipping order_batching back would NOT undo the code (the Phase 14 lesson)" \
  || bad "rollback: $(json "[s['kind'] for s in d['report']['rollback']]" < "$TMP/d4821.json")"
json "set(e for f in d['report']['factors'] for e in f['evidence_ids']) <= set(e['evidence_key'] for e in d['evidence'])" < "$TMP/d4821.json" \
  | grep -q True && ok "every factor's evidence is in the report" || bad "a factor cites evidence the report does not hold"
ID=$(json "d['id']" < "$TMP/d4821.json")
BASE=$(json "d['report']['base_sha']" < "$TMP/d4821.json"); HEAD=$(json "d['report']['head_sha']" < "$TMP/d4821.json")
code=$(rr again '{"deploy_key":"DEPLOY-4821"}' -H "Idempotency-Key: $KEY")
[ "$code" = "200" ] && [ "$(json "d['id']" < "$TMP/again.json")" = "$ID" ] \
  && ok "retry with the same Idempotency-Key: 200, the same report" || bad "retry: $code"

# ---- 2. before it ships: the PR and a release-candidate range ------------------------------
code=$(rr p812 '{"repository":"northwind/checkout","pull_request":812}')
json "(d['band'], d['report']['head_sha'][:12])" < "$TMP/p812.json" | grep -q "('HIGH', '$(echo "$HEAD" | cut -c1-12)')" \
  && ok "PR #812 (before merge-to-deploy): HIGH, the same commit as DEPLOY-4821" \
  || bad "PR #812: $code $(head -c 300 "$TMP/p812.json")"
code=$(rr rng "{\"service_key\":\"checkout-api\",\"base_sha\":\"$BASE\",\"head_sha\":\"$HEAD\"}")
json "(d['band'], d['report']['target_kind'])" < "$TMP/rng.json" | grep -q "('HIGH', 'range')" \
  && ok "release candidate checkout-api ${BASE:0:7}..${HEAD:0:7}: HIGH before it ships" \
  || bad "range: $code $(head -c 300 "$TMP/rng.json")"
code=$(rr p377 '{"repository":"northwind/shipping","pull_request":377}')
json "[f['statement'] for f in d['report']['factors'] if f['kind']=='breaking_change' and f['severity']=='HIGH']" < "$TMP/p377.json" \
  | grep -q "4 other service" && ok "PR #377: HIGH breaking change - 4 other services still read ShipmentResponse.address" \
  || bad "PR #377: $(json "(d['band'], [(f['kind'], f['severity']) for f in d['report']['factors']], d.get('error'))" < "$TMP/p377.json") - run: make code-index"

# ---- 3. it must stay quiet when nothing is wrong (a warning that always fires is ignored) ---
rr r1247 '{"deploy_key":"DEPLOY-1247"}' >/dev/null
json "(d['band'], [f['kind'] for f in d['report']['factors']])" < "$TMP/r1247.json" | grep -q "('LOW', \['nothing_shipped'\])" \
  && ok "DEPLOY-1247 (a redeploy of the running commit): LOW, nothing ships" \
  || bad "DEPLOY-1247: $(json "(d['band'], [f['kind'] for f in d['report']['factors']])" < "$TMP/r1247.json")"
rr r1186 '{"deploy_key":"DEPLOY-1186"}' >/dev/null
json "(d['band'], d['report']['coverage'])" < "$TMP/r1186.json" | grep -q "('LOW', 'full')" \
  && ok "DEPLOY-1186 (routine commits): LOW, fully checked" \
  || bad "DEPLOY-1186: $(json "(d['band'], [(f['kind'], f['severity']) for f in d['report']['factors']])" < "$TMP/r1186.json")"

# ---- 4. who may ask and read ---------------------------------------------------------------
code=$(TOKEN="$MGR" rr mgr '{"deploy_key":"DEPLOY-4821"}')
[ "$code" = "403" ] && ok "MANAGER cannot ask (no code:read): 403" || bad "MANAGER POST: $code"
code=$(curl -s -o /dev/null -w '%{http_code}' "$API/api/v1/release-risk/$ID" -H "Authorization: Bearer $MGR")
[ "$code" = "403" ] && ok "MANAGER cannot read a report (it quotes code): 403" || bad "MANAGER GET: $code"
curl -s "$API/api/v1/release-risk?deploy_key=DEPLOY-4821" -H "Authorization: Bearer $SRE" > "$TMP/list.json"
json "'$ID' in [x['id'] for x in d]" < "$TMP/list.json" | grep -q True \
  && ok "GET ?deploy_key=DEPLOY-4821 lists the stored report" || bad "list: $(head -c 200 "$TMP/list.json")"

echo; echo "------------------------------------------------------------ DEPLOY-4821 (read it)"
make -s release-risk DEPLOY=DEPLOY-4821 2>/dev/null || true
echo "passed=$pass failed=$fail   report=$ID"
[ "$fail" = "0" ]
