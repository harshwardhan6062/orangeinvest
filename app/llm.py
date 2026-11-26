"""The model behind the agent. Messages are Anthropic Messages-API shaped
dicts; the agent stores them verbatim so history is append-only and thinking
blocks are passed back exactly as received."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Protocol

import anthropic

from app.mcp_client import ToolSchema

# The loop makes several calls per turn, so latency and cost matter: Sonnet by
# default. Override with e.g. ORANGEINVEST_MODEL=claude-opus-5-5.
MODEL_ENV = "ORANGEINVEST_MODEL"
DEFAULT_MODEL = "claude-sonnet-5-5"
MAX_TOKENS = 16000
EFFORT = "medium"  # recommended starting point for multi-step tool use on Sonnet 5.5


def model_from_env() -> str:
    return os.environ.get(MODEL_ENV) or DEFAULT_MODEL


@dataclass(frozen=True)
class LLMResponse:
    stop_reason: str | None
    content: list[dict[str, Any]]  # API-shaped blocks, appended to history as-is
    stop_details: dict[str, Any] | None = None  # set on refusal (category, explanation)


class LLMUnavailable(Exception):
    """Transient: network, rate limit, overload. The SDK has already retried."""


class LLM(Protocol):
    async def complete(
        self, system: str, messages: list[dict[str, Any]], tools: list[ToolSchema]
    ) -> LLMResponse: ...


class AnthropicLLM:
    """No refusal fallbacks on purpose: a silent model switch would make evals
    irreproducible. A refusal ends the turn politely and is audited."""

    def __init__(
        self,
        client: anthropic.AsyncAnthropic | None = None,
        model: str | None = None,
        effort: str = EFFORT,
    ) -> None:
        self._client = client or anthropic.AsyncAnthropic()  # credentials from the environment
        self.model = model or model_from_env()
        self._effort = effort

    async def complete(
        self, system: str, messages: list[dict[str, Any]], tools: list[ToolSchema]
    ) -> LLMResponse:
        try:
            resp = await self._client.messages.create(
                model=self.model,
                max_tokens=MAX_TOKENS,
                system=system,
                messages=messages,
                tools=[{"name": t.name, "description": t.description, "input_schema": t.input_schema}
                       for t in tools],
                thinking={"type": "adaptive"},
                output_config={"effort": self._effort},
                cache_control={"type": "ephemeral"},  # frozen system + tools + growing history
            )
        except anthropic.APIConnectionError as e:  # includes timeouts
            raise LLMUnavailable(str(e)) from e
        except anthropic.APIStatusError as e:
            if e.status_code == 429 or e.status_code >= 500:
                raise LLMUnavailable(f"{e.status_code}: {e.message}") from e
            raise  # 4xx other than 429 is our bug; don't hide it
        return LLMResponse(
            resp.stop_reason,
            [b.to_dict(mode="json") for b in resp.content],
            resp.stop_details.to_dict(mode="json") if resp.stop_details else None,
        )
