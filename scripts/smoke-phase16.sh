#!/usr/bin/env bash
# Phase 16 smoke (ADR-026): human approval -> controlled execution, through the api gateway.
# On a THROWAWAY service (scripts/phase16_fixture.py), never a demo deploy:
#   IC #1 requests a rollback -> IC #1 cannot approve it (separation of duties) -> an SRE cannot
#   decide -> nobody runs the tool by hand -> IC #2 approves -> it runs AS IC #2 -> new CD record of the previous commit ->
#   incident MITIGATED -> a retry is refused -> MANAGER reads the record, cannot decide.
# Needs: make db-upgrade, make db-users (cd_svc), make tools-tokens, make agents-tokens, and
# incident-service, api, orchestrator, agents, tool-gateway restarted after those. bash 3.2 OK.
set -u
API="${AEOI_API_URL:-http://localhost:8000}"
pass=0; fail=0
ok()   { echo "✔ $1"; pass=$((pass+1)); }
bad()  { echo "✘ $1"; fail=$((fail+1)); }
json() { python3 -c "import sys,json; d=json.load(sys.stdin); print($1)"; }
TMP="$(mktemp -d)"
cleanup() { uv run --quiet python scripts/phase16_fixture.py clean >/dev/null 2>&1; rm -rf "$TMP"; }
trap cleanup EXIT

for pair in "api 8000" "incident-service 8001" "orchestrator 8002" "agents 8003" "tool-gateway 8006"; do
  set -- $pair
  code=$(curl -s -o /dev/null -w '%{http_code}' "localhost:$2/health/live")
  [ "$code" = "200" ] && ok "$1 up" || { bad "$1 not running on :$2"; exit 1; }
done
for f in secrets/action_executor_token.txt secrets/tools_incident_token.txt secrets/cd_svc_password.txt; do
  [ -s "$f" ] && ok "$f exists" || { bad "$f missing (make agents-tokens / tools-tokens / db-users)"; exit 1; }
done

IC1="$(make -s token ROLE=INCIDENT_COMMANDER NTH=1 2>/dev/null)"
IC2="$(make -s token ROLE=INCIDENT_COMMANDER NTH=2 2>/dev/null)"
SRE="$(make -s token ROLE=SRE 2>/dev/null)"
MGR="$(make -s token ROLE=MANAGER 2>/dev/null)"
[ -n "$IC2" ] || { bad "need two INCIDENT_COMMANDER users (make db-seed)"; exit 1; }

read -r SVC NEW OLD_SHA <<<"$(uv run --quiet python scripts/phase16_fixture.py plant)"
[ -n "${OLD_SHA:-}" ] && ok "planted $SVC: $NEW (running) after an earlier release" || { bad "plant failed"; exit 1; }

# post NAME TOKEN PATH BODY [extra curl args] -> status code, body in $TMP/NAME.json
post() { curl -s -o "$TMP/$1.json" -w '%{http_code}' -X POST "$API$3" -H "Authorization: Bearer $2" \
           -H "Content-Type: application/json" "${@:5}" -d "$4"; }
getj() { curl -s "$API$2" -H "Authorization: Bearer $1"; }

code=$(post inc "$IC1" /api/v1/incidents "{\"title\":\"Phase 16 smoke on $SVC\",\"severity\":\"SEV3\",\"affected_services\":[\"$SVC\"],\"detected_at\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}" -H "Idempotency-Key: smoke16-inc-$SVC")
[ "$code" = "201" ] || { bad "create incident: $code $(head -c 300 "$TMP/inc.json")"; exit 1; }
INC=$(json "d['key']" < "$TMP/inc.json")

# ---- 1. request (IC #1) ---------------------------------------------------------------------
BODY="{\"action\":\"rollback_deployment\",\"args\":{\"deploy_key\":\"$NEW\",\"reason\":\"Phase 16 smoke: errors began with $NEW\"}}"
code=$(post req "$IC1" "/api/v1/incidents/$INC/actions" "$BODY" -H "Idempotency-Key: smoke16-req-$SVC")
[ "$code" = "201" ] && [ "$(json "d['status']" < "$TMP/req.json")" = "PENDING" ] \
  && ok "$INC: rollback of $NEW requested by IC #1 (PENDING, expires in 30 min)" \
  || { bad "request: $code $(head -c 300 "$TMP/req.json")"; exit 1; }
