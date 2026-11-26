"""Deterministic stand-in for Swiggy Dineout's MCP server.

Realism that the evals rely on:
- two "Phurr" outlets in Bangalore (ambiguous restaurant)
- some slots always full (slot-full path)
- some slots always fail with UPSTREAM_ERROR (failure path), scripted, never random
- book_table honours idempotency keys (duplicate confirm never double-books)
"""

from __future__ import annotations

import threading
from copy import deepcopy
from datetime import date, datetime, time
from typing import Any

from app.mcp_client import ErrorCode, MCPError, ToolSchema
from app.models import IST

RESTAURANTS: dict[str, dict[str, str]] = {
    r["id"]: r
    for r in [
        {"id": "phurr-indiranagar", "name": "Phurr", "area": "Indiranagar",
         "city": "Bangalore", "cuisine": "Modern Indian"},
        {"id": "phurr-koramangala", "name": "Phurr", "area": "Koramangala",
         "city": "Bangalore", "cuisine": "Modern Indian"},
        {"id": "orange-tree-bistro", "name": "Orange Tree Bistro", "area": "HSR Layout",
         "city": "Bangalore", "cuisine": "Continental"},
    ]
}

SLOT_TIMES = [time(h, m) for h in range(18, 23) for m in (0, 30)]  # 18:00–22:30 every day

# (restaurant_id, time of day). Applies to every date.
DEFAULT_FULL = frozenset({
    ("phurr-indiranagar", time(20, 0)),
    ("phurr-indiranagar", time(20, 30)),
})
DEFAULT_FAILING = frozenset({
    ("phurr-koramangala", time(21, 0)),
})


def _schema(name: str, description: str, **props: dict[str, Any]) -> ToolSchema:
    return ToolSchema(name, description, {
        "type": "object", "properties": props, "required": list(props),
        "additionalProperties": False,
    })


_STR = {"type": "string"}
_PARTY = {"type": "integer", "minimum": 1, "maximum": 20}

SCHEMAS = [
    _schema("search_restaurants", "Find restaurants by name in a city.",
            query=_STR, city=_STR),
    _schema("get_restaurant", "Get one restaurant's details.",
            restaurant_id=_STR),
    _schema("get_available_slots", "List table slots for a date. Each slot has an "
            "ISO time and whether it is available.",
            restaurant_id=_STR, date={"type": "string", "format": "date"}, party_size=_PARTY),
    _schema("book_table", "Book a table. `time` must be one of the slot times from "
            "get_available_slots, ISO 8601 with UTC offset.",
            restaurant_id=_STR, time={"type": "string", "format": "date-time"},
            party_size=_PARTY),
    _schema("get_booking_status", "Get a booking's current status.",
            booking_id=_STR),
]


class MockDineout:
    """Trusts its inputs: the gateway validates args before they get here."""

    def __init__(self, full=DEFAULT_FULL, failing=DEFAULT_FAILING) -> None:
        self._full = frozenset(full)
        self._failing = frozenset(failing)
        self._bookings: dict[str, dict[str, Any]] = {}
        self._booking_by_key: dict[str, str] = {}
        self._seq = 1000
        self._lock = threading.Lock()

    def list_tools(self) -> list[ToolSchema]:
        return list(SCHEMAS)

    def call_tool(
        self, name: str, args: dict[str, Any], *, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        match name:
            case "search_restaurants": return self._search(args)
            case "get_restaurant": return deepcopy(self._restaurant(args["restaurant_id"]))
            case "get_available_slots": return self._slots(args)
            case "book_table": return self._book(args, idempotency_key)
            case "get_booking_status": return self._status(args)
        raise MCPError(ErrorCode.NOT_FOUND, f"unknown tool {name!r}")

    def _restaurant(self, rid: str) -> dict[str, str]:
        if rid not in RESTAURANTS:
            raise MCPError(ErrorCode.NOT_FOUND, f"no restaurant {rid!r}")
        return RESTAURANTS[rid]

    def _search(self, args: dict[str, Any]) -> dict[str, Any]:
        q, city = args["query"].casefold(), args["city"].casefold()
        hits = [r for r in RESTAURANTS.values()
                if q in r["name"].casefold() and r["city"].casefold() == city]
        return {"restaurants": deepcopy(hits)}

    def _slots(self, args: dict[str, Any]) -> dict[str, Any]:
        rid = self._restaurant(args["restaurant_id"])["id"]
        d = date.fromisoformat(args["date"])
        return {"restaurant_id": rid, "slots": [
            {"time": datetime.combine(d, t, IST).isoformat(),
             "available": (rid, t) not in self._full}
            for t in SLOT_TIMES
        ]}

    def _book(self, args: dict[str, Any], key: str | None) -> dict[str, Any]:
        with self._lock:
            if key is not None and key in self._booking_by_key:
                return deepcopy(self._bookings[self._booking_by_key[key]])

            r = self._restaurant(args["restaurant_id"])
            when = datetime.fromisoformat(args["time"]).astimezone(IST)
            slot = (r["id"], when.time())
            if when.time() not in SLOT_TIMES or slot in self._full:
                raise MCPError(ErrorCode.SLOT_UNAVAILABLE,
                               f"{r['name']}, {r['area']} has no table at {when:%H:%M}")
            if slot in self._failing:
                raise MCPError(ErrorCode.UPSTREAM_ERROR, "Dineout could not confirm the booking")

            self._seq += 1
            booking = {
                "booking_id": f"SWG-{self._seq}",
                "status": "CONFIRMED",
                "restaurant_id": r["id"],
                "restaurant": f"{r['name']}, {r['area']}",
                "time": when.isoformat(),
                "party_size": args["party_size"],
            }
            self._bookings[booking["booking_id"]] = booking
            if key is not None:
                self._booking_by_key[key] = booking["booking_id"]
            return deepcopy(booking)

    def _status(self, args: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            b = self._bookings.get(args["booking_id"])
            if b is None:
                raise MCPError(ErrorCode.NOT_FOUND, f"no booking {args['booking_id']!r}")
            return deepcopy(b)
