from datetime import datetime, timedelta

import pytest

from app.mcp_client import ToolSchema
from app.mock_dineout import MockDineout
from app.models import IST, RiskTier, ToolContext
from app.registry import (
    REGISTRY,
    ToolSpec,
    booking_summary,
    record_restaurants,
    record_slots,
    visible_tools,
)
from app.validators import slot_key

NOW = datetime(2026, 11, 26, 12, 0, tzinfo=IST)


def test_write_tool_requires_summary():
    with pytest.raises(ValueError, match="summary"):
        ToolSpec("book_table", RiskTier.WRITE)


def test_only_book_table_is_a_write():
    assert [n for n, s in REGISTRY.items() if s.tier is RiskTier.WRITE] == ["book_table"]


async def test_every_registered_tool_exists_on_the_server():
    # guards against registry/server drift
    assert REGISTRY.keys() <= {s.name for s in await MockDineout().list_tools()}


async def test_visible_tools_is_default_deny():
    schemas = await MockDineout().list_tools() + [ToolSchema("cancel_booking", "Cancel.", {})]
    names = [s.name for s in visible_tools(schemas)]
    assert "cancel_booking" not in names
    assert set(names) == REGISTRY.keys()


async def test_record_restaurants_accumulates():
    ctx = ToolContext(NOW, {})
    m = MockDineout()
    for q in ("phurr", "orange"):
        args = {"query": q, "city": "Bangalore"}
        record_restaurants(args, await m.call_tool("search_restaurants", args), ctx)
    assert set(ctx.facts["restaurants"]) == {
        "phurr-indiranagar", "phurr-koramangala", "orange-tree-bistro"}


async def test_record_slots_only_keeps_available():
    ctx = ToolContext(NOW, {})
    args = {"restaurant_id": "phurr-indiranagar", "date": "2026-11-26", "party_size": 2}
    record_slots(args, await MockDineout().call_tool("get_available_slots", args), ctx)
    slots = ctx.facts["slots"]
    assert slots[slot_key("phurr-indiranagar", 2, "2026-11-26T19:30:00+05:30")] == NOW.isoformat()
    assert slot_key("phurr-indiranagar", 2, "2026-11-26T20:00:00+05:30") not in slots  # full


def test_record_slots_newer_answer_removes_slot():
    key = slot_key("r1", 2, "2026-11-26T20:00:00+05:30")
    ctx = ToolContext(NOW, {"slots": {key: NOW.isoformat()}})
    record_slots({"restaurant_id": "r1", "party_size": 2},
                 {"slots": [{"time": "2026-11-26T20:00:00+05:30", "available": False}]}, ctx)
    assert key not in ctx.facts["slots"]


def test_record_slots_refreshes_seen_at():
    key = slot_key("r1", 2, "2026-11-26T20:00:00+05:30")
    later = NOW + timedelta(minutes=10)
    ctx = ToolContext(later, {"slots": {key: NOW.isoformat()}})
    record_slots({"restaurant_id": "r1", "party_size": 2},
                 {"slots": [{"time": "2026-11-26T14:30:00+00:00", "available": True}]}, ctx)
    assert ctx.facts["slots"][key] == later.isoformat()


def _ctx_with(rid, name, area):
    return ToolContext(NOW, {"restaurants": {rid: {"id": rid, "name": name, "area": area}}})


def test_booking_summary_uses_cached_server_facts():
    s = booking_summary({"restaurant_id": "phurr-indiranagar",
                         "time": "2026-11-26T20:00:00+05:30", "party_size": 2},
                        _ctx_with("phurr-indiranagar", "Phurr", "Indiranagar"))
    assert s == "Book a table for 2 people at Phurr, Indiranagar on Thu 26 Nov at 8:00 PM"


def test_booking_summary_singular():
    s = booking_summary({"restaurant_id": "x", "time": "2026-11-26T19:30:00+05:30",
                         "party_size": 1}, _ctx_with("x", "Orange Tree Bistro", "HSR Layout"))
    assert "for 1 person at Orange Tree Bistro, HSR Layout" in s and "7:30 PM" in s
