import asyncio
import json
import logging

import pytest

from app.agent import (
    CANT_HELP,
    CONFIRM_PROMPT,
    GAVE_UP,
    UNAVAILABLE,
    Agent,
    outcome_note,
)
from app.gateway import Gateway
from app.llm import LLMResponse, LLMUnavailable
from app.mock_dineout import MockDineout
from app.models import ActionStatus, AuditEvent
from app.repos import InMemoryAuditLog, InMemoryPendingActionRepo, InMemorySessionRepo

from tests.fakes import BOOK, SEARCH, SLOTS, T0, Clock, FakeLLM, assert_valid, say, use


@pytest.fixture
def env():
    clock, sessions, audit = Clock(), InMemorySessionRepo(), InMemoryAuditLog()
    mock = MockDineout()
    gw = Gateway(mock, sessions, InMemoryPendingActionRepo(), audit, clock=clock)

    def make(*script, **kw):
        llm = FakeLLM(*script)
        return Agent(llm, gw, sessions, audit, clock=clock, **kw), llm

    make.audit = audit
    return make, gw, sessions, mock


async def check_history(sessions, sid="s1"):
    h = await sessions.history(sid)
    assert_valid(h)
    return h


# --- the main flow ------------------------------------------------------------

async def test_booking_turn_ends_on_proposal(env):
    make, gw, sessions, mock = env
    agent, llm = make(use(SEARCH), use(SLOTS), use(BOOK))
    r = await agent.run_turn("s1", "book Phurr Indiranagar for 2 at 7:30pm tonight")
    assert r.pending is not None and r.pending.status == ActionStatus.PENDING
    assert r.pending.summary == "Book a table for 2 people at Phurr, Indiranagar on Thu 26 Nov at 7:30 PM"
    assert r.reply == CONFIRM_PROMPT
    assert len(llm.calls) == 3          # no extra model call after the proposal
    assert mock._bookings == {}        # nothing booked
    for sent in llm.calls:
        assert_valid(sent)
    h = await check_history(sessions)
    last = json.loads(h[-1]["content"][0]["content"])
    assert last["status"] == "needs_confirmation"


async def test_model_text_is_used_as_reply_when_present(env):
    make, *_ = env
    resp = use(SEARCH)
    agent, _ = make(LLMResponse("tool_use", [{"type": "text", "text": "Let me check."}] + resp.content),
                    use(SLOTS), LLMResponse("tool_use", [{"type": "text", "text": "Here it is:"}] + use(BOOK).content))
    r = await agent.run_turn("s1", "book it")
    assert r.reply == "Here it is:"


async def test_each_turn_gets_time_as_system_message(env):
    make, *_ = env
    agent, llm = make(say("hi"))
    await agent.run_turn("s1", "hello")
    user, system = llm.calls[0][-2:]
    assert user == {"role": "user", "content": "hello"}
    assert system["role"] == "system"
    assert system["content"].startswith("Current time: Thursday 26 November 2026, 12:00 IST.")


async def test_no_notes_means_no_header(env):
    make, *_ = env
    agent, llm = make(say("hi"))
    await agent.run_turn("s1", "hello")
    assert "booking system" not in llm.calls[0][-1]["content"]


async def test_empty_end_turn_never_sends_blank_reply(env):
    make, *_ = env
    agent, _ = make(LLMResponse("end_turn", [{"type": "thinking", "thinking": "", "signature": "s"}]))
    assert (await agent.run_turn("s1", "hello")).reply == GAVE_UP


async def test_system_prompt_and_tools_are_frozen(env):
    make, *_ = env
    agent, llm = make(say("a"), say("b"))
    seen = []
    orig = llm.complete

    async def spy(system, messages, tools):
        seen.append((system, [t.name for t in tools]))
        return await orig(system, messages, tools)

    llm.complete = spy
    await agent.run_turn("s1", "one")
    await agent.run_turn("s1", "two")
    assert seen[0] == seen[1]
    assert seen[0][1] == ["search_restaurants", "get_available_slots", "book_table", "get_booking_status"]


