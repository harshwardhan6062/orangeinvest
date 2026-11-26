"""AnthropicLLM request shape and error mapping, against a stub client (no network)."""

import anthropic
import httpx2
import pytest
from anthropic.types import Message

from app.llm import AnthropicLLM, LLMUnavailable, model_from_env
from app.mcp_client import ToolSchema

TOOLS = [ToolSchema("search_restaurants", "Find restaurants.", {"type": "object", "properties": {}})]
URL = "https://api.anthropic.com/v1/messages"


class StubClient:
    def __init__(self, result):
        self.result, self.kwargs = result, None
        self.messages = self

    async def create(self, **kwargs):
        self.kwargs = kwargs
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def message(**over):
    return Message.model_validate({
        "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-sonnet-5-5",
        "content": [{"type": "thinking", "thinking": "", "signature": "sig"},
                    {"type": "tool_use", "id": "tu_1", "name": "search_restaurants",
                     "input": {"query": "Phurr", "city": "Bangalore"}}],
        "stop_reason": "tool_use", "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 5}, **over})


async def test_request_shape(monkeypatch):
    monkeypatch.delenv("ORANGEINVEST_MODEL", raising=False)
    stub = StubClient(message())
    await AnthropicLLM(stub).complete("SYS", [{"role": "user", "content": "hi"}], TOOLS)
    k = stub.kwargs
    assert k["model"] == "claude-sonnet-5-5"
    assert k["system"] == "SYS"
    assert k["tools"] == [{"name": "search_restaurants", "description": "Find restaurants.",
                           "input_schema": {"type": "object", "properties": {}}}]
    assert k["thinking"] == {"type": "adaptive"}
    assert k["output_config"] == {"effort": "medium"}
    assert k["cache_control"] == {"type": "ephemeral"}
    assert k["max_tokens"] == 16000
    assert "fallbacks" not in k and "betas" not in k  # no silent model switching


def test_model_from_env(monkeypatch):
    monkeypatch.setenv("ORANGEINVEST_MODEL", "claude-opus-5-5")
    assert model_from_env() == "claude-opus-5-5"
    assert AnthropicLLM(StubClient(None)).model == "claude-opus-5-5"
    assert AnthropicLLM(StubClient(None), model="x").model == "x"  # explicit wins
    monkeypatch.setenv("ORANGEINVEST_MODEL", "")
    assert model_from_env() == "claude-sonnet-5-5"


async def test_response_blocks_round_trip_as_sent():
    r = await AnthropicLLM(StubClient(message())).complete("S", [], TOOLS)
    assert r.stop_reason == "tool_use" and r.stop_details is None
    assert r.content == [
        {"type": "thinking", "thinking": "", "signature": "sig"},
        {"type": "tool_use", "id": "tu_1", "name": "search_restaurants",
         "input": {"query": "Phurr", "city": "Bangalore"}},
    ]


async def test_refusal_details_are_passed_through():
    m = message(content=[], stop_reason="refusal",
                stop_details={"type": "refusal", "category": "cyber", "explanation": "nope"})
    r = await AnthropicLLM(StubClient(m)).complete("S", [], TOOLS)
    assert r.stop_reason == "refusal"
    assert r.stop_details["category"] == "cyber"


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
