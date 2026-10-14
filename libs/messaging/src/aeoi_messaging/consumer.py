"""A consumer that never loses a message and never lets one bad message block the rest (ADR-029).

For each message, in partition order:
  handler OK            -> commit
  PoisonError (bad message)  -> to the DLQ at once, commit, go on. It can never succeed; retrying
                           only blocks every message behind it.
  TransientError (a dependency is down: DB, network) -> NOT the message's fault: back off and try
                           again, without limit and WITHOUT committing. Nothing goes to the DLQ
                           during an outage; the lag grows and shows it (`status`, `lag`).
  anything else         -> `max_attempts` tries with backoff, then the DLQ (a bug that only
                           this message triggers must not stop the consumer forever).
The offset is committed only AFTER the handler (or the DLQ write) succeeded: a crash in between
means the message comes again, so handlers must be idempotent (dedupe on event_id). If the DLQ
write itself fails, the message is NOT committed: it is retried as transient (never dropped).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

import structlog

from aeoi_messaging.transport import Message, Transport

log = structlog.get_logger("aeoi.consumer")


class PoisonError(Exception):
    """This message can never be handled (undecodable, invalid, breaks a constraint)."""


class TransientError(Exception):
    """A dependency is unavailable; the message itself is fine."""


class Kind(StrEnum):
    POISON = "poison"
    TRANSIENT = "transient"
    UNKNOWN = "unknown"


Handler = Callable[[Message], Awaitable[None]]
Classifier = Callable[[BaseException], Kind]


def default_classify(exc: BaseException) -> Kind:
    if isinstance(exc, PoisonError):
        return Kind.POISON
    if isinstance(exc, TransientError | ConnectionError | TimeoutError):
        return Kind.TRANSIENT
    return Kind.UNKNOWN


def dlq_topic(topic: str) -> str:
    return f"{topic}.dlq"


# A DLQ record = the original + ~2 KB of dlq_* headers. Kafka's default limit is ~1 MB: an
# original near it could never be dead-lettered and would block its partition forever (review
# H3). Above this size the DLQ keeps a POINTER (where the original is, its sha256), not the bytes.
MAX_DLQ_VALUE_BYTES = 900_000


def event_id_of(value: bytes) -> str | None:
    try:
        doc = json.loads(value)
    except (ValueError, UnicodeDecodeError):
        return None
    return str(doc.get("event_id")) if isinstance(doc, dict) and doc.get("event_id") else None


def int_header(msg: Message, name: str) -> int:
    """A count from a header; never raises (review H2, re-review L-6)."""
    try:
        return max(0, int(msg.header(name) or 0))
    except ValueError:
        return 0


def replay_count(msg: Message) -> int:
    """Never raises (review H2): a bad header must not turn the DLQ write into an endless retry."""
    return int_header(msg, "dlq_replay_count")


def describe(exc: BaseException, limit: int = 500) -> str:
    """Error text WITHOUT SQL parameters (review L4): SQLAlchemy errors embed the bound values
    (here: event payloads) - they must not reach readiness bodies or DLQ headers."""
    inner = getattr(exc, "orig", None) or exc
    first = (str(inner).strip().splitlines() or [""])[0]
    return f"{type(exc).__name__}: {first}"[:limit]


@dataclass
class Status:
    state: str = "starting"  # starting | running | degraded (dependency down) | stopped
    processed: int = 0
    dead_lettered: int = 0
    last_error: str | None = None
    last_error_at: datetime | None = None

    @property
    def healthy(self) -> bool:
        """Joined AND not waiting on a dependency. Readiness uses this (review M4): a consumer
        stuck retrying is not doing its job, even though its task is alive."""
        return self.state == "running"


@dataclass
class Consumer:
    name: str  # the consumer group: one name per kind of work
    transport: Transport
    handler: Handler
    classify: Classifier = default_classify
    max_attempts: int = 3
    backoff_base_s: float = 0.2
    backoff_max_s: float = 30.0
    poll_timeout_s: float = 1.0
    batch: int = 100
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    status: Status = field(default_factory=Status)
    _stopping: asyncio.Event = field(default_factory=asyncio.Event)

    def _backoff(self, n: int) -> float:
        # exponential with FULL jitter (ADR-014): many consumers don't retry in lockstep
        return random.uniform(0, min(self.backoff_max_s, self.backoff_base_s * 2**n))  # noqa: S311

    def _note(self, exc: BaseException) -> None:
        self.status.last_error = describe(exc)
        self.status.last_error_at = datetime.now(UTC)

    async def _dead_letter(
        self, msg: Message, exc: BaseException, kind: Kind, attempts: int
    ) -> None:
        replays = replay_count(msg)
        keep = tuple((k, v) for k, v in msg.headers if not k.startswith("dlq_"))
        value = msg.value
        size = len(value) + sum(len(k) + len(v) for k, v in keep)  # headers count too
        if size > MAX_DLQ_VALUE_BYTES:  # keep where it is, not the bytes (review H3)
            value = json.dumps({"dlq_truncated": True, "bytes": len(msg.value),
                                "sha256": hashlib.sha256(msg.value).hexdigest(),
                                # so `replay --event-id` still finds it (re-review L-1)
                                "event_id": event_id_of(msg.value)}).encode()  # fmt: skip
            keep = tuple((k, v) for k, v in keep if len(v) < 4096)
            if sum(len(k) + len(v) for k, v in keep) > MAX_DLQ_VALUE_BYTES // 4:
                keep = ()  # many small headers can still overflow (re-review L-3): drop them all
        meta = {
            "dlq_consumer": self.name, "dlq_error_kind": kind.value,
            "dlq_error": describe(exc, 1000), "dlq_attempts": str(attempts),
            "dlq_source_topic": msg.topic, "dlq_source_partition": str(msg.partition),
            "dlq_source_offset": str(msg.offset), "dlq_failed_at": datetime.now(UTC).isoformat(),
            "dlq_replay_count": str(replays),
        }  # fmt: skip
        if value is not msg.value:
            meta["dlq_truncated"] = "1"
        headers = keep + tuple((k, v.encode()) for k, v in meta.items())
        await self.transport.send(dlq_topic(msg.topic), msg.key, value, headers)
        self.status.dead_lettered += 1
        log.warning("dead_lettered", consumer=self.name, topic=msg.topic, partition=msg.partition,
                    offset=msg.offset, kind=kind.value, error=type(exc).__name__)  # fmt: skip

    async def _handle(self, msg: Message) -> bool:
        """Handle one message until it is committed. False = stopping before it was done."""
        attempts = 0
        transient = 0
        while True:
            try:
                await self.handler(msg)
            except Exception as exc:
                kind = self.classify(exc)
                self._note(exc)
                if kind is Kind.UNKNOWN:
                    attempts += 1
                if kind is Kind.POISON or (kind is Kind.UNKNOWN and attempts >= self.max_attempts):
                    try:
                        await self._dead_letter(msg, exc, kind, max(attempts, 1))
                    except Exception as dlq_exc:
                        self._note(dlq_exc)
                        self.status.state = "degraded"  # blocked on the DLQ (re-review M-2)
                        log.error(
                            "dlq_write_failed", consumer=self.name, error=type(dlq_exc).__name__
                        )
                        transient += 1
                        if not await self._wait(transient):
                            return False
                        continue
                    break  # dead-lettered: commit and move on
                if kind is Kind.TRANSIENT:
                    transient += 1
                    self.status.state = "degraded"
                    log.warning("dependency_down", consumer=self.name, error=type(exc).__name__,
                                attempt=transient)  # fmt: skip
                if not await self._wait(transient if kind is Kind.TRANSIENT else attempts):
                    return False
                continue
            self.status.processed += 1
            break
        await self.transport.commit(msg)
        self.status.state = "running"
        return True

    async def _wait(self, n: int) -> bool:
        """Back off; False if asked to stop meanwhile (the message stays uncommitted)."""
        if self._stopping.is_set():
            return False
        await self.sleep(self._backoff(n))
        return not self._stopping.is_set()

    async def run_once(self) -> int:
        """Poll one batch and handle it in order. Returns how many were committed."""
        done = 0
        msgs = await self.transport.poll(self.poll_timeout_s, self.batch)
        if self.status.state == "degraded" and not msgs:
            # an idle topic must not keep one blip's "degraded" for ever - but an EMPTY poll is
            # also what aiokafka returns while the broker is down: only a probe can tell
            # (re-review M-1). A message in hand recovers through its commit instead.
            with contextlib.suppress(Exception):
                await self.transport.probe()
                self.status.state = "running"
        for msg in msgs:
            if self._stopping.is_set():  # stop promptly: the rest stays uncommitted (review L4)
                break
            if not await self._handle(msg):
                break
            done += 1
        return done

    async def _start(self) -> bool:
        """Join the group; if the broker is not there yet, keep trying (review M4) - a consumer
        that gives up at boot stays dead until someone restarts the service."""
        n = 0
        while not self._stopping.is_set():
            try:
                await self.transport.start()
                return True
            except Exception as exc:
                self._note(exc)
                self.status.state = "degraded"
                log.warning("consumer_start_failed", consumer=self.name, error=type(exc).__name__)
                n += 1
                await self.sleep(self._backoff(min(n, 6)))
        return False

    async def run_forever(self) -> None:
        if not await self._start():
            self.status.state = "stopped"
            return
        self.status.state = "running"
        try:
            while not self._stopping.is_set():
                try:
                    await self.run_once()
                except Exception as exc:
                    self._note(exc)
                    self.status.state = "degraded"
                    log.warning(
                        "consumer_poll_failed", consumer=self.name, error=type(exc).__name__
                    )
                    await self.sleep(self._backoff(3))
        finally:
            self.status.state = "stopped"
            await self.transport.stop()

    def stop(self) -> None:
        self._stopping.set()
