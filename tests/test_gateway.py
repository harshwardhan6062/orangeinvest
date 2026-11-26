import asyncio
from datetime import datetime, timedelta

import pytest

from app.gateway import ActionNotFound, Gateway
from app.mcp_client import ToolSchema
from app.mock_dineout import MockDineout
from app.models import IST, ActionStatus, AuditEvent as E
from app.repos import InMemoryAuditLog, InMemoryPendingActionRepo, InMemorySessionRepo

T0 = datetime(2026, 11, 26, 12, 0, tzinfo=IST)
DAY = "2026-11-26"


def at(hhmm: str) -> str:
    return f"{DAY}T{hhmm}:00+05:30"


class Spy:
    """Wraps the mock: records calls, can yield mid-call, can fail in the ways
    a real network does.

    explode:       raise before reaching the server (nothing happened)
    lose_response: server does the work, then the response is lost
    hang:          block until cancelled
    """

    def __init__(self, inner, extra_tools=(), yield_first=False,
                 explode=False, lose_response=False, hang=False):
        self.inner, self.calls = inner, []
        self.extra_tools, self.yield_first = list(extra_tools), yield_first
        self.explode, self.lose_response, self.hang = explode, lose_response, hang
        self.entered = asyncio.Event()

    async def list_tools(self):
        return await self.inner.list_tools() + self.extra_tools

    async def call_tool(self, name, args, *, idempotency_key=None):
        self.calls.append((name, args, idempotency_key))
        self.entered.set()
        if self.yield_first:
            await asyncio.sleep(0)  # let a concurrent caller interleave
        if self.explode:
            raise TimeoutError("socket timed out")
        if self.hang:
            await asyncio.Event().wait()
        result = await self.inner.call_tool(name, args, idempotency_key=idempotency_key)
        if self.lose_response:
            raise ConnectionResetError("connection reset by peer")
        return result

    def count(self, name):
        return sum(1 for n, _, _ in self.calls if n == name)


class Clock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now

    def advance(self, **kw):
        self.now += timedelta(**kw)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def spy():
    return Spy(MockDineout())


@pytest.fixture
def audit():
    return InMemoryAuditLog()


@pytest.fixture
def gw(spy, audit, clock):
    return Gateway(spy, InMemorySessionRepo(), InMemoryPendingActionRepo(), audit, clock=clock)


async def events(audit, sid="s1"):
    return [e.event for e in await audit.entries(sid)]


async def look(gw, rid="phurr-indiranagar", party=2, sid="s1"):
    """search -> slots, the reads a booking needs first."""
    await gw.call(sid, "search_restaurants", {"query": "phurr", "city": "Bangalore"})
    await gw.call(sid, "search_restaurants", {"query": "orange", "city": "Bangalore"})
    await gw.call(sid, "get_available_slots", {"restaurant_id": rid, "date": DAY, "party_size": party})


async def propose(gw, rid="phurr-indiranagar", hhmm="19:30", party=2, sid="s1"):
    return await gw.call(sid, "book_table", {"restaurant_id": rid, "time": at(hhmm), "party_size": party})


# --- reads, deny, validation ------------------------------------------------

async def test_read_passes_through_and_is_audited(gw, spy, audit):
    r = await gw.call("s1", "search_restaurants", {"query": "phurr", "city": "Bangalore"})
    assert len(r.content["restaurants"]) == 2 and r.pending is None
    assert spy.count("search_restaurants") == 1
    assert await events(audit) == [E.READ]


async def test_unknown_tool_denied_and_not_called(gw, spy, audit):
    r = await gw.call("s1", "cancel_booking", {"booking_id": "SWG-1"})
    assert r.content["error"]["code"] == "NOT_ALLOWED"
    assert spy.calls == []
    assert await events(audit) == [E.DENIED]


async def test_tools_hides_unregistered_server_tools(audit, clock):
    spy = Spy(MockDineout(), extra_tools=[ToolSchema("cancel_booking", "Cancel.", {})])
    gw = Gateway(spy, InMemorySessionRepo(), InMemoryPendingActionRepo(), audit, clock=clock)
    assert "cancel_booking" not in {t.name for t in await gw.tools()}