ID=$(json "d['id']" < "$TMP/req.json")
[ "$(getj "$IC1" "/api/v1/incidents/$INC" | json "d['status']")" = "AWAITING_REVIEW" ] \
  && ok "incident is AWAITING_REVIEW" || bad "incident status not AWAITING_REVIEW"

# ---- 2. who may NOT decide ------------------------------------------------------------------
DEC='{"decision":"APPROVED","reason":"Phase 16 smoke: rollback is the safest mitigation"}'
code=$(post d1 "$IC1" "/api/v1/approvals/$ID/decision" "$DEC")
[ "$code" = "403" ] && ok "IC #1 cannot approve their own request: 403 (separation of duties)" || bad "requester decision: $code"
code=$(post d2 "$SRE" "/api/v1/approvals/$ID/decision" "$DEC")
[ "$code" = "403" ] && ok "an SRE cannot decide: 403" || bad "SRE decision: $code"
code=$(post d3 "$MGR" "/api/v1/approvals/$ID/decision" "$DEC")
[ "$code" = "403" ] && ok "a MANAGER cannot decide: 403" || bad "MANAGER decision: $code"

# ---- 3. IC #2 approves: it runs now, as IC #2 -----------------------------------------------
INCID=$(json "d['id']" < "$TMP/inc.json")
code=$(post man "$IC2" /api/v1/tools/rollback_deployment/invoke "{\"args\":{\"deploy_key\":\"$NEW\",\"reason\":\"Phase 16 smoke: errors began with $NEW\"},\"incident_id\":\"$INCID\",\"approval_id\":\"$ID\"}")
[ "$code" = "403" ] && [ "$(json "d['reason']" < "$TMP/man.json")" = "executor_only" ] \
  && ok "nobody runs the tool by hand, not even the approver: 403 executor_only" || bad "manual tool call: $code $(head -c 200 "$TMP/man.json")"
code=$(post ok "$IC2" "/api/v1/approvals/$ID/decision" "$DEC")
if [ "$code" = "200" ] && [ "$(json "d['executed']" < "$TMP/ok.json")" = "True" ]; then
  ok "IC #2 approved: executed ($(json "d['result']['items'][0]['rolled_back_from']+' -> '+d['result']['items'][0]['deploy_key']" < "$TMP/ok.json"))"
else
  bad "approve: $code $(head -c 400 "$TMP/ok.json")"
fi
[ "$(json "d['result']['items'][0]['rolled_back_to']" < "$TMP/ok.json" 2>/dev/null)" = "$OLD_SHA" ] \
  && ok "the new CD record runs the PREVIOUS commit (${OLD_SHA:0:12})" || bad "rolled back to the wrong commit"
json "d['result']['items'][0]['config_restored']" < "$TMP/ok.json" 2>/dev/null | grep -q smoke_path \
  && ok "the flag this deploy changed is set back too" || bad "config not restored"
[ "$(getj "$IC1" "/api/v1/approvals/$ID" | json "d['status']")" = "EXECUTED" ] \
  && ok "approval EXECUTED (outcome recorded)" || bad "approval status"
[ "$(getj "$IC1" "/api/v1/incidents/$INC" | json "d['status']")" = "MITIGATED" ] \
  && ok "incident MITIGATED" || bad "incident not MITIGATED"

# ---- 4. never twice --------------------------------------------------------------------------
code=$(post again "$IC2" "/api/v1/approvals/$ID/execute" "{}")
[ "$code" = "409" ] && ok "execute again: 409, nothing runs twice" || bad "replay: $code"
code=$(post dec2 "$IC2" "/api/v1/approvals/$ID/decision" '{"decision":"REJECTED","reason":"second decision attempt"}')
[ "$code" = "409" ] && ok "a second decision: 409" || bad "second decision: $code"

# ---- 5. the record ---------------------------------------------------------------------------
getj "$MGR" "/api/v1/incidents/$INC/approvals" > "$TMP/list.json"
json "[(a['status'], a['source'], a['decided_by'] is not None) for a in d]" < "$TMP/list.json" \
  | grep -q "('EXECUTED', 'manual', True)" && ok "MANAGER reads the decision record (who, why, what ran)" \
  || bad "record: $(head -c 300 "$TMP/list.json")"

echo
make -s approvals INCIDENT="$INC" 2>/dev/null || true
echo "passed=$pass failed=$fail   incident=$INC approval=$ID   (smoke deploy rows removed on exit)"
[ "$fail" = "0" ]
