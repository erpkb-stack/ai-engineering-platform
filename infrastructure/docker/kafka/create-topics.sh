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
# Phase 30: only the topic something produces AND consumes. investigation.tasks/results (agent
# tasks stay on HTTP, ADR-030) and catalog.changes (the catalog has no Kafka, ADR-032) had
# neither - they are no longer created (an old broker volume may still list them: harmless).
create incident.lifecycle        3 "$WEEK"
create incident.lifecycle.dlq    1 "$MONTH"
"$KT" --bootstrap-server "$BS" --list
echo "AEOI topics ready."