async def test_invalid_args_rejected_before_server(gw, spy, audit):
    r = await gw.call("s1", "get_available_slots",
                      {"restaurant_id": "phurr-indiranagar", "date": DAY, "party_size": 50})
    assert r.content["error"]["code"] == "INVALID_ARGUMENTS"
    assert "party_size" in r.content["error"]["message"]
    assert spy.calls == []
    assert await events(audit) == [E.INVALID]


async def test_read_mcp_error_is_structured(gw):
    r = await gw.call("s1", "get_booking_status", {"booking_id": "SWG-404"})
    assert r.content == {"error": {"code": "NOT_FOUND", "message": "no booking 'SWG-404'"}}


async def test_read_unexpected_exception_becomes_upstream_error(audit, clock):
    gw = Gateway(Spy(MockDineout(), explode=True), InMemorySessionRepo(),
                 InMemoryPendingActionRepo(), audit, clock=clock)
    r = await gw.call("s1", "search_restaurants", {"query": "phurr", "city": "Bangalore"})
    assert r.content["error"]["code"] == "UPSTREAM_ERROR"
    assert "socket" not in r.content["error"]["message"]  # internals stay in the audit log
    [entry] = await audit.entries("s1")
    assert entry.event == E.READ_FAILED and "socket timed out" in entry.detail["exception"]


# --- proposing writes -------------------------------------------------------

async def test_write_is_proposed_not_executed(gw, spy, audit):
    await look(gw)
    r = await propose(gw)
    assert r.content["status"] == "needs_confirmation"
    assert r.content["summary"] == "Book a table for 2 people at Phurr, Indiranagar on Thu 26 Nov at 7:30 PM"
    assert r.pending.status == ActionStatus.PENDING
    assert r.pending.args == {"restaurant_id": "phurr-indiranagar", "time": at("19:30"), "party_size": 2}
    assert spy.count("book_table") == 0
    assert (await events(audit))[-1] == E.PROPOSED


async def test_book_without_search_is_rejected(gw, spy):
    r = await propose(gw)
    assert r.content["error"]["code"] == "INVALID_ARGUMENTS" and r.pending is None
    assert "search_restaurants" in r.content["error"]["message"]


async def test_book_full_slot_is_rejected_at_proposal(gw):
    await look(gw)
    r = await propose(gw, hhmm="20:00")  # mock has Indiranagar 20:00 full
    assert "get_available_slots" in r.content["error"]["message"]


async def test_book_with_stale_slots_is_rejected(gw, clock):
    await look(gw)
    clock.advance(minutes=6)
    r = await propose(gw)
    assert r.content["error"]["code"] == "INVALID_ARGUMENTS"


async def test_new_write_supersedes_old(gw, spy, audit):
    await look(gw)
    first = (await propose(gw, hhmm="19:00")).pending
    second = (await propose(gw, hhmm="19:30")).pending
    assert (await events(audit))[-2:] == [E.SUPERSEDED, E.PROPOSED]
    old = await gw.confirm("s1", first.id)
    assert old.status == ActionStatus.SUPERSEDED
    assert spy.count("book_table") == 0
    assert (await gw.confirm("s1", second.id)).status == ActionStatus.SUCCEEDED


# --- confirm ----------------------------------------------------------------

async def test_confirm_executes_stored_args_with_key(gw, spy, audit):
    await look(gw)
    a = (await propose(gw)).pending
    done = await gw.confirm("s1", a.id)
    assert done.status == ActionStatus.SUCCEEDED
    assert done.result["booking_id"].startswith("SWG-")
    [(name, args, key)] = [c for c in spy.calls if c[0] == "book_table"]
    assert args == a.args and key == a.idempotency_key
    assert (await events(audit))[-2:] == [E.CONFIRMED, E.SUCCEEDED]


