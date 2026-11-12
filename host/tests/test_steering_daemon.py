"""Every typing verb through its session's lane (`docs/steering-queue.md`)."""
import asyncio
from types import SimpleNamespace

import pytest

from dark_army_daemon import daemon as dm
from dark_army_daemon import session_io, steering
from dark_army_daemon.board import BoardStore
from dark_army_daemon.daemon import BobDaemon

LANDED = {"matched": True, "sent": True, "terminalName": "zsh"}


class World:
    def __init__(self, monkeypatch, tmp_path=None, board=True, delay=0.05):
        self.events = []
        self.sent = []
        self.delay = delay
        self.result = LANDED
        self.d = BobDaemon(sessions_path=(tmp_path / "s.json") if tmp_path
                           else None, headless=True)
        self.store = None
        if board and tmp_path is not None:
            self.store = BoardStore(tmp_path / "board.db")
            self.store.connect()
            self.d._board = self.store
        d = self.d
        d.typed_reply_enabled = True
        d._session_states["s1"] = {"pid": 4242, "last_event": 0,
                                   "state": "idle", "provider": "claude"}
        monkeypatch.setattr(d, "_reconciled_categories",
                            lambda: {"s1": "waiting"})
        monkeypatch.setattr(d, "_prompts_by_session", lambda: {})
        monkeypatch.setattr(d, "_channel_for_session", lambda sid: None)
        monkeypatch.setattr(session_io, "can_send_text",
                            lambda pid, tty="": True)
        world = self

        async def send(pid, tty, text, newline=True):
            world.events.append(("begin", text))
            await asyncio.sleep(world.delay)
            world.events.append(("end", text))
            world.sent.append((pid, text, newline))
            return world.result

        monkeypatch.setattr(session_io, "send_text", send)

    def card(self, text="do it"):
        card, _ = self.store.create({
            "title": "t", "project": "bob", "root": "/tmp", "prompt": "go",
            "tool": "claude", "summary": "s"})
        self.store.update(card["id"], {"session_id": "s1",
                                       "link_state": "live"})
        return card["id"]

    def close(self):
        if self.store:
            self.store.close()


@pytest.fixture
def world(monkeypatch, tmp_path):
    w = World(monkeypatch, tmp_path)
    yield w
    w.close()


def _whole(events):
    """No begin lands between another message's begin and end."""
    depth = 0
    for kind, _text in events:
        depth += 1 if kind == "begin" else -1
        assert 0 <= depth <= 1, events
    return True


@pytest.mark.asyncio
async def test_two_sources_never_interleave_and_keep_order(world):
    cid = world.card()

    async def from_phone():
        with steering.calling(steering.Caller("phone", "")):
            return await world.d.reply_to_session("s1", "from the phone")

    results = await asyncio.gather(
        from_phone(), world.d.message_card(cid, "from the panel"))
    assert [r[0] for r in results] == [True, True]
    assert _whole(world.events)
    assert [t for _k, t in world.events if _k == "begin"] == [
        "\x15from the phone", "\x15from the panel"]


@pytest.mark.asyncio
async def test_three_replies_arrive_in_the_order_sent(world):
    out = await asyncio.gather(*[world.d.reply_to_session("s1", f"m{n}")
                                 for n in range(3)])
    assert all(ok for ok, _ in out)
    assert [t for _p, t, _n in world.sent] == ["\x15m0", "\x15m1", "\x15m2"]
    assert _whole(world.events)


@pytest.mark.asyncio
async def test_a_second_answer_waits_where_it_used_to_be_refused(world):
    d = world.d
    log = []

    async def batch(session_id, question_id, indexes):
        assert session_id not in d._answering
        d._answering.add(session_id)
        try:
            log.append("batch begin")
            await asyncio.sleep(0.05)
            log.append("batch end")
            return True, ""
        finally:
            d._answering.discard(session_id)

    async def single(session_id, question_id, index):
        if session_id in d._answering:
            return False, "Already typing an answer into that session"
        log.append("single")
        return True, ""

    d._answer_questions_now = batch
    d._answer_question_now = single
    first = asyncio.ensure_future(d.answer_questions("s1", "q", [0, 1, 2]))
    await asyncio.sleep(0.01)
    second = await d.answer_question("s1", "q", 1)
    assert await first == (True, "")
    assert second == (True, "")
    assert log == ["batch begin", "batch end", "single"]


@pytest.mark.asyncio
async def test_an_idle_lane_makes_exactly_todays_calls(world):
    world.delay = 0
    assert await world.d.reply_to_session("s1", "accept") == (True, "")
    assert world.sent == [(4242, "\x15accept", True)]


