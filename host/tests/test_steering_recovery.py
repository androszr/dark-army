"""Restart, replay, drop, interrupt and dedupe for the steering queue
(`docs/steering-queue.md`). The store is a temp file reopened under a fresh
daemon, as a relaunch finds it."""
import asyncio
import time

import pytest

from dark_army_daemon import dispatch, steering
from tests.test_steering_daemon import World


def _rows(w, **kw):
    return w.store.steer_enqueue({
        "session_id": "s1", "kind": "reply", "source": "mac",
        "payload": {"text": "hello"}, "action_id": "", **kw})


def _age(seconds):
    return time.time() - seconds


@pytest.fixture
def relaunch(monkeypatch, tmp_path):
    """`rows(w)` seeds the dead process's rows; the fresh daemon is returned."""
    monkeypatch.setattr(steering, "STEER_REPLAY_SETTLE_SECONDS", 0)
    made = []

    def build(seed):
        dead = World(monkeypatch, tmp_path)
        seed(dead)
        dead.close()
        w = World(monkeypatch, tmp_path, delay=0)
        w.logged = []
        w.d._log_event = lambda kind, **f: w.logged.append((kind, f))
        made.append(w)
        return w

    yield build
    for w in made:
        w.close()


async def _recover(w):
    await w.d.recover_actions()
    rep = await w.d.recover_steering()
    await w.d._steering_lanes().drained()
    return rep


def _kinds(w):
    return [k for k, _f in w.logged]


@pytest.mark.asyncio
async def test_a_fresh_queued_reply_is_typed_once(relaunch):
    w = relaunch(lambda dead: _rows(dead, created_at=_age(60)))
    rep = await _recover(w)
    assert rep["replayed"] == 1
    assert [t for _p, t, _n in w.sent] == ["\x15hello"]
    assert _kinds(w) == ["steer_replayed"]
    assert w.logged[0][1]["detail"]["kind"] == "reply"
    assert w.store.steer_counts() == {"typed": 1}
    assert w.store._conn.execute(
        "SELECT payload FROM steer_queue").fetchone()[0] == ""


@pytest.mark.asyncio
async def test_a_stale_queued_reply_is_not_delivered(relaunch):
    w = relaunch(lambda dead: _rows(dead, created_at=_age(180)))
    rep = await _recover(w)
    assert rep["dropped"] == 1 and w.sent == []
    assert _kinds(w) == ["steer_not_delivered"]
    detail = w.logged[0][1]["detail"]
    assert detail["kind"] == "reply" and detail["source"] == "mac"
    assert all(isinstance(v, str) for v in detail.values())
    assert "hello" not in str(w.logged)
    assert w.store.steer_counts() == {"not_delivered": 1}


@pytest.mark.asyncio
async def test_a_replay_that_waits_out_its_freshness_is_dropped_at_its_turn(
        relaunch, monkeypatch):
    w = relaunch(lambda dead: _rows(dead, created_at=_age(119.8)))
    monkeypatch.setattr(steering, "STEER_REPLAY_SETTLE_SECONDS", 0.5)
    rep = await _recover(w)
    assert rep["replayed"] == 1
    assert w.sent == []
    assert _kinds(w) == ["steer_not_delivered"]
    assert w.store.steer_counts() == {"not_delivered": 1}


@pytest.mark.asyncio
async def test_replays_wait_for_the_settle_pause(relaunch, monkeypatch):
    w = relaunch(lambda dead: _rows(dead, created_at=_age(10)))
    monkeypatch.setattr(steering, "STEER_REPLAY_SETTLE_SECONDS", 0.3)
    await w.d.recover_steering()
    await asyncio.sleep(0.1)
    assert w.sent == []
    await w.d._steering_lanes().drained()
    assert len(w.sent) == 1


@pytest.mark.asyncio
async def test_replays_keep_their_original_order(relaunch):
    def seed(dead):
        _rows(dead, created_at=_age(20), payload={"text": "second"})
        _rows(dead, created_at=_age(50), payload={"text": "first"})

    w = relaunch(seed)
    await _recover(w)
    assert [t for _p, t, _n in w.sent] == ["\x15first", "\x15second"]


