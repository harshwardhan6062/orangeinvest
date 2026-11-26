"""Every tool call goes through here. The LLM decides what to do; the gateway
decides whether it's allowed.

- Unknown tools are denied (default deny).
- Args are validated before anything reaches the MCP server.
- READ tools run immediately; recorders cache what the server told us.
- WRITE tools never run from a model turn. They become a PendingAction that
  only `confirm()` (called by the client, never exposed to the LLM) executes,
  using the stored args and a gateway-generated idempotency key.
- Every decision is written to the audit log.

Tool calls within one session must be made one at a time: facts are
read-modify-write per call.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any
from uuid import uuid4

from app.mcp_client import ErrorCode, MCPClient, MCPError, ToolSchema
from app.models import (
    ActionStatus,
    AuditEntry,
    AuditEvent,
    PendingAction,
    RiskTier,
    ToolContext,
    utcnow,
)
from app.registry import REGISTRY, ToolSpec, visible_tools
from app.repos import AuditLog, PendingActionRepo, SessionRepo
from app.validators import ValidationError

PENDING_TTL = timedelta(minutes=5)


class GatewayCode(StrEnum):
    NOT_ALLOWED = "NOT_ALLOWED"
    INVALID_ARGUMENTS = "INVALID_ARGUMENTS"


class ActionNotFound(Exception):
    """No such action in this session. Same error either way, so one session
    can't probe another's action ids."""


@dataclass(frozen=True)
class ToolResult:
    content: dict[str, Any]                 # goes back to the LLM as the tool result
    pending: PendingAction | None = None    # set when a write was proposed: end the turn


def _error(code: str, message: str) -> dict[str, Any]:
    return {"error": {"code": str(code), "message": message}}


def _upstream_error(e: Exception) -> dict[str, Any]:
    if isinstance(e, MCPError):
        return e.to_result()
    # Unexpected failure (bug, timeout, transport). Don't leak internals to the LLM.
    return _error(ErrorCode.UPSTREAM_ERROR, "the restaurant service failed; try again")


class Gateway:
    def __init__(
        self,
        client: MCPClient,
        sessions: SessionRepo,
        pending: PendingActionRepo,
        audit: AuditLog,
        registry: dict[str, ToolSpec] = REGISTRY,
        clock: Callable[[], datetime] = utcnow,
        ttl: timedelta = PENDING_TTL,
    ) -> None:
        self._client = client
        self._sessions = sessions
        self._pending = pending
        self._audit = audit
        self._registry = registry
        self._clock = clock
        self._ttl = ttl

    async def tools(self) -> list[ToolSchema]:
        """Schemas the LLM may see: the server's, filtered by the registry."""
        return visible_tools(await self._client.list_tools(), self._registry)

    async def call(self, session_id: str, name: str, args: dict[str, Any]) -> ToolResult:
        spec = self._registry.get(name)
        if spec is None:
            await self._log(session_id, AuditEvent.DENIED, name, args)
            return ToolResult(_error(GatewayCode.NOT_ALLOWED, f"tool {name!r} is not available"))

        ctx = ToolContext(now=self._clock(), facts=await self._sessions.facts(session_id))
        if spec.validator is not None:
            try:
                args = spec.validator(args, ctx)
            except ValidationError as e:
                await self._log(session_id, AuditEvent.INVALID, name, args, detail={"message": str(e)})
                return ToolResult(_error(GatewayCode.INVALID_ARGUMENTS, str(e)))

        if spec.tier is RiskTier.WRITE:
            return await self._propose(session_id, spec, args, ctx)

        try:
            result = await self._client.call_tool(name, args)
        except Exception as e:
            err = _upstream_error(e)
            await self._log(session_id, AuditEvent.READ_FAILED, name, args,
                            detail={**err, "exception": repr(e)})
            return ToolResult(err)

        if spec.record is not None:
            spec.record(args, result, ctx)
            await self._sessions.save_facts(session_id, ctx.facts)
        await self._log(session_id, AuditEvent.READ, name, args)
        return ToolResult(result)

    async def _propose(
        self, session_id: str, spec: ToolSpec, args: dict[str, Any], ctx: ToolContext
    ) -> ToolResult:
        assert spec.summary is not None  # enforced by ToolSpec
        action = PendingAction(
            id=uuid4().hex,
            session_id=session_id,
            tool=spec.name,
            args=args,
            summary=spec.summary(args, ctx),
            idempotency_key=uuid4().hex,
            expires_at=ctx.now + self._ttl,
            created_at=ctx.now,
        )
        old = await self._pending.replace_pending(session_id, action)
        if old is not None:
            await self._log(session_id, AuditEvent.SUPERSEDED, old.tool, old.args,
                            action_id=old.id, detail={"superseded_by": action.id})
        await self._log(session_id, AuditEvent.PROPOSED, action.tool, action.args,
                        action_id=action.id, detail={"summary": action.summary})
        return ToolResult(
            {"status": "needs_confirmation", "action_id": action.id, "summary": action.summary},
            pending=action,
        )

    async def confirm(self, session_id: str, action_id: str) -> PendingAction:
        """Client-only. Executes the stored action at most once. Confirming an
        action that is no longer PENDING returns its current state unchanged."""
        a = await self._get(session_id, action_id)
        if a.status is not ActionStatus.PENDING:
            return a

        if a.is_expired(self._clock()):
            expired = await self._pending.transition(a.id, ActionStatus.PENDING, ActionStatus.EXPIRED)
            if expired is None:  # lost a race; report whatever won
                return await self._get(session_id, action_id)
            await self._log(session_id, AuditEvent.EXPIRED, a.tool, a.args, action_id=a.id)
            return expired

        running = await self._pending.transition(a.id, ActionStatus.PENDING, ActionStatus.EXECUTING)
        if running is None:
            return await self._get(session_id, action_id)
        await self._log(session_id, AuditEvent.CONFIRMED, a.tool, a.args, action_id=a.id)

        try:
            result = await self._client.call_tool(
                a.tool, a.args, idempotency_key=a.idempotency_key)
        except Exception as e:
            err = _upstream_error(e)["error"]
            done = await self._pending.transition(
                a.id, ActionStatus.EXECUTING, ActionStatus.FAILED, error=err)
            await self._log(session_id, AuditEvent.FAILED, a.tool, a.args, action_id=a.id,
                            detail={"error": err, "exception": repr(e)})
        else:
            done = await self._pending.transition(
                a.id, ActionStatus.EXECUTING, ActionStatus.SUCCEEDED, result=result)
            await self._log(session_id, AuditEvent.SUCCEEDED, a.tool, a.args, action_id=a.id,
                            detail={"result": result})
        assert done is not None  # only this call can move it out of EXECUTING
        return done

    async def cancel(self, session_id: str, action_id: str) -> PendingAction:
        """Client-only. Cancelling a non-PENDING action returns its state unchanged."""
        a = await self._get(session_id, action_id)
        done = await self._pending.transition(a.id, ActionStatus.PENDING, ActionStatus.CANCELLED)
        if done is None:
            return await self._get(session_id, action_id)
        await self._log(session_id, AuditEvent.CANCELLED, a.tool, a.args, action_id=a.id)
        return done

    async def _get(self, session_id: str, action_id: str) -> PendingAction:
        a = await self._pending.get(action_id)
        if a is None or a.session_id != session_id:
            raise ActionNotFound(action_id)
        return a

    async def _log(self, session_id: str, event: AuditEvent, tool: str, args: dict[str, Any],
                   action_id: str | None = None, detail: dict[str, Any] | None = None) -> None:
        await self._audit.append(AuditEntry(
            session_id=session_id, event=event, tool=tool, args=args,
            action_id=action_id, detail=detail, at=self._clock()))
