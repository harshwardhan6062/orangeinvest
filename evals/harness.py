"""Runs a scripted conversation through the real HTTP API, agent, gateway and
mock, with any LLM (the real model, or a scripted fake in plain pytest).

Checks may only look at state: the audit log, stored actions, and the mock's
bookings. Never at reply text: wording varies run to run, state shouldn't.
Replies are kept on the World only so humans can read failing runs.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx

from app.agent import Agent
from app.api import create_app
from app.gateway import Gateway
from app.llm import LLM
from app.mock_dineout import MockDineout
from app.models import AuditEntry, AuditEvent, PendingAction
from app.repos import InMemoryAuditLog, InMemoryPendingActionRepo, InMemorySessionRepo

SID = "eval"


@dataclass(frozen=True)
class Say:
    text: str


@dataclass(frozen=True)
class Confirm:
    """Press Confirm on the latest proposed action, `times` presses at once."""
    times: int = 1


@dataclass(frozen=True)
class Cancel:
    pass


Step = Say | Confirm | Cancel


@dataclass(frozen=True)
class Case:
    name: str
    now: datetime  # frozen clock: the agent's "current time" and the gateway's
    steps: list[Step]
    check: Callable[[World], Awaitable[None]]  # raises AssertionError on failure
    about: str = ""


class World:
    def __init__(self, llm: LLM, now: datetime) -> None:
        clock = lambda: now  # noqa: E731
        sessions = InMemorySessionRepo()
        self.audit = InMemoryAuditLog()
        self.pending = InMemoryPendingActionRepo()
        self.mock = MockDineout()
        self.gateway = Gateway(self.mock, sessions, self.pending, self.audit, clock=clock)
        agent = Agent(llm, self.gateway, sessions, self.audit, clock=clock)
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(agent, self.gateway)),
                                      base_url="http://eval")
        self.marks: list[int] = []  # audit length when each step started
        self.replies: list[str] = []  # for humans only; never asserted on

    async def run(self, steps: list[Step]) -> None:
        async with self.http:
            for i, step in enumerate(steps):
                self.marks.append(len(await self.audit.entries(SID)))
                await self._do(i, step)
        self.marks.append(len(await self.audit.entries(SID)))

    async def _do(self, i: int, step: Step) -> None:
        if isinstance(step, Say):
            r = await self.http.post(f"/sessions/{SID}/messages", json={"text": step.text})
            r.raise_for_status()
            self.replies.append(r.json()["reply"])
            return
        proposed = await self.events(AuditEvent.PROPOSED)
        assert proposed, f"step {i} ({type(step).__name__}): no proposed action to act on"
        verb = "confirm" if isinstance(step, Confirm) else "cancel"
        url = f"/sessions/{SID}/actions/{proposed[-1].action_id}/{verb}"
        responses = await asyncio.gather(*(self.http.post(url) for _ in range(getattr(step, "times", 1))))
        for r in responses:
            r.raise_for_status()

    # --- state queries for checks ------------------------------------------------

    async def events(self, event: AuditEvent | None = None, tool: str | None = None,
                     step: int | None = None) -> list[AuditEntry]:
        entries = await self.audit.entries(SID)
        if step is not None:
            entries = entries[self.marks[step]:self.marks[step + 1]]
        return [e for e in entries
                if (event is None or e.event == event) and (tool is None or e.tool == tool)]

    async def proposals(self, step: int | None = None) -> list[dict[str, Any]]:
        """Args of every write the model proposed (in that step)."""
        return [e.args for e in await self.events(AuditEvent.PROPOSED, step=step)]

    async def action(self, action_id: str) -> PendingAction:
        a = await self.pending.get(action_id)
        assert a is not None
        return a

    @property
    def bookings(self) -> list[dict[str, Any]]:
        return list(self.mock._bookings.values())


@dataclass
class Outcome:
    case: str
    passed: bool
    error: str | None
    world: World = field(repr=False)


async def run_case(case: Case, llm: LLM) -> Outcome:
    world = World(llm, case.now)
    try:
        await world.run(case.steps)
        await case.check(world)
    except AssertionError as e:
        return Outcome(case.name, False, str(e) or "assertion failed", world)
    return Outcome(case.name, True, None, world)
