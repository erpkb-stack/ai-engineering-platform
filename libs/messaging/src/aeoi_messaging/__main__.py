"""Operate consumers: lag, dead letters, replay (ADR-029).

  python -m aeoi_messaging lag --group audit --topic incident.lifecycle
  python -m aeoi_messaging dlq --topic incident.lifecycle.dlq
  python -m aeoi_messaging replay --topic incident.lifecycle.dlq [--event-id X | --offset N] [--dry-run]
DEV ONLY (refused when AEOI_ENV=production):
  python -m aeoi_messaging produce-raw --topic incident.lifecycle --value 'not json'
  python -m aeoi_messaging redeliver --topic incident.lifecycle --event-id X   (a duplicate on purpose)
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from typing import Any

from aeoi_messaging.consumer import event_id_of
from aeoi_messaging.dlq import MAX_REPLAYS, KafkaIO, list_dead_letters, replay
from aeoi_messaging.transport import lag_of, topic_partitions


async def _lag(bootstrap: str, group: str, topic: str) -> int:
    from aiokafka import AIOKafkaConsumer, TopicPartition

    c: Any = AIOKafkaConsumer(bootstrap_servers=bootstrap, group_id=group, enable_auto_commit=False)
    await c.start()
    try:
        parts = await topic_partitions(c, topic)
        tps = [TopicPartition(topic, p) for p in parts or []]
        if not tps:  # review L1: "total lag 0" for a typo'd topic looked healthy
            print(f"unknown topic {topic!r} (auto-create is off: make kafka-init)", file=sys.stderr)
            return 2
        ends = await c.end_offsets(tps)
        starts = await c.beginning_offsets(tps)
        total = 0
        for tp in tps:
            done = await c.committed(tp)
            n = lag_of(ends[tp], done, starts[tp])
            total += n
            print(f"{topic}[{tp.partition}]  end={ends[tp]}  committed={done}  lag={n}")
        print(f"group {group}: total lag {total}")
        return 0
    finally:
        await c.stop()


async def _dlq(bootstrap: str, topic: str) -> int:
    async with KafkaIO(bootstrap) as io:
        letters = await list_dead_letters(io, topic)
    for d in letters:
        print(f"[{d.message.partition}:{d.message.offset}] {d.failed_at}  consumer={d.consumer}  "
              f"kind={d.kind}  attempts={d.attempts}  replays={d.replay_count}  event={d.event_id}\n"
              f"    {d.error}")  # fmt: skip
    print(f"{len(letters)} dead letter(s) in {topic}")
    return 0


async def _replay(
    bootstrap: str, topic: str, event_id: str | None, offset: int | None, dry: bool
) -> int:
    async with KafkaIO(bootstrap) as io:
        res = await replay(io, topic, event_id=event_id, offset=offset, dry_run=dry)
    verb = "would replay" if dry else "replayed"
    for d in res.replayed:
        print(
            f"{verb} [{d.message.partition}:{d.message.offset}] event={d.event_id} -> {d.source_topic}"
        )
    for d, why in res.refused:
        print(f"REFUSED [{d.message.partition}:{d.message.offset}] event={d.event_id}: {why}")
    print(
        f"{verb} {len(res.replayed)}, refused {len(res.refused)} (max {MAX_REPLAYS} replays each)"
    )
    if not res.replayed and not res.refused:  # a typo'd id/offset must not look like success
        print("nothing matched: check EVENT= / OFFSET= against make kafka-dlq", file=sys.stderr)
        return 1
    return 1 if res.refused and not res.replayed else 0


async def _produce_raw(bootstrap: str, topic: str, key: str | None, value: str) -> int:
    async with KafkaIO(bootstrap) as io:
        await io.send(
            topic, key.encode() if key else None, value.encode(), (("source", b"dev-cli"),)
        )
    print(f"sent {len(value)} bytes to {topic}")
    return 0


async def _redeliver(bootstrap: str, topic: str, event_id: str) -> int:
    async with KafkaIO(bootstrap) as io:
        found = [m for m in await io.read(topic) if event_id_of(m.value) == event_id]
        if not found:
            print(f"event {event_id} not found in {topic}", file=sys.stderr)
            return 1
        m = found[0]
        await io.send(topic, m.key, m.value, (*m.headers, ("redelivered", b"dev-cli")))
    print(f"sent event {event_id} to {topic} AGAIN (consumers must not apply it twice)")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="aeoi_messaging", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)  # fmt: skip
    p.add_argument(
        "--bootstrap", default=os.environ.get("AEOI_KAFKA_BOOTSTRAP_SERVERS", "localhost:9094")
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("lag")
    s.add_argument("--group", required=True)
    s.add_argument("--topic", required=True)
    s = sub.add_parser("dlq")
    s.add_argument("--topic", required=True)
    s = sub.add_parser("replay")
    s.add_argument("--topic", required=True)
    s.add_argument("--event-id")
    s.add_argument("--offset", type=int)
    s.add_argument("--dry-run", action="store_true")
    s = sub.add_parser("produce-raw")
    s.add_argument("--topic", required=True)
    s.add_argument("--key")
    s.add_argument("--value", required=True)
    s = sub.add_parser("redeliver")
    s.add_argument("--topic", required=True)
    s.add_argument("--event-id", required=True)
    a = p.parse_args(argv)
    if a.cmd in {"produce-raw", "redeliver"} and os.environ.get("AEOI_ENV") == "production":
        print("refusing: dev-only command", file=sys.stderr)
        return 2
    if a.cmd == "lag":
        try:  # committed() waits for the group coordinator with no limit (re-review L-4)
            return asyncio.run(asyncio.wait_for(_lag(a.bootstrap, a.group, a.topic), 20))
        except TimeoutError:
            print("lag: no answer from Kafka in 20 s", file=sys.stderr)
            return 3
    try:
        if a.cmd == "dlq":
            return asyncio.run(_dlq(a.bootstrap, a.topic))
        if a.cmd == "replay":
            return asyncio.run(_replay(a.bootstrap, a.topic, a.event_id, a.offset, a.dry_run))
        if a.cmd == "produce-raw":
            return asyncio.run(_produce_raw(a.bootstrap, a.topic, a.key, a.value))
        return asyncio.run(_redeliver(a.bootstrap, a.topic, a.event_id))
    except LookupError as exc:  # an unknown topic is an error, never "nothing there"
        print(f"{a.cmd}: {exc.args[0] if exc.args else exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
