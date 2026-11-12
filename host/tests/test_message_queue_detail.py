"""A session's waiting and not-delivered messages ride its agent row as
`message_queue` (`docs/steering-queue.md`, *What the agent detail shows*).

The pure composition first, then the daemon wiring on the steering tests'
`World`, then restart."""
import asyncio
import time

import pytest

from dark_army_daemon import daemon as dm
from dark_army_daemon import steering
from tests.test_steering_daemon import World
from tests.test_steering_recovery import _age, _rows

ITEM_KEYS = {"group", "label", "status", "excerpt", "why"}
TOP_KEYS = {"headline", "items", "more_line"}
FORBIDDEN = {"id", "action_id", "dedupe", "command_token", "token", "payload",
             "text", "source", "created_at", "session_id"}


@pytest.fixture
def utc(monkeypatch):
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def _item(**kw):
    base = dict(kind="reply", source="phone", created_at=43200.0,
                group="waiting", state="waiting", text="hi")
    base.update(kw)
    return steering.detail_item(**base)


# --- the pure rules ----------------------------------------------------------

def test_excerpt_collapses_whitespace_and_cuts_at_seventy_nine_plus_ellipsis():
    assert steering.excerpt("a\n\tb   c") == "a b c"
    long = "x" * 120
    cut = steering.excerpt(long)
    assert cut == "x" * 79 + "…" and len(cut) == 80
    assert steering.excerpt("y" * 80) == "y" * 80
    assert steering.excerpt(None) == ""
    assert steering.excerpt(5) == ""


def test_detail_item_has_exactly_the_five_keys(utc):
    assert set(_item()) == ITEM_KEYS


def test_the_label_names_the_noun_the_source_and_the_time(utc):
    assert _item()["label"] == "A message from the phone, 12:00"
    assert _item(source="mac")["label"] == "A message from the Mac, 12:00"
    assert _item(kind="autocompact", source="mac")["label"] \
        == "An auto-compact from Dark Army, 12:00"


def test_refused_and_failed_both_read_not_delivered(utc):
    assert _item(state="refused")["status"] == "not delivered"
    assert _item(state="failed")["status"] == "not delivered"
    assert _item(state="typing")["status"] == "being typed now"
    assert _item(state="waiting")["status"] == "waiting its turn"
    assert "check the terminal" in _item(state="interrupted")["status"]


@pytest.mark.parametrize("kind", ["terminal_line", "answer", "wrap_up",
                                  "low_priority", "autocompact", "raw",
                                  "permission_key"])
def test_only_three_kinds_ever_carry_words(kind, utc):
    assert _item(kind=kind, text="my password")["excerpt"] == ""


@pytest.mark.parametrize("kind", sorted(steering.EXCERPT_KINDS))
def test_the_three_message_kinds_carry_an_excerpt(kind, utc):
    assert _item(kind=kind, text="hello there")["excerpt"] == "hello there"


def test_the_reason_is_one_clamped_line(utc):
    why = _item(why="a\nb " + "z" * 400)["why"]
    assert "\n" not in why and len(why) == steering.DETAIL_WHY_CHARS


def test_compose_detail_is_none_for_nothing():
    assert steering.compose_detail([], []) is None


def test_the_headlines():
    w, u = [_item()], [_item(group="undelivered", state="refused")]
    assert steering.compose_detail(w, [])["headline"] == "1 waiting to be typed"
    assert steering.compose_detail([], u * 2)["headline"] == "2 not delivered"
    both = steering.compose_detail(w, u)
    assert both["headline"] == "1 waiting to be typed · 1 not delivered"
    assert set(both) == TOP_KEYS


def test_waiting_comes_before_undelivered_in_the_given_order():
    w = [_item(text="a"), _item(text="b")]
    u = [_item(group="undelivered", text="newest"),
         _item(group="undelivered", text="older")]
    items = steering.compose_detail(w, u)["items"]
    assert [i["excerpt"] for i in items] == ["a", "b", "newest", "older"]


def test_nine_items_give_six_and_a_more_line():
    out = steering.compose_detail([_item()] * 9, [])
    assert len(out["items"]) == 6 and out["more_line"] == "and 3 more"
    assert steering.compose_detail([_item()], [])["more_line"] == ""


