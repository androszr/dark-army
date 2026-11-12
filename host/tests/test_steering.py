"""The steering queue's pure half: the rules and the lanes
(`docs/steering-queue.md`). No store, no daemon, `asyncio` only."""
import asyncio

import pytest

from dark_army_daemon import command_receipts, steering
from dark_army_daemon.steering import Entry, Lanes


# --- the rules -----------------------------------------------------------------

@pytest.mark.parametrize("age,fresh", [(0, True), (60, True), (120, True),
                                       (120.001, False), (-1, False)])
def test_is_fresh_at_the_edges(age, fresh):
    assert steering.is_fresh(1000.0, 1000.0 + age) is fresh


def test_is_fresh_refuses_a_garbled_stamp():
    assert steering.is_fresh(None, 5) is False
    assert steering.is_fresh("x", 5) is False


def test_the_retention_is_the_phone_marks_own():
    assert steering.STEER_RETENTION_SECONDS == command_receipts.RECEIPT_SECONDS


def test_the_kinds_are_exactly_the_ten_and_six_replay():
    assert list(steering.KINDS) == [
        "reply", "card_message", "card_ask", "terminal_line", "wrap_up",
        "low_priority", "answer", "autocompact", "permission_key", "raw"]
    assert [k for k, v in steering.KINDS.items() if v.replay] == [
        "reply", "card_message", "card_ask", "terminal_line", "wrap_up",
        "low_priority"]
    assert [k for k, v in steering.KINDS.items() if v.durable] == [
        "reply", "card_message", "card_ask", "terminal_line", "wrap_up",
        "low_priority", "answer"]
    assert steering.KINDS["answer"].journal == "own"
    assert steering.KINDS["autocompact"].journal == "own"
    assert steering.KINDS["reply"].journal == "steer"
    assert set(steering.WORDS) == set(steering.KINDS)


def test_the_words_never_say_the_old_name():
    for text in list(steering.WORDS.values()) + [
            steering.STEER_QUEUED_NOTE, steering.STEER_FULL_REFUSAL,
            steering.STEER_INTERRUPTED_REFUSAL,
            steering.STEER_CARD_NOT_DELIVERED_NOTE,
            steering.STEER_CARD_INTERRUPTED_NOTE]:
        assert "bob" not in text.lower()


def test_the_dedupe_digest():
    a = steering.dedupe_digest("dev-1", "token-aaaaaaaa")
    assert len(a) == 32 and int(a, 16) >= 0
    assert a == steering.dedupe_digest("dev-1", "token-aaaaaaaa")
    assert a != steering.dedupe_digest("dev-2", "token-aaaaaaaa")
    assert a != steering.dedupe_digest("dev-1", "token-bbbbbbbb")
    assert "token-aaaaaaaa" not in a
    assert steering.dedupe_digest("", "token-aaaaaaaa") == ""
    assert steering.dedupe_digest("dev-1", "") == ""
    assert steering.dedupe_digest(None, None) == ""


def test_the_caller_defaults_to_the_mac_and_is_restored():
    assert steering.CALLER.get() == steering.Caller("mac", "")
    with steering.calling(steering.Caller("phone", "abc")):
        assert steering.CALLER.get().source == "phone"
    assert steering.CALLER.get() == steering.Caller("mac", "")


def test_the_caller_is_restored_when_the_block_raises():
    with pytest.raises(RuntimeError):
        with steering.calling(steering.Caller("phone", "abc")):
            raise RuntimeError("boom")
    assert steering.CALLER.get().source == "mac"


@pytest.mark.parametrize("kind", ["reply", "card_message", "card_ask",
                                  "terminal_line", "wrap_up", "low_priority"])
@pytest.mark.parametrize("age,state,verdict", [
    (60, "none", "replay"),
    (120, "none", "replay"),
    (180, "none", "not_delivered"),
    (-5, "none", "not_delivered"),
    (60, "open", "interrupted"),
    (180, "interrupted", "interrupted"),
    (60, "finished:typed", "finished"),
    (180, "finished:refused", "finished"),
])
def test_the_verdict_for_a_row_the_queue_journals(kind, age, state, verdict):
    row = {"kind": kind, "created_at": 1000.0}
    assert steering.replay_verdict(row, state, 1000.0 + age) == verdict