async def test_double_confirm_books_once(gw, spy):
    await look(gw)
    a = (await propose(gw)).pending
    first = await gw.confirm("s1", a.id)
    second = await gw.confirm("s1", a.id)
    assert first == second
    assert spy.count("book_table") == 1


async def test_concurrent_confirms_book_once(audit, clock):
    spy = Spy(MockDineout(), yield_first=True)
    gw = Gateway(spy, InMemorySessionRepo(), InMemoryPendingActionRepo(), audit, clock=clock)
    await look(gw)
    a = (await propose(gw)).pending
    results = await asyncio.gather(*(gw.confirm("s1", a.id) for _ in range(10)))
    assert spy.count("book_table") == 1
    assert (await events(audit)).count(E.CONFIRMED) == 1
    # the session lock serialises them: everyone after the first sees its outcome
    assert {r.status for r in results} == {ActionStatus.SUCCEEDED}
    assert len({r.result["booking_id"] for r in results}) == 1


class YieldingRepo(InMemoryPendingActionRepo):
    """Yields on reads, like a real DB, so every concurrent confirm sees
    PENDING and the CAS is the only thing standing between them."""

    async def get(self, action_id):
        a = await super().get(action_id)
        await asyncio.sleep(0)
        return a


async def test_concurrent_confirms_that_all_saw_pending_book_once(audit, clock):
    spy = Spy(MockDineout())
    gw = Gateway(spy, InMemorySessionRepo(), YieldingRepo(), audit, clock=clock)
    await look(gw)
    a = (await propose(gw)).pending
    results = await asyncio.gather(*(gw.confirm("s1", a.id) for _ in range(10)))
    assert spy.count("book_table") == 1
    assert (await events(audit)).count(E.CONFIRMED) == 1
    # losers re-read after the CAS and get the winner's outcome, not a second booking
    assert {r.result["booking_id"] for r in results} == {results[0].result["booking_id"]}


async def test_confirm_after_expiry_does_not_execute(gw, spy, clock, audit):
    await look(gw)
    a = (await propose(gw)).pending
    clock.advance(minutes=5)
    done = await gw.confirm("s1", a.id)
    assert done.status == ActionStatus.EXPIRED
    assert spy.count("book_table") == 0
    assert (await events(audit))[-1] == E.EXPIRED
    assert (await gw.confirm("s1", a.id)).status == ActionStatus.EXPIRED


async def test_confirm_just_before_expiry_executes(gw, clock):
    await look(gw)
    a = (await propose(gw)).pending
    clock.advance(minutes=4, seconds=59)
    assert (await gw.confirm("s1", a.id)).status == ActionStatus.SUCCEEDED


async def test_confirm_from_other_session_is_not_found(gw, spy):
    await look(gw)
    a = (await propose(gw)).pending
    with pytest.raises(ActionNotFound):
        await gw.confirm("s2", a.id)
    with pytest.raises(ActionNotFound):
        await gw.confirm("s1", "no-such-id")
    assert spy.count("book_table") == 0


async def test_confirm_mcp_failure_marks_failed(gw, spy, audit):
    await look(gw, rid="phurr-koramangala")
    a = (await propose(gw, rid="phurr-koramangala", hhmm="21:00")).pending  # scripted failure
    done = await gw.confirm("s1", a.id)
    assert done.status == ActionStatus.FAILED
    assert done.error["code"] == "UPSTREAM_ERROR"
    assert (await events(audit))[-1] == E.FAILED
    # confirming again does not retry
    assert (await gw.confirm("s1", a.id)).status == ActionStatus.FAILED
    assert spy.count("book_table") == 1


async def test_confirm_slot_filled_meanwhile(audit, clock):
    mock = MockDineout(full=set())
    gw = Gateway(Spy(mock), InMemorySessionRepo(), InMemoryPendingActionRepo(), audit, clock=clock)
    await look(gw)
    a = (await propose(gw, hhmm="20:00")).pending
    mock._full = frozenset({("phurr-indiranagar", datetime.fromisoformat(at("20:00")).time())})
    done = await gw.confirm("s1", a.id)
    assert done.status == ActionStatus.FAILED and done.error["code"] == "SLOT_UNAVAILABLE"