def test_a_lone_entry_is_not_listed_but_two_are_and_a_held_one_is():
    def view(kind="reply", running=False):
        return steering.EntryView(kind, "mac", "s1", 1.0, running, "x")
    assert steering.listable([view(running=True)], False) == []
    assert len(steering.listable([view(running=True), view()], False)) == 2
    assert len(steering.listable([view()], True)) == 1
    assert steering.listable([view("raw"), view()], False) == []


# --- the daemon --------------------------------------------------------------

@pytest.fixture
def world(monkeypatch, tmp_path):
    w = World(monkeypatch, tmp_path)
    w.pushes = 0
    w.d._schedule_agents_push = lambda: setattr(w, "pushes", w.pushes + 1)
    yield w
    w.close()


def _row(w, sid="s1"):
    for stub in w.d._collect_agent_stubs():
        if stub.get("session_id") == sid:
            return stub
    raise AssertionError("no row for " + sid)


def _walk(value, found):
    if isinstance(value, dict):
        for k, v in value.items():
            found.append(k)
            _walk(v, found)
    elif isinstance(value, list):
        for v in value:
            _walk(v, found)
    elif isinstance(value, str):
        assert len(value) <= 200
    return found


@pytest.mark.asyncio
async def test_a_waiting_and_a_refused_message_ride_the_row(world):
    d = world.d
    samples = []

    async def sample():
        await asyncio.sleep(0.02)
        samples.append(_row(world).get("message_queue"))

    first = asyncio.ensure_future(d.reply_to_session("s1", "first words"))
    second = asyncio.ensure_future(d.reply_to_session("s1", "second words"))
    await sample()
    assert await asyncio.gather(first, second) == [(True, ""), (True, "")]
    mid = samples[0]
    assert mid is not None
    assert [i["status"] for i in mid["items"]] == [
        "being typed now", "waiting its turn"]
    assert mid["headline"] == "2 waiting to be typed"
    assert mid["items"][0]["excerpt"] == "first words"
    await d._steering_lanes().drained()
    assert "message_queue" not in _row(world)

    ok, why = await d.reply_to_session("s1", "first words\nsecond line")
    assert ok is False
    row = _row(world)["message_queue"]
    (item,) = row["items"]
    assert item["group"] == "undelivered" and item["why"] == why
    assert item["status"] == "not delivered"
    assert item["excerpt"] == "first words second line"
    assert row["headline"] == "1 not delivered"


@pytest.mark.asyncio
async def test_a_lone_reply_never_appears_and_buys_no_push(world):
    d = world.d
    seen = []
    orig = dm.session_io.send_text

    async def spy(*a, **kw):
        seen.append("message_queue" in _row(world))
        return await orig(*a, **kw)

    world.d  # keep the fixture alive
    import dark_army_daemon.session_io as sio
    sio.send_text = spy
    try:
        assert (await d.reply_to_session("s1", "just one"))[0] is True
    finally:
        sio.send_text = orig
    assert seen == [False]
    assert "message_queue" not in _row(world)
    assert world.pushes == 0


@pytest.mark.asyncio
async def test_raw_keys_behind_a_busy_line_are_not_listed_and_buy_no_push(world):
    d = world.d
    release = asyncio.Event()

    async def slow():
        await release.wait()
        return True, ""

    task = asyncio.ensure_future(d._steer("reply", "s1", {"text": "a"},
                                          work=slow))
    await asyncio.sleep(0.01)
    before = world.pushes
    for _ in range(5):
        assert d._steer_raw("sid:s1", lambda: (True, "")) == (True, "")
    assert "message_queue" not in _row(world)
    assert world.pushes == before
    release.set()
    await task
    await d._steering_lanes().drained()


@pytest.mark.asyncio
async def test_a_queued_terminal_line_lists_without_its_words(world):
    d = world.d
    release = asyncio.Event()

    async def slow():
        await release.wait()
        return True, ""

    first = asyncio.ensure_future(d._steer("reply", "s1", {"text": "a"},
                                           work=slow))
    second = asyncio.ensure_future(d._steer(
        "terminal_line", "s1", {"text": "hunter2"}, work=slow))
    await asyncio.sleep(0.01)
    mq = _row(world)["message_queue"]
    assert [i["status"] for i in mq["items"]] == [
        "being typed now", "waiting its turn"]
    assert mq["items"][1]["excerpt"] == "" and "hunter2" not in str(mq)
    assert mq["items"][1]["label"].startswith("A line from the Mac")
    release.set()
    await asyncio.gather(first, second)
    await d._steering_lanes().drained()