async def test_thinking_blocks_are_stored_verbatim(env):
    make, _, sessions, _ = env
    agent, _ = make(say("hi"))
    await agent.run_turn("s1", "hello")
    assert (await sessions.history("s1"))[-1]["content"][0] == {
        "type": "thinking", "thinking": "", "signature": "sig"}


async def test_history_is_append_only_across_turns(env):
    make, _, sessions, _ = env
    agent, llm = make(use(SEARCH), say("Which outlet?"), use(SLOTS), say("19:30 is free"))
    await agent.run_turn("s1", "book Phurr")
    before = await sessions.history("s1")
    await agent.run_turn("s1", "Indiranagar")
    after = await sessions.history("s1")
    assert after[:len(before)] == before
    assert llm.calls[2][:len(before)] == before  # what was sent is a prefix of what's sent next


# --- tool errors and policy ---------------------------------------------------

async def test_tool_error_is_marked_is_error(env):
    make, _, sessions, _ = env
    bad = ("get_available_slots", {"restaurant_id": "phurr-indiranagar", "date": "tonight", "party_size": 2})
    agent, llm = make(use(bad), say("What date?"))
    r = await agent.run_turn("s1", "slots?")
    assert r.reply == "What date?"
    result = llm.calls[1][-1]["content"][0]
    assert result["is_error"] is True
    assert json.loads(result["content"])["error"]["code"] == "INVALID_ARGUMENTS"


async def test_unknown_tool_is_denied_and_loop_continues(env):
    make, *_ = env
    agent, llm = make(use(("cancel_booking", {"booking_id": "SWG-1"})), say("I can't cancel."))
    r = await agent.run_turn("s1", "cancel it")
    assert r.reply == "I can't cancel."
    assert json.loads(llm.calls[1][-1]["content"][0]["content"])["error"]["code"] == "NOT_ALLOWED"


async def test_parallel_tool_calls_return_results_in_one_message(env):
    make, *_ = env
    agent, llm = make(use(SEARCH, ("search_restaurants", {"query": "Orange", "city": "Bangalore"})), say("ok"))
    await agent.run_turn("s1", "find both")
    results = llm.calls[1][-1]["content"]
    assert [b["type"] for b in results] == ["tool_result", "tool_result"]
    assert_valid(llm.calls[1])


async def test_book_without_looking_first_is_rejected_by_gateway(env):
    make, _, _, mock = env
    agent, llm = make(use(BOOK), say("Let me check first."))
    r = await agent.run_turn("s1", "book it")
    assert r.pending is None
    assert mock._bookings == {}


# --- ending a turn badly, safely ------------------------------------------------

async def test_max_steps_gives_up_gracefully(env, caplog):
    make, _, sessions, _ = env
    agent, llm = make(*[use(SEARCH)] * 3, max_steps=3)
    with caplog.at_level(logging.WARNING, logger="app.agent"):
        r = await agent.run_turn("s1", "loop forever")
    assert r.reply == GAVE_UP and r.pending is None
    assert len(llm.calls) == 3
    assert "max steps" in caplog.text
    await check_history(sessions)


@pytest.mark.parametrize("reason,reply", [("refusal", CANT_HELP), ("max_tokens", GAVE_UP)])
async def test_bad_first_stop_stores_nothing(env, reason, reply):
    make, _, sessions, _ = env
    agent, _ = make(LLMResponse(reason, [{"type": "text", "text": "partial"}]))
    r = await agent.run_turn("s1", "something")
    assert r.reply == reply
    assert await sessions.history("s1") == []


async def test_refusal_is_audited(env):
    make, *_ = env
    details = {"type": "refusal", "category": "cyber", "explanation": "x"}
    agent, _ = make(LLMResponse("refusal", [], details))
    assert (await agent.run_turn("s1", "something")).reply == CANT_HELP
    [entry] = await make.audit.entries("s1")
    assert entry.event == AuditEvent.REFUSED and entry.tool is None
    assert entry.detail == {"step": 0, "stop_details": details}


