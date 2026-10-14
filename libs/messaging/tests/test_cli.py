"""The operator CLI's output is parsed by scripts/smoke-phase18a.sh: pin the lines it reads.
The Kafka IO is replaced by the in-memory broker (same TopicIO protocol)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from aeoi_messaging import Consumer, MemoryBroker, MemoryTransport, Message, PoisonError
from aeoi_messaging import __main__ as cli
from aeoi_messaging.dlq import MemoryIO

TOPIC = "incident.lifecycle"


@pytest.fixture
def broker(monkeypatch: pytest.MonkeyPatch) -> MemoryBroker:
    b = MemoryBroker()

    class FakeKafkaIO(MemoryIO):
        def __init__(self, _bootstrap: str) -> None:
            super().__init__(b)

        async def __aenter__(self) -> FakeKafkaIO:
            return self

        async def __aexit__(self, *exc: Any) -> None:
            return None

    monkeypatch.setattr(cli, "KafkaIO", FakeKafkaIO)
    return b


async def no_sleep(_s: float) -> None:
    return None


async def dead_letter_one(b: MemoryBroker) -> None:
    async def h(_m: Message) -> None:
        raise PoisonError("not JSON")

    c = Consumer("audit", MemoryTransport(b, "audit", [TOPIC]), h, sleep=no_sleep)
    await c.transport.start()
    await c.run_once()


def test_the_lines_the_smoke_reads(
    broker: MemoryBroker, capsys: pytest.CaptureFixture[str]
) -> None:
    """The smoke's own steps, in order, with a STALE dead letter from an earlier run in the DLQ
    (Kafka keeps them): every check must match this run's event id only (review M5)."""
    import asyncio

    def lines(*argv: str) -> list[str]:
        capsys.readouterr()  # drop the consumer's own log lines (another process on the Mac)
        cli.main(list(argv))
        return capsys.readouterr().out.splitlines()

    def mine(pid: str) -> list[str]:
        return [ln for ln in lines("dlq", "--topic", f"{TOPIC}.dlq") if f"event={pid}" in ln]

    assert lines("dlq", "--topic", f"{TOPIC}.dlq")[-1].split()[0] == "0"  # "0 dead letter(s)"
    old = json.dumps({"event_id": "old-run"}).encode()  # an earlier run, replayed once already
    broker.produce(TOPIC, b"inc-0", old, (("dlq_replay_count", b"1"),))
    asyncio.run(dead_letter_one(broker))
    assert lines("dlq", "--topic", f"{TOPIC}.dlq")[-1].startswith("1 dead letter(s)")

    pid = "3f1c2b9e-0000-4000-8000-000000000018"
    assert cli.main(["produce-raw", "--topic", TOPIC, "--key", "inc-1",
                     "--value", json.dumps({"event_id": pid, "smoke": "phase 18a"})]) == 0  # fmt: skip
    asyncio.run(dead_letter_one(broker))
    [line] = mine(pid)
    assert "consumer=audit  kind=poison  attempts=1  replays=0" in line  # the smoke's grep

    out = lines("replay", "--topic", f"{TOPIC}.dlq", "--event-id", pid)
    assert out[-1].startswith("replayed 1, refused 0")  # the smoke's "^replayed 1, refused 0"
    asyncio.run(dead_letter_one(broker))  # still poison: back in the DLQ
    assert len(mine(pid)) == 2 and "replays=1" in mine(pid)[-1]  # newest last
    assert len(lines("dlq", "--topic", f"{TOPIC}.dlq")) == 2 * 3 + 1  # 3 letters, 2 lines each


def test_redeliver_sends_the_same_event_again(
    broker: MemoryBroker, capsys: pytest.CaptureFixture[str]
) -> None:
    broker.produce(TOPIC, b"k", json.dumps({"event_id": "e-1"}).encode())
    assert cli.main(["redeliver", "--topic", TOPIC, "--event-id", "e-1"]) == 0
    assert [json.loads(m.value)["event_id"] for m in broker.messages(TOPIC)] == ["e-1", "e-1"]
    assert cli.main(["redeliver", "--topic", TOPIC, "--event-id", "nope"]) == 1


def test_dev_only_commands_are_refused_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AEOI_ENV", "production")
    assert cli.main(["produce-raw", "--topic", TOPIC, "--value", "x"]) == 2
    assert cli.main(["redeliver", "--topic", TOPIC, "--event-id", "e"]) == 2


def test_a_replay_that_matches_nothing_fails(
    broker: MemoryBroker, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["replay", "--topic", f"{TOPIC}.dlq", "--event-id", "typo"]) == 1
    assert "nothing matched" in capsys.readouterr().err
