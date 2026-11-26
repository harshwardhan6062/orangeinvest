"""The model behind the agent. Messages are Anthropic Messages-API shaped
dicts; the agent stores them verbatim so history is append-only and thinking
blocks are passed back exactly as received."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import anthropic

from app.mcp_client import ToolSchema

MODEL = "claude-opus-5-5"
MAX_TOKENS = 16000
EFFORT = "medium"  # Opus 5.5's default, set explicitly; multi-step tool use, short replies
FALLBACK_BETA = "server-side-fallback-2026-07-01"


@dataclass(frozen=True)
class LLMResponse:
    stop_reason: str | None
    content: list[dict[str, Any]]  # API-shaped blocks, appended to history as-is


class LLMUnavailable(Exception):
    """Transient: network, rate limit, overload. The SDK has already retried."""


class LLM(Protocol):
    async def complete(
        self, system: str, messages: list[dict[str, Any]], tools: list[ToolSchema]
    ) -> LLMResponse: ...


class AnthropicLLM:
    def __init__(
        self,
        client: anthropic.AsyncAnthropic | None = None,
        model: str = MODEL,
        effort: str = EFFORT,
    ) -> None:
        self._client = client or anthropic.AsyncAnthropic()  # credentials from the environment
        self._model = model
        self._effort = effort

    async def complete(
        self, system: str, messages: list[dict[str, Any]], tools: list[ToolSchema]
    ) -> LLMResponse:
        try:
            resp = await self._client.beta.messages.create(
                model=self._model,
                max_tokens=MAX_TOKENS,
                system=system,
                messages=messages,
                tools=[{"name": t.name, "description": t.description, "input_schema": t.input_schema}
                       for t in tools],
                thinking={"type": "adaptive"},
                output_config={"effort": self._effort},
                cache_control={"type": "ephemeral"},  # frozen system + tools + growing history
                # On a safety decline, the API re-runs the request on a fallback model.
                betas=[FALLBACK_BETA],
                fallbacks="default",
            )
        except anthropic.APIConnectionError as e:  # includes timeouts
            raise LLMUnavailable(str(e)) from e
        except anthropic.APIStatusError as e:
            if e.status_code == 429 or e.status_code >= 500:
                raise LLMUnavailable(f"{e.status_code}: {e.message}") from e
            raise  # 4xx other than 429 is our bug; don't hide it
        return LLMResponse(resp.stop_reason, [b.to_dict(mode="json") for b in resp.content])
