#!/bin/bash
# One-shot: create AEOI topics (architecture.md §17). Idempotent (--if-not-exists).
# Auto-create is disabled on the broker, so a typo in a topic name fails loudly.
set -euo pipefail
BS="${BOOTSTRAP:-kafka:19092}"
KT=/opt/kafka/bin/kafka-topics.sh

create() { # name partitions retention_ms
  "$KT" --bootstrap-server "$BS" --create --if-not-exists --topic "$1" \
    --partitions "$2" --replication-factor 1 --config "retention.ms=$3" --config min.insync.replicas=1
}

WEEK=604800000
MONTH=2592000000
create incident.lifecycle        3 "$WEEK"
create investigation.tasks       6 "$WEEK"
create investigation.results     6 "$WEEK"
create catalog.changes           3 "$WEEK"
create incident.lifecycle.dlq    1 "$MONTH"
create investigation.tasks.dlq   1 "$MONTH"
create investigation.results.dlq 1 "$MONTH"
create catalog.changes.dlq       1 "$MONTH"
"$KT" --bootstrap-server "$BS" --list
echo "AEOI topics ready."
