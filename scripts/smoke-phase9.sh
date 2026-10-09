#!/usr/bin/env bash
# Phase 9 smoke: the PRODUCT path. POST /api/v1/incidents/{key}/investigate -> orchestrator
# (202) -> delegation grant -> LangGraph graph -> agent -> evidence; then the read APIs and the
# delegation record. The crash + resume check is manual (docs/phases/phase-09.md §10): this
# script must not kill the orchestrator you are watching.
# Needs: make dev, run-llm, run-tools, run-audit, run-agents, run-orch. macOS bash 3.2 OK.
set -u
API="${AEOI_API_URL:-http://localhost:8000}"
pass=0; fail=0; note=0
ok()   { echo "✔ $1"; pass=$((pass+1)); }
bad()  { echo "✘ $1"; fail=$((fail+1)); }
warn() { echo "! $1"; note=$((note+1)); }
json() { python3 -c "import sys,json; d=json.load(sys.stdin); print($1)"; }
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

# Phase 10: the product path runs all four agents, so knowledge needs rag (:8004) too
for pair in "api 8000" "incident 8001" "orchestrator 8002" "agents 8003" "rag 8004" "llm-gateway 8005" "tool-gateway 8006"; do
  set -- $pair
  code=$(curl -s -o /dev/null -w '%{http_code}' "localhost:$2/health/live")
  [ "$code" = "200" ] && ok "$1 up" || { bad "$1 not running on :$2"; exit 1; }
done
[ -s secrets/delegation_public.pem ] && ok "delegation keys present" || { bad "run: make delegation-keys, then restart make dev"; exit 1; }
scopes=$(python3 -c "import base64,json,sys; p=open('secrets/orchestrator_service_token.txt').read().split('.')[1]; p+='='*(-len(p)%4); print(json.loads(base64.urlsafe_b64decode(p))['scope'])")
case "$scopes" in *delegation:create*) ok "orchestrator token has delegation:create";;
  *) bad "orchestrator token lacks delegation:create - run: make agents-tokens, then restart run-orch"; exit 1;; esac

SRE="$(make -s token ROLE=SRE 2>/dev/null)"
code=$(curl -s -o "$TMP/inc.json" -w '%{http_code}' -X POST "$API/api/v1/incidents" \
  -H "Authorization: Bearer $SRE" -H "Content-Type: application/json" -H "Idempotency-Key: smoke9-$(date +%s)" \
  -d '{"title":"Checkout 5xx after deploy (phase 9 smoke)","severity":"SEV2","affected_services":["checkout-api"],"detected_at":"2026-10-02T09:50:00Z"}')
[ "$code" = "201" ] || { bad "create incident: $code $(head -c 300 "$TMP/inc.json")"; exit 1; }
KEY=$(json "d['key']" < "$TMP/inc.json"); ok "demo incident $KEY"

code=$(curl -s -D "$TMP/h.txt" -o "$TMP/start.json" -w '%{http_code}' -X POST "$API/api/v1/incidents/$KEY/investigate" \
  -H "Authorization: Bearer $SRE" -H "Idempotency-Key: smoke9-inv-$(date +%s)")
[ "$code" = "202" ] || { bad "investigate: $code $(head -c 300 "$TMP/start.json")"; exit 1; }
INV=$(json "d['investigation_id']" < "$TMP/start.json")
grep -qi "^location: /api/v1/investigations/$INV" "$TMP/h.txt" && ok "202 + Location /api/v1/investigations/$INV" || bad "no Location header"

echo "  waiting for the graph (local CPU model: ~1-2 min)..."
status=RUNNING; i=0
while [ "$status" = "RUNNING" ] && [ $i -lt 120 ]; do
  sleep 3; i=$((i+1))
  curl -s "$API/api/v1/investigations/$INV" -H "Authorization: Bearer $SRE" > "$TMP/inv.json"
  status=$(json "d['status']" < "$TMP/inv.json" 2>/dev/null || echo "?")
