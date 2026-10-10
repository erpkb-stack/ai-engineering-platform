#!/usr/bin/env bash
# Phase 5 smoke test against a RUNNING llm-gateway (make run-llm). Exits 1 on failure.
# Works with any policy: it reads /v1/routes and tells you which provider really answered.
# Needs: curl, python3. macOS bash 3.2 compatible.
set -u
LLM="${AEOI_LLM_URL:-http://localhost:8005}"
ROUTE="${LLM_SMOKE_ROUTE:-fast}"
pass=0; fail=0; warn=0
ok()   { echo "✔ $1"; pass=$((pass+1)); }
bad()  { echo "✘ $1"; fail=$((fail+1)); }
note() { echo "! $1"; warn=$((warn+1)); }
json() { python3 -c "import sys,json; d=json.load(sys.stdin); print($1)"; }
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

STOKEN="$(make -s service-token SERVICE=smoke SCOPES=llm:invoke 2>/dev/null)"
NOSCOPE="$(make -s service-token SERVICE=smoke SCOPES=tools:invoke 2>/dev/null)"
USER_T="$(make -s token ROLE=ADMIN 2>/dev/null)"
[ -n "$STOKEN" ] && ok "service token minted (scope llm:invoke)" || { bad "service token (run make setup?)"; exit 1; }
H=(-H "Authorization: Bearer $STOKEN" -H "Content-Type: application/json")

code=$(curl -s -o /dev/null -w '%{http_code}' "$LLM/health/live")
[ "$code" = "200" ] && ok "health 200" || { bad "health: $code (is make run-llm running?)"; exit 1; }

code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$LLM/v1/generate" -d '{}')
[ "$code" = "401" ] && ok "no token -> 401" || bad "no token: $code"
if [ -n "$USER_T" ]; then
  code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$LLM/v1/generate" -H "Authorization: Bearer $USER_T" \
    -H "Content-Type: application/json" -d '{"messages":[{"role":"user","content":"hi"}]}')
  [ "$code" = "403" ] && ok "USER token (even ADMIN) -> 403: only services may call the gateway" || bad "user token: $code"
fi
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$LLM/v1/generate" -H "Authorization: Bearer $NOSCOPE" \
  -H "Content-Type: application/json" -d '{"messages":[{"role":"user","content":"hi"}]}')
[ "$code" = "403" ] && ok "service token without llm:invoke -> 403" || bad "no-scope token: $code"

curl -s "$LLM/v1/routes" "${H[@]}" > "$TMP/routes.json"
echo "   providers:"; json "'\n'.join(f'     {k:<12} configured={v[\"configured\"]!s:<5} breakers={v[\"breakers\"] or \"{}\"} {v[\"unavailable_reason\"] or \"\"}' for k,v in d['providers'].items())" < "$TMP/routes.json"
echo "   $ROUTE chain: $(json "' -> '.join(d['routes']['$ROUTE']['chain'])" < "$TMP/routes.json")"

# Preflight for local OpenAI-compatible servers (Ollama): reachable? models pulled?
# Runs from THIS shell, like the gateway does, so a proxy or port problem shows up here too.
python3 - "$TMP/routes.json" <<'PY' || fail=$((fail+1))
import json, sys, urllib.request
r = json.load(open(sys.argv[1]))
bad = 0
for name, p in r["providers"].items():
    base = p.get("base_url")
    if p["hosted"] or not base or p["kind"] != "openai_compat":
        continue
    root = base.rstrip("/").removesuffix("/v1")
    wanted = sorted(m for m, prov in r.get("models", {}).items() if prov == name)
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # no proxy, like the gateway
        tags = json.load(opener.open(root + "/api/tags", timeout=5))
    except Exception as exc:
        print(f"✘ preflight: {name} at {root} is not reachable ({type(exc).__name__}: {exc}). Start it: open -a Ollama")
        bad = 1
        continue
    have = {m["name"] for m in tags.get("models", [])} | {m["name"].removesuffix(":latest") for m in tags.get("models", [])}
    missing = [m for m in wanted if m not in have]
    if missing:
        print(f"✘ preflight: {name} is up but these models are missing: {missing}. Run: make ollama-pull")
        bad = 1
    else:
        print(f"✔ preflight: {name} up at {root}, models present: {wanted}")
sys.exit(bad)
PY

