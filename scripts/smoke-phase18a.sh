#!/usr/bin/env bash
# Phase 18a smoke (ADR-029): incident events reach the audit trail THROUGH KAFKA, once;
# poison goes to the DLQ without blocking; replay works and is bounded; lag returns to 0.
# Needs: make up PROFILE=kafka; incident-service with AEOI_INCIDENT_EVENT_TRANSPORT=kafka and
# audit with AEOI_AUDIT_CONSUMER_ENABLED=true (restarted after setting them). bash 3.2 OK.
set -u
API="${AEOI_API_URL:-http://localhost:8000}"
TOPIC=incident.lifecycle
DLQ=incident.lifecycle.dlq
pass=0; fail=0
ok()  { echo "✔ $1"; pass=$((pass+1)); }
bad() { echo "✘ $1"; fail=$((fail+1)); }
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
# stdout only: a library warning on stderr must never become the line we parse
msg() { uv run --quiet python -m aeoi_messaging "$@" 2>>"$TMP/stderr"; }
json() { python3 -c "import sys,json; d=json.load(sys.stdin); print($1)"; }
# Kafka keeps every earlier run's dead letters: every DLQ check matches THIS run's event id only
# (review M5), never "some poison entry exists".
POISON_ID="$(python3 -c 'import uuid; print(uuid.uuid4())')"
mine()    { msg dlq --topic $DLQ | grep "event=$POISON_ID"; }           # this run's DLQ lines
lag_now() { msg lag --group audit --topic $TOPIC | tail -1 | awk '{print $NF}'; }
wait_lag0() { for _ in $(seq 1 30); do [ "$(lag_now)" = "0" ] && return 0; sleep 1; done; return 1; }

for pair in "api 8000" "incident-service 8001" "audit 8008"; do
  set -- $pair
  [ "$(curl -s -o /dev/null -w '%{http_code}' "localhost:$2/health/live")" = "200" ] \
    && ok "$1 up" || { bad "$1 not running on :$2"; exit 1; }
done
# right after a restart the consumer is still joining its group: give it 30 s
for _ in $(seq 1 30); do
  cstate="$(curl -s localhost:8008/health/ready | json "d.get('checks', {}).get('consumer', 'absent')" 2>/dev/null || echo unreadable)"
  [ "$cstate" = "ok" ] || [ "$cstate" = "absent" ] && break; sleep 1
done
case "$cstate" in   # review M6: the key existing is not enough - "fail: ..." also has the key
  ok)     ok "audit consumer is running and ready" ;;
  absent) bad "audit has no consumer: set AEOI_AUDIT_CONSUMER_ENABLED=true and restart make run-audit"; exit 1 ;;
  *)      bad "audit consumer not ready ($cstate): Kafka up? AEOI_KAFKA_BOOTSTRAP_SERVERS? see make run-audit logs"; exit 1 ;;
esac
msg lag --group audit --topic $TOPIC > "$TMP/lag0" || { bad "lag check failed: $(tail -1 "$TMP/stderr")"; exit 1; }
ok "lag readable: $(tail -1 "$TMP/lag0")"

# ---- 1. an incident event reaches the audit trail through Kafka -------------------------
SRE="$(make -s token ROLE=SRE 2>/dev/null)"
code=$(curl -s -o "$TMP/inc.json" -w '%{http_code}' -X POST "$API/api/v1/incidents" -H "Authorization: Bearer $SRE" \
  -H "Content-Type: application/json" -H "Idempotency-Key: smoke18a-$(date +%s)" \
  -d "{\"title\":\"Phase 18a smoke\",\"severity\":\"SEV4\",\"affected_services\":[\"checkout-api\"],\"detected_at\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}")
[ "$code" = "201" ] || { bad "create incident: $code $(head -c 200 "$TMP/inc.json")"; exit 1; }
INC_ID=$(json "d['id']" < "$TMP/inc.json"); INC=$(json "d['key']" < "$TMP/inc.json")
n=0; for _ in $(seq 1 30); do read -r n EID <<<"$(uv run --quiet python scripts/phase18_audit_rows.py "$INC_ID")"; [ "$n" = "1" ] && break; sleep 1; done
[ "$n" = "1" ] && ok "$INC: IncidentCreated -> outbox -> Kafka -> audit consumer -> 1 audit row (event $EID)" \
  || bad "no audit row via Kafka after 30 s (is incident-service on AEOI_INCIDENT_EVENT_TRANSPORT=kafka?)"