@pytest.mark.asyncio
async def test_a_fresh_card_message_refused_at_replay_says_why(relaunch):
    def seed(dead):
        cid = dead.card()
        dead.cid = cid
        dead.store.update(cid, {"session_id": "", "link_state": ""})
        _rows(dead, kind="card_message", created_at=_age(30),
              payload={"card_id": cid, "text": "ping"})
        seed.cid = cid

    w = relaunch(seed)
    await _recover(w)
    assert w.sent == []
    assert _kinds(w) == ["steer_not_delivered"]
    assert w.logged[0][1]["detail"]["why"]
    assert w.store.steer_counts() == {"refused": 1}


@pytest.mark.asyncio
async def test_a_stale_card_message_leaves_a_note_with_the_excerpt(relaunch):
    ids = {}

    def seed(dead):
        ids["cid"] = dead.card()
        _rows(dead, kind="card_message", created_at=_age(300),
              payload={"card_id": ids["cid"], "text": "x" * 200})

    w = relaunch(seed)
    await _recover(w)
    assert w.sent == []
    notes = [m for m in w.store.messages(ids["cid"]) if m["kind"] == "note"]
    assert len(notes) == 1 and notes[0]["author"] == "bob"
    assert "x" * 80 in notes[0]["text"] and "x" * 81 not in notes[0]["text"]
    assert w.store._conn.execute(
        "SELECT payload FROM steer_queue").fetchone()[0] == ""


@pytest.mark.asyncio
async def test_a_fresh_answer_is_never_typed_and_is_surfaced(relaunch):
    w = relaunch(lambda dead: _rows(
        dead, kind="answer", created_at=_age(5),
        payload={"question_id": "q", "option_index": 0}))
    rep = await _recover(w)
    assert rep["dropped"] == 1 and w.sent == []
    assert _kinds(w) == ["steer_not_delivered"]
    assert w.logged[0][1]["detail"]["kind"] == "answer"


@pytest.mark.asyncio
async def test_an_answer_with_its_own_action_is_resolved_silently(relaunch):
    def seed(dead):
        _rows(dead, kind="answer", created_at=_age(5),
              payload={"question_id": "q", "option_index": 0})
        aid = dead.store.journal_begin("answer_burst", "s1")
        dead.store.journal_intent(aid, "answer_burst", "s1", "type", {})

    w = relaunch(seed)
    rep = await _recover(w)
    assert rep["interrupted"] == 1 and w.sent == []
    assert "steer_not_delivered" not in _kinds(w)
    assert w.store.steer_counts() == {"interrupted": 1}


@pytest.mark.asyncio
async def test_an_open_intent_is_interrupted_and_never_typed(relaunch):
    def seed(dead):
        aid = dead.store.journal_begin("steer", "s1")
        row = _rows(dead, created_at=_age(10), action_id=aid)
        dead.store.journal_intent(aid, "steer", "s1", "type",
                                  {"entry_id": row["id"]})

    w = relaunch(seed)
    rep = await _recover(w)
    assert rep["interrupted"] == 1 and w.sent == []
    assert _kinds(w) == ["action_interrupted"]
    assert w.store.steer_counts() == {"interrupted": 1}
    assert w.store.journal_open() == []


@pytest.mark.asyncio
async def test_an_interrupted_card_message_says_to_check_the_terminal(relaunch):
    ids = {}

    def seed(dead):
        ids["cid"] = dead.card()
        aid = dead.store.journal_begin("steer", "s1")
        row = _rows(dead, kind="card_message", created_at=_age(10),
                    action_id=aid, payload={"card_id": ids["cid"], "text": "q"})
        dead.store.journal_intent(aid, "steer", "s1", "type",
                                  {"entry_id": row["id"]})

    w = relaunch(seed)
    await _recover(w)
    notes = w.store.messages(ids["cid"])
    assert [m["text"] for m in notes] == [steering.STEER_CARD_INTERRUPTED_NOTE]


@pytest.mark.asyncio
async def test_a_row_whose_result_exists_is_copied_as_finished(relaunch):
    def seed(dead):
        aid = dead.store.journal_begin("steer", "s1")
        row = _rows(dead, created_at=_age(10), action_id=aid)
        intent = dead.store.journal_intent(aid, "steer", "s1", "type", {})
        dead.store.journal_result(intent, "typed")

    w = relaunch(seed)
    rep = await _recover(w)
    assert rep["finished"] == 1 and w.sent == []
    assert w.logged == []
    assert w.store.steer_counts() == {"typed": 1}