@pytest.mark.asyncio
async def test_an_idle_refusal_is_the_bodys_own_answer(world):
    world.delay = 0
    ok, detail = await world.d.reply_to_session("s1", "   ")
    assert (ok, detail) == (False, "Nothing to send.")
    assert world.sent == []


@pytest.mark.asyncio
async def test_an_auto_compact_lands_after_the_replys_enter(world):
    from dark_army_daemon import autocompact
    d = world.d
    d._autocompact = autocompact.AutoCompactPolicy()
    reply = asyncio.ensure_future(d.reply_to_session("s1", "words"))
    await asyncio.sleep(0.01)
    d._compact_queue = [("s1", 4242, "claude")]
    await d._flush_auto_compacts()
    await reply
    await d._steering_lanes().drained()
    assert [t for _p, t, _n in world.sent] == ["\x15words", "\x15/compact"]
    assert _whole(world.events)


@pytest.mark.asyncio
async def test_note_failed_fires_on_a_miss_and_not_on_success(world):
    d = world.d
    calls = []

    class Policy:
        def note_failed(self, sid):
            calls.append(sid)

    d._autocompact = Policy()
    world.delay = 0
    d._compact_queue = [("s1", 4242, "claude")]
    await d._flush_auto_compacts()
    await d._steering_lanes().drained()
    assert calls == []
    world.result = {"sent": False}
    d._compact_queue = [("s1", 4242, "claude")]
    await d._flush_auto_compacts()
    await d._steering_lanes().drained()
    assert calls == ["s1"]


@pytest.mark.asyncio
async def test_a_full_lane_hands_the_compact_back(world):
    d = world.d
    calls = []
    d._autocompact = SimpleNamespace(note_failed=calls.append)
    world.delay = 0.2
    lanes = d._steering_lanes()
    gate = asyncio.Event()

    async def hold():
        await gate.wait()
        return True, ""

    for _ in range(steering.STEER_MAX_PENDING):
        d._steer_nowait("permission_key", "s1", hold)
    d._compact_queue = [("s1", 4242, "claude")]
    await d._flush_auto_compacts()
    assert calls == ["s1"]
    gate.set()
    await lanes.drained()


def _pty_daemon(world, monkeypatch):
    d = world.d
    writes = []

    async def drain(handle, seconds):
        return True

    d._pty = SimpleNamespace(
        write=lambda handle, text="", data=None: writes.append(
            data if data is not None else text) or True,
        drain=drain, owns=lambda pid: None, get=lambda h: None,
        screen=lambda h: None)
    monkeypatch.setattr(d, "_pty_handle_for", lambda sid, row=None: "h1")
    monkeypatch.setattr(d, "_pty_exited", lambda handle: False)
    monkeypatch.setattr(session_io, "PTY_ENTER_GAP_SECONDS", 0.05)
    return writes


@pytest.mark.asyncio
async def test_raw_frames_during_a_queued_line_land_after_it(world, monkeypatch):
    d = world.d
    writes = _pty_daemon(world, monkeypatch)
    line = asyncio.ensure_future(d.terminal_input("s1", "hello"))
    await asyncio.sleep(0.02)
    assert d.terminal_stream_input("h1", b"x") == (True, "")
    assert d.terminal_stream_input("h1", b"y") == (True, "")
    assert writes == ["hello"]
    assert await line == (True, "")
    await d._steering_lanes().drained()
    assert writes == ["hello", "\r", b"x", b"y"]


@pytest.mark.asyncio
async def test_raw_frames_on_an_idle_terminal_are_written_at_once(world, monkeypatch):
    writes = _pty_daemon(world, monkeypatch)
    assert world.d.terminal_stream_input("h1", b"k") == (True, "")
    assert writes == [b"k"]


@pytest.mark.asyncio
async def test_each_durable_verb_leaves_one_resolved_row_with_no_words(world):
    world.delay = 0
    d = world.d
    assert await d.reply_to_session("s1", "secret words") == (True, "")
    counts = world.store.steer_counts()
    assert counts == {"typed": 1}
    row = world.store._conn.execute("SELECT * FROM steer_queue").fetchone()
    assert row["payload"] == "" and row["kind"] == "reply"
    assert row["source"] == "mac" and row["action_id"]
    intents = world.store.journal_recent("steer", "s1")
    assert len(intents) == 1
    step = intents[0]["steps"][0]
    assert step["step"] == "type" and step["result"]["outcome"] == "typed"
    assert "secret words" not in str(intents)
    assert step["payload"]["entry_id"] == row["id"]


@pytest.mark.asyncio
async def test_a_refusal_resolves_the_row_refused(world):
    world.delay = 0
    world.result = {"sent": False}
    ok, _ = await world.d.reply_to_session("s1", "x")
    assert ok is False
    assert world.store.steer_counts() == {"refused": 1}
    assert world.store.journal_recent("steer", "s1")[0]["steps"][0][
        "result"]["outcome"] == "refused"