# ---- 2. the same event again: still one row, and not a failure -------------------------
if [ "$n" = "1" ]; then
  DLQ0="$(msg dlq --topic $DLQ | tail -1 | awk '{print $1}')"
  # the copy must really be sent: a failed redeliver would leave "1 row, DLQ unchanged" true
  if ! msg redeliver --topic $TOPIC --event-id "$EID" > "$TMP/redeliver" || ! grep -q "AGAIN" "$TMP/redeliver"; then
    bad "redeliver failed: $(tail -1 "$TMP/stderr")"
  # wait until the copy was CONSUMED (lag 0) - "still 1 row" before that proves nothing
  elif wait_lag0; then
    read -r n2 _ <<<"$(uv run --quiet python scripts/phase18_audit_rows.py "$INC_ID")"
    DLQ1="$(msg dlq --topic $DLQ | tail -1 | awk '{print $1}')"
    [ "$n2" = "1" ] && [ "$DLQ1" = "$DLQ0" ] && ok "a duplicate delivery (consumed: lag 0) adds nothing and is not dead-lettered" \
      || bad "duplicate: rows=$n2, DLQ $DLQ0 -> $DLQ1"
  else
    bad "duplicate: the redelivered copy was not consumed in 30 s (lag $(lag_now))"
  fi
fi

# ---- 3. poison goes to the DLQ, the next good event is still audited ---------------------
# a valid event id, no occurred_at: the audit consumer must call it poison (never guess a time)
msg produce-raw --topic $TOPIC --key "$INC_ID" --value "{\"event_id\":\"$POISON_ID\",\"smoke\":\"phase 18a\"}" > /dev/null
for _ in $(seq 1 20); do mine > "$TMP/mine"; [ -s "$TMP/mine" ] && break; sleep 1; done
grep -q "consumer=audit  kind=poison  attempts=1  replays=0" "$TMP/mine" \
  && ok "poison $POISON_ID -> $DLQ at once (consumer=audit, kind=poison, attempts=1)" \
  || bad "this run's poison not in the DLQ as expected: $(cat "$TMP/mine")"
# only an INCIDENT_COMMANDER has incidents:write (rbac.py); SRE may create but not change
IC="$(make -s token ROLE=INCIDENT_COMMANDER 2>/dev/null)"
code=$(curl -s -o "$TMP/patch.json" -w '%{http_code}' -X PATCH "$API/api/v1/incidents/$INC" -H "Authorization: Bearer $IC" \
  -H "Content-Type: application/json" -H "If-Match: \"1\"" -d '{"severity":"SEV3","reason":"phase 18a smoke: an event after the poison message"}')
u=0; for _ in $(seq 1 30); do read -r u _ <<<"$(uv run --quiet python scripts/phase18_audit_rows.py "$INC_ID" IncidentUpdated)"; [ "$u" = "1" ] && break; sleep 1; done
[ "$code" = "200" ] && [ "$u" = "1" ] && ok "the next event AFTER the poison message is audited (one bad message blocks nothing)" \
  || bad "after poison: PATCH $code $(head -c 160 "$TMP/patch.json"), IncidentUpdated rows $u"

# ---- 4. replay: back to the source topic, fails again, counted --------------------------
msg replay --topic $DLQ --event-id "$POISON_ID" > "$TMP/replay"
grep -q "^replayed 1, refused 0" "$TMP/replay" && ok "replayed this run's dead letter to $TOPIC" || bad "replay: $(tail -1 "$TMP/replay")"
for _ in $(seq 1 20); do [ "$(mine | wc -l | tr -d ' ')" = "2" ] && break; sleep 1; done
mine | tail -1 | grep -q "replays=1" && [ "$(mine | wc -l | tr -d ' ')" = "2" ] \
  && ok "still poison: back in the DLQ with replays=1 (a fix is needed, not more replays)" \
  || bad "replayed message not back in the DLQ once with replays=1: $(mine)"

# ---- 5. caught up ----------------------------------------------------------------------
wait_lag0 && ok "audit consumer lag on $TOPIC: 0" || bad "lag still $(lag_now)"

echo "passed=$pass failed=$fail   incident=$INC   (dead letters stay in $DLQ: Kafka records are immutable)"
[ "$fail" = "0" ]
