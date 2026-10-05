#!/usr/bin/env bash
# Smoke-test the running local infra. Exit 0 only if every check passes.
# macOS bash 3.2 compatible.
set -u
C="docker compose --profile infra --profile kafka --profile tools"
pass=0; fail=0
ok()  { echo "✔ $1"; pass=$((pass+1)); }
bad() { echo "✘ $1"; fail=$((fail+1)); }
running() { [ -n "$($C ps -q --status running "$1" 2>/dev/null)" ]; }
psql_q() { $C exec -T postgres psql -U "${2:-aeoi}" -d aeoi -tAX -v ON_ERROR_STOP=1 -c "$1" 2>&1; }

echo "== postgres =="
if running postgres; then
  v="$(psql_q "SELECT extversion FROM pg_extension WHERE extname='vector'")"
  [ -n "$v" ] && ok "pgvector installed (v$v)" || bad "pgvector extension missing"
  d="$(psql_q "SELECT round(('[1,2,3]'::vector <=> '[1,2,4]'::vector)::numeric, 4)")"
  [ "$d" = "0.0085" ] && ok "vector cosine distance works ($d)" || bad "vector math unexpected: $d"
  t="$(psql_q "SELECT count(*) FROM pg_extension WHERE extname='pg_trgm'")"
  [ "$t" = "1" ] && ok "pg_trgm installed" || bad "pg_trgm missing"
  r="$(psql_q "SHOW default_transaction_read_only" aeoi_readonly)"
  [ "$r" = "on" ] && ok "aeoi_readonly role is read-only" || bad "aeoi_readonly role wrong: $r"
  w="$(psql_q "CREATE TABLE public.should_fail(i int)" aeoi_readonly)"
  case "$w" in *"read-only"*|*"permission denied"*) ok "aeoi_readonly cannot write";; *) bad "aeoi_readonly could write!";; esac
else
  bad "postgres not running (make up)"
fi

echo "== redis =="
if running redis; then
  [ "$($C exec -T redis redis-cli ping 2>&1)" = "PONG" ] && ok "redis PING" || bad "redis PING failed"
  $C exec -T redis redis-cli set aeoi:verify ok EX 10 >/dev/null 2>&1
  [ "$($C exec -T redis redis-cli get aeoi:verify 2>&1)" = "ok" ] && ok "redis SET/GET with TTL" || bad "redis SET/GET"
  p="$($C exec -T redis redis-cli config get maxmemory-policy 2>&1 | tail -1)"
  [ "$p" = "allkeys-lru" ] && ok "redis eviction = allkeys-lru (cache, not a database)" || bad "redis policy: $p"
else
  bad "redis not running (make up)"
fi

echo "== kafka (only with PROFILE=kafka) =="
if running kafka; then
  topics="$($C exec -T kafka /opt/kafka/bin/kafka-topics.sh --bootstrap-server localhost:19092 --list 2>&1)"
  for t in incident.lifecycle investigation.tasks investigation.results catalog.changes investigation.tasks.dlq; do
    echo "$topics" | grep -qx "$t" && ok "topic $t" || bad "topic $t missing (make kafka-init)"
  done
  parts="$($C exec -T kafka /opt/kafka/bin/kafka-topics.sh --bootstrap-server localhost:19092 \
           --describe --topic investigation.tasks 2>&1 | grep -c 'Partition:')"
  [ "$parts" = "6" ] && ok "investigation.tasks has 6 partitions" || bad "investigation.tasks partitions: $parts"
  msg="verify-$$-$(date +%s)"
  echo "$msg" | $C exec -T kafka /opt/kafka/bin/kafka-console-producer.sh \
      --bootstrap-server localhost:19092 --topic incident.lifecycle.dlq >/dev/null 2>&1
  got="$($C exec -T kafka /opt/kafka/bin/kafka-console-consumer.sh --bootstrap-server localhost:19092 \
         --topic incident.lifecycle.dlq --from-beginning --timeout-ms 10000 2>/dev/null | grep -c "$msg")"
  [ "$got" -ge 1 ] && ok "kafka produce -> consume round trip" || bad "kafka round trip failed"
else
  echo "- kafka not running (skipped; start with: make up PROFILE=kafka)"
fi

echo "== result: $pass passed, $fail failed =="
[ "$fail" -eq 0 ]