@pytest.mark.asyncio
async def test_an_answer_leaves_a_row_but_no_steer_intent(world):
    world.delay = 0
    d = world.d

    async def single(sid, qid, index):
        return True, ""

    d._answer_question_now = single
    assert await d.answer_question("s1", "q", 0) == (True, "")
    assert world.store.steer_counts() == {"typed": 1}
    assert world.store.journal_recent("steer", "s1") == []


@pytest.mark.asyncio
async def test_memory_only_without_a_board(monkeypatch):
    w = World(monkeypatch, None, board=False, delay=0)
    assert w.d._board is None
    assert await w.d.reply_to_session("s1", "hi") == (True, "")
    assert await w.d.wrap_up_session("s1") == (True, "")
    assert [t for _p, t, _n in w.sent] == ["\x15hi", "\x15/clear"]


@pytest.mark.asyncio
async def test_a_slow_lane_answers_queued_and_still_types(world, monkeypatch):
    monkeypatch.setattr(steering, "STEER_REPLY_WAIT_SECONDS", 0.05)
    world.delay = 0.2
    first = asyncio.ensure_future(world.d.reply_to_session("s1", "one"))
    await asyncio.sleep(0.01)
    assert await world.d.reply_to_session("s1", "two") == (
        True, steering.STEER_QUEUED_NOTE)
    await first
    await world.d._steering_lanes().drained()
    assert [t for _p, t, _n in world.sent] == ["\x15one", "\x15two"]
    assert world.store.steer_counts() == {"typed": 2}


@pytest.mark.asyncio
async def test_cancelling_the_caller_does_not_cancel_the_typing(world):
    task = asyncio.ensure_future(world.d.reply_to_session("s1", "keep going"))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await world.d._steering_lanes().drained()
    assert world.sent == [(4242, "\x15keep going", True)]
    assert world.store.steer_counts() == {"typed": 1}


@pytest.mark.asyncio
async def test_wrap_up_or_close_does_not_deadlock(world):
    world.delay = 0
    d = world.d
    d.board_close_terminal_enabled = False
    assert await asyncio.wait_for(d.wrap_up_or_close_session("s1"), 5) == (
        True, "")
    assert [t for _p, t, _n in world.sent] == ["\x15/clear"]


@pytest.mark.asyncio
async def test_a_body_calling_a_verb_on_its_own_lane_runs_inline(world):
    d = world.d
    world.delay = 0

    async def nested():
        return await d._steer("reply", "s1", {"text": "inner"})

    ok = await asyncio.wait_for(
        d._steer("permission_key", "s1", {}, work=nested), 5)
    assert ok == (True, "")
    assert [t for _p, t, _n in world.sent] == ["\x15inner"]


@pytest.mark.asyncio
async def test_a_ninth_pending_entry_is_refused(world):
    d = world.d
    gate = asyncio.Event()

    async def hold():
        await gate.wait()
        return True, ""

    futures = [d._steer_nowait("permission_key", "s1", hold)
               for _ in range(steering.STEER_MAX_PENDING)]
    assert all(futures)
    assert await d.reply_to_session("s1", "late") == (
        False, steering.STEER_FULL_REFUSAL)
    assert world.store.steer_counts() == {}
    gate.set()
    await d._steering_lanes().drained()


@pytest.mark.asyncio
async def test_a_card_ask_whose_channel_has_gone_refuses_and_never_consults(
        world, monkeypatch):
    d = world.d
    cid = world.card()
    channel = {"name": "dark-army"}
    state = {"channel": channel}
    monkeypatch.setattr(d, "_channel_for_session",
                        lambda sid: state["channel"])

    async def forbidden(*a, **k):
        raise AssertionError("a replayed ask must never consult")

    d._ask_card_consult_locked = forbidden
    state["channel"] = None
    ok, detail = await d._steer("card_ask", "s1",
                                {"card_id": cid, "text": "why?"})
    assert (ok, detail) == (False, "That session is no longer listening.")
    assert world.store.steer_counts() == {"refused": 1}


@pytest.mark.asyncio
async def test_a_card_ask_pushes_in_the_lane_and_records(world, monkeypatch):
    d = world.d
    cid = world.card()
    pushed = []
    monkeypatch.setattr(d, "_channel_for_session",
                        lambda sid: {"name": "dark-army"})

    async def push(sid, text, extra=None):
        pushed.append((sid, extra))
        return True

    d.push_channel_event = push
    assert await d.ask_card(cid, "why?") == (True, "asked")
    assert pushed == [("s1", {"kind": "user"})]
    assert world.store.steer_counts() == {"typed": 1}


