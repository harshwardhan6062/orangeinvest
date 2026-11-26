import pytest

from app.mcp_client import ErrorCode, MCPError
from app.mock_dineout import MockDineout

DAY = "2026-11-26"


def at(hhmm: str) -> str:
    return f"{DAY}T{hhmm}:00+05:30"


def book(m, rid="orange-tree-bistro", hhmm="20:00", party=2, key=None):
    return m.call_tool("book_table",
                       {"restaurant_id": rid, "time": at(hhmm), "party_size": party},
                       idempotency_key=key)


def code_of(fn) -> ErrorCode:
    with pytest.raises(MCPError) as e:
        fn()
    return e.value.code


@pytest.fixture
def m():
    return MockDineout()


def test_list_tools(m):
    assert {s.name for s in m.list_tools()} == {
        "search_restaurants", "get_restaurant", "get_available_slots",
        "book_table", "get_booking_status",
    }


def test_search_finds_both_phurr_outlets(m):
    res = m.call_tool("search_restaurants", {"query": "phurr", "city": "bangalore"})
    assert sorted(r["area"] for r in res["restaurants"]) == ["Indiranagar", "Koramangala"]


def test_search_other_city_is_empty_not_an_error(m):
    assert m.call_tool("search_restaurants", {"query": "phurr", "city": "Mumbai"}) == {"restaurants": []}


def test_slots_mark_full_ones_unavailable(m):
    res = m.call_tool("get_available_slots",
                      {"restaurant_id": "phurr-indiranagar", "date": DAY, "party_size": 2})
    avail = {s["time"]: s["available"] for s in res["slots"]}
    assert avail[at("20:00")] is False
    assert avail[at("19:30")] is True


def test_slots_unknown_restaurant(m):
    assert code_of(lambda: m.call_tool(
        "get_available_slots", {"restaurant_id": "nope", "date": DAY, "party_size": 2}
    )) == ErrorCode.NOT_FOUND


def test_book_and_status(m):
    b = book(m)
    assert b["status"] == "CONFIRMED" and b["restaurant"] == "Orange Tree Bistro, HSR Layout"
    assert m.call_tool("get_booking_status", {"booking_id": b["booking_id"]}) == b


def test_book_accepts_equivalent_utc_time(m):
    b = m.call_tool("book_table", {"restaurant_id": "orange-tree-bistro",
                                   "time": f"{DAY}T14:30:00+00:00", "party_size": 2})
    assert b["time"] == at("20:00")


@pytest.mark.parametrize("rid,hhmm", [
    ("phurr-indiranagar", "20:00"),  # scripted full
    ("orange-tree-bistro", "20:15"),  # not a slot
    ("orange-tree-bistro", "12:00"),  # outside service hours
])
def test_book_unavailable(m, rid, hhmm):
    assert code_of(lambda: book(m, rid, hhmm)) == ErrorCode.SLOT_UNAVAILABLE


def test_book_scripted_failure_is_deterministic(m):
    for _ in range(3):
        assert code_of(lambda: book(m, "phurr-koramangala", "21:00")) == ErrorCode.UPSTREAM_ERROR


def test_failed_booking_does_not_consume_key(m):
    assert code_of(lambda: book(m, "phurr-koramangala", "21:00", key="k1")) == ErrorCode.UPSTREAM_ERROR
    # the key was not recorded, so it books fresh rather than replaying a failure
    assert book(m, "phurr-koramangala", "19:00", key="k1")["status"] == "CONFIRMED"


def test_same_idempotency_key_books_once(m):
    first, second = book(m, key="k1"), book(m, key="k1")
    assert first == second
    assert book(m, key="k2")["booking_id"] != first["booking_id"]


def test_no_key_books_every_time(m):
    assert book(m)["booking_id"] != book(m)["booking_id"]


def test_unknown_booking_and_tool(m):
    assert code_of(lambda: m.call_tool("get_booking_status", {"booking_id": "X"})) == ErrorCode.NOT_FOUND
    assert code_of(lambda: m.call_tool("cancel_booking", {})) == ErrorCode.NOT_FOUND


def test_returned_data_is_a_copy(m):
    b = book(m, key="k1")
    b["party_size"] = 99
    assert book(m, key="k1")["party_size"] == 2


def test_error_to_result_shape():
    e = MCPError(ErrorCode.SLOT_UNAVAILABLE, "full")
    assert e.to_result() == {"error": {"code": "SLOT_UNAVAILABLE", "message": "full"}}
