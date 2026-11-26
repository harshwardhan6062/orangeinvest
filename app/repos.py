"""State behind interfaces. In-memory now, Postgres later."""

from __future__ import annotations

import threading
from copy import deepcopy
from dataclasses import replace
from typing import Any, Protocol

from app.models import ActionStatus, AuditEntry, PendingAction

# transition() may only fill these; everything else on an action is fixed at creation.
_UPDATABLE = frozenset({"result", "error"})


class SessionRepo(Protocol):
    def history(self, session_id: str) -> list[dict[str, Any]]: ...
    def append(self, session_id: str, *messages: dict[str, Any]) -> None: ...


class PendingActionRepo(Protocol):
    def get(self, action_id: str) -> PendingAction | None: ...

    def replace_pending(self, session_id: str, action: PendingAction) -> PendingAction | None:
        """Atomically mark the session's current PENDING action SUPERSEDED and
        store `action`. Returns the superseded action, if any."""
        ...

    def transition(
        self, action_id: str, frm: ActionStatus, to: ActionStatus, **updates: Any
    ) -> PendingAction | None:
        """Atomic compare-and-set. Returns the updated action, or None if the
        action is missing or its status is not `frm`. Only `result` and `error`
        may be updated; anything else raises ValueError."""
        ...


class AuditLog(Protocol):
    def append(self, entry: AuditEntry) -> None: ...
    def entries(self, session_id: str | None = None) -> list[AuditEntry]: ...


class InMemorySessionRepo:
    def __init__(self) -> None:
        self._msgs: dict[str, list[dict[str, Any]]] = {}
        self._lock = threading.Lock()

    def history(self, session_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return deepcopy(self._msgs.get(session_id, []))

    def append(self, session_id: str, *messages: dict[str, Any]) -> None:
        with self._lock:
            self._msgs.setdefault(session_id, []).extend(deepcopy(messages))


class InMemoryPendingActionRepo:
    """Stores private copies: nothing a caller holds aliases stored state."""

    def __init__(self) -> None:
        self._by_id: dict[str, PendingAction] = {}
        self._lock = threading.Lock()

    def get(self, action_id: str) -> PendingAction | None:
        with self._lock:
            return deepcopy(self._by_id.get(action_id))

    def replace_pending(self, session_id: str, action: PendingAction) -> PendingAction | None:
        if action.session_id != session_id:
            raise ValueError("action belongs to a different session")
        if action.status != ActionStatus.PENDING:
            raise ValueError("new action must be PENDING")
        with self._lock:
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

    def transition(
        self, action_id: str, frm: ActionStatus, to: ActionStatus, **updates: Any
    ) -> PendingAction | None:
        bad = updates.keys() - _UPDATABLE
        if bad:
            raise ValueError(f"cannot update {sorted(bad)}")
        with self._lock:
            a = self._by_id.get(action_id)
            if a is None or a.status != frm:
                return None
            a = replace(a, status=to, **deepcopy(updates))
            self._by_id[action_id] = a
            return deepcopy(a)


class InMemoryAuditLog:
    def __init__(self) -> None:
        self._entries: list[AuditEntry] = []
        self._lock = threading.Lock()

    def append(self, entry: AuditEntry) -> None:
        with self._lock:
            self._entries.append(deepcopy(entry))

    def entries(self, session_id: str | None = None) -> list[AuditEntry]:
        with self._lock:
            return deepcopy(
                [e for e in self._entries if session_id is None or e.session_id == session_id]
            )
