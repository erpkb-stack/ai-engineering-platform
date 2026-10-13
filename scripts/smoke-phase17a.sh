#!/usr/bin/env bash
# Phase 17a smoke (ADR-027): every service trusts Keycloak USER tokens; our directory stays the
# source of truth; the refusals hold. Needs: make keycloak-up, the AEOI_OIDC_* lines in .env,
# and api, incident-service, orchestrator, agents, tool-gateway, rag RESTARTED after that.
# The release-risk call sends ONE Keycloak token through api -> orchestrator -> agents ->
# tool-gateway -> rag (code only, no model): each service verifies it. bash 3.2 OK.
set -u
API="${AEOI_API_URL:-http://localhost:8000}"
ISS="${AEOI_OIDC_ISSUER:-}"
pass=0; fail=0
ok()   { echo "✔ $1"; pass=$((pass+1)); }
bad()  { echo "✘ $1"; fail=$((fail+1)); }
json() { python3 -c "import sys,json; d=json.load(sys.stdin); print($1)"; }
claim() { python3 -c "import sys,json,base64; p=sys.argv[1].split('.')[1]; p+='='*(-len(p)%4); print(json.loads(base64.urlsafe_b64decode(p)).get('$2'))" "$1"; }
code_of() { curl -s -o /dev/null -w '%{http_code}' "$@"; }
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

[ -n "$ISS" ] || { bad "AEOI_OIDC_ISSUER is not set - add the Phase 17 lines of .env.example to .env"; exit 1; }
code=$(code_of "$ISS/.well-known/openid-configuration")
[ "$code" = "200" ] && ok "Keycloak realm up: $ISS" || { bad "Keycloak not reachable at $ISS ($code) - make keycloak-up"; exit 1; }
for pair in "api 8000" "incident-service 8001" "orchestrator 8002" "agents 8003" "rag 8004" "tool-gateway 8006"; do
  set -- $pair
  [ "$(code_of "localhost:$2/health/live")" = "200" ] && ok "$1 up" || { bad "$1 not running on :$2"; exit 1; }
done

kc_token() {  # the script's OWN error is shown (it was hidden: a cold-start timeout read as "no tokens")
  uv run --quiet python scripts/kc_token.py --role "$1" 2>"$TMP/kc.err" \
    || { echo "✘ Keycloak token for $1: $(grep -v '^#' "$TMP/kc.err" | tail -1)" >&2; exit 1; }
}
SRE="$(kc_token SRE)" || exit 1
IC="$(kc_token INCIDENT_COMMANDER)" || exit 1
MGR="$(kc_token MANAGER)" || exit 1
[ "$(claim "$SRE" iss)" = "$ISS" ] && [ "$(claim "$SRE" typ)" = "Bearer" ] \
  && ok "token from Keycloak: iss=$ISS, uid=$(claim "$SRE" uid) (OUR directory id)" || bad "unexpected token claims"

# ---- 1. every service on the path verifies it ---------------------------------------------
code=$(code_of "$API/api/v1/incidents?limit=1" -H "Authorization: Bearer $SRE")
[ "$code" = "200" ] && ok "api + incident-service accept a Keycloak SRE token" || bad "incidents with Keycloak token: $code"
code=$(curl -s -o "$TMP/rr.json" -w '%{http_code}' -X POST "$API/api/v1/release-risk" \
  -H "Authorization: Bearer $SRE" -H "Content-Type: application/json" -d '{"deploy_key":"DEPLOY-4821"}')
[ "$code" = "201" ] && [ "$(json "d['band']" < "$TMP/rr.json")" = "HIGH" ] \
  && ok "release risk DEPLOY-4821 = HIGH with a Keycloak token (api -> orchestrator -> agents -> tool-gateway -> rag)" \
  || bad "release risk with Keycloak token: $code $(head -c 300 "$TMP/rr.json")"

# ---- 2. authorisation is OUR permissions, from the realm roles ----------------------------
code=$(code_of -X POST "$API/api/v1/release-risk" -H "Authorization: Bearer $MGR" \
  -H "Content-Type: application/json" -d '{"deploy_key":"DEPLOY-4821"}')
[ "$code" = "403" ] && ok "MANAGER (Keycloak) cannot ask for release risk: 403" || bad "MANAGER release risk: $code"
code=$(code_of -X POST "$API/api/v1/approvals/00000000-0000-7000-8000-000000000000/decision" \
  -H "Authorization: Bearer $SRE" -H "Content-Type: application/json" \
  -d '{"decision":"APPROVED","reason":"kc smoke: an SRE may not decide"}')
