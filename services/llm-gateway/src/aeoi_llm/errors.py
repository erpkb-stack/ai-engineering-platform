"""Gateway errors -> problem+json. Callers branch on `type`, never on message text."""

from __future__ import annotations

from aeoi_common.errors import AEOIError
from aeoi_llm.providers.base import Usage


class BudgetExceededError(AEOIError):
    status = 429
    type_slug = "llm-budget-exceeded"
    title = "LLM budget exceeded for this investigation"


class AllProvidersFailedError(AEOIError):
    status = 503
    type_slug = "llm-unavailable"
    title = "No model in the route could serve the request"


class GatewayTimeoutError(AEOIError):
    status = 504
    type_slug = "llm-timeout"
    title = "LLM request exceeded its deadline"


class StructuredOutputError(AEOIError):
    status = 422
    type_slug = "llm-structured-output-invalid"
    title = "Model output did not match the JSON Schema"

    def __init__(self, detail: str, *, usage: Usage | None = None) -> None:
        super().__init__(detail)
        self.usage = usage or Usage()  # tokens were still spent - they get recorded


class UpstreamRejectedError(AEOIError):
    """Provider said the REQUEST is bad (400/413/422). Falling back would hide our bug."""

    status = 400
    type_slug = "llm-request-rejected"
    title = "The model provider rejected the request"
