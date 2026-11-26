"""HTTP layer. Thin: parse, call the agent or gateway, shape the response.

    POST /sessions/{session_id}/messages                       chat turn
    POST /sessions/{session_id}/actions/{action_id}/confirm    client-only
    POST /sessions/{session_id}/actions/{action_id}/cancel     client-only

Confirm and cancel are the only way a write executes; the model has no tool for
them, and typing "yes" in chat is just another message. Both always answer 200
with the action's current status (repeating one is safe); the client renders
from `status`.

Run: uvicorn app.api:create_app --factory
No auth yet: anyone holding a session id can act on it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import FastAPI, HTTPException, Path
from pydantic import BaseModel, Field

from app.agent import Agent
from app.gateway import ActionNotFound, Gateway
from app.llm import AnthropicLLM
from app.mock_dineout import MockDineout
from app.models import ActionStatus, PendingAction
from app.repos import InMemoryAuditLog, InMemoryPendingActionRepo, InMemorySessionRepo

SessionId = Annotated[str, Path(pattern=r"^[A-Za-z0-9_-]{1,64}$")]
ActionId = Annotated[str, Path(pattern=r"^[a-f0-9]{32}$")]


class MessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


class PendingOut(BaseModel):
    id: str
    summary: str
    expires_at: datetime


class MessageOut(BaseModel):
    reply: str
    pending_action: PendingOut | None = None


class ActionOut(BaseModel):
    id: str
    status: ActionStatus
    summary: str
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None

    @classmethod
    def of(cls, a: PendingAction) -> ActionOut:
        return cls(id=a.id, status=a.status, summary=a.summary, result=a.result, error=a.error)


def default_components() -> tuple[Agent, Gateway]:
    """In-memory state + mock Dineout + Claude. Swap pieces here to go live."""
    sessions, pending, audit = InMemorySessionRepo(), InMemoryPendingActionRepo(), InMemoryAuditLog()
    gateway = Gateway(MockDineout(), sessions, pending, audit)
    return Agent(AnthropicLLM(), gateway, sessions, audit), gateway


def create_app(agent: Agent | None = None, gateway: Gateway | None = None) -> FastAPI:
    if agent is None or gateway is None:
        agent, gateway = default_components()
    app = FastAPI(title="orangeinvest")

    @app.post("/sessions/{session_id}/messages")
    async def post_message(session_id: SessionId, body: MessageIn) -> MessageOut:
        turn = await agent.run_turn(session_id, body.text)
        p = turn.pending
        return MessageOut(
            reply=turn.reply,
            pending_action=p and PendingOut(id=p.id, summary=p.summary, expires_at=p.expires_at))

    @app.post("/sessions/{session_id}/actions/{action_id}/confirm")
    async def confirm(session_id: SessionId, action_id: ActionId) -> ActionOut:
        try:
            action = await gateway.confirm(session_id, action_id)
        except ActionNotFound:
            raise HTTPException(404, "no such action in this session") from None
        await agent.record_outcome(action)
        return ActionOut.of(action)

    @app.post("/sessions/{session_id}/actions/{action_id}/cancel")
    async def cancel(session_id: SessionId, action_id: ActionId) -> ActionOut:
        try:
            action = await gateway.cancel(session_id, action_id)
        except ActionNotFound:
            raise HTTPException(404, "no such action in this session") from None
        await agent.record_outcome(action)
        return ActionOut.of(action)

    return app
