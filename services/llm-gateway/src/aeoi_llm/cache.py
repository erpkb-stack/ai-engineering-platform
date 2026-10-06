"""Exact-match response cache for deterministic calls (temperature == 0).

Prototype: in-process LRU + TTL. Production: Redis (shared across replicas), same key.
Only PRIMARY-model answers are cached: caching a fallback answer would serve degraded
output for the whole TTL after the primary recovers.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import asdict
from typing import Any

from aeoi_llm.providers.base import ChatRequest, ChatResult


def cache_key(operation: str, route: str, chain: list[str], req: ChatRequest) -> str:
    material = {
        "op": operation,
        "route": route,
        "chain": chain,  # policy change (new primary model) => new key, no stale answers
        "req": {k: v for k, v in asdict(req).items() if k != "model"},
    }
    blob = json.dumps(material, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


class ResponseCache:
    def __init__(self, max_entries: int, ttl_s: float, clock: Callable[[], float] = time.monotonic):
        self._data: OrderedDict[str, tuple[float, ChatResult, dict[str, Any]]] = OrderedDict()
        self._max = max_entries
        self._ttl = ttl_s
        self._clock = clock
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> tuple[ChatResult, dict[str, Any]] | None:
        item = self._data.get(key)
        if item is None or self._clock() - item[0] > self._ttl:
            self._data.pop(key, None)
            self.misses += 1
            return None
        self._data.move_to_end(key)
        self.hits += 1
        return item[1], item[2]

    def put(self, key: str, result: ChatResult, info: dict[str, Any]) -> None:
        self._data[key] = (self._clock(), result, info)
        self._data.move_to_end(key)
        while len(self._data) > self._max:
            self._data.popitem(last=False)

    def __len__(self) -> int:
        return len(self._data)