@pytest.mark.parametrize("age", [0, 60, 500])
def test_an_answer_is_never_replayed_whatever_its_age(age):
    row = {"kind": "answer", "created_at": 1000.0}
    assert steering.replay_verdict(row, "none", 1000.0 + age) == "not_delivered"


@pytest.mark.parametrize("state", ["open", "interrupted", "finished:typed",
                                   "own"])
def test_an_answer_with_its_own_action_belongs_to_that_action(state):
    row = {"kind": "answer", "created_at": 1000.0}
    assert steering.replay_verdict(row, state, 1010.0) == "own"


def test_an_unknown_kind_is_never_replayed():
    row = {"kind": "from_a_newer_build", "created_at": 1000.0}
    assert steering.replay_verdict(row, "none", 1001.0) == "not_delivered"


def test_the_typing_sites_are_named():
    assert "_type_answer_burst" in steering.TYPING_SITES
    assert "_message_card_now" in steering.TYPING_SITES


# --- the lanes -------------------------------------------------------------------

def _lanes(log, delay=0.01):
    async def run(entry):
        log.append(("start", entry.payload.get("n")))
        await asyncio.sleep(delay)
        log.append(("end", entry.payload.get("n")))
        return True, str(entry.payload.get("n"))
    return Lanes(run)


def _e(n, kind="reply", **kw):
    return Entry(kind=kind, session_id="s", payload={"n": n}, **kw)


@pytest.mark.asyncio
async def test_one_key_runs_strictly_in_submit_order():
    log = []
    lanes = _lanes(log)
    futures = [lanes.submit("k", _e(n)) for n in (1, 2, 3)]
    answers = [await f for f in futures]
    assert answers == [(True, "1"), (True, "2"), (True, "3")]
    assert log == [("start", 1), ("end", 1), ("start", 2), ("end", 2),
                   ("start", 3), ("end", 3)]


@pytest.mark.asyncio
async def test_two_keys_run_at_the_same_time():
    log = []
    lanes = _lanes(log, delay=0.05)
    a = lanes.submit("a", _e("a"))
    b = lanes.submit("b", _e("b"))
    await asyncio.gather(a, b)
    assert log[:2] == [("start", "a"), ("start", "b")]


@pytest.mark.asyncio
async def test_the_cap_refuses_the_ninth():
    gate = asyncio.Event()

    async def run(entry):
        await gate.wait()
        return True, ""

    lanes = Lanes(run)
    futures = [lanes.submit("k", _e(n)) for n in range(8)]
    assert all(f is not None for f in futures)
    assert lanes.full("k")
    assert lanes.submit("k", _e(9)) is None
    assert lanes.submit("other", _e(9)) is not None
    gate.set()
    await asyncio.gather(*[f for f in futures if f])
    await lanes.drained()
    assert lanes.pending("k") == 0


@pytest.mark.asyncio
async def test_a_forced_submit_passes_the_cap():
    gate = asyncio.Event()

    async def run(entry):
        await gate.wait()
        return True, ""

    lanes = Lanes(run)
    for n in range(8):
        lanes.submit("k", _e(n))
    assert lanes.submit("k", _e(99), force=True) is not None
    gate.set()
    await lanes.drained()


@pytest.mark.asyncio
async def test_a_reserved_place_holds_the_order_until_released():
    log = []
    lanes = _lanes(log)
    first = _e(1)
    second = _e(2)
    f1 = lanes.reserve("k", first)
    f2 = lanes.submit("k", second)
    await asyncio.sleep(0.05)
    assert log == []          # the second is ready but waits behind the first
    lanes.release(first)
    await asyncio.gather(f1, f2)
    assert [x for x in log if x[0] == "start"] == [("start", 1), ("start", 2)]


@pytest.mark.asyncio
async def test_a_withdrawn_place_lets_the_next_entry_run():
    log = []
    lanes = _lanes(log)
    first = _e(1)
    lanes.reserve("k", first)
    f2 = lanes.submit("k", _e(2))
    await asyncio.sleep(0.01)
    lanes.withdraw(first)
    assert await first.future == (False, steering.STEER_FAILED_REFUSAL)
    assert await f2 == (True, "2")
    assert log == [("start", 2), ("end", 2)]


@pytest.mark.asyncio
async def test_a_worker_exits_when_the_lane_is_empty():
    lanes = _lanes([])
    await lanes.submit("k", _e(1))
    await lanes.drained()
    assert lanes._workers == {} and lanes._queues == {}
    assert not lanes.busy("k")


