#!/usr/bin/env bash
# Phase 7 smoke: tools through the api gateway (manual mode) and directly as an agent.
# Needs running: make run-api, make run-tools, make run-audit (+ run-llm/run-rag for knowledge).
# Uses the seeded demo (checkout-api, DEPLOY-4821). macOS bash 3.2 compatible. Exit 1 on failure.
set -u
API="${AEOI_API_URL:-http://localhost:8000}"
TOOLS="${AEOI_TOOLS_URL:-http://localhost:8006}"
AUDIT="${AEOI_AUDIT_URL:-http://localhost:8008}"
pass=0; fail=0; note=0
ok()   { echo "✔ $1"; pass=$((pass+1)); }
bad()  { echo "✘ $1"; fail=$((fail+1)); }
warn() { echo "! $1"; note=$((note+1)); }
json() { python3 -c "import sys,json; d=json.load(sys.stdin); print($1)"; }
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
W='"start":"2026-10-02T09:00:00Z","end":"2026-10-02T11:00:00Z"'

SRE="$(make -s token ROLE=SRE 2>/dev/null)"
MGR="$(make -s token ROLE=MANAGER 2>/dev/null)"
IC="$(make -s token ROLE=INCIDENT_COMMANDER 2>/dev/null)"
AGT="$(make -s service-token SERVICE=agents SCOPES=tools:invoke 2>/dev/null)"
[ -n "$SRE" ] && [ -n "$MGR" ] && [ -n "$IC" ] && [ -n "$AGT" ] && ok "tokens: SRE, MANAGER, IC, service:agents" || { bad "token minting"; exit 1; }

for pair in "tool-gateway $TOOLS" "audit $AUDIT"; do
  set -- $pair
  code=$(curl -s -o /dev/null -w '%{http_code}' "$2/health/ready")
  [ "$code" = "200" ] && ok "$1 ready" || { bad "$1 not ready ($code) - is make run-${1%%-*} running?"; exit 1; }
done

tool() { # $1 token, $2 tool, $3 json body, $4 outfile -> http code (via the api gateway)
  curl -s -o "$4" -w '%{http_code}' -X POST "$API/api/v1/tools/$2/invoke" -H "Authorization: Bearer $1" \
    -H "Content-Type: application/json" -d "$3" --max-time 30
}
agent() { # $1 agent, $2 tool, $3 args json, $4 outfile [$5 extra header] -> http code (direct)
  curl -s -o "$4" -w '%{http_code}' -X POST "$TOOLS/v1/tools/$2/invoke" -H "Authorization: Bearer $AGT" \
    -H "X-On-Behalf-Of: Bearer $SRE" -H "Content-Type: application/json" \
    -d "{\"agent_name\":\"$1\",\"args\":$3}" --max-time 30
}

code=$(curl -s -o "$TMP/list.json" -w '%{http_code}' "$API/api/v1/tools" -H "Authorization: Bearer $SRE")
[ "$code" = "200" ] && ok "SRE sees $(json "len(d)" < "$TMP/list.json") tools: $(json "', '.join(t['name'] for t in d)" < "$TMP/list.json")" || bad "list tools: $code"

code=$(tool "$SRE" search_logs "{\"args\":{\"service_key\":\"checkout-api\",$W}}" "$TMP/logs.json")
if [ "$code" = "200" ]; then
  ok "search_logs: $(json "f\"{d['data']['total_matching']} matching, top codes {dict(list(d['data']['counts_by_error_code'].items())[:3])}, {d['latency_ms']} ms\"" < "$TMP/logs.json")"
  CALL_ID=$(json "d['tool_call_id']" < "$TMP/logs.json")
else bad "search_logs: $code $(head -c 300 "$TMP/logs.json")"; CALL_ID=""; fi

code=$(tool "$SRE" query_metrics "{\"args\":{\"service_key\":\"checkout-api\",\"metric\":\"db_pool_utilization\",$W}}" "$TMP/m.json")
[ "$code" = "200" ] && ok "query_metrics db_pool_utilization: $(json "d['data']['items'][0]['stats'] if d['data']['items'] else 'no data'" < "$TMP/m.json")" || bad "query_metrics: $code"

