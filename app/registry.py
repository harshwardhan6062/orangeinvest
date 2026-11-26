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

from app.mcp_client import ToolSchema
from app.models import RiskTier, ToolContext
from app.validators import (
    slot_key,
    to_ist,
    validate_booking,
    validate_booking_id,
    validate_search,
    validate_slots_query,
)

# All three are sync and pure apart from Recorder mutating ctx.facts.
Validator = Callable[[dict[str, Any], ToolContext], dict[str, Any]]
Summarizer = Callable[[dict[str, Any], ToolContext], str]  # gets validated args
Recorder = Callable[[dict[str, Any], dict[str, Any], ToolContext], None]  # (args, result, ctx) after a read


@dataclass(frozen=True)
class ToolSpec:
    name: str
    tier: RiskTier
    validator: Validator | None = None
    summary: Summarizer | None = None
    record: Recorder | None = None

    def __post_init__(self) -> None:
        if self.tier is RiskTier.WRITE and self.summary is None:
            raise ValueError(f"WRITE tool {self.name!r} needs a summary builder")


def record_restaurants(args: dict[str, Any], result: dict[str, Any], ctx: ToolContext) -> None:
    ctx.facts.setdefault("restaurants", {}).update({r["id"]: r for r in result["restaurants"]})


def record_slots(args: dict[str, Any], result: dict[str, Any], ctx: ToolContext) -> None:
    slots = ctx.facts.setdefault("slots", {})
    for s in result["slots"]:
        key = slot_key(args["restaurant_id"], args["party_size"], to_ist(s["time"]))
        if s["available"]:
            slots[key] = ctx.now.isoformat()
        else:
            slots.pop(key, None)  # a newer answer says it's gone


def booking_summary(args: dict[str, Any], ctx: ToolContext) -> str:
    # validate_booking guarantees the restaurant is in facts, so these are
    # server facts, not names the LLM typed.
    r = ctx.facts["restaurants"][args["restaurant_id"]]
    when = datetime.fromisoformat(args["time"])
    hour = when.strftime("%I:%M %p").lstrip("0")
    n = args["party_size"]
    return (f"Book a table for {n} {'person' if n == 1 else 'people'} at "
            f"{r['name']}, {r['area']} on {when:%a %d %b} at {hour}")


REGISTRY: dict[str, ToolSpec] = {s.name: s for s in [
    ToolSpec("search_restaurants", RiskTier.READ, validator=validate_search, record=record_restaurants),
    ToolSpec("get_available_slots", RiskTier.READ, validator=validate_slots_query, record=record_slots),
    ToolSpec("book_table", RiskTier.WRITE, validator=validate_booking, summary=booking_summary),
    ToolSpec("get_booking_status", RiskTier.READ, validator=validate_booking_id),
]}


def visible_tools(schemas: list[ToolSchema], registry: dict[str, ToolSpec] = REGISTRY) -> list[ToolSchema]:
    """What the LLM gets to see: server schemas, filtered by the allowlist."""
    return [s for s in schemas if s.name in registry]