@pytest.mark.asyncio
async def test_offer_raw_on_an_idle_lane_writes_inline():
    lanes = _lanes([])
    calls = []

    def write():
        calls.append("w")
        return False, "the pty is gone"

    assert lanes.offer_raw("k", write) == (False, "the pty is gone")
    assert calls == ["w"]


@pytest.mark.asyncio
async def test_offer_raw_on_a_busy_lane_runs_after_the_busy_entry():
    log = []
    lanes = _lanes(log, delay=0.05)
    f = lanes.submit("k", _e(1))
    answer = lanes.offer_raw("k", lambda: log.append("raw") or (True, ""))
    assert answer == (True, "")
    assert log == []        # not written inline
    await f
    await lanes.drained()
    assert log == [("start", 1), ("end", 1), "raw"]


@pytest.mark.asyncio
async def test_raw_keys_keep_their_order_behind_a_busy_lane():
    log = []
    lanes = _lanes(log, delay=0.03)
    f = lanes.submit("k", _e(1))
    for n in range(3):
        lanes.offer_raw("k", lambda n=n: log.append(("raw", n)) or (True, ""))
    await f
    await lanes.drained()
    assert log[-3:] == [("raw", 0), ("raw", 1), ("raw", 2)]


@pytest.mark.asyncio
async def test_hold_until_delays_the_first_entry_and_later_submits_queue_behind():
    log = []
    lanes = _lanes(log, delay=0)
    lanes.hold_until("k", asyncio.get_running_loop().time() + 0.0)
    import time
    lanes.hold_until("k", time.monotonic() + 0.15)
    f1 = lanes.submit("k", _e(1))
    f2 = lanes.submit("k", _e(2))
    await asyncio.sleep(0.05)
    assert log == []
    assert lanes.busy("k")
    await asyncio.gather(f1, f2)
    assert [x for x in log if x[0] == "start"] == [("start", 1), ("start", 2)]


@pytest.mark.asyncio
async def test_a_body_that_raises_resolves_failed_and_the_next_still_runs():
    async def run(entry):
        if entry.payload["n"] == 1:
            raise RuntimeError("boom")
        return True, "ok"

    lanes = Lanes(run)
    f1 = lanes.submit("k", _e(1))
    f2 = lanes.submit("k", _e(2))
    assert await f1 == (False, steering.STEER_FAILED_REFUSAL)
    assert await f2 == (True, "ok")


@pytest.mark.asyncio
async def test_running_key_is_set_only_inside_the_worker():
    seen = []

    async def run(entry):
        seen.append(lanes.running_key())
        return True, ""

    lanes = Lanes(run)
    assert lanes.running_key() == ""
    await lanes.submit("k", _e(1))
    assert seen == ["k"]
    assert lanes.running_key() == ""


@pytest.mark.asyncio
async def test_running_key_of_another_lanes_object_is_not_ours():
    seen = []
    other = Lanes()

    async def run(entry):
        seen.append(other.running_key())
        return True, ""

    lanes = Lanes(run)
    await lanes.submit("k", _e(1))
    assert seen == [""]


@pytest.mark.asyncio
async def test_stop_lets_the_running_entry_finish_inside_the_grace():
    log = []
    lanes = _lanes(log, delay=0.05)
    f1 = lanes.submit("k", _e(1))
    f2 = lanes.submit("k", _e(2))
    await asyncio.sleep(0.01)
    await lanes.stop(1.0)
    assert await f1 == (True, "1")
    assert await f2 == (False, steering.STEER_SHUTDOWN_REFUSAL)
    assert ("start", 2) not in log


@pytest.mark.asyncio
async def test_stop_cancels_the_running_entry_after_the_grace():
    started = asyncio.Event()

    async def run(entry):
        started.set()
        await asyncio.sleep(30)
        return True, ""

    lanes = Lanes(run)
    f = lanes.submit("k", _e(1))
    await started.wait()
    await lanes.stop(0.05)
    # Cut off mid-entry: part of its words may have landed.
    assert await f == (False, steering.STEER_INTERRUPTED_REFUSAL)
    assert lanes._workers == {}


@pytest.mark.asyncio
async def test_stop_ends_a_held_lane_at_once():
    import time
    lanes = _lanes([])
    lanes.hold_until("k", time.monotonic() + 60)
    f = lanes.submit("k", _e(1))
    await lanes.stop(0.05)
    assert await f == (False, steering.STEER_SHUTDOWN_REFUSAL)


