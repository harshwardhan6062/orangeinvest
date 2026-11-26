"""HTTP layer over the real agent + gateway + mock, with a scripted LLM."""

from datetime import timedelta

import httpx
import pytest

from app.agent import Agent
from app.api import create_app
from app.gateway import Gateway
from app.mock_dineout import MockDineout
from app.repos import InMemoryAuditLog, InMemoryPendingActionRepo, InMemorySessionRepo
from tests.fakes import BOOK, SEARCH, SLOTS, Clock, FakeLLM, assert_valid, say, use

BOOKING_TURN = (use(SEARCH), use(SLOTS), use(BOOK))


@pytest.fixture
def world():
    clock, sessions, audit, mock = Clock(), InMemorySessionRepo(), InMemoryAuditLog(), MockDineout()
    gw = Gateway(mock, sessions, InMemoryPendingActionRepo(), audit, clock=clock)

    def client(*script):
        llm = FakeLLM(*script)
        app = create_app(Agent(llm, gw, sessions, audit, clock=clock), gw)
        c = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        c.llm = llm
        return c

    client.mock, client.clock, client.sessions = mock, clock, sessions
    return client


async def book(c, sid="s1"):
    r = await c.post(f"/sessions/{sid}/messages", json={"text": "book Phurr Indiranagar for 2 at 7:30pm"})
    assert r.status_code == 200
    return r.json()


async def test_message_returns_reply_and_pending_action(world):
    async with world(*BOOKING_TURN) as c:
        body = await book(c)
    p = body["pending_action"]
    assert p["summary"] == "Book a table for 2 people at Phurr, Indiranagar on Thu 26 Nov at 7:30 PM"
    assert set(p) == {"id", "summary", "expires_at"}  # no args, no idempotency key
    assert world.mock._bookings == {}


async def test_plain_reply_has_no_pending_action(world):
    async with world(say("Hi! Where would you like to eat?")) as c:
        r = await c.post("/sessions/s1/messages", json={"text": "hello"})
    assert r.json() == {"reply": "Hi! Where would you like to eat?", "pending_action": None}


async def test_confirm_books_and_model_hears_about_it(world):
    async with world(*BOOKING_TURN, say("Booked!")) as c:
        aid = (await book(c))["pending_action"]["id"]
        r = await c.post(f"/sessions/s1/actions/{aid}/confirm")
        assert r.status_code == 200
        action = r.json()
        assert action["status"] == "SUCCEEDED"
        assert action["result"]["booking_id"] in world.mock._bookings
        await c.post("/sessions/s1/messages", json={"text": "did it work?"})
        sent = c.llm.calls[-1]
    assert_valid(sent)
    assert sent[-1]["role"] == "system" and action["result"]["booking_id"] in sent[-1]["content"]


async def test_double_confirm_is_safe(world):
    async with world(*BOOKING_TURN) as c:
        aid = (await book(c))["pending_action"]["id"]
        first = (await c.post(f"/sessions/s1/actions/{aid}/confirm")).json()
        second = (await c.post(f"/sessions/s1/actions/{aid}/confirm")).json()
    assert first == second
    assert len(world.mock._bookings) == 1
    assert len(await world.sessions.notes("s1")) == 1  # told once


async def test_cancel(world):
    async with world(*BOOKING_TURN) as c:
        aid = (await book(c))["pending_action"]["id"]
        r = await c.post(f"/sessions/s1/actions/{aid}/cancel")
        assert r.json()["status"] == "CANCELLED"
        confirm = await c.post(f"/sessions/s1/actions/{aid}/confirm")
    assert confirm.json()["status"] == "CANCELLED"
    assert world.mock._bookings == {}
    assert "pressed Cancel" in next(iter((await world.sessions.notes("s1")).values()))


async def test_confirm_after_expiry(world):
    async with world(*BOOKING_TURN) as c:
        aid = (await book(c))["pending_action"]["id"]
        world.clock.now += timedelta(minutes=6)
        r = await c.post(f"/sessions/s1/actions/{aid}/confirm")
    assert r.json()["status"] == "EXPIRED"
    assert world.mock._bookings == {}


async def test_typing_yes_never_books(world):
    # Even if the model reacts to "yes" by calling book_table again, that only proposes.
    async with world(*BOOKING_TURN, use(SLOTS), use(BOOK)) as c:
        first = (await book(c))["pending_action"]["id"]
        r = await c.post("/sessions/s1/messages", json={"text": "yes"})
    assert world.mock._bookings == {}
    assert r.json()["pending_action"]["id"] != first  # a new proposal; the old one is superseded


@pytest.mark.parametrize("path", [
    "/sessions/s2/actions/{aid}/confirm",              # another session's action
    "/sessions/s1/actions/" + "0" * 32 + "/confirm",   # no such action
    "/sessions/s2/actions/{aid}/cancel",
])
async def test_unknown_action_is_404(world, path):
    async with world(*BOOKING_TURN) as c:
        aid = (await book(c))["pending_action"]["id"]
        r = await c.post(path.format(aid=aid))
    assert r.status_code == 404
    assert world.mock._bookings == {}


@pytest.mark.parametrize("path,body", [
    ("/sessions/s1/messages", {"text": ""}),
    ("/sessions/s1/messages", {"text": "x" * 2001}),
    ("/sessions/s1/messages", {}),
    ("/sessions/bad%20id/messages", {"text": "hi"}),
    ("/sessions/" + "a" * 65 + "/messages", {"text": "hi"}),
    ("/sessions/s1/actions/not-hex/confirm", None),
])
async def test_bad_input_is_422(world, path, body):
    async with world() as c:
        r = await c.post(path, json=body)
    assert r.status_code == 422
    assert c.llm.calls == []  # never reaches the model


def test_app_builds_with_defaults(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")  # client is built, never called
    routes = {r.path for r in create_app().routes}
    assert {"/sessions/{session_id}/messages",
            "/sessions/{session_id}/actions/{action_id}/confirm",
            "/sessions/{session_id}/actions/{action_id}/cancel"} <= routes


async def test_root_serves_the_chat_page(world):
    async with world() as c:
        r = await c.get("/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    page = r.text
    assert "<title>orangeinvest" in page
    # the page drives exactly the three API endpoints
    for needle in ("/messages", "/actions/${this.p.id}/${verb}", "crypto.randomUUID()"):
        assert needle in page
