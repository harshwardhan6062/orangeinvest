from datetime import datetime

import pytest

from app.models import IST
from app.validators import (
    ValidationError,
    validate_booking,
    validate_booking_id,
    validate_slots_query,
)

NOW = datetime(2026, 11, 26, 12, 0, tzinfo=IST)


def booking(**over):
    return {"restaurant_id": "phurr-indiranagar", "time": "2026-11-26T20:00:00+05:30",
            "party_size": 2} | over


def slots(**over):
    return {"restaurant_id": "phurr-indiranagar", "date": "2026-11-26", "party_size": 2} | over


def rejects(fn, args, match):
    with pytest.raises(ValidationError, match=match):
        fn(args, NOW)


def test_valid_booking_passes_through():
    assert validate_booking(booking(), NOW) == booking()


def test_booking_normalises_to_ist():
    out = validate_booking(booking(time="2026-11-26T14:30:00+00:00"), NOW)
    assert out["time"] == "2026-11-26T20:00:00+05:30"


@pytest.mark.parametrize("n", [1, 20])
def test_party_size_bounds_ok(n):
    assert validate_booking(booking(party_size=n), NOW)["party_size"] == n


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
    validate_booking(args, NOW)
    assert args["time"] == "2026-11-26T14:30:00+00:00"


def test_slots_today_ok_yesterday_rejected():
    assert validate_slots_query(slots(), NOW) == slots()
    rejects(validate_slots_query, slots(date="2026-11-25"), "past")


def test_slots_today_is_judged_in_ist():
    # 20:00 UTC on the 25th is already the 26th in Bangalore
    late_utc = datetime.fromisoformat("2026-11-25T20:00:00+00:00")
    with pytest.raises(ValidationError, match="past"):
        validate_slots_query(slots(date="2026-11-25"), late_utc)


@pytest.mark.parametrize("d", ["26/11/2026", "tonight", None])
def test_slots_bad_date_rejected(d):
    rejects(validate_slots_query, slots(date=d), "YYYY-MM-DD")


def test_booking_id():
    assert validate_booking_id({"booking_id": " SWG-1 "}, NOW) == {"booking_id": "SWG-1"}
    rejects(validate_booking_id, {"booking_id": ""}, "booking_id")