# Every route whose PRIMARY is a hosted model must answer with that model, fallback OFF.
# (Phase 11 finding: `reasoning` -> claude-sonnet-5-5 returned HTTP 400 since Phase 5 and nothing
# noticed, because this smoke only called the default route and fallback hid failures.)
python3 - "$TMP/routes.json" "$LLM" "$STOKEN" <<'PY' || fail=$((fail+1))
import json, sys, urllib.request, urllib.error
r, base, tok = json.load(open(sys.argv[1])), sys.argv[2], sys.argv[3]
bad = 0
for name, route in sorted(r["routes"].items()):
    chain = route.get("chain") or []
    if not chain:
        continue
    prov = r.get("models", {}).get(chain[0])
    p = r["providers"].get(prov or "", {})
    if not p.get("hosted") or not p.get("configured") or route.get("operation", "chat") != "chat":
        continue
    # plain AND structured: the critic's structured call failed on Sonnet (forced tool choice)
    # while plain text worked - Phase 11 finding
    msgs = [{"role": "user", "content": "Classify: checkout returns HTTP 500. Severity SEV1-SEV4?"}]
    calls = {
        "plain": ("/v1/generate", {"max_tokens": 10, "messages": [{"role": "user", "content": "Reply with one word: ready"}]}),
        "structured": ("/v1/generate_structured", {"max_tokens": 200, "messages": msgs, "schema_name": "sev",
            "json_schema": {"type": "object", "properties": {"severity": {"type": "string", "enum": ["SEV1", "SEV2", "SEV3", "SEV4"]}},
                            "required": ["severity"], "additionalProperties": False}}),
    }
    for kind, (path, extra) in calls.items():
        body = json.dumps({"route": name, "allow_fallback": False, "cache": False, **extra}).encode()
        req = urllib.request.Request(base + path, data=body, method="POST",
            headers={"Authorization": "Bearer " + tok, "Content-Type": "application/json"})
        try:
            d = json.load(urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=120))
            ok = d.get("model") == chain[0] and not d.get("fallback_used")
            print(("✔" if ok else "✘") + f" route {name} {kind}: primary {chain[0]} answered (fallback off) model={d.get('model')}")
            bad |= not ok
        except urllib.error.HTTPError as exc:
            detail = json.loads(exc.read() or b"{}").get("detail", "")
            print(f"✘ route {name} {kind}: primary {chain[0]} FAILED with fallback off: HTTP {exc.code} {detail}")
            bad = 1
sys.exit(1 if bad else 0)
PY

NONCE="$(date +%s)-$$"
BODY="{\"route\":\"$ROUTE\",\"max_tokens\":60,\"messages\":[{\"role\":\"user\",\"content\":\"Reply with one short sentence: what is a connection pool? ($NONCE)\"}],\"metadata\":{\"agent_name\":\"smoke\",\"prompt_id\":\"smoke\",\"prompt_version\":1}}"
code=$(curl -s -o "$TMP/g1.json" -w '%{http_code}' -X POST "$LLM/v1/generate" "${H[@]}" -d "$BODY" --max-time 200)
if [ "$code" = "200" ]; then
  ok "generate -> 200: $(json "f\"model={d['model']} provider={d['provider']} fallback={d['fallback_used']} tokens={d['usage']['input_tokens']}/{d['usage']['output_tokens']} cost=\${d['cost_usd']} latency={d['latency_ms']}ms\"" < "$TMP/g1.json")"
  echo "   text: $(json "d['text'][:120].replace(chr(10),' ')" < "$TMP/g1.json")"
  [ "$(json "d['fallback_used']" < "$TMP/g1.json")" = "True" ] && note "answered by a FALLBACK model - see attempts: $(json "d['attempts']" < "$TMP/g1.json")"
else
  bad "generate: $code $(cat "$TMP/g1.json")"
fi

code=$(curl -s -o "$TMP/g2.json" -w '%{http_code}' -X POST "$LLM/v1/generate" "${H[@]}" -d "$BODY" --max-time 200)
if [ "$code" = "200" ]; then
  if [ "$(json "d['cached']" < "$TMP/g2.json")" = "True" ]; then ok "same request again -> served from cache, \$0"
  elif [ "$(json "d['fallback_used']" < "$TMP/g1.json")" = "True" ]; then ok "not cached because the first answer was a fallback (by design)"
  else bad "expected a cache hit"; fi
