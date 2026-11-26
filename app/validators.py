"""Deterministic arg checks. They run before any tool call, so the mock and the
live server only ever see clean args. Each returns a normalised copy.

Strict on purpose: unknown fields are rejected, not dropped.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from app.models import IST, ToolContext

MIN_PARTY, MAX_PARTY = 1, 20
SLOT_FRESHNESS = timedelta(minutes=5)


class ValidationError(ValueError):
    """Message is shown to the LLM so it can fix the call or ask the user."""


def to_ist(iso: str) -> str:
    return datetime.fromisoformat(iso).astimezone(IST).isoformat()


def slot_key(restaurant_id: str, party_size: int, time_iso: str) -> str:
    """Key into facts["slots"]. `time_iso` must already be IST-normalised."""
    return f"{restaurant_id}|{party_size}|{time_iso}"


def _check_keys(args: dict[str, Any], expected: set[str]) -> None:
    missing, extra = expected - args.keys(), args.keys() - expected
    if missing:
        raise ValidationError(f"missing fields: {sorted(missing)}")
    if extra:
        raise ValidationError(f"unexpected fields: {sorted(extra)}")


def _non_empty_str(args: dict[str, Any], key: str) -> str:
    v = args[key]
    if not isinstance(v, str) or not v.strip():
        raise ValidationError(f"{key} must be a non-empty string")
    return v.strip()


def _party_size(args: dict[str, Any]) -> int:
    v = args["party_size"]
    if type(v) is not int or not MIN_PARTY <= v <= MAX_PARTY:  # rejects bool and "2"
        raise ValidationError(f"party_size must be an integer from {MIN_PARTY} to {MAX_PARTY}")
    return v


def validate_search(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    _check_keys(args, {"query", "city"})
    return {"query": _non_empty_str(args, "query"), "city": _non_empty_str(args, "city")}


def validate_slots_query(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    _check_keys(args, {"restaurant_id", "date", "party_size"})
    try:
        d = date.fromisoformat(args["date"])
    except (TypeError, ValueError):
        raise ValidationError("date must be YYYY-MM-DD") from None
    if d < ctx.now.astimezone(IST).date():
        raise ValidationError("date is in the past")
    return {"restaurant_id": _non_empty_str(args, "restaurant_id"),
            "date": d.isoformat(), "party_size": _party_size(args)}


def validate_booking(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    _check_keys(args, {"restaurant_id", "time", "party_size"})
    try:
        when = datetime.fromisoformat(args["time"])
    except (TypeError, ValueError):
        raise ValidationError("time must be ISO 8601, e.g. 2026-11-26T20:00:00+05:30") from None
    if when.tzinfo is None:
        raise ValidationError("time must include a UTC offset, e.g. +05:30")
    if when <= ctx.now:
        raise ValidationError("time is in the past")
    out = {"restaurant_id": _non_empty_str(args, "restaurant_id"),
           "time": when.astimezone(IST).isoformat(), "party_size": _party_size(args)}

    # Only book what the server actually showed us, recently, for this party size.
    if out["restaurant_id"] not in ctx.facts.get("restaurants", {}):
        raise ValidationError(
            "restaurant_id was not in any search results this session; call search_restaurants first")
    seen = ctx.facts.get("slots", {}).get(slot_key(out["restaurant_id"], out["party_size"], out["time"]))
    if seen is None or ctx.now - datetime.fromisoformat(seen) > SLOT_FRESHNESS:
        raise ValidationError(
            "this time was not offered as available for this restaurant and party size in the "
            "last 5 minutes; call get_available_slots first")
    return out


def validate_booking_id(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    _check_keys(args, {"booking_id"})
    return {"booking_id": _non_empty_str(args, "booking_id")}