# --- ambiguous outcomes: UNKNOWN, retry with the same key ---------------------

@pytest.fixture
def setup(audit, clock):
    """Gateway over a spy we can switch failure modes on, plus the raw mock."""
    mock = MockDineout()
    spy = Spy(mock)
    gw = Gateway(spy, InMemorySessionRepo(), InMemoryPendingActionRepo(), audit, clock=clock)
    return gw, spy, mock


def keys_used(spy):
    return [k for n, _, k in spy.calls if n == "book_table"]


async def test_timeout_marks_unknown_not_failed(setup, audit):
    gw, spy, _ = setup
    await look(gw)
    a = (await propose(gw)).pending
    spy.explode = True
    done = await gw.confirm("s1", a.id)
    assert done.status == ActionStatus.UNKNOWN
    assert done.error["code"] == "UPSTREAM_ERROR" and "socket" not in done.error["message"]
    [entry] = [e for e in await audit.entries("s1") if e.event == E.UNKNOWN]
    assert "socket timed out" in entry.detail["exception"]


async def test_unknown_retry_uses_same_key_and_succeeds(setup, audit):
    gw, spy, _ = setup
    await look(gw)
    a = (await propose(gw)).pending
    spy.explode = True
    await gw.confirm("s1", a.id)
    spy.explode = False
    done = await gw.confirm("s1", a.id)
    assert done.status == ActionStatus.SUCCEEDED and done.error is None
    assert keys_used(spy) == [a.idempotency_key] * 2
    confirms = [e for e in await audit.entries("s1") if e.event == E.CONFIRMED]
    assert [c.detail for c in confirms] == [None, {"retry": True}]


async def test_lost_response_then_retry_books_exactly_once(setup):
    # The dangerous case: the server booked, we never heard back.
    gw, spy, mock = setup
    await look(gw)
    a = (await propose(gw)).pending
    spy.lose_response = True
    assert (await gw.confirm("s1", a.id)).status == ActionStatus.UNKNOWN
    spy.lose_response = False
    done = await gw.confirm("s1", a.id)
    assert done.status == ActionStatus.SUCCEEDED
    assert len(mock._bookings) == 1  # the retry replayed, it didn't book again
    assert done.result["booking_id"] in mock._bookings


async def test_unknown_retry_ignores_ttl(setup, clock):
    # The user already confirmed in time; a retry is finishing that, not a new decision.
    gw, spy, _ = setup
    await look(gw)
    a = (await propose(gw)).pending
    spy.explode = True
    await gw.confirm("s1", a.id)
    clock.advance(minutes=30)
    spy.explode = False
    assert (await gw.confirm("s1", a.id)).status == ActionStatus.SUCCEEDED


async def test_unknown_can_still_end_in_failed(setup, audit):
    gw, spy, mock = setup
    await look(gw)
    a = (await propose(gw, hhmm="19:30")).pending
    spy.explode = True
    await gw.confirm("s1", a.id)
    mock._full = frozenset({("phurr-indiranagar", datetime.fromisoformat(at("19:30")).time())})
    spy.explode = False
    done = await gw.confirm("s1", a.id)
    assert done.status == ActionStatus.FAILED and done.error["code"] == "SLOT_UNAVAILABLE"
    assert (await gw.confirm("s1", a.id)).status == ActionStatus.FAILED  # terminal


async def test_unknown_cannot_be_cancelled(setup):
    gw, spy, _ = setup
    await look(gw)
    a = (await propose(gw)).pending
    spy.explode = True
    await gw.confirm("s1", a.id)
    assert (await gw.cancel("s1", a.id)).status == ActionStatus.UNKNOWN