fi

SCHEMA='{"type":"object","properties":{"severity":{"enum":["SEV1","SEV2","SEV3"]},"reason":{"type":"string","maxLength":200}},"required":["severity","reason"],"additionalProperties":false}'
SBODY="{\"route\":\"$ROUTE\",\"max_tokens\":200,\"json_schema\":$SCHEMA,\"schema_name\":\"triage\",\"messages\":[{\"role\":\"user\",\"content\":\"Checkout returns HTTP 500 for 40% of users after a deploy. Classify severity.\"}]}"
code=$(curl -s -o "$TMP/s.json" -w '%{http_code}' -X POST "$LLM/v1/generate_structured" "${H[@]}" -d "$SBODY" --max-time 200)
if [ "$code" = "200" ]; then
  ok "structured -> 200 and schema-valid: $(json "f\"{d['data']} repaired={d['repaired']} model={d['model']}\"" < "$TMP/s.json")"
else
  bad "structured: $code $(head -c 300 "$TMP/s.json")"
fi

FAKE_SECRET="sk-ant-smoke$(python3 -c 'import secrets;print(secrets.token_hex(8))')"
RBODY="{\"route\":\"$ROUTE\",\"max_tokens\":20,\"cache\":false,\"messages\":[{\"role\":\"user\",\"content\":\"Say OK. Log line: api_key=$FAKE_SECRET\"}]}"
code=$(curl -s -o "$TMP/r.json" -w '%{http_code}' -X POST "$LLM/v1/generate" "${H[@]}" -d "$RBODY" --max-time 200)
if [ "$code" = "200" ]; then
  hosted=$(python3 -c "import json;r=json.load(open('$TMP/routes.json'));g=json.load(open('$TMP/r.json'));print(r['providers'][g['provider']]['hosted'])")
  n=$(json "d['redactions']" < "$TMP/r.json")
  if [ "$hosted" = "True" ]; then [ "$n" -ge 1 ] && ok "secret redacted before the hosted provider (redactions=$n)" || bad "secret NOT redacted"
  else ok "local provider answered: no redaction needed (data stayed on this machine)"; fi
fi

curl -s -N -X POST "$LLM/v1/stream" "${H[@]}" --max-time 200 \
  -d "{\"route\":\"$ROUTE\",\"max_tokens\":40,\"messages\":[{\"role\":\"user\",\"content\":\"Count from 1 to 5.\"}]}" > "$TMP/stream.txt"
evs=$(grep '^event: ' "$TMP/stream.txt" | sed 's/event: //' | sort | uniq -c | tr -s ' ' | tr '\n' ',')
grep -q '^event: done' "$TMP/stream.txt" && ok "stream (SSE): $evs" || bad "stream: $(head -c 300 "$TMP/stream.txt")"

code=$(curl -s -o "$TMP/e.json" -w '%{http_code}' -X POST "$LLM/v1/embed" "${H[@]}" \
  -d '{"inputs":["checkout-api connection pool exhausted","db pool timeout after deploy"]}' --max-time 200)
if [ "$code" = "200" ]; then ok "embed -> $(json "f\"{len(d['vectors'])} vectors x {d['dimensions']}-d model={d['model']}\"" < "$TMP/e.json")"
else note "embed: $code $(json "d.get('detail','')" < "$TMP/e.json" 2>/dev/null)"; fi

rows=$(uv run --quiet python - <<'PY' 2>/dev/null
import psycopg
from aeoi_db.config import libpq_dsn
with psycopg.connect(libpq_dsn()) as c:
    for r in c.execute("""SELECT status, provider, model, count(*), sum(cost_usd)
        FROM llm.model_usage WHERE created_at > now() - interval '15 minutes'
        GROUP BY 1,2,3 ORDER BY 1,2,3""").fetchall():
        print(f"     {r[0]:<12} {r[1]:<10} {r[2]:<28} n={r[3]:<3} ${r[4]}")
PY
)
[ -n "$rows" ] && { ok "usage rows in llm.model_usage (last 15 min):"; echo "$rows"; } || bad "no usage rows (did you run make db-users and make db-upgrade?)"

echo; echo "passed=$pass failed=$fail notes=$warn"
[ "$fail" = "0" ]
