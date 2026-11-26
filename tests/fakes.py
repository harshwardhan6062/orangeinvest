"""Shared test doubles: a scripted LLM, a settable clock, and the API's message-shape rules."""

import itertools
from copy import deepcopy
from datetime import datetime

from app.llm import LLMResponse
from app.models import IST

T0 = datetime(2026, 11, 26, 12, 0, tzinfo=IST)
SLOT = "2026-11-26T19:30:00+05:30"
_ids = itertools.count(1)


def use(*calls):
    """A model turn that calls tools: use(("name", {...}), ...)."""
    blocks = [{"type": "thinking", "thinking": "", "signature": "sig"}]
    blocks += [{"type": "tool_use", "id": f"tu_{next(_ids)}", "name": n, "input": i} for n, i in calls]
    return LLMResponse("tool_use", blocks)


def say(text):
    return LLMResponse("end_turn", [{"type": "thinking", "thinking": "", "signature": "sig"},
                                    {"type": "text", "text": text}])


SEARCH = ("search_restaurants", {"query": "Phurr", "city": "Bangalore"})
SLOTS = ("get_available_slots", {"restaurant_id": "phurr-indiranagar", "date": "2026-11-26", "party_size": 2})
BOOK = ("book_table", {"restaurant_id": "phurr-indiranagar", "time": SLOT, "party_size": 2})


class FakeLLM:
    def __init__(self, *script):
        self.script, self.calls = list(script), []

    async def complete(self, system, messages, tools):
        self.calls.append(deepcopy(messages))
        self.tools = tools
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


def assert_valid(messages):
    """The shape rules the Messages API enforces on what we send."""
    assert messages[0]["role"] == "user"
    for i, m in enumerate(messages):
        if m["role"] == "system":
            assert messages[i - 1]["role"] == "user", f"system at {i} must follow a user message"
            assert i == len(messages) - 1 or messages[i + 1]["role"] == "assistant", \
                f"system at {i} must be last or followed by assistant"
        if m["role"] == "assistant":
            ids = [b["id"] for b in m["content"] if b["type"] == "tool_use"]
            if ids:
                nxt = messages[i + 1]
                assert nxt["role"] == "user"
                assert [b["tool_use_id"] for b in nxt["content"]] == ids


class Clock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now