[ "$code" = "403" ] && ok "SRE (Keycloak) cannot decide an approval: 403" || bad "SRE decision: $code"
code=$(code_of -X POST "$API/api/v1/approvals/00000000-0000-7000-8000-000000000000/decision" \
  -H "Authorization: Bearer $IC" -H "Content-Type: application/json" \
  -d '{"decision":"APPROVED","reason":"kc smoke: an IC passes authorisation"}')
[ "$code" = "404" ] && ok "IC (Keycloak) passes authorisation (404: no such approval)" || bad "IC decision: $code"

# ---- 3. refusals -------------------------------------------------------------------------
TAMPERED="${SRE%?????}AAAAA"
code=$(code_of "$API/api/v1/incidents?limit=1" -H "Authorization: Bearer $TAMPERED")
[ "$code" = "401" ] && ok "tampered Keycloak token: 401" || bad "tampered token: $code"
read -r EMAIL PW <<<"$(head -1 secrets/keycloak/users.txt)"
curl -s -X POST "$ISS/protocol/openid-connect/token" -d grant_type=password -d client_id=aeoi-cli \
  -d scope=openid --data-urlencode "username=$EMAIL" --data-urlencode "password=$PW" > "$TMP/id.json"
IDT="$(json "d.get('id_token','')" < "$TMP/id.json")"
code=$(code_of "$API/api/v1/incidents?limit=1" -H "Authorization: Bearer $IDT")
[ -n "$IDT" ] && [ "$code" = "401" ] && ok "an ID token used as an access token: 401" || bad "ID token as bearer: $code"
DEV="$(make -s token ROLE=SRE 2>/dev/null)"
code=$(code_of "$API/api/v1/incidents?limit=1" -H "Authorization: Bearer $DEV")
[ "$code" = "200" ] && ok "dev tokens (make token) still work for tests and smokes (production refuses them)" \
  || bad "dev token: $code"

# ---- 4. nobody can rewrite who they are --------------------------------------------------
KC="${ISS%/realms/*}"
ADMIN="$(curl -s -X POST "$KC/realms/master/protocol/openid-connect/token" -d grant_type=password \
  -d client_id=admin-cli -d username=admin --data-urlencode "password=$(cat secrets/keycloak_admin_password.txt)" \
  | json "d['access_token']")"
H="Authorization: Bearer $ADMIN"
KID="$(curl -s -H "$H" "$KC/admin/realms/aeoi/users?username=$EMAIL&exact=true" | json "d[0]['id']")"
ACC="$(curl -s -H "$H" "$KC/admin/realms/aeoi/clients?clientId=account" | json "d[0]['id']")"
ROLE="$(curl -s -H "$H" "$KC/admin/realms/aeoi/clients/$ACC/roles/manage-account")"
curl -s -o /dev/null -X POST -H "$H" -H "Content-Type: application/json" \
  "$KC/admin/realms/aeoi/users/$KID/role-mappings/clients/$ACC" -d "[$ROLE]"   # let them TRY
UT="$(curl -s -X POST "$ISS/protocol/openid-connect/token" -d grant_type=password -d client_id=aeoi-cli \
  --data-urlencode "username=$EMAIL" --data-urlencode "password=$PW" | json "d['access_token']")"
for field in uid aeoi_sub firstName; do   # EACH alone (review: together, one guard hid the other)
  curl -s -H "Accept: application/json" -H "Authorization: Bearer $UT" "$ISS/account/" \
    | python3 -c "import sys,json; a=json.load(sys.stdin); f='$field'
if f=='firstName': a['firstName']='Someone'
else: a.setdefault('attributes',{})[f]=['tampered']
print(json.dumps(a))" > "$TMP/acct.json"
  code=$(code_of -X POST -H "Accept: application/json" -H "Authorization: Bearer $UT" \
    -H "Content-Type: application/json" "$ISS/account/" -d @"$TMP/acct.json")
  [ "$code" = "400" ] && ok "a user cannot rewrite their own $field (400 read-only)" || bad "self-edit of $field: $code"
done
curl -s -o /dev/null -X DELETE -H "$H" -H "Content-Type: application/json" \
  "$KC/admin/realms/aeoi/users/$KID/role-mappings/clients/$ACC" -d "[$ROLE]"   # undo
code=$(code_of -X POST "$ISS/protocol/openid-connect/token" -d grant_type=password -d client_id=aeoi-cli \
  -d scope=offline_access --data-urlencode "username=$EMAIL" --data-urlencode "password=$PW")
[ "$code" = "400" ] && ok "no offline (30-day) tokens: 400" || bad "offline token: $code"
grep -q "$PW" secrets/keycloak/aeoi-realm.json && bad "plaintext password in the realm file" \
  || ok "the realm file holds password hashes only"

echo "passed=$pass failed=$fail"
[ "$fail" = "0" ]
