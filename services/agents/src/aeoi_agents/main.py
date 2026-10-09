"""agents entry point: uvicorn aeoi_agents.main:build_app --factory --port 8003"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI

from aeoi_agents import __version__
from aeoi_agents.api import router
from aeoi_agents.config import (
    DeploymentBudget,
    KnowledgeBudget,
    LogAgentBudget,
    MetricsBudget,
    Settings,
)
from aeoi_agents.deployment.agent import DeploymentAgent
from aeoi_agents.deployment.agent import Deps as DeployDeps
from aeoi_agents.knowledge.agent import Deps as KnowledgeDeps
from aeoi_agents.knowledge.agent import KnowledgeAgent
from aeoi_agents.log_analysis.agent import Deps, LogAnalysisAgent
from aeoi_agents.metrics.agent import Deps as MetricsDeps
from aeoi_agents.metrics.agent import MetricsAgent
from aeoi_llm_client import HttpLLMClient, LLMClient
from aeoi_tool_client import HttpToolClient, ToolClient
from aeoi_web import Authenticator, create_app


class FileToken:
    """Re-reads the token file when it changes (rotation without restart)."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._cached: tuple[float, str] | None = None

    async def __call__(self) -> str:
        mtime = self._path.stat().st_mtime
        if self._cached is None or self._cached[0] != mtime:
            self._cached = (mtime, self._path.read_text().strip())
        return self._cached[1]


def build_app(
    settings: Settings | None = None,
    *,
    tools: ToolClient | None = None,
    llm: LLMClient | None = None,
    budget: LogAgentBudget | None = None,
    tool_transport: httpx.AsyncBaseTransport | None = None,
    llm_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    settings = settings or Settings()
    token = FileToken(settings.service_token_file)
    tool_client = tools or HttpToolClient(
        settings.tool_gateway_url, token, transport=tool_transport
    )
    llm_client = llm or HttpLLMClient(
        settings.llm_gateway_url, token, timeout_s=130.0, transport=llm_transport
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            for c in (tool_client, llm_client):
                close = getattr(c, "aclose", None)
                if close is not None:
                    await close()

    app = create_app(
        service_name=settings.service_name,
        version=__version__,
        routers=[router],
        authenticator=Authenticator(
            settings.jwt_public_key_file.read_text(),
            settings.jwt_issuer,
            settings.jwt_audience,
            delegation_public_key=(
                settings.delegation_public_key_file.read_text()
                if settings.delegation_public_key_file.is_file()
                else None
            ),
        ),
        # stateless worker: ready = process up. A dead tool/LLM gateway shows up per task
        # (FAILED / degraded), not as "take every worker out of the load balancer".
        readiness={},
        lifespan=lifespan,
        log_level=settings.log_level,
        log_json=settings.log_json,
        environment=settings.environment.value,
    )
    app.state.settings = settings
    app.state.log_agent = LogAnalysisAgent(
        Deps(tools=tool_client, llm=llm_client, budget=budget or LogAgentBudget())
    )
    # Phase 10 (ADR-020): code-only agents - no LLM client is passed to them at all
    app.state.agents = {
        "log_analysis": app.state.log_agent,
        "metrics": MetricsAgent(MetricsDeps(tools=tool_client, budget=MetricsBudget())),
        "deployment": DeploymentAgent(DeployDeps(tools=tool_client, budget=DeploymentBudget())),
        "knowledge": KnowledgeAgent(KnowledgeDeps(tools=tool_client, budget=KnowledgeBudget())),
    }
    return app
