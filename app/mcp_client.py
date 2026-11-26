"""The boundary to Swiggy Dineout's MCP server. Mock now, live later."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol


class ErrorCode(StrEnum):
    SLOT_UNAVAILABLE = "SLOT_UNAVAILABLE"
    NOT_FOUND = "NOT_FOUND"
    UPSTREAM_ERROR = "UPSTREAM_ERROR"


class MCPError(Exception):
    """A failure the LLM can recover from, e.g. by offering another slot."""

    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message

    def to_result(self) -> dict[str, Any]:
        """Structured tool result handed back to the LLM instead of a traceback."""
        return {"error": {"code": str(self.code), "message": self.message}}


@dataclass(frozen=True)
class ToolSchema:
    """Owned by the MCP server. The registry only decides which ones are visible."""

    name: str
    description: str
    input_schema: dict[str, Any]


class MCPClient(Protocol):
    def list_tools(self) -> list[ToolSchema]: ...

    def call_tool(
        self, name: str, args: dict[str, Any], *, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        """Returns the tool's JSON result. Raises MCPError on failure."""
        ...
