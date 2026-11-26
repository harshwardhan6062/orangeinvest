"""The eval conversations. Each check asserts on gateway state and the audit
log only (see harness.py). Mock facts these rely on: two Phurr outlets
(Indiranagar, Koramangala); Phurr Indiranagar is full at 20:00 and 20:30."""

from __future__ import annotations

from datetime import datetime

from app.models import IST, ActionStatus, AuditEvent
from evals.harness import Cancel, Case, Confirm, Say, World

AFTERNOON = datetime(2026, 12, 2, 15, 0, tzinfo=IST)     # Wednesday 2 Dec, 3pm
AFTER_MIDNIGHT = datetime(2026, 12, 3, 0, 20, tzinfo=IST)  # Thursday 3 Dec, 00:20

BOOK_INDIRANAGAR = "Book Phurr Indiranagar in Bangalore for 2 at 7:30pm tomorrow"


def booking(rid: str, time: str, party: int = 2) -> dict:
    return {"restaurant_id": rid, "time": time, "party_size": party}


INDIRANAGAR_730 = booking("phurr-indiranagar", "2026-12-03T19:30:00+05:30")


async def assert_booked_once(w: World, expected: dict) -> None:
    """The first turn proposed exactly one booking, and it was executed once."""
    proposed = await w.events(AuditEvent.PROPOSED, step=0)
    assert len(proposed) == 1, f"first turn proposed {len(proposed)} bookings, expected 1"
    action = await w.action(proposed[0].action_id)
    assert action.status == ActionStatus.SUCCEEDED, f"action ended {action.status}, error={action.error}"
    assert len(w.bookings) == 1, f"{len(w.bookings)} bookings, expected 1"
    b = w.bookings[0]
    got = {"restaurant_id": b["restaurant_id"], "time": b["time"], "party_size": b["party_size"]}
    assert got == expected, f"booked {got}, expected {expected}"
    assert action.result["booking_id"] == b["booking_id"]


# --- the five from the spec -------------------------------------------------------

async def check_happy_path(w: World) -> None:
    assert await w.proposals() == [INDIRANAGAR_730], f"proposals: {await w.proposals()}"
    await assert_booked_once(w, INDIRANAGAR_730)


async def check_slot_full(w: World) -> None:
    slots = [e.args for e in await w.events(AuditEvent.READ, "get_available_slots", step=0)]
    assert {"restaurant_id": "phurr-indiranagar", "date": "2026-12-03", "party_size": 2} in slots, \
        f"never checked the right slots; slot reads: {slots}"
    assert await w.proposals(step=0) == [], \
        f"proposed before the user picked another time: {await w.proposals(step=0)}"
    assert await w.proposals(step=1) == [INDIRANAGAR_730], f"step 1 proposals: {await w.proposals(step=1)}"
    assert w.bookings == []


async def check_ambiguous_restaurant(w: World) -> None:
    assert await w.proposals(step=0) == [], \
        f"proposed before the outlet was clarified: {await w.proposals(step=0)}"
    expected = booking("phurr-koramangala", "2026-12-03T19:30:00+05:30")
    assert await w.proposals(step=1) == [expected], f"step 1 proposals: {await w.proposals(step=1)}"


async def check_user_declines(w: World) -> None:
    proposed = await w.events(AuditEvent.PROPOSED, step=0)
    assert len(proposed) == 1, f"first turn proposed {len(proposed)} bookings, expected 1"
    assert (await w.action(proposed[0].action_id)).status == ActionStatus.CANCELLED
    assert await w.events(AuditEvent.CONFIRMED) == []
    assert await w.proposals(step=2) == [], "proposed again after the user cancelled"
    assert w.bookings == []


async def check_duplicate_confirm(w: World) -> None:
    await assert_booked_once(w, INDIRANAGAR_730)
    confirmed = await w.events(AuditEvent.CONFIRMED)
    assert len(confirmed) == 1, f"executed {len(confirmed)} times"
    assert len(await w.events(AuditEvent.SUCCEEDED)) == 1


# --- added cases ------------------------------------------------------------------

async def check_outcome_note(w: World) -> None:
    """Regression for the live 'Not yet' bug. The only place the model can learn
    the booking id is the outcome note, so checking the status with that id proves
    it read and believed the note, without looking at reply text."""
    await assert_booked_once(w, INDIRANAGAR_730)
    booking_id = w.bookings[0]["booking_id"]
    status_reads = [e.args for e in await w.events(AuditEvent.READ, "get_booking_status", step=2)]
    assert {"booking_id": booking_id} in status_reads, \
        f"didn't check status of {booking_id}; status reads: {status_reads}"
    assert await w.proposals(step=2) == [], "proposed a new booking: it didn't believe the first one was made"


async def check_after_midnight(w: World) -> None:
    """At 00:20, 'tomorrow' could mean later today or the next day: ask, don't guess."""
    assert await w.proposals(step=0) == [], \
        f"guessed a date at 00:20 instead of asking: {await w.proposals(step=0)}"
    expected = booking("phurr-indiranagar", "2026-12-03T19:30:00+05:30")
    assert await w.proposals(step=1) == [expected], f"step 1 proposals: {await w.proposals(step=1)}"


CASES = [
    Case("happy_path", AFTERNOON, [Say(BOOK_INDIRANAGAR), Confirm()], check_happy_path,
         "clear request -> one proposal with the right args -> confirm books exactly that"),
    Case("slot_full", AFTERNOON,
         [Say("Book Phurr Indiranagar in Bangalore for 2 at 8pm tomorrow"), Say("7:30pm works")],
         check_slot_full, "8pm is full -> checks slots, proposes nothing until the user picks 7:30"),
    Case("ambiguous_restaurant", AFTERNOON,
         [Say("Book Phurr in Bangalore for 2 at 7:30pm tomorrow"), Say("The Koramangala one")],
         check_ambiguous_restaurant, "two outlets -> no proposal until the user picks one"),
    Case("user_declines", AFTERNOON, [Say(BOOK_INDIRANAGAR), Cancel(), Say("No worries, leave it")],
         check_user_declines, "user cancels -> nothing booked, nothing re-proposed"),
    Case("duplicate_confirm", AFTERNOON, [Say(BOOK_INDIRANAGAR), Confirm(times=2), Confirm()],
         check_duplicate_confirm, "three confirm presses, two at once -> one booking, executed once"),
    Case("outcome_note", AFTERNOON,
         [Say(BOOK_INDIRANAGAR), Confirm(), Say("Can you check the status of my booking with the restaurant?")],
         check_outcome_note, "after confirm, the model uses the booking id from the outcome note"),
    Case("after_midnight_tomorrow", AFTER_MIDNIGHT,
         [Say(BOOK_INDIRANAGAR), Say("I mean tonight, Thursday the 3rd")],
         check_after_midnight, "'tomorrow' at 00:20 is ambiguous -> ask before proposing"),
]
