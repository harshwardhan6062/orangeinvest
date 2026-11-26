"""AnthropicLLM request shape and error mapping, against a stub client (no network)."""

import anthropic
import httpx2
import pytest
from anthropic.types.beta import BetaMessage

from app.llm import MODEL, AnthropicLLM, LLMUnavailable
from app.mcp_client import ToolSchema

TOOLS = [ToolSchema("search_restaurants", "Find restaurants.", {"type": "object", "properties": {}})]


class StubClient:
    def __init__(self, result):
        self.result, self.kwargs = result, None
        self.beta = self
        self.messages = self

    async def create(self, **kwargs):
        self.kwargs = kwargs
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def message(**over):
    return BetaMessage.model_validate({
        "id": "msg_1", "type": "message", "role": "assistant", "model": MODEL,
        "content": [{"type": "thinking", "thinking": "", "signature": "sig"},
                    {"type": "tool_use", "id": "tu_1", "name": "search_restaurants",
                     "input": {"query": "Phurr", "city": "Bangalore"}}],
        "stop_reason": "tool_use", "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 5}, **over})


async def test_request_shape():
    stub = StubClient(message())
    await AnthropicLLM(stub).complete("SYS", [{"role": "user", "content": "hi"}], TOOLS)
    k = stub.kwargs
    assert k["model"] == "claude-opus-5-5"
    assert k["system"] == "SYS"
    assert k["tools"] == [{"name": "search_restaurants", "description": "Find restaurants.",
                           "input_schema": {"type": "object", "properties": {}}}]
    assert k["thinking"] == {"type": "adaptive"}
    assert k["output_config"] == {"effort": "medium"}
    assert k["cache_control"] == {"type": "ephemeral"}
    assert k["betas"] == ["server-side-fallback-2026-07-01"] and k["fallbacks"] == "default"
    assert k["max_tokens"] == 16000


async def test_response_blocks_round_trip_as_sent():
    r = await AnthropicLLM(StubClient(message())).complete("S", [], TOOLS)
    assert r.stop_reason == "tool_use"
    assert r.content == [
        {"type": "thinking", "thinking": "", "signature": "sig"},
        {"type": "tool_use", "id": "tu_1", "name": "search_restaurants",
         "input": {"query": "Phurr", "city": "Bangalore"}},
    ]


URL = "https://api.anthropic.com/v1/messages"


def _status_error(code):
    resp = httpx2.Response(code, request=httpx2.Request("POST", URL))
    return anthropic.APIStatusError("m", response=resp, body=None)


@pytest.mark.parametrize("exc", [
    anthropic.APIConnectionError(request=httpx2.Request("POST", URL)),
    _status_error(429),
    _status_error(529),
    _status_error(500),
])
async def test_transient_errors_become_unavailable(exc):
    with pytest.raises(LLMUnavailable):
        await AnthropicLLM(StubClient(exc)).complete("S", [], TOOLS)


async def test_bad_request_is_not_hidden():
    with pytest.raises(anthropic.APIStatusError):
        await AnthropicLLM(StubClient(_status_error(400))).complete("S", [], TOOLS)
