"""Generic tool-use loop. Knows nothing about restaurants: the system prompt
and the gateway's tools carry all of that.

Each turn:
  user message + a system message (current time, notes since last turn)
  -> model -> tool calls through the gateway -> model -> ... until it answers.

A proposed write ends the turn: the client shows the summary as a Confirm
button and calls the gateway directly. The outcome reaches the model as a
system message on the next turn, never as something the user "said".

History rules the API enforces, and how this file keeps them:
- append-only (thinking blocks are only valid in the conversation that made
  them): nothing stored is ever edited or removed.
- a `system` message must follow a user message and be followed by an
  assistant turn: the opening [user, system] pair is stored together with the
  first assistant reply, so a failed first call stores nothing.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.gateway import Gateway
from app.llm import LLM, LLMUnavailable
from app.locks import SessionLocks
from app.models import IST, ActionStatus, AuditEntry, AuditEvent, PendingAction, utcnow
from app.repos import AuditLog, SessionRepo

log = logging.getLogger(__name__)

MAX_STEPS = 8

SYSTEM_PROMPT = """\
You help people book restaurant tables through Swiggy Dineout.

How to book:
1. Find the restaurant with search_restaurants. If more than one outlet matches, \
ask which one. Don't guess.
2. Check get_available_slots for the date and party size. Only offer times marked \
available. If the requested time is full, say so and suggest the nearest available times.
3. Call book_table with a time exactly as get_available_slots listed it. This does \
not book anything yet: it prepares the booking, and the person confirms it with a \
button in the app. Don't ask them to type "yes", and don't say it's booked.

A booking is only made or cancelled when the person presses Confirm or Cancel. \
The booking system reports the outcome to you in a system message on their next \
turn. Only then tell them it's booked, and give the booking ID.

All times are in India (Asia/Kolkata). After each user message, the booking app \
adds a system message with the current date and time. It is accurate: use it to work \
out "today", "tonight" and "tomorrow", and never ask the person for today's date.

Between midnight and 4 a.m., "tonight" and "tomorrow" are ambiguous: people often \
mean the coming evening, not the next calendar date. At those hours, ask which date \
they mean before checking slots or booking.