code=$(tool "$SRE" get_config_diff '{"args":{"deploy_key":"DEPLOY-4821"}}' "$TMP/cd.json")
[ "$code" = "200" ] && json "[c['key'] for c in d['data']['items'][0]['changes']]" < "$TMP/cd.json" | grep -q order_batching \
  && ok "get_config_diff DEPLOY-4821: $(json "[(c['key'], c['before'], c['after']) for c in d['data']['items'][0]['changes']]" < "$TMP/cd.json")" \
  || bad "get_config_diff: $code $(head -c 200 "$TMP/cd.json")"

code=$(tool "$SRE" get_commit '{"args":{"sha":"42d79c2"}}' "$TMP/c.json")
[ "$code" = "200" ] && ok "get_commit 42d79c2 (prefix): $(json "d['data']['items'][0]['message'] if d['data']['items'] else 'NOT FOUND'" < "$TMP/c.json")" || bad "get_commit: $code"

code=$(tool "$SRE" search_runbooks '{"args":{"query":"checkout-api database connection pool exhausted"}}' "$TMP/rb.json")
if [ "$code" = "200" ]; then ok "search_runbooks (as the user, via rag): $(json "d['data']['items'][0]['source_uri'] if d['data']['items'] else 'no hits'" < "$TMP/rb.json")"
else warn "search_runbooks: $code - is make run-rag running? ($(json "d.get('detail')" < "$TMP/rb.json" 2>/dev/null))"; fi

code=$(tool "$MGR" search_logs "{\"args\":{\"service_key\":\"checkout-api\",$W}}" "$TMP/mgr.json")
[ "$code" = "403" ] && ok "MANAGER search_logs -> 403 $(json "d['reason']" < "$TMP/mgr.json")" || bad "MANAGER logs: $code"

code=$(tool "$IC" rollback_deployment '{"args":{"deploy_key":"DEPLOY-4821","reason":"p95 regression after deploy"}}' "$TMP/rb2.json")
[ "$code" = "403" ] && ok "IC rollback without approval -> 403 $(json "d['reason']" < "$TMP/rb2.json")" || bad "rollback: $code (MUST be 403)"

code=$(tool "$SRE" search_logs '{"args":{"service_key":"checkout-api","start":"2026-10-01T00:00:00Z","end":"2026-10-03T00:00:00Z"}}' "$TMP/wide.json")
[ "$code" = "422" ] && ok "48h window -> 422 (bounded inputs)" || bad "wide window: $code"

code=$(agent log_analysis search_logs "{\"service_key\":\"checkout-api\",$W}" "$TMP/a1.json")
[ "$code" = "200" ] && ok "agent log_analysis -> search_logs 200 (service token + user OBO)" || bad "agent call: $code $(head -c 200 "$TMP/a1.json")"
code=$(agent log_analysis get_commit '{"sha":"42d79c2"}' "$TMP/a2.json")
[ "$code" = "403" ] && ok "agent log_analysis -> get_commit 403 $(json "d['reason']" < "$TMP/a2.json")" || bad "allow-list: $code"
code=$(curl -s -o "$TMP/a3.json" -w '%{http_code}' -X POST "$TOOLS/v1/tools/search_logs/invoke" -H "Authorization: Bearer $AGT" \
  -H "Content-Type: application/json" -d "{\"agent_name\":\"log_analysis\",\"args\":{\"service_key\":\"checkout-api\",$W}}")
[ "$code" = "403" ] && ok "service token WITHOUT a user -> 403 $(json "d['reason']" < "$TMP/a3.json")" || bad "no OBO: $code"

if [ -n "$CALL_ID" ]; then
  found=""
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    curl -s "$API/api/v1/audit/events?limit=200" -H "Authorization: Bearer $SRE" > "$TMP/aud.json"
    if json "[i['details'].get('tool_call_id') for i in d['items']]" < "$TMP/aud.json" 2>/dev/null | grep -q "$CALL_ID"; then found=1; break; fi
    sleep 1
  done
  [ -n "$found" ] && ok "audit event for the search_logs call delivered (scope=$(json "d['scope']" < "$TMP/aud.json"))" \
    || bad "audit event not delivered in 10 s - relay running? token? (make tools-tokens; tool-gateway logs)"
fi

echo; echo "passed=$pass failed=$fail notes=$note"
[ "$fail" = "0" ]
