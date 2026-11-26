from datetime import datetime, timedelta

import pytest

from app.models import IST, ToolContext
from app.validators import (
    ValidationError,
    slot_key,
    validate_booking,
    validate_booking_id,
    validate_search,
    validate_slots_query,
)

NOW = datetime(2026, 11, 26, 12, 0, tzinfo=IST)
EIGHT_PM = "2026-11-26T20:00:00+05:30"


def facts(seen_at=NOW, party=2, time=EIGHT_PM, rid="phurr-indiranagar"):
    """Facts as if the session searched for the restaurant and saw the slot."""
    return {
        "restaurants": {rid: {"id": rid, "name": "Phurr", "area": "Indiranagar"}},
        "slots": {slot_key(rid, party, time): seen_at.isoformat()},
    }


def ctx(now=NOW, **kw):
    return ToolContext(now=now, facts=facts(**kw))


def booking(**over):
    return {"restaurant_id": "phurr-indiranagar", "time": EIGHT_PM, "party_size": 2} | over


def slots(**over):
    return {"restaurant_id": "phurr-indiranagar", "date": "2026-11-26", "party_size": 2} | over


def rejects(fn, args, match, c=None):
    with pytest.raises(ValidationError, match=match):
        fn(args, c or ctx())


# --- booking: shape ---------------------------------------------------------

def test_valid_booking_passes_through():
    assert validate_booking(booking(), ctx()) == booking()


def test_booking_normalises_to_ist():
    assert validate_booking(booking(time="2026-11-26T14:30:00+00:00"), ctx())["time"] == EIGHT_PM


@pytest.mark.parametrize("n", [1, 20])
def test_party_size_bounds_ok(n):
    assert validate_booking(booking(party_size=n), ctx(party=n))["party_size"] == n


@pytest.mark.parametrize("n", [0, 21, -1, "2", 2.0, True, None])
def test_party_size_rejected(n):
    rejects(validate_booking, booking(party_size=n), "party_size")


@pytest.mark.parametrize("t", ["2026-11-26T11:00:00+05:30", "2026-11-26T12:00:00+05:30"])
def test_past_or_now_time_rejected(t):
    rejects(validate_booking, booking(time=t), "past")


def test_naive_time_rejected():
    rejects(validate_booking, booking(time="2026-11-26T20:00:00"), "UTC offset")


@pytest.mark.parametrize("t", ["8pm tonight", "", None, 2000])
def test_unparseable_time_rejected(t):
    rejects(validate_booking, booking(time=t), "ISO 8601")


def test_missing_and_extra_fields_rejected():
    args = booking()
    del args["party_size"]
    rejects(validate_booking, args, "missing")
    rejects(validate_booking, booking(note="window seat"), "unexpected")


@pytest.mark.parametrize("rid", ["", "   ", None, 7])
def test_bad_restaurant_id_rejected(rid):
    rejects(validate_booking, booking(restaurant_id=rid), "restaurant_id")


def test_validator_does_not_mutate_input():
    args = booking(time="2026-11-26T14:30:00+00:00")
    validate_booking(args, ctx())
    assert args["time"] == "2026-11-26T14:30:00+00:00"


# --- booking: must match what the server showed this session ----------------

def test_booking_restaurant_never_searched():
    rejects(validate_booking, booking(), "search_restaurants", ToolContext(NOW, {}))


def test_booking_slot_never_seen():
    c = ctx()
    c.facts["slots"].clear()
    rejects(validate_booking, booking(), "get_available_slots", c)


def test_booking_slot_seen_for_different_party_size():
    rejects(validate_booking, booking(party_size=4), "get_available_slots", ctx(party=2))


def test_booking_different_time_than_seen():
    rejects(validate_booking, booking(time="2026-11-26T20:30:00+05:30"), "get_available_slots")


def test_booking_slot_freshness_boundary():
    seen = NOW - timedelta(minutes=5)
    assert validate_booking(booking(), ctx(seen_at=seen))  # exactly 5 min: ok
    rejects(validate_booking, booking(), "get_available_slots",
            ctx(seen_at=seen - timedelta(seconds=1)))


def test_booking_slot_seen_as_utc_matches_ist_key():
    # the LLM may echo the slot time in another offset; normalisation makes it match
    assert validate_booking(booking(time="2026-11-26T14:30:00+00:00"), ctx())


# --- other validators -------------------------------------------------------

def test_slots_today_ok_yesterday_rejected():
    assert validate_slots_query(slots(), ctx()) == slots()
    rejects(validate_slots_query, slots(date="2026-11-25"), "past")


def test_slots_today_is_judged_in_ist():
    # 20:00 UTC on the 25th is already the 26th in Bangalore
    late_utc = datetime.fromisoformat("2026-11-25T20:00:00+00:00")
    rejects(validate_slots_query, slots(date="2026-11-25"), "past", ToolContext(late_utc, {}))


@pytest.mark.parametrize("d", ["26/11/2026", "tonight", None])
def test_slots_bad_date_rejected(d):
    rejects(validate_slots_query, slots(date=d), "YYYY-MM-DD")


def test_search():
    assert validate_search({"query": " Phurr ", "city": "Bangalore"}, ctx()) == \
        {"query": "Phurr", "city": "Bangalore"}
    rejects(validate_search, {"query": "", "city": "Bangalore"}, "query")
    rejects(validate_search, {"query": "Phurr"}, "missing")


def test_booking_id():
    assert validate_booking_id({"booking_id": " SWG-1 "}, ctx()) == {"booking_id": "SWG-1"}
    rejects(validate_booking_id, {"booking_id": ""}, "booking_id")