@pytest.mark.asyncio
async def test_a_card_with_no_live_session_queues_nothing(world):
    card, _ = world.store.create({"title": "t", "project": "bob",
                                  "root": "/tmp", "prompt": "go",
                                  "tool": "claude", "summary": "s"})
    ok, detail = await world.d.message_card(card["id"], "hello")
    assert ok is False and detail == dm.CARD_MESSAGE_NO_SESSION_REFUSAL
    assert world.store.steer_counts() == {}


@pytest.mark.asyncio
async def test_a_phone_resend_with_the_same_mark_types_once(world):
    world.delay = 0
    digest = steering.dedupe_digest("dev", "token-aaaaaaaa")
    with steering.calling(steering.Caller("phone", digest)):
        first = await world.d.reply_to_session("s1", "once")
        again = await world.d.reply_to_session("s1", "once")
    assert first == again == (True, "")
    assert len(world.sent) == 1
    row = world.store._conn.execute("SELECT * FROM steer_queue").fetchone()
    assert row["dedupe"] == digest and "token-aaaaaaaa" not in str(dict(row))


@pytest.mark.asyncio
async def test_a_resend_while_the_original_waits_joins_it(world):
    digest = steering.dedupe_digest("dev", "token-bbbbbbbb")

    async def press():
        with steering.calling(steering.Caller("phone", digest)):
            return await world.d.reply_to_session("s1", "once")

    a = asyncio.ensure_future(press())
    await asyncio.sleep(0.02)
    b = await press()
    assert await a == b == (True, "")
    assert len(world.sent) == 1


@pytest.mark.asyncio
async def test_a_store_that_cannot_be_written_still_types(world, monkeypatch):
    world.delay = 0

    def boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(world.store, "steer_enqueue", boom)
    assert await world.d.reply_to_session("s1", "still typed") == (True, "")
    assert len(world.sent) == 1


@pytest.mark.asyncio
async def test_the_grok_permission_key_rides_the_terminal_lane(world, monkeypatch):
    d = world.d
    order = []

    async def key(request_id, request, behavior, sid, handle, confirm=None):
        order.append("key")
        return True, ""

    d._grok_permission_key_now = key
    d._permission_requests["r1"] = {"via": "grok", "session_id": "s1"}
    monkeypatch.setattr(d, "_pty_handle_for", lambda sid, row=None: "h1")
    assert await d.answer_permission("r1", "allow") == (True, "")
    assert order == ["key"]


@pytest.mark.asyncio
async def test_shutdown_leaves_a_queued_row_on_disk(world):
    d = world.d
    world.delay = 0.3
    first = asyncio.ensure_future(d.reply_to_session("s1", "one"))
    await asyncio.sleep(0.02)
    second = asyncio.ensure_future(d.reply_to_session("s1", "two"))
    await asyncio.sleep(0.02)
    await d._stop_steering()
    await asyncio.gather(first, second, return_exceptions=True)
    states = world.store.steer_counts()
    assert states.get("queued", 0) >= 1


@pytest.mark.asyncio
async def test_the_grok_confirmation_poll_does_not_hold_the_lane(world, monkeypatch):
    from dark_army_daemon import grok_events, prompt_dialogs
    d = world.d
    writes = []

    async def drain(handle, seconds):
        return True

    d._pty = SimpleNamespace(
        write=lambda handle, text="", data=None: writes.append(
            data if data is not None else text) or True,
        drain=drain, screen=lambda h: None, owns=lambda pid: None,
        get=lambda h: None)
    monkeypatch.setattr(d, "_pty_handle_for", lambda sid, row=None: "h1")
    monkeypatch.setattr(d, "_pty_exited", lambda handle: False)
    monkeypatch.setattr(prompt_dialogs, "grok_choice", lambda text, b: "1")
    state = {"pending": True}
    monkeypatch.setattr(grok_events, "permission_pending",
                        lambda path: state["pending"])
    d._permission_requests["r1"] = {"via": "grok", "session_id": "s1",
                                    "events_path": "x"}
    answer = asyncio.ensure_future(d.answer_permission("r1", "allow"))
    await asyncio.sleep(0.1)
    assert writes == ["1"] and not answer.done()
    assert "s1" in d._answering        # the second press is still refused
    # The lane is free: raw keys for the terminal go straight through.
    assert d.terminal_stream_input("h1", b"y") == (True, "")
    assert writes == ["1", b"y"]
    state["pending"] = False
    assert await asyncio.wait_for(answer, 5) == (True, "")
    assert "s1" not in d._answering
    assert "r1" not in d._permission_requests
