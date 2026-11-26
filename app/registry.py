"""Tool policy as plain data. The gateway reads it; nothing here wraps a call.

Schemas are NOT duplicated here: they come from MCPClient.list_tools() and are
filtered by this allowlist. A tool the server exposes but the registry doesn't
know about is invisible to the LLM and never executed (default deny).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.mcp_client import MCPClient, ToolSchema
from app.models import RiskTier
from app.validators import validate_booking, validate_booking_id, validate_slots_query

Validator = Callable[[dict[str, Any], datetime], dict[str, Any]]
# Gets read access to the MCP server so the summary shows facts from the
# server, not names the LLM typed. Called only on already-validated args.
Summarizer = Callable[[dict[str, Any], MCPClient], str]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    tier: RiskTier
    validator: Validator | None = None
    summary: Summarizer | None = None

    def __post_init__(self) -> None:
        if self.tier is RiskTier.WRITE and self.summary is None:
            raise ValueError(f"WRITE tool {self.name!r} needs a summary builder")


def booking_summary(args: dict[str, Any], client: MCPClient) -> str:
    r = client.call_tool("get_restaurant", {"restaurant_id": args["restaurant_id"]})
    when = datetime.fromisoformat(args["time"])
    hour = when.strftime("%I:%M %p").lstrip("0")
    n = args["party_size"]
    return (f"Book a table for {n} {'person' if n == 1 else 'people'} at "
            f"{r['name']}, {r['area']} on {when:%a %d %b} at {hour}")


REGISTRY: dict[str, ToolSpec] = {s.name: s for s in [
    ToolSpec("search_restaurants", RiskTier.READ),
    ToolSpec("get_restaurant", RiskTier.READ),
    ToolSpec("get_available_slots", RiskTier.READ, validator=validate_slots_query),
    ToolSpec("book_table", RiskTier.WRITE, validator=validate_booking, summary=booking_summary),
    ToolSpec("get_booking_status", RiskTier.READ, validator=validate_booking_id),
]}


def visible_tools(schemas: list[ToolSchema], registry: dict[str, ToolSpec] = REGISTRY) -> list[ToolSchema]:
    """What the LLM gets to see: server schemas, filtered by the allowlist."""
    return [s for s in schemas if s.name in registry]
