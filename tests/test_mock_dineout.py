import pytest

from app.mcp_client import ErrorCode, MCPError
from app.mock_dineout import MockDineout

DAY = "2026-11-26"


def at(hhmm: str) -> str:
    return f"{DAY}T{hhmm}:00+05:30"


async def book(m, rid="orange-tree-bistro", hhmm="20:00", party=2, key=None):
    return await m.call_tool("book_table",
                             {"restaurant_id": rid, "time": at(hhmm), "party_size": party},
                             idempotency_key=key)


async def code_of(coro) -> ErrorCode:
    with pytest.raises(MCPError) as e:
        await coro
    return e.value.code


@pytest.fixture
def m():
    return MockDineout()


async def test_list_tools(m):
    assert {s.name for s in await m.list_tools()} == {
        "search_restaurants", "get_available_slots", "book_table", "get_booking_status",
    }


async def test_search_finds_both_phurr_outlets(m):
    res = await m.call_tool("search_restaurants", {"query": "phurr", "city": "bangalore"})
    assert sorted(r["area"] for r in res["restaurants"]) == ["Indiranagar", "Koramangala"]


@pytest.mark.parametrize("q,ids", [
    ("Phurr Indiranagar", ["phurr-indiranagar"]),
    ("indiranagar", ["phurr-indiranagar"]),
    ("phurr koramangala", ["phurr-koramangala"]),
    ("Phurr HSR", []),
])
async def test_search_matches_name_and_area(m, q, ids):
    res = await m.call_tool("search_restaurants", {"query": q, "city": "Bangalore"})
    assert [r["id"] for r in res["restaurants"]] == ids


async def test_search_other_city_is_empty_not_an_error(m):
    res = await m.call_tool("search_restaurants", {"query": "phurr", "city": "Mumbai"})
    assert res == {"restaurants": []}


async def test_slots_mark_full_ones_unavailable(m):
    res = await m.call_tool("get_available_slots",
                            {"restaurant_id": "phurr-indiranagar", "date": DAY, "party_size": 2})
    avail = {s["time"]: s["available"] for s in res["slots"]}
    assert avail[at("20:00")] is False
    assert avail[at("19:30")] is True


async def test_slots_unknown_restaurant(m):
    assert await code_of(m.call_tool(
        "get_available_slots", {"restaurant_id": "nope", "date": DAY, "party_size": 2}
    )) == ErrorCode.NOT_FOUND


async def test_book_and_status(m):
    b = await book(m)
    assert b["status"] == "CONFIRMED" and b["restaurant"] == "Orange Tree Bistro, HSR Layout"
    assert await m.call_tool("get_booking_status", {"booking_id": b["booking_id"]}) == b


async def test_book_accepts_equivalent_utc_time(m):
    b = await m.call_tool("book_table", {"restaurant_id": "orange-tree-bistro",
                                         "time": f"{DAY}T14:30:00+00:00", "party_size": 2})
    assert b["time"] == at("20:00")


@pytest.mark.parametrize("rid,hhmm", [
    ("phurr-indiranagar", "20:00"),  # scripted full
    ("orange-tree-bistro", "20:15"),  # not a slot
    ("orange-tree-bistro", "12:00"),  # outside service hours
])
async def test_book_unavailable(m, rid, hhmm):
    assert await code_of(book(m, rid, hhmm)) == ErrorCode.SLOT_UNAVAILABLE


async def test_book_scripted_failure_is_deterministic(m):
    for _ in range(3):
        assert await code_of(book(m, "phurr-koramangala", "21:00")) == ErrorCode.UPSTREAM_ERROR


async def test_failed_booking_does_not_consume_key(m):
    assert await code_of(book(m, "phurr-koramangala", "21:00", key="k1")) == ErrorCode.UPSTREAM_ERROR
    # the key was not recorded, so it books fresh rather than replaying a failure
    assert (await book(m, "phurr-koramangala", "19:00", key="k1"))["status"] == "CONFIRMED"


async def test_same_key_same_args_books_once(m):
    first, second = await book(m, key="k1"), await book(m, key="k1")
    assert first == second
    assert (await book(m, key="k2"))["booking_id"] != first["booking_id"]


async def test_same_key_equivalent_time_is_not_a_conflict(m):
    first = await book(m, key="k1")
    again = await m.call_tool("book_table", {"restaurant_id": "orange-tree-bistro",
                                             "time": f"{DAY}T14:30:00+00:00", "party_size": 2},
                              idempotency_key="k1")
    assert again == first


@pytest.mark.parametrize("over", [{"hhmm": "19:30"}, {"party": 3}, {"rid": "phurr-koramangala"}])
async def test_same_key_different_args_conflicts(m, over):
    await book(m, key="k1")
    assert await code_of(book(m, key="k1", **over)) == ErrorCode.IDEMPOTENCY_CONFLICT


async def test_no_key_books_every_time(m):
    assert (await book(m))["booking_id"] != (await book(m))["booking_id"]


async def test_unknown_booking_and_tool(m):
    assert await code_of(m.call_tool("get_booking_status", {"booking_id": "X"})) == ErrorCode.NOT_FOUND
    assert await code_of(m.call_tool("get_restaurant", {"restaurant_id": "x"})) == ErrorCode.NOT_FOUND


async def test_returned_data_is_a_copy(m):
    b = await book(m, key="k1")
    b["party_size"] = 99
    assert (await book(m, key="k1"))["party_size"] == 2


def test_error_to_result_shape():
    e = MCPError(ErrorCode.SLOT_UNAVAILABLE, "full")
    assert e.to_result() == {"error": {"code": "SLOT_UNAVAILABLE", "message": "full"}}
