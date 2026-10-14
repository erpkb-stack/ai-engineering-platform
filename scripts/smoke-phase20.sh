#!/usr/bin/env bash
# Phase 20 smoke (ADR-005, ADR-032): the Java service catalog serves its contract, refuses the
# wrong callers, and answers the tool EXACTLY like the JSON file (parity over every key/team).
# Needs: make db-seed, make catalog-build, make catalog-import, make db-users, make tools-tokens,
# make run-catalog. For the gateway check also run-api + run-tools. bash 3.2 OK.
set -u
CAT="${AEOI_CATALOG_URL:-http://localhost:8010}"
API="${AEOI_API_URL:-http://localhost:8000}"
pass=0; fail=0
ok()  { echo "✔ $1"; pass=$((pass+1)); }
bad() { echo "✘ $1"; fail=$((fail+1)); }
json() { python3 -c "import sys,json; d=json.load(sys.stdin); print($1)"; }
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

code=$(curl -s -o "$TMP/h.json" -w '%{http_code}' "$CAT/actuator/health")
[ "$code" = "200" ] && ok "service-catalog up: $(head -c 80 "$TMP/h.json")" \
  || { bad "service-catalog not running on $CAT ($code): make run-catalog"; exit 1; }
pid=$(lsof -nP -t -iTCP:"${CAT##*:}" -sTCP:LISTEN 2>/dev/null | head -1)
[ -n "$pid" ] && echo "  (JVM resident memory now: $(( $(ps -o rss= -p "$pid") / 1024 )) MB, -Xmx256m)"

curl -s "$CAT/v1/openapi.yaml" > "$TMP/served.yaml"
cmp -s "$TMP/served.yaml" services/service-catalog/openapi.yaml \
  && ok "the running jar serves the committed contract (built from this openapi.yaml)" \
  || bad "the served contract differs from services/service-catalog/openapi.yaml: make catalog-build"

TOK="$(cat secrets/tools_catalog_token.txt 2>/dev/null)"
[ -n "$TOK" ] || { bad "no secrets/tools_catalog_token.txt: make tools-tokens"; exit 1; }
OTHER="$(make -s service-token SERVICE=tool-gateway SCOPES=audit:write 2>/dev/null)"
USER_TOK="$(make -s token ROLE=SRE 2>/dev/null)"
q="$CAT/v1/services?service_key=checkout-api"
c1=$(curl -s -o /dev/null -w '%{http_code}' "$q")
c2=$(curl -s -o /dev/null -w '%{http_code}' "$q" -H "Authorization: Bearer $USER_TOK")
c3=$(curl -s -o /dev/null -w '%{http_code}' "$q" -H "Authorization: Bearer $OTHER")
c4=$(curl -s -o "$TMP/one.json" -w '%{http_code}' "$q" -H "Authorization: Bearer $TOK")
[ "$c1" = "401" ] && [ "$c2" = "401" ] && [ "$c3" = "403" ] && [ "$c4" = "200" ] \
  && ok "no token 401, a USER token 401, a service without catalog:read 403, the gateway's token 200" \
  || bad "auth: none=$c1 user=$c2 wrong-scope=$c3 right=$c4 (expected 401 401 403 200)"

c=$(curl -s -o "$TMP/p.json" -w '%{http_code}' "$CAT/v1/services" -H "Authorization: Bearer $TOK")
[ "$c" = "400" ] && ok "no filter = 400 problem (no full dumps): $(json "d.get('detail','')" < "$TMP/p.json")" \
  || bad "no filter: $c (expected 400)"

uv run --quiet python scripts/phase20_parity.py "$CAT" > "$TMP/parity" 2>&1; pc=$?
sed 's/^/  /' "$TMP/parity"
[ "$pc" = "0" ] && ok "parity: the Java catalog answers exactly like the JSON file ($(tail -1 "$TMP/parity"))" \
  || bad "parity: the two sources disagree (above)"

# the tool through the api + gateway (whichever source the gateway is configured with)
if [ "$(curl -s -o /dev/null -w '%{http_code}' "$API/health/live")" = "200" ]; then
  src=$(grep -E '^AEOI_TOOLS_CATALOG_URL=.+' .env 2>/dev/null | head -1 | cut -d= -f2-)
  c=$(curl -s -o "$TMP/t.json" -w '%{http_code}' -X POST "$API/api/v1/tools/query_service_catalog/invoke" \
      -H "Authorization: Bearer $USER_TOK" -H "Content-Type: application/json" \
      -d '{"args":{"service_key":"checkout-api"}}' --max-time 30)
  same=$(python3 - "$TMP/t.json" "$TMP/one.json" <<'EOF'
import json, sys
tool = json.load(open(sys.argv[1]))["data"]["items"]
java = json.load(open(sys.argv[2]))["items"]
strip = lambda xs: [{k: v for k, v in x.items() if k != "evidence_id"} for x in xs]
print("yes" if strip(tool) == java else "no")
EOF
)
  [ "$c" = "200" ] && [ "$same" = "yes" ] \
    && ok "tool query_service_catalog via api -> gateway = the Java answer (gateway source: ${src:-JSON file})" \
    || bad "tool via gateway: $c, same as Java: $same"
else
  echo "  (api not running: gateway check skipped - make dev + make run-tools to include it)"
fi

echo "passed=$pass failed=$fail"
[ "$fail" = "0" ]
