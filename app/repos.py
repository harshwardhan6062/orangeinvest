"""State behind async interfaces. In-memory now, Postgres later.

The in-memory implementations never `await` inside a method, so each method
runs atomically on the event loop; that is what makes `transition` a real
compare-and-set here. A Postgres implementation gets the same guarantee from
`UPDATE ... WHERE status = :frm`.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from typing import Any, Protocol

from app.models import ActionStatus, AuditEntry, PendingAction

# transition() may only fill these; everything else on an action is fixed at creation.
_UPDATABLE = frozenset({"result", "error"})


class SessionRepo(Protocol):
    async def history(self, session_id: str) -> list[dict[str, Any]]: ...
    async def append(self, session_id: str, *messages: dict[str, Any]) -> None: ...
    async def facts(self, session_id: str) -> dict[str, Any]: ...
    async def save_facts(self, session_id: str, facts: dict[str, Any]) -> None: ...


class PendingActionRepo(Protocol):
    async def get(self, action_id: str) -> PendingAction | None: ...

    async def replace_pending(self, session_id: str, action: PendingAction) -> PendingAction | None:
        """Atomically mark the session's current PENDING action SUPERSEDED and
        store `action`. Returns the superseded action, if any."""
        ...

    async def transition(
        self, action_id: str, frm: ActionStatus, to: ActionStatus, **updates: Any
    ) -> PendingAction | None:
        """Atomic compare-and-set. Returns the updated action, or None if the
        action is missing or its status is not `frm`. Only `result` and `error`
        may be updated; anything else raises ValueError."""
        ...


class AuditLog(Protocol):
    async def append(self, entry: AuditEntry) -> None: ...
    async def entries(self, session_id: str | None = None) -> list[AuditEntry]: ...


class InMemorySessionRepo:
    def __init__(self) -> None:
        self._msgs: dict[str, list[dict[str, Any]]] = {}
        self._facts: dict[str, dict[str, Any]] = {}

    async def history(self, session_id: str) -> list[dict[str, Any]]:
        return deepcopy(self._msgs.get(session_id, []))

    async def append(self, session_id: str, *messages: dict[str, Any]) -> None:
        self._msgs.setdefault(session_id, []).extend(deepcopy(messages))

    async def facts(self, session_id: str) -> dict[str, Any]:
        return deepcopy(self._facts.get(session_id, {}))

    async def save_facts(self, session_id: str, facts: dict[str, Any]) -> None:
        self._facts[session_id] = deepcopy(facts)


class InMemoryPendingActionRepo:
    """Stores private copies: nothing a caller holds aliases stored state."""

    def __init__(self) -> None:
        self._by_id: dict[str, PendingAction] = {}

    async def get(self, action_id: str) -> PendingAction | None:
        return deepcopy(self._by_id.get(action_id))

    async def replace_pending(self, session_id: str, action: PendingAction) -> PendingAction | None:
        if action.session_id != session_id:
            raise ValueError("action belongs to a different session")
        if action.status != ActionStatus.PENDING:
            raise ValueError("new action must be PENDING")
        old = next(
            (a for a in self._by_id.values()
             if a.session_id == session_id and a.status == ActionStatus.PENDING),
            None,
        )
        if old is not None:
            old = replace(old, status=ActionStatus.SUPERSEDED)
            self._by_id[old.id] = old
        self._by_id[action.id] = deepcopy(action)
        return deepcopy(old)

    async def transition(
        self, action_id: str, frm: ActionStatus, to: ActionStatus, **updates: Any
    ) -> PendingAction | None:
        bad = updates.keys() - _UPDATABLE
        if bad:
            raise ValueError(f"cannot update {sorted(bad)}")
        a = self._by_id.get(action_id)
        if a is None or a.status != frm:
            return None
        a = replace(a, status=to, **deepcopy(updates))
        self._by_id[action_id] = a
        return deepcopy(a)


class InMemoryAuditLog:
    def __init__(self) -> None:
        self._entries: list[AuditEntry] = []

    async def append(self, entry: AuditEntry) -> None:
        self._entries.append(deepcopy(entry))

    async def entries(self, session_id: str | None = None) -> list[AuditEntry]:
        return deepcopy(
            [e for e in self._entries if session_id is None or e.session_id == session_id]
        )
