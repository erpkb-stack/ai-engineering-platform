from __future__ import annotations

from pathlib import Path

import pytest

from aeoi_llm.cache import ResponseCache
from aeoi_llm.config import RoutingConfig, load_routing
from aeoi_llm.gateway import Gateway
from aeoi_llm.main import build_gateway
from aeoi_llm.providers import FakeProvider
from aeoi_llm.usage import MemoryUsageSink

CONFIG = Path(__file__).resolve().parents[1] / "config"


async def no_sleep(_: float) -> None:
    return None


@pytest.fixture
def routing() -> RoutingConfig:
    return load_routing(CONFIG / "routing.test.yaml")


@pytest.fixture
def hosted() -> FakeProvider:
    return FakeProvider(name="hosted_fake", hosted=True)


@pytest.fixture
def local() -> FakeProvider:
    return FakeProvider(name="local_fake", hosted=False)


@pytest.fixture
def sink() -> MemoryUsageSink:
    return MemoryUsageSink()


@pytest.fixture
def gateway(
    routing: RoutingConfig, hosted: FakeProvider, local: FakeProvider, sink: MemoryUsageSink
) -> Gateway:
    gw = build_gateway(
        routing,
        sink,
        cache=ResponseCache(100, 3600),
        request_timeout_s=5,
        providers={"hosted_fake": hosted, "local_fake": local},
    )
    gw._sleep = no_sleep  # retries without real waiting
    return gw