async def test_cancellation_mid_booking_marks_unknown_and_reraises(setup, audit):
    gw, spy, mock = setup
    await look(gw)
    a = (await propose(gw)).pending
    spy.hang, spy.entered = True, asyncio.Event()
    task = asyncio.create_task(gw.confirm("s1", a.id))
    await spy.entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    stored = await gw._pending.get(a.id)
    assert stored.status == ActionStatus.UNKNOWN
    assert (await events(audit))[-1] == E.UNKNOWN
    # and it is recoverable, with the same key
    spy.hang = False
    done = await gw.confirm("s1", a.id)
    assert done.status == ActionStatus.SUCCEEDED
    assert keys_used(spy) == [a.idempotency_key] * 2
    assert len(mock._bookings) == 1


async def test_cancellation_releases_session_lock(setup):
    gw, spy, _ = setup
    await look(gw)
    a = (await propose(gw)).pending
    spy.hang, spy.entered = True, asyncio.Event()
    task = asyncio.create_task(gw.confirm("s1", a.id))
    await spy.entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    spy.hang = False
    r = await asyncio.wait_for(gw.call("s1", "search_restaurants",
                                       {"query": "phurr", "city": "Bangalore"}), timeout=1)
    assert len(r.content["restaurants"]) == 2


# --- per-session lock ---------------------------------------------------------

async def test_concurrent_reads_in_one_session_keep_all_facts(audit, clock):
    # Without the lock, both calls read empty facts and the second save
    # overwrites the first.
    gw = Gateway(Spy(MockDineout(), yield_first=True), InMemorySessionRepo(),
                 InMemoryPendingActionRepo(), audit, clock=clock)
    await asyncio.gather(
        gw.call("s1", "search_restaurants", {"query": "phurr", "city": "Bangalore"}),
        gw.call("s1", "search_restaurants", {"query": "orange", "city": "Bangalore"}),
    )
    await gw.call("s1", "get_available_slots",
                  {"restaurant_id": "orange-tree-bistro", "date": DAY, "party_size": 2})
    r = await propose(gw, rid="orange-tree-bistro")
    assert r.pending is not None, r.content
    r = await propose(gw, rid="phurr-indiranagar")
    assert "get_available_slots" in r.content["error"]["message"]  # restaurant known, slot not


async def test_other_sessions_are_not_blocked(setup):
    gw, spy, _ = setup
    await look(gw)
    a = (await propose(gw)).pending
    spy.hang, spy.entered = True, asyncio.Event()
    task = asyncio.create_task(gw.confirm("s1", a.id))
    await spy.entered.wait()
    spy.hang = False
    r = await asyncio.wait_for(gw.call("s2", "search_restaurants",
                                       {"query": "phurr", "city": "Bangalore"}), timeout=1)
    assert len(r.content["restaurants"]) == 2
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# --- cancel -----------------------------------------------------------------

async def test_cancel_then_confirm_does_not_execute(gw, spy, audit):
    await look(gw)
    a = (await propose(gw)).pending
    assert (await gw.cancel("s1", a.id)).status == ActionStatus.CANCELLED
    assert (await gw.confirm("s1", a.id)).status == ActionStatus.CANCELLED
    assert spy.count("book_table") == 0
    assert (await events(audit))[-1] == E.CANCELLED


async def test_cancel_after_success_is_a_noop(gw, audit):
    await look(gw)
    a = (await propose(gw)).pending
    await gw.confirm("s1", a.id)
    assert (await gw.cancel("s1", a.id)).status == ActionStatus.SUCCEEDED
    assert E.CANCELLED not in await events(audit)


# --- end to end -------------------------------------------------------------

async def test_status_after_booking(gw):
    await look(gw)
    a = (await propose(gw)).pending
    booking = (await gw.confirm("s1", a.id)).result
    r = await gw.call("s1", "get_booking_status", {"booking_id": booking["booking_id"]})
    assert r.content["status"] == "CONFIRMED"


async def test_sessions_do_not_share_facts(gw):
    await look(gw, sid="s1")
    r = await propose(gw, sid="s2")
    assert r.content["error"]["code"] == "INVALID_ARGUMENTS"
