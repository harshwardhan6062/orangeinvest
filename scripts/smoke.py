"""Live smoke test: two turns against the mock with the real model, recording
every Messages API request and response.

    uv run python -m scripts.smoke [--mode confirm|cancel] [--out smoke.json]

Turn 1 asks for a booking (search -> slots -> propose). The script then
confirms or cancels like the client would, and turn 2 asks whether it went
through, so the request carries a mid-history system message plus a new one
with the outcome note. Needs ANTHROPIC_API_KEY (or any SDK credential).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time

import anthropic

from app.agent import Agent
from app.gateway import Gateway
from app.llm import AnthropicLLM
from app.mock_dineout import MockDineout
from app.repos import InMemoryAuditLog, InMemoryPendingActionRepo, InMemorySessionRepo


def record_calls(client: anthropic.AsyncAnthropic, calls: list) -> None:
    real = client.messages.create

    async def create(**kw):
        t = time.monotonic()
        resp = await real(**kw)
        calls.append({
            "request": json.loads(json.dumps(kw, default=str)),
            "response": resp.to_dict(mode="json"),
            "request_id": resp._request_id,
            "seconds": round(time.monotonic() - t, 2),
        })
        return resp

    client.messages.create = create


def check_round_trip(calls: list) -> list[str]:
    """Every response must be resent byte-identical, and every system message
    placed where the API allows it. Returns problems found."""
    problems = []
    for i, c in enumerate(calls):
        sent = c["request"]["messages"]
        for later in calls[i + 1:]:
            resent = later["request"]["messages"]
            if len(resent) > len(sent) and resent[len(sent)]["role"] == "assistant" \
                    and resent[len(sent)]["content"] != c["response"]["content"]:
                problems.append(f"call {i + 1}'s response was altered when resent")
        for j, m in enumerate(sent):
            if m["role"] == "system" and (sent[j - 1]["role"] != "user" or
                                          (j < len(sent) - 1 and sent[j + 1]["role"] != "assistant")):
                problems.append(f"call {i + 1}: system message at {j} misplaced")
    return problems


async def main(mode: str, out: str) -> None:
    calls: list = []
    client = anthropic.AsyncAnthropic()
    record_calls(client, calls)
    sessions, pending, audit, mock = (InMemorySessionRepo(), InMemoryPendingActionRepo(),
                                      InMemoryAuditLog(), MockDineout())
    gw = Gateway(mock, sessions, pending, audit)
    llm = AnthropicLLM(client)
    agent = Agent(llm, gw, sessions, audit)
    print("model:", llm.model)

    r1 = await agent.run_turn("smoke", "Book Phurr Indiranagar in Bangalore for 2 at 7:30pm tomorrow")
    n1 = len(calls)
    print(f"\nturn 1: {n1} model calls")
    print("  reply:  ", r1.reply)
    print("  pending:", r1.pending and r1.pending.summary)
    print("  bookings before confirm:", len(mock._bookings))

    if r1.pending:
        act = gw.confirm if mode == "confirm" else gw.cancel
        done = await act("smoke", r1.pending.id)
        await agent.record_outcome(done)
        print(f"  {mode} ->", done.status, done.result and done.result["booking_id"])

    r2 = await agent.run_turn("smoke", "Did that go through? What's my booking ID?")
    print(f"\nturn 2: {len(calls) - n1} model calls")
    print("  reply:", r2.reply)

    thinking = sum(b["type"] == "thinking" for c in calls for b in c["response"]["content"])
    problems = check_round_trip(calls)
    print(f"\n{len(calls)} calls, thinking blocks returned: {thinking}, "
          f"round-trip problems: {problems or 'none'}")

    with open(out, "w") as f:
        json.dump({"calls": calls, "audit": [
            {"event": e.event, "tool": e.tool, "args": e.args, "detail": e.detail}
            for e in await audit.entries("smoke")]}, f, indent=2, default=str)
    print("transcript:", out)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--mode", choices=["confirm", "cancel"], default="confirm")
    p.add_argument("--out", default="smoke.json")
    a = p.parse_args()
    asyncio.run(main(a.mode, a.out))
