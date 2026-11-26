import asyncio
from datetime import timedelta

import pytest

from app.models import ActionStatus as S
from app.models import PendingAction, utcnow
from app.repos import InMemoryPendingActionRepo, InMemorySessionRepo


def action(id="a1", session="s1"):
    return PendingAction(
        id=id, session_id=session, tool="book_table",
        args={"party_size": 2}, summary="Book Phurr for 2",
        idempotency_key=f"k-{id}", expires_at=utcnow() + timedelta(minutes=5),
    )


@pytest.fixture
def repo():
    return InMemoryPendingActionRepo()


async def test_cas_succeeds_only_once(repo):
    await repo.replace_pending("s1", action())
    assert await repo.transition("a1", S.PENDING, S.EXECUTING) is not None
    assert await repo.transition("a1", S.PENDING, S.EXECUTING) is None
    assert (await repo.get("a1")).status == S.EXECUTING


async def test_cas_concurrent_confirms_exactly_one_wins(repo):
    await repo.replace_pending("s1", action())
    wins = await asyncio.gather(*(repo.transition("a1", S.PENDING, S.EXECUTING) for _ in range(50)))
    assert sum(w is not None for w in wins) == 1


async def test_transition_missing_action_returns_none(repo):
    assert await repo.transition("nope", S.PENDING, S.EXECUTING) is None


async def test_replace_pending_supersedes_old(repo):
    assert await repo.replace_pending("s1", action("a1")) is None
    old = await repo.replace_pending("s1", action("a2"))
    assert old.id == "a1" and old.status == S.SUPERSEDED
    assert (await repo.get("a1")).status == S.SUPERSEDED
    assert (await repo.get("a2")).status == S.PENDING


async def test_replace_pending_leaves_other_sessions_alone(repo):
    await repo.replace_pending("s1", action("a1", "s1"))
    assert await repo.replace_pending("s2", action("a2", "s2")) is None
    assert (await repo.get("a1")).status == S.PENDING


async def test_replace_pending_rejects_session_mismatch(repo):
    with pytest.raises(ValueError):
        await repo.replace_pending("s2", action(session="s1"))


@pytest.mark.parametrize("field", ["args", "id", "status", "idempotency_key", "summary"])
async def test_transition_rejects_non_result_updates(repo, field):
    await repo.replace_pending("s1", action())
    with pytest.raises(ValueError):
        await repo.transition("a1", S.PENDING, S.EXECUTING, **{field: "x"})
    assert (await repo.get("a1")).status == S.PENDING  # rejected before any change


async def test_transition_sets_result(repo):
    await repo.replace_pending("s1", action())
    await repo.transition("a1", S.PENDING, S.EXECUTING)
    done = await repo.transition("a1", S.EXECUTING, S.SUCCEEDED, result={"booking_id": "B1"})
    assert done.result == {"booking_id": "B1"}


async def test_mutating_returned_action_does_not_touch_stored(repo):
    await repo.replace_pending("s1", action())
    (await repo.get("a1")).args["party_size"] = 10
    assert (await repo.get("a1")).args == {"party_size": 2}


async def test_mutating_input_after_store_does_not_touch_stored(repo):
    a = action()
    await repo.replace_pending("s1", a)
    a.args["party_size"] = 10
    assert (await repo.get("a1")).args == {"party_size": 2}


async def test_mutating_transition_result_does_not_touch_stored(repo):
    await repo.replace_pending("s1", action())
    await repo.transition("a1", S.PENDING, S.EXECUTING)
    res = {"booking_id": "B1"}
    done = await repo.transition("a1", S.EXECUTING, S.SUCCEEDED, result=res)
    res["booking_id"] = "X"
    done.result["booking_id"] = "Y"
    assert (await repo.get("a1")).result == {"booking_id": "B1"}


async def test_session_facts_are_copied():
    s = InMemorySessionRepo()
    facts = {"restaurants": {"r1": {"name": "Phurr"}}}
    await s.save_facts("s1", facts)
    facts["restaurants"]["r1"]["name"] = "X"
    (await s.facts("s1"))["restaurants"]["r1"]["name"] = "Y"
    assert await s.facts("s1") == {"restaurants": {"r1": {"name": "Phurr"}}}
    assert await s.facts("other") == {}