done
[ "$status" = "COMPLETE" ] && ok "investigation COMPLETE" || bad "investigation $status: $(json "d.get('error')" < "$TMP/inv.json" 2>/dev/null)"
json "', '.join(t['agent']+'='+t['status']+' x'+str(t['attempt'])+' '+str(t['model']) for t in d['tasks'])" < "$TMP/inv.json" | sed 's/^/    tasks: /'
n=$(json "d.get('evidence_inserted') or 0" < "$TMP/inv.json"); [ "$n" -gt 0 ] && ok "$n evidence rows stored" || bad "no evidence stored"

curl -s "$API/api/v1/investigations/$INV/trace" -H "Authorization: Bearer $SRE" > "$TMP/trace.json"
json "[e['model']+' prompt v'+str(e['prompt_version'])+' msgs='+str(len(e['messages'])) for e in d['executions'] if e['agent']=='log_analysis'][0]" < "$TMP/trace.json" > "$TMP/t.txt" 2>/dev/null \
  && ok "trace: $(cat "$TMP/t.txt")" || bad "trace missing"
json "' '.join(f['statement'] for e in d['executions'] if e['agent']=='log_analysis' for f in e['output']['facts'])" < "$TMP/trace.json" 2>/dev/null | grep -q ERR_POOL_TIMEOUT \
  && ok "facts name ERR_POOL_TIMEOUT" || bad "no ERR_POOL_TIMEOUT fact"
grep -q '"labelled_by": *"llm"\|"labelled_by":"llm"' "$TMP/trace.json" && ok "model labels present" || warn "no model labels (degraded run? see the trace)"

curl -s "$API/api/v1/incidents/$KEY/investigations" -H "Authorization: Bearer $SRE" > "$TMP/list.json"
json "d[0]['investigation_id']" < "$TMP/list.json" 2>/dev/null | grep -q "$INV" && ok "listed under the incident (agents view)" || bad "not listed"

uv run --quiet python - "$INV" > "$TMP/deleg.txt" 2>&1 <<'PY'
import sys, psycopg
from aeoi_db.config import libpq_dsn
inv = sys.argv[1]
with psycopg.connect(libpq_dsn()) as c:
    g = c.execute("SELECT id, revoked_reason, tokens_issued FROM identity.delegation_grants WHERE investigation_id=%s", (inv,)).fetchone()
    ev = [r[0] for r in c.execute("SELECT event FROM identity.delegation_events WHERE grant_id=%s ORDER BY created_at", (g[0],))]
    audit = c.execute("SELECT count(*) FROM tools.audit_outbox WHERE event->'details'->>'investigation_id'=%s AND event->'details'->>'delegation_grant'=%s", (inv, str(g[0]))).fetchone()[0]
    print(f"grant {g[0]} revoked={g[1]!r} tokens={g[2]} events={ev} audited_tool_calls={audit}")
PY
grep -q "revoked='investigation COMPLETE'" "$TMP/deleg.txt" && grep -q "audited_tool_calls=[1-9]" "$TMP/deleg.txt" \
  && ok "delegation: $(cat "$TMP/deleg.txt")" || bad "delegation record: $(cat "$TMP/deleg.txt")"

MGR="$(make -s token ROLE=MANAGER 2>/dev/null)"
code=$(curl -s -o "$TMP/mgr.json" -w '%{http_code}' -X POST "$API/api/v1/incidents/$KEY/investigate" \
  -H "Authorization: Bearer $MGR" -H "Idempotency-Key: smoke9-mgr-$(date +%s)")
[ "$code" = "403" ] && ok "MANAGER cannot start an investigation (403)" || bad "MANAGER investigate: $code"

echo; echo "passed=$pass failed=$fail notes=$note   incident=$KEY investigation=$INV"
echo "next: the crash + resume check in docs/phases/phase-09.md §10 (Ctrl-C run-orch mid-run)."
[ "$fail" = "0" ]