If a tool returns an error, explain it plainly and offer a way forward. Keep \
replies short."""

CONFIRM_PROMPT = "Please check the booking details and tap Confirm."
# Framed as coming from the booking system: phrased as "the user confirmed",
# the model read it as an unverified claim from the user and ignored it.
NOTES_HEADER = "Update from the booking system since your last reply (not from the user):"
UNAVAILABLE = "I'm having trouble right now. Please try again in a moment."
CANT_HELP = "Sorry, I can't help with that."
GAVE_UP = "Sorry, I couldn't finish that. Could you try again, maybe with a bit more detail?"


@dataclass(frozen=True)
class TurnResult:
    reply: str
    pending: PendingAction | None = None  # set when the client should show Confirm/Cancel


def _text(content: list[dict[str, Any]]) -> str:
    return "\n".join(b["text"] for b in content if b.get("type") == "text").strip()


def outcome_note(a: PendingAction) -> str | None:
    """What the model should be told after the client confirmed or cancelled."""
    match a.status:
        case ActionStatus.SUCCEEDED:
            return ("The user pressed Confirm in the app, and the booking you prepared was made.\n"
                    f"Booking: {json.dumps(a.result)}")
        case ActionStatus.FAILED:
            return ("The user pressed Confirm in the app, but the booking failed, so nothing was "
                    f"booked.\nPrepared: {a.summary}\nError: {json.dumps(a.error)}")
        case ActionStatus.UNKNOWN:
            return ("The user pressed Confirm in the app, but we couldn't tell whether the booking "
                    "went through. They can press Confirm again to retry safely; it won't book "
                    f"twice.\nPrepared: {a.summary}")
        case ActionStatus.CANCELLED:
            return f"The user pressed Cancel in the app. Nothing was booked.\nPrepared: {a.summary}"
        case ActionStatus.EXPIRED:
            return ("The booking you prepared expired before the user confirmed it. Nothing was "
                    "booked; check availability again if they still want it.\n"
                    f"Prepared: {a.summary}")
    return None  # PENDING / EXECUTING / SUPERSEDED: nothing settled to report


class Agent:
    def __init__(
        self,
        llm: LLM,
        gateway: Gateway,
        sessions: SessionRepo,
        audit: AuditLog,
        clock: Callable[[], datetime] = utcnow,
        max_steps: int = MAX_STEPS,
    ) -> None:
        self._llm = llm
        self._gateway = gateway
        self._sessions = sessions
        self._audit = audit
        self._clock = clock
        self._max_steps = max_steps
        self._locks = SessionLocks()  # turns in one session never interleave in history

    async def record_outcome(self, action: PendingAction) -> None:
        """Called by the API after confirm/cancel; delivered on the next turn."""
        note = outcome_note(action)
        if note is not None:
            await self._sessions.add_note(action.session_id, f"{action.id}:{action.status}", note)

    async def run_turn(self, session_id: str, user_text: str) -> TurnResult:
        async with self._locks.hold(session_id):
            return await self._run_turn(session_id, user_text)

    async def _run_turn(self, session_id: str, user_text: str) -> TurnResult:
        notes = await self._sessions.notes(session_id)
        now = self._clock().astimezone(IST)
        parts = [NOTES_HEADER + "\n" + "\n\n".join(notes.values())] if notes else []
        # Attributed explicitly: unlabelled, the model sometimes took this line for
        # something it had written itself and refused to trust the date (evals).
        clock_line = f"From the booking app (not the user): it is now {now:%A} {now.day} {now:%B %Y, %H:%M} IST."
        context = "\n\n".join([*parts, clock_line])
        opening = [{"role": "user", "content": user_text},
                   {"role": "system", "content": context}]
        tools = await self._gateway.tools()

        for step in range(self._max_steps):
            history = await self._sessions.history(session_id)
            try:
                resp = await self._llm.complete(SYSTEM_PROMPT, history + opening, tools)
            except LLMUnavailable:
                log.warning("llm unavailable", extra={"session_id": session_id, "step": step},
                            exc_info=True)
                return TurnResult(UNAVAILABLE)

            if resp.stop_reason not in ("end_turn", "tool_use"):
                # refusal, max_tokens, ...: don't store a partial or declined turn.
                log.warning("turn stopped: %s", resp.stop_reason,
                            extra={"session_id": session_id, "step": step})
                if resp.stop_reason == "refusal":
                    await self._audit.append(AuditEntry(
                        session_id, AuditEvent.REFUSED, at=self._clock(),
                        detail={"step": step, "stop_details": resp.stop_details}))
                    return TurnResult(CANT_HELP)
                return TurnResult(GAVE_UP)

            await self._sessions.append(session_id, *opening,
                                        {"role": "assistant", "content": resp.content})
            if opening:
                opening = []
                await self._sessions.clear_notes(session_id, list(notes))

            if not any(b.get("type") == "tool_use" for b in resp.content):
                # Seen live: an end_turn with no text. Never send the user a blank reply.
                return TurnResult(_text(resp.content) or GAVE_UP)

            results, pending = [], None
            for block in resp.content:
                if block.get("type") != "tool_use":
                    continue
                r = await self._gateway.call(session_id, block["name"], block["input"])
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block["id"],
                    "content": json.dumps(r.content),
                    **({"is_error": True} if "error" in r.content else {}),
                })
                pending = r.pending or pending
            await self._sessions.append(session_id, {"role": "user", "content": results})

            if pending is not None:
                return TurnResult(_text(resp.content) or CONFIRM_PROMPT, pending)

        log.warning("turn hit max steps (%d)", self._max_steps, extra={"session_id": session_id})
        return TurnResult(GAVE_UP)