def test_an_undelivered_item_expires_and_the_memory_is_bounded(world):
    d = world.d
    now = 1_000_000.0
    d._steer_note_undelivered(session_id="s1", kind="reply", source="mac",
                              created_at=now, state="refused", text="old",
                              why="no", now=now)
    assert d._message_queue_details([{"session_id": "s1"}], now=now + 10)
    assert d._message_queue_details(
        [{"session_id": "s1"}],
        now=now + steering.UNDELIVERED_SECONDS + 1) == {}
    assert d.__dict__["_steer_undelivered"] == {}

    for i in range(9):
        d._steer_note_undelivered(
            session_id="s1", kind="reply", source="mac", created_at=now,
            state="refused", text=f"m{i}", why="no", now=now + i)
    assert len(d.__dict__["_steer_undelivered"]["s1"]) == 8
    out = d._message_queue_details([{"session_id": "s1"}], now=now + 10)
    assert [i["excerpt"] for i in out["s1"]["items"]][0] == "m8"

    for n in range(65):
        d._steer_note_undelivered(
            session_id=f"x{n}", kind="reply", source="mac", created_at=now,
            state="refused", text="t", why="no", now=now + 100 + n)
    assert len(d.__dict__["_steer_undelivered"]) \
        == steering.UNDELIVERED_MAX_SESSIONS
    assert "x0" not in d.__dict__["_steer_undelivered"]


def test_hidden_kinds_record_nothing(world):
    for kind in ("raw", "permission_key"):
        world.d._steer_note_undelivered(
            session_id="s1", kind=kind, source="mac", created_at=1.0,
            state="refused", text="", why="no")
    assert not world.d.__dict__.get("_steer_undelivered")


def test_the_tombstone_row_keeps_the_undelivered_item(world):
    d = world.d
    d._steer_note_undelivered(session_id="s1", kind="reply", source="phone",
                              created_at=time.time(), state="refused",
                              text="gone", why="no")
    d._forget_session("s1", "evicted")
    rows = [s for s in d._collect_agent_stubs() if s.get("session_id") == "s1"]
    assert rows and rows[0]["message_queue"]["items"][0]["excerpt"] == "gone"


@pytest.mark.asyncio
async def test_a_pty_lane_lists_on_the_row_whose_lane_key_matches(world):
    d = world.d
    d._session_states["s2"] = {"pid": 4243, "last_event": 0, "state": "idle",
                               "provider": "claude"}
    d._pty_handle_for = lambda sid: "h1" if sid in ("s1", "s2") else None
    release = asyncio.Event()

    async def slow():
        await release.wait()
        return True, ""

    t1 = asyncio.ensure_future(d._steer("reply", "s1", {"text": "a"}, work=slow))
    t2 = asyncio.ensure_future(d._steer("reply", "s1", {"text": "b"}, work=slow))
    await asyncio.sleep(0.01)
    assert d._steer_lane_key("s2") == "pty:h1"
    stubs = [{"session_id": "s1"}, {"session_id": "s2"}, {"session_id": "s3"}]
    out = d._message_queue_details(stubs)
    assert set(out) == {"s1", "s2"}
    release.set()
    await asyncio.gather(t1, t2)
    await d._steering_lanes().drained()


def test_a_raising_composition_costs_the_field_never_the_fleet(world,
                                                               monkeypatch):
    def boom(stubs):
        raise RuntimeError("no")

    monkeypatch.setattr(world.d, "_message_queue_details", boom)
    stubs = world.d._collect_agent_stubs()
    assert any(s.get("session_id") == "s1" for s in stubs)
    assert all("message_queue" not in s for s in stubs)


@pytest.mark.asyncio
async def test_a_raising_on_change_never_stops_the_lane():
    typed = []

    async def run(entry):
        typed.append(entry.payload["text"])
        return True, ""

    def boom():
        raise RuntimeError("callback")

    lanes = steering.Lanes(run, on_change=boom)
    futures = [lanes.submit("sid:s1", steering.Entry(
        kind="reply", session_id="s1", payload={"text": t}))
        for t in ("one", "two")]
    assert [await f for f in futures] == [(True, ""), (True, "")]
    assert typed == ["one", "two"]


