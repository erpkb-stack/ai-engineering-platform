#!/usr/bin/env bash
# Phase 4 smoke test against RUNNING services (make dev). Prints each step; exits 1 on failure.
# Needs: curl, python3. macOS bash 3.2 compatible.
set -u
API="${AEOI_API_URL:-http://localhost:8000}"
pass=0; fail=0
ok()  { echo "✔ $1"; pass=$((pass+1)); }
bad() { echo "✘ $1"; fail=$((fail+1)); }
json() { python3 -c "import sys,json; d=json.load(sys.stdin); print($1)"; }
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

SRE="$(make -s token ROLE=SRE 2>/dev/null)"
IC="$(make -s token ROLE=INCIDENT_COMMANDER 2>/dev/null)"
MGR="$(make -s token ROLE=MANAGER 2>/dev/null)"
[ -n "$SRE" ] && [ -n "$IC" ] && [ -n "$MGR" ] && ok "dev tokens minted for SRE, IC, MANAGER" || { bad "token minting (run make db-seed?)"; exit 1; }

code=$(curl -s -o /dev/null -w '%{http_code}' "$API/api/v1/health")
[ "$code" = "200" ] && ok "gateway health 200" || bad "gateway health: $code"

code=$(curl -s -o /dev/null -w '%{http_code}' "$API/api/v1/incidents")
[ "$code" = "401" ] && ok "no token -> 401" || bad "no token: $code"

KEY="smoke-$(date +%s)-$$"
BODY='{"title":"HTTP 500 errors increased 42% after deployment","severity":"SEV2","affected_services":["checkout-api"]}'
code=$(curl -s -o "$TMP/inc.json" -D "$TMP/h1" -w '%{http_code}' -X POST "$API/api/v1/incidents" \
  -H "Authorization: Bearer $SRE" -H "Content-Type: application/json" -H "Idempotency-Key: $KEY" -d "$BODY")
[ "$code" = "201" ] && ok "SRE declares incident -> 201" || { bad "create: $code $(cat "$TMP/inc.json")"; exit 1; }
ID=$(json "d['id']" < "$TMP/inc.json"); INC=$(json "d['key']" < "$TMP/inc.json")
echo "   $INC  id=$ID"

code=$(curl -s -o "$TMP/inc2.json" -D "$TMP/h2" -w '%{http_code}' -X POST "$API/api/v1/incidents" \
  -H "Authorization: Bearer $SRE" -H "Content-Type: application/json" -H "Idempotency-Key: $KEY" -d "$BODY")
ID2=$(json "d['id']" < "$TMP/inc2.json")
grep -qi "idempotent-replayed: true" "$TMP/h2" && [ "$ID" = "$ID2" ] && ok "retry with same Idempotency-Key -> same incident (replayed)" || bad "idempotency replay"

code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$API/api/v1/incidents" -H "Authorization: Bearer $MGR" \
  -H "Content-Type: application/json" -H "Idempotency-Key: mgr-$KEY" -d "$BODY")
[ "$code" = "403" ] && ok "MANAGER cannot declare -> 403" || bad "manager create: $code"

code=$(curl -s -o "$TMP/get.json" -w '%{http_code}' "$API/api/v1/incidents/$INC" -H "Authorization: Bearer $SRE")
[ "$code" = "200" ] && ok "GET by human key $INC -> 200" || bad "get: $code"

code=$(curl -s -o /dev/null -w '%{http_code}' -X PATCH "$API/api/v1/incidents/$ID" -H "Authorization: Bearer $IC" \
  -H "Content-Type: application/json" -H 'If-Match: "99"' -d '{"severity":"SEV1","reason":"customer impact"}')
[ "$code" = "412" ] && ok "stale If-Match -> 412 (optimistic locking)" || bad "stale patch: $code"

code=$(curl -s -o /dev/null -w '%{http_code}' -X PATCH "$API/api/v1/incidents/$ID" -H "Authorization: Bearer $IC" \
  -H "Content-Type: application/json" -H 'If-Match: "1"' -d '{"severity":"SEV1","reason":"customer impact"}')
[ "$code" = "200" ] && ok "IC raises severity with If-Match -> 200" || bad "patch: $code"

code=$(curl -s -o "$TMP/inv.json" -w '%{http_code}' -X POST "$API/api/v1/incidents/$ID/investigate" \
  -H "Authorization: Bearer $SRE" -H "Idempotency-Key: inv-$KEY")
[ "$code" = "202" ] && ok "investigation requested -> 202 (async)" || bad "investigate: $code"

curl -s "$API/api/v1/incidents/$ID/timeline" -H "Authorization: Bearer $SRE" > "$TMP/tl.json"
events=$(json "','.join(e['event_type'] for e in d)" < "$TMP/tl.json")
[ "$events" = "IncidentCreated,IncidentUpdated,InvestigationRequested" ] && ok "timeline: $events" || bad "timeline: $events"

code=$(curl -s -o "$TMP/nf.json" -w '%{http_code}' "$API/api/v1/incidents/INC-1" -H "Authorization: Bearer $SRE")
cid=$(json "d.get('correlation_id','')" < "$TMP/nf.json")
[ "$code" = "404" ] && [ -n "$cid" ] && ok "unknown incident -> 404 problem+json (correlation_id $cid)" || bad "404: $code"

echo "== $pass passed, $fail failed =="
[ "$fail" -eq 0 ]
