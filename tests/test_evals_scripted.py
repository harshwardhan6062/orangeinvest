"""The eval cases in plain pytest, no API key: a scripted model plays each
conversation through the same harness and the same state-only checks.

- GOOD: a well-behaved model must pass every case.
- BAD: scripts that reproduce failures we've seen (or could see) live must
  fail the checks, which proves the checks catch them.
"""

import pytest

from evals.cases import CASES
from evals.harness import run_case
from tests.fakes import FakeLLM, say, use

CASE = {c.name: c for c in CASES}


def search(q):
    return ("search_restaurants", {"query": q, "city": "Bangalore"})


def slots(rid, date):
    return ("get_available_slots", {"restaurant_id": rid, "date": date, "party_size": 2})


def book(rid, date, hhmm):
    return ("book_table", {"restaurant_id": rid, "time": f"{date}T{hhmm}:00+05:30", "party_size": 2})


I, K = "phurr-indiranagar", "phurr-koramangala"
D3, D4 = "2026-12-03", "2026-12-04"
LOOK_AND_BOOK = [use(search("Phurr Indiranagar")), use(slots(I, D3)), use(book(I, D3, "19:30"))]

GOOD = {
    "happy_path": LOOK_AND_BOOK,
    "slot_full": [use(search("Phurr Indiranagar")), use(slots(I, D3)), say("8pm is full. 7:30?"),
                  use(book(I, D3, "19:30"))],
    "ambiguous_restaurant": [use(search("Phurr")), say("Which outlet?"),
                             use(slots(K, D3)), use(book(K, D3, "19:30"))],
    "user_declines": [*LOOK_AND_BOOK, say("No problem.")],
    "duplicate_confirm": LOOK_AND_BOOK,
    "outcome_note": [*LOOK_AND_BOOK, use(("get_booking_status", {"booking_id": "SWG-1001"})),
                     say("It's confirmed.")],
    "after_midnight_tomorrow": [say("Do you mean tonight (Thu 3 Dec) or Fri 4 Dec?"),
                                use(search("Phurr Indiranagar")), use(slots(I, D3)),
                                use(book(I, D3, "19:30"))],
}

BAD = [
    ("happy_path", "books a different time", [
        use(search("Phurr Indiranagar")), use(slots(I, D3)), use(book(I, D3, "19:00"))]),
    ("happy_path", "distrusts the clock and asks for today's date (seen live)", [
        use(search("Phurr Indiranagar")), say("What's today's date?")]),
    ("slot_full", "proposes another time without asking", [
        use(search("Phurr Indiranagar")), use(slots(I, D3)), use(book(I, D3, "19:30")), say("ok")]),
    ("ambiguous_restaurant", "guesses the outlet", [
        use(search("Phurr")), use(slots(I, D3)), use(book(I, D3, "19:30")), say("ok")]),
    ("user_declines", "proposes again after the user cancelled", [
        *LOOK_AND_BOOK, use(book(I, D3, "19:30"))]),
    ("outcome_note", "doesn't believe the outcome note (seen live: 'Not yet')", [
        *LOOK_AND_BOOK, say("Not yet, please press Confirm.")]),
    ("outcome_note", "thinks nothing was booked and proposes again", [
        *LOOK_AND_BOOK, use(book(I, D3, "19:30"))]),
    ("after_midnight_tomorrow", "guesses the next calendar date (seen live)", [
        use(search("Phurr Indiranagar")), use(slots(I, D4)), use(book(I, D4, "19:30")),
        use(slots(I, D3)), use(book(I, D3, "19:30"))]),
]


def test_every_case_has_a_good_script():
    assert GOOD.keys() == CASE.keys()


@pytest.mark.parametrize("name", GOOD)
async def test_well_behaved_model_passes(name):
    out = await run_case(CASE[name], FakeLLM(*GOOD[name]))
    assert out.passed, out.error


@pytest.mark.parametrize("name,label,script", BAD, ids=[f"{n}: {l}" for n, l, _ in BAD])
async def test_checks_catch_bad_behaviour(name, label, script):
    llm = FakeLLM(*script)
    out = await run_case(CASE[name], llm)
    assert not out.passed, f"check missed: {label}"
    assert llm.script == [], "script not fully consumed: the bad path wasn't actually exercised"
