#!/usr/bin/env bash
# Phase 17b demo: a PENDING rollback request to decide IN THE CONSOLE (not by curl).
# On a THROWAWAY service (scripts/phase16_fixture.py), never a demo deploy. IC #1 requests it
# with a Keycloak token through the api; you then sign in as IC #2 and approve it next to the
# evidence. Clean up with `make ui-demo-clean`. Needs: the Phase 16 services (api, incident,
# orchestrator, agents, tool-gateway), make keycloak-up, make ui-dev. bash 3.2 OK.
set -u
API="${AEOI_API_URL:-http://localhost:8000}"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
json() { python3 -c "import sys,json; d=json.load(sys.stdin); print($1)"; }
kc() { uv run --quiet python scripts/kc_token.py --role INCIDENT_COMMANDER --nth "$1" 2>"$TMP/kc$1.err" \
  || { echo "✘ Keycloak token for IC #$1: $(grep -v '^#' "$TMP/kc$1.err" | tail -1)" >&2; exit 1; }; }
IC1="$(kc 1)" || exit 1
kc 2 >/dev/null || exit 1
IC2_EMAIL="$(sed -n 's/^# \([^ ]*\) role=.*/\1/p' "$TMP/kc2.err")"

read -r SVC NEW OLD_SHA <<<"$(uv run --quiet python scripts/phase16_fixture.py plant)"
[ -n "${OLD_SHA:-}" ] || { echo "✘ plant failed (make db-users for cd_svc?)"; exit 1; }
post() { curl -s -o "$TMP/$1.json" -w '%{http_code}' -X POST "$API$2" -H "Authorization: Bearer $IC1" \
           -H "Content-Type: application/json" -H "Idempotency-Key: ui17b-$1-$SVC" -d "$3"; }
code=$(post inc /api/v1/incidents "{\"title\":\"UI demo: errors after $NEW on $SVC\",\"severity\":\"SEV3\",\"affected_services\":[\"$SVC\"],\"detected_at\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}")
[ "$code" = "201" ] || { echo "✘ create incident: $code $(head -c 300 "$TMP/inc.json")"; exit 1; }
INC=$(json "d['key']" < "$TMP/inc.json")
code=$(post req "/api/v1/incidents/$INC/actions" "{\"action\":\"rollback_deployment\",\"args\":{\"deploy_key\":\"$NEW\",\"reason\":\"UI demo: errors began with $NEW\"}}")
[ "$code" = "201" ] || { echo "✘ request: $code $(head -c 300 "$TMP/req.json")"; exit 1; }
cat <<MSG
✔ $INC: rollback of $NEW requested by IC #1 (PENDING, expires in 30 min) on throwaway service $SVC
Now, in the browser (make ui-dev running):
  1. http://localhost:5173/approvals      - the request is in the inbox (and the nav badge)
  2. sign in as IC #2: $IC2_EMAIL  (password: its line in secrets/keycloak/users.txt)
  3. open it, read the evidence, give a reason, "Approve and run" -> Executed, incident MITIGATED
  Also try: signed in as IC #1 the buttons are off (you requested it); as an SRE too.
Afterwards: make ui-demo-clean   (removes the throwaway deploy rows)
MSG