@pytest.mark.asyncio
async def test_a_phone_resend_after_restart_is_deduped(relaunch):
    digest = steering.dedupe_digest("dev", "token-cccccccc")

    def seed(dead):
        row = _rows(dead, created_at=_age(5), source="phone", dedupe=digest)
        dead.store.steer_resolve(row["id"], "typed", True, "")

    w = relaunch(seed)
    await _recover(w)
    with steering.calling(steering.Caller("phone", digest)):
        assert await w.d.reply_to_session("s1", "hello") == (True, "")
    assert w.sent == []


@pytest.mark.asyncio
async def test_a_phone_resend_while_the_replay_waits_joins_it(relaunch, monkeypatch):
    digest = steering.dedupe_digest("dev", "token-dddddddd")
    w = relaunch(lambda dead: _rows(dead, created_at=_age(5), source="phone",
                                    dedupe=digest))
    monkeypatch.setattr(steering, "STEER_REPLAY_SETTLE_SECONDS", 0.2)
    await w.d.recover_steering()
    with steering.calling(steering.Caller("phone", digest)):
        assert await w.d.reply_to_session("s1", "hello") == (True, "")
    await w.d._steering_lanes().drained()
    assert len(w.sent) == 1


@pytest.mark.asyncio
async def test_a_phone_resend_of_a_dropped_message_is_not_typed_late(relaunch):
    digest = steering.dedupe_digest("dev", "token-eeeeeeee")
    w = relaunch(lambda dead: _rows(dead, created_at=_age(500), source="phone",
                                    dedupe=digest))
    await _recover(w)
    with steering.calling(steering.Caller("phone", digest)):
        ok, detail = await w.d.reply_to_session("s1", "hello")
    assert ok is False and w.sent == []
    assert detail == steering.STEER_NOT_DELIVERED_WHY


@pytest.mark.asyncio
async def test_a_mac_press_joins_its_replay_twin(relaunch, monkeypatch):
    w = relaunch(lambda dead: _rows(dead, created_at=_age(5)))
    monkeypatch.setattr(steering, "STEER_REPLAY_SETTLE_SECONDS", 0.2)
    await w.d.recover_steering()
    assert await w.d.reply_to_session("s1", "hello") == (True, "")
    await w.d._steering_lanes().drained()
    assert len(w.sent) == 1


@pytest.mark.asyncio
async def test_two_live_identical_mac_presses_type_twice(relaunch):
    w = relaunch(lambda dead: None)
    await asyncio.gather(w.d.reply_to_session("s1", "yes"),
                         w.d.reply_to_session("s1", "yes"))
    assert len(w.sent) == 2


@pytest.mark.asyncio
async def test_recovering_twice_changes_nothing(relaunch):
    def seed(dead):
        _rows(dead, created_at=_age(10))
        _rows(dead, created_at=_age(500), payload={"text": "old"})

    w = relaunch(seed)
    first = await _recover(w)
    counts = w.store.steer_counts()
    sent = list(w.sent)
    second = await _recover(w)
    assert first["replayed"] == 1 and first["dropped"] == 1
    assert second == {"replayed": 0, "dropped": 0, "interrupted": 0,
                      "finished": 0}
    assert w.store.steer_counts() == counts and w.sent == sent


@pytest.mark.asyncio
async def test_recovery_never_launches_anything(relaunch, monkeypatch):
    def seed(dead):
        cid = dead.card()
        _rows(dead, kind="card_ask", created_at=_age(10),
              payload={"card_id": cid, "text": "why?"})

    w = relaunch(seed)

    async def boom(*a, **k):
        raise AssertionError("recovery launched something")

    monkeypatch.setattr(dispatch, "spawn", boom)
    monkeypatch.setattr(dispatch, "spawn_local", boom)
    w.d._ask_card_consult_locked = boom
    w.d.dispatch_card = boom
    await _recover(w)
    assert w.sent == []
    assert w.store.steer_counts() == {"refused": 1}


@pytest.mark.asyncio
async def test_without_a_board_recovery_is_a_no_op(monkeypatch):
    w = World(monkeypatch, None, board=False)
    assert await w.d.recover_steering() == {
        "replayed": 0, "dropped": 0, "interrupted": 0, "finished": 0}