async def test_failed_turn_does_not_break_the_next_one(env):
    make, _, sessions, _ = env
    agent, llm = make(LLMResponse("refusal", []), LLMUnavailable("down"), say("hello again"))
    assert (await agent.run_turn("s1", "one")).reply == CANT_HELP
    assert (await agent.run_turn("s1", "two")).reply == UNAVAILABLE
    assert (await agent.run_turn("s1", "three")).reply == "hello again"
    assert_valid(llm.calls[-1])
    h = await check_history(sessions)
    assert [m["role"] for m in h] == ["user", "system", "assistant"]  # only the turn that worked


async def test_bad_stop_mid_turn_keeps_completed_steps(env):
    make, _, sessions, _ = env
    agent, llm = make(use(SEARCH), LLMResponse("max_tokens", []), say("ok"))
    assert (await agent.run_turn("s1", "one")).reply == GAVE_UP
    await agent.run_turn("s1", "two")
    assert_valid(llm.calls[-1])
    await check_history(sessions)


# --- outcomes reach the model as system notes ---------------------------------

async def test_confirm_outcome_is_told_on_next_turn(env):
    make, gw, sessions, mock = env
    agent, llm = make(use(SEARCH), use(SLOTS), use(BOOK), say("You're booked: SWG-1001"))
    pending = (await agent.run_turn("s1", "book Phurr Indiranagar 2 at 7:30pm")).pending
    done = await gw.confirm("s1", pending.id)
    await agent.record_outcome(done)
    await agent.record_outcome(done)  # duplicate confirm response: told once

    await agent.run_turn("s1", "did it work?")
    sent = llm.calls[-1]
    assert_valid(sent)
    user, system = sent[-2:]
    assert user == {"role": "user", "content": "did it work?"}
    assert system["role"] == "system"
    assert system["content"].startswith("Update from the booking system")
    assert system["content"].count("booking you prepared was made") == 1
    assert done.result["booking_id"] in system["content"]
    assert system["content"].rstrip().endswith("IST.")  # outcome first, time last
    assert await sessions.notes("s1") == {}  # delivered, then cleared


async def test_note_survives_a_failed_turn(env):
    make, gw, sessions, _ = env
    agent, llm = make(use(SEARCH), use(SLOTS), use(BOOK), LLMUnavailable("down"), say("Booked"))
    pending = (await agent.run_turn("s1", "book")).pending
    await agent.record_outcome(await gw.cancel("s1", pending.id))
    assert (await agent.run_turn("s1", "hi")).reply == UNAVAILABLE
    await agent.run_turn("s1", "hi again")
    assert "pressed Cancel" in llm.calls[-1][-1]["content"]


def test_outcome_note_texts():
    from app.models import PendingAction
    base = PendingAction(id="a", session_id="s", tool="book_table", args={}, summary="Book X",
                         idempotency_key="k", expires_at=T0)
    from dataclasses import replace
    assert outcome_note(replace(base, status=ActionStatus.PENDING)) is None
    assert outcome_note(replace(base, status=ActionStatus.SUPERSEDED)) is None
    assert "retry safely" in outcome_note(replace(base, status=ActionStatus.UNKNOWN))
    assert "Nothing was booked" in outcome_note(replace(base, status=ActionStatus.EXPIRED))
    failed = outcome_note(replace(base, status=ActionStatus.FAILED,
                                  error={"code": "SLOT_UNAVAILABLE", "message": "full"}))
    assert "SLOT_UNAVAILABLE" in failed


# --- concurrency ---------------------------------------------------------------

async def test_concurrent_turns_in_one_session_do_not_interleave(env):
    make, _, sessions, _ = env

    class SlowLLM(FakeLLM):
        async def complete(self, system, messages, tools):
            await asyncio.sleep(0)
            return await super().complete(system, messages, tools)

    clock, gw = Clock(), env[1]
    llm = SlowLLM(use(SEARCH), say("a"), use(SEARCH), say("b"))
    agent = Agent(llm, gw, sessions, make.audit, clock=clock)
    await asyncio.gather(agent.run_turn("s1", "one"), agent.run_turn("s1", "two"))
    h = await check_history(sessions)
    assert [m["role"] for m in h] == ["user", "system", "assistant", "user", "assistant"] * 2