@pytest.mark.asyncio
async def test_the_privacy_walk_finds_no_id_digest_or_text_key(world, tmp_path):
    d = world.d
    release = asyncio.Event()

    async def slow():
        await release.wait()
        return True, ""

    with steering.calling(steering.Caller("phone", "digestdigest")):
        t1 = asyncio.ensure_future(d._steer(
            "reply", "s1", {"text": "a" * 300}, work=slow))
        t2 = asyncio.ensure_future(d._steer(
            "terminal_line", "s1", {"text": "secret"}, work=slow))
    await asyncio.sleep(0.01)
    await d.reply_to_session("s1", "x\ny")
    mq = _row(world)["message_queue"]
    keys = _walk(mq, [])
    assert not FORBIDDEN & set(keys)
    assert "digestdigest" not in str(mq) and "secret" not in str(mq)
    for item in mq["items"]:
        assert set(item) == ITEM_KEYS
    assert set(mq) == TOP_KEYS
    release.set()
    await asyncio.gather(t1, t2)
    await d._steering_lanes().drained()


@pytest.mark.asyncio
async def test_the_pushed_snapshot_carries_the_same_field(world):
    d = world.d
    await d.reply_to_session("s1", "one\ntwo")
    class Observer:
        snapshots = []

        def on_agents_change(self, snapshot):
            self.snapshots.append(snapshot)

    obs = Observer()
    d._observers.append(obs)
    await d._push_agents_snapshot()
    assert obs.snapshots
    rows = [r for bucket in obs.snapshots[-1].values()
            if isinstance(bucket, list) for r in bucket
            if isinstance(r, dict)]
    row = next(r for r in rows if r.get("session_id") == "s1")
    assert row["message_queue"] == _row(world)["message_queue"]


@pytest.mark.asyncio
async def test_a_refusal_buys_a_push_and_a_quiet_list_buys_none(world):
    d = world.d
    before = world.pushes
    await d.reply_to_session("s1", "one\ntwo")
    assert world.pushes > before
    settled = world.pushes
    d._message_queue_changed()
    assert world.pushes == settled


# --- restart -----------------------------------------------------------------

@pytest.fixture
def relaunch(monkeypatch, tmp_path):
    monkeypatch.setattr(steering, "STEER_REPLAY_SETTLE_SECONDS", 0)
    made = []

    def build(seed):
        dead = World(monkeypatch, tmp_path)
        seed(dead)
        dead.close()
        w = World(monkeypatch, tmp_path, delay=0)
        w.d._schedule_agents_push = lambda: None
        w.d._log_event = lambda kind, **f: None
        made.append(w)
        return w

    yield build
    for w in made:
        w.close()


async def _recover(w):
    await w.d.recover_actions()
    await w.d.recover_steering()
    await w.d._steering_lanes().drained()


@pytest.mark.asyncio
async def test_a_stale_queued_reply_is_listed_with_its_excerpt_and_blanked(
        relaunch):
    w = relaunch(lambda dead: _rows(dead, created_at=_age(180)))
    await _recover(w)
    (item,) = _row(w)["message_queue"]["items"]
    assert item["group"] == "undelivered"
    assert item["excerpt"] == "hello"
    assert item["why"] == steering.RESTART_WHY
    assert w.store._conn.execute(
        "SELECT payload FROM steer_queue").fetchone()[0] == ""


@pytest.mark.asyncio
async def test_an_open_intent_lists_as_possibly_partly_landed(relaunch):
    def seed(dead):
        aid = dead.store.journal_begin("steer", "s1")
        row = _rows(dead, created_at=_age(10), action_id=aid)
        dead.store.journal_intent(aid, "steer", "s1", "type",
                                  {"entry_id": row["id"]})

    w = relaunch(seed)
    await _recover(w)
    (item,) = _row(w)["message_queue"]["items"]
    assert item["status"] == steering.STATUS_WORDS["interrupted"]
    assert item["why"] == ""


@pytest.mark.asyncio
async def test_a_row_resolved_finished_leaves_nothing(relaunch):
    def seed(dead):
        aid = dead.store.journal_begin("steer", "s1")
        row = _rows(dead, created_at=_age(10), action_id=aid)
        intent = dead.store.journal_intent(aid, "steer", "s1", "type",
                                           {"entry_id": row["id"]})
        dead.store.journal_result(intent, "typed", "")

    w = relaunch(seed)
    await _recover(w)
    assert "message_queue" not in _row(w)
