from datetime import timedelta

import pytest

from app.models import ActionStatus as S
from app.models import PendingAction, utcnow
from app.repos import InMemoryPendingActionRepo


def action(id="a1", session="s1"):
    return PendingAction(
        id=id, session_id=session, tool="book_table",
        args={"party_size": 2}, summary="Book Phurr for 2",
        idempotency_key=f"k-{id}", expires_at=utcnow() + timedelta(minutes=5),
    )


@pytest.fixture
def repo():
    return InMemoryPendingActionRepo()


def test_cas_succeeds_only_once(repo):
    repo.replace_pending("s1", action())
    assert repo.transition("a1", S.PENDING, S.EXECUTING) is not None
    assert repo.transition("a1", S.PENDING, S.EXECUTING) is None
    assert repo.get("a1").status == S.EXECUTING


def test_cas_concurrent_confirms_exactly_one_wins(repo):
    from concurrent.futures import ThreadPoolExecutor

    repo.replace_pending("s1", action())
    with ThreadPoolExecutor(max_workers=20) as pool:
        wins = list(pool.map(lambda _: repo.transition("a1", S.PENDING, S.EXECUTING), range(50)))
    assert sum(w is not None for w in wins) == 1


def test_transition_missing_action_returns_none(repo):
    assert repo.transition("nope", S.PENDING, S.EXECUTING) is None


def test_replace_pending_supersedes_old(repo):
    assert repo.replace_pending("s1", action("a1")) is None
    old = repo.replace_pending("s1", action("a2"))
    assert old.id == "a1" and old.status == S.SUPERSEDED
    assert repo.get("a1").status == S.SUPERSEDED
    assert repo.get("a2").status == S.PENDING


def test_replace_pending_leaves_other_sessions_alone(repo):
    repo.replace_pending("s1", action("a1", "s1"))
    assert repo.replace_pending("s2", action("a2", "s2")) is None
    assert repo.get("a1").status == S.PENDING


def test_replace_pending_rejects_session_mismatch(repo):
    with pytest.raises(ValueError):
        repo.replace_pending("s2", action(session="s1"))


@pytest.mark.parametrize("field", ["args", "id", "status", "idempotency_key", "summary"])
def test_transition_rejects_non_result_updates(repo, field):
    repo.replace_pending("s1", action())
    with pytest.raises(ValueError):
        repo.transition("a1", S.PENDING, S.EXECUTING, **{field: "x"})
    assert repo.get("a1").status == S.PENDING  # rejected before any change


def test_transition_sets_result(repo):
    repo.replace_pending("s1", action())
    repo.transition("a1", S.PENDING, S.EXECUTING)
    done = repo.transition("a1", S.EXECUTING, S.SUCCEEDED, result={"booking_id": "B1"})
    assert done.result == {"booking_id": "B1"}


def test_mutating_returned_action_does_not_touch_stored(repo):
    repo.replace_pending("s1", action())
    repo.get("a1").args["party_size"] = 10
    assert repo.get("a1").args == {"party_size": 2}


def test_mutating_input_after_store_does_not_touch_stored(repo):
    a = action()
    repo.replace_pending("s1", a)
    a.args["party_size"] = 10
    assert repo.get("a1").args == {"party_size": 2}


def test_mutating_transition_result_does_not_touch_stored(repo):
    repo.replace_pending("s1", action())
    repo.transition("a1", S.PENDING, S.EXECUTING)
    res = {"booking_id": "B1"}
    done = repo.transition("a1", S.EXECUTING, S.SUCCEEDED, result=res)
    res["booking_id"] = "X"
    done.result["booking_id"] = "Y"
    assert repo.get("a1").result == {"booking_id": "B1"}
