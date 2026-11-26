import pytest

from app.mcp_client import ErrorCode, MCPError, ToolSchema
from app.mock_dineout import MockDineout
from app.models import RiskTier
from app.registry import REGISTRY, ToolSpec, booking_summary, visible_tools


def test_write_tool_requires_summary():
    with pytest.raises(ValueError, match="summary"):
        ToolSpec("book_table", RiskTier.WRITE)


def test_only_book_table_is_a_write():
    assert [n for n, s in REGISTRY.items() if s.tier is RiskTier.WRITE] == ["book_table"]


def test_every_registered_tool_exists_on_the_server():
    # guards against registry/server drift
    assert REGISTRY.keys() <= {s.name for s in MockDineout().list_tools()}


def test_visible_tools_is_default_deny():
    schemas = MockDineout().list_tools() + [ToolSchema("cancel_booking", "Cancel.", {})]
    names = [s.name for s in visible_tools(schemas)]
    assert "cancel_booking" not in names
    assert set(names) == REGISTRY.keys()


def test_booking_summary_uses_server_facts():
    s = booking_summary({"restaurant_id": "phurr-indiranagar",
                         "time": "2026-11-26T20:00:00+05:30", "party_size": 2}, MockDineout())
    assert s == "Book a table for 2 people at Phurr, Indiranagar on Thu 26 Nov at 8:00 PM"


def test_booking_summary_singular():
    s = booking_summary({"restaurant_id": "orange-tree-bistro",
                         "time": "2026-11-26T19:30:00+05:30", "party_size": 1}, MockDineout())
    assert "for 1 person at" in s


def test_booking_summary_unknown_restaurant_raises():
    with pytest.raises(MCPError) as e:
        booking_summary({"restaurant_id": "nope", "time": "2026-11-26T20:00:00+05:30",
                         "party_size": 2}, MockDineout())
    assert e.value.code == ErrorCode.NOT_FOUND