@pytest.mark.asyncio
async def test_a_stopped_lanes_object_takes_nothing_new():
    lanes = _lanes([])
    await lanes.stop(0.01)
    assert lanes.submit("k", _e(1)) is None
    assert lanes.offer_raw("k", lambda: (True, "")) == (True, "")


# --- the replay twin ----------------------------------------------------------

@pytest.mark.asyncio
async def test_a_twin_is_found_only_for_a_replayed_mac_entry_with_the_same_words():
    import time
    gate = asyncio.Event()

    async def run(entry):
        await gate.wait()
        return True, ""

    lanes = Lanes(run)
    now = time.time()
    lanes.submit("k", Entry(kind="reply", session_id="s",
                            payload={"text": "yes"}, created_at=now - 30,
                            source="mac", replayed=True, id="r1"))
    assert lanes.find_replay_twin("k", "reply", {"text": " yes "}, "mac",
                                  now) is not None
    assert lanes.find_replay_twin("k", "reply", {"text": "no"}, "mac",
                                  now) is None
    assert lanes.find_replay_twin("k", "card_message", {"text": "yes"}, "mac",
                                  now) is None
    assert lanes.find_replay_twin("k", "reply", {"text": "yes"}, "phone",
                                  now) is None
    assert lanes.find_replay_twin("elsewhere", "reply", {"text": "yes"},
                                  "mac", now) is None
    gate.set()
    await lanes.drained()


@pytest.mark.asyncio
async def test_two_live_presses_are_never_twins():
    import time
    gate = asyncio.Event()

    async def run(entry):
        await gate.wait()
        return True, ""

    lanes = Lanes(run)
    now = time.time()
    lanes.submit("k", Entry(kind="reply", session_id="s",
                            payload={"text": "yes"}, created_at=now))
    assert lanes.find_replay_twin("k", "reply", {"text": "yes"}, "mac",
                                  now) is None
    gate.set()
    await lanes.drained()


@pytest.mark.asyncio
async def test_a_replayed_twin_past_the_fresh_window_is_not_matched():
    import time
    lanes = _lanes([], delay=0)
    now = time.time()
    f = lanes.submit("k", Entry(kind="reply", session_id="s",
                                payload={"text": "yes", "n": 1},
                                created_at=now - 500, replayed=True))
    await f
    assert lanes.find_replay_twin("k", "reply", {"text": "yes"}, "mac",
                                  now) is None


@pytest.mark.asyncio
async def test_a_finished_replayed_entry_is_still_a_twin_for_its_window():
    import time
    lanes = _lanes([], delay=0)
    now = time.time()
    f = lanes.submit("k", Entry(kind="reply", session_id="s",
                                payload={"text": "yes", "n": 1},
                                created_at=now - 10, replayed=True))
    await f
    twin = lanes.find_replay_twin("k", "reply", {"text": "yes"}, "mac", now)
    assert twin is not None and twin.future.done()


@pytest.mark.asyncio
async def test_stop_tells_a_recorded_queued_entry_it_will_be_typed_after_the_restart():
    started = asyncio.Event()

    async def run(entry):
        started.set()
        await asyncio.sleep(30)
        return True, ""

    lanes = Lanes(run)
    lanes.submit("k", _e(1))
    recorded = _e(2, recorded=True)
    memory = _e(3)
    f2 = lanes.submit("k", recorded)
    f3 = lanes.submit("k", memory)
    await started.wait()
    await lanes.stop(0.01)
    assert await f2 == (False, steering.STEER_RESTARTING_NOTE)
    assert "nothing was typed" not in steering.STEER_RESTARTING_NOTE
    assert await f3 == (False, steering.STEER_SHUTDOWN_REFUSAL)


@pytest.mark.asyncio
async def test_raw_keys_do_not_count_toward_the_lane_cap():
    gate = asyncio.Event()

    async def run(entry):
        await gate.wait()
        return True, ""

    lanes = Lanes(run)
    lanes.submit("k", _e(0))
    for _ in range(20):
        assert lanes.offer_raw("k", lambda: (True, "")) == (True, "")
    assert lanes.pending("k") == 1 and not lanes.full("k")
    futures = [lanes.submit("k", _e(n)) for n in range(1, 8)]
    assert all(f is not None for f in futures)
    assert lanes.submit("k", _e(9)) is None
    gate.set()
    await lanes.drained()
