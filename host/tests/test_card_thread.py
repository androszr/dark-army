# host/tests/test_card_thread.py
"""Ask a card a question: the direct channel, the consultant, bind/expiry,
answer attribution, and the consultant's inability to close.
"""

import time

import pytest

from dark_army_daemon import dispatch
from dark_army_daemon.board import BoardStore
from dark_army_daemon.daemon import BobDaemon


@pytest.fixture
def daemon(tmp_path):
    d = BobDaemon(sessions_path=tmp_path / "sessions.json")
    store = BoardStore(tmp_path / "board.db")
    store.connect()
    d._board = store
    try:
        yield d, store
    finally:
        store.close()


def _make(store, **kw):
    fields = {"title": "do the thing", "project": "bob", "root": "/tmp",
              "prompt": "go", "tool": "claude",
              "summary": "make the thing work"}
    fields.update(kw)
    card, detail = store.create(fields)
    assert card is not None, detail
    return card


def _arm_spawn(daemon_obj, monkeypatch, spawns=None):
    monkeypatch.setattr(daemon_obj, "_known_project_roots",
                        lambda: {"/private/tmp", "/tmp"})
    monkeypatch.setattr(dispatch, "resolve_executable",
                        lambda tool: "/bin/claude")

    async def accept(root, argv, name, **_kw):
        if spawns is not None:
            spawns.append({"root": root, "argv": argv, "name": name})
        return True, "bob", None

    monkeypatch.setattr(dispatch, "spawn", accept)


def _card_fields(store, card_id):
    got = store.get(card_id)
    return {k: got[k] for k in
            ("session_id", "link_state", "column_name", "dispatch_error")}


def _live_claude(d, store, **kw):
    card = _make(store, **kw)
    store.update(card["id"], {"session_id": "sess-live",
                              "link_state": "live"})
    d._session_states["sess-live"] = {"provider": "claude"}
    return store.get(card["id"])


@pytest.mark.asyncio
async def test_ask_on_a_live_claude_channel_pushes_and_stores(daemon,
                                                              monkeypatch):
    d, store = daemon
    card = _live_claude(d, store)
    monkeypatch.setattr(d, "_channel_for_session",
                        lambda sid: {"port": 1} if sid == "sess-live" else None)
    pushed = []

    async def push(session_id, content, meta=None):
        pushed.append({"session_id": session_id, "content": content,
                       "meta": meta})
        return True

    monkeypatch.setattr(d, "push_channel_event", push)
    ok, detail = await d.ask_card(card["id"], "did you take X into account?")
    assert ok, detail
    assert pushed[0]["session_id"] == "sess-live"
    assert pushed[0]["meta"] == {"kind": "user"}
    assert "did you take X into account?" in pushed[0]["content"]
    # A registry entry with no name is a channel from before the dual-name
    # window, so it was born under `bob` and has `bob_answer_card`.
    assert "bob_answer_card" in pushed[0]["content"]
    rows = store.messages(card["id"])
    assert len(rows) == 1
    assert rows[0]["kind"] == "question"
    assert rows[0]["author"] == "user"
    assert rows[0]["text"] == "did you take X into account?"


@pytest.mark.asyncio
async def test_ask_on_an_unbound_card_with_dispatch_off_refuses_without_storing(
        daemon, monkeypatch):
    d, store = daemon
    card = _make(store)
    d.board_dispatch_enabled = False

    def boom(*a, **k):
        raise AssertionError("spawn must not run with dispatch off")

    monkeypatch.setattr(dispatch, "spawn", boom)
    ok, detail = await d.ask_card(card["id"], "is Y implemented?")
    assert not ok
    assert "not allowed to start sessions" in detail
    assert store.messages(card["id"]) == []


@pytest.mark.asyncio
async def test_ask_spawns_a_consult_and_leaves_the_card_untouched(
        daemon, monkeypatch):
    d, store = daemon
    card = _make(store)
    before = _card_fields(store, card["id"])
    spawns = []
    _arm_spawn(d, monkeypatch, spawns)
    ok, detail = await d.ask_card(card["id"], "is Y implemented?")
    assert ok, detail
    assert spawns
    assert spawns[0]["root"] in ("/tmp", "/private/tmp")
    # A helper is a new session, born under the current channel name.
    assert "dark_army_answer_card" in spawns[0]["argv"][-1]
    assert "bob_answer_card" not in spawns[0]["argv"][-1]
    assert card["id"] in d._consults
    after = _card_fields(store, card["id"])
    assert after == before
    rows = store.messages(card["id"])
    assert [r["kind"] for r in rows] == ["question"]


@pytest.mark.asyncio
async def test_a_second_ask_while_a_consult_is_pending_is_refused(
        daemon, monkeypatch):
    d, store = daemon
    card = _make(store)
    _arm_spawn(d, monkeypatch)
    ok, detail = await d.ask_card(card["id"], "first")
    assert ok, detail
    ok, detail = await d.ask_card(card["id"], "second")
    assert not ok
    assert "already looking" in detail
    assert [r["text"] for r in store.messages(card["id"])] == ["first"]


@pytest.mark.asyncio
async def test_consult_and_dispatching_card_in_one_project_are_mutually_refused(
        daemon, monkeypatch):
    d, store = daemon
    starting = _make(store, title="starting", column_name="backlog")
    asking = _make(store, title="asking")
    _arm_spawn(d, monkeypatch)
    ok, detail = await d.dispatch_card(starting["id"], allow_unplanned=True)
    assert ok, detail
    ok, detail = await d.ask_card(asking["id"], "hello")
    assert not ok
    assert "another card in this project" in detail
    assert store.messages(asking["id"]) == []

    # The other direction: a pending consult blocks a Start in the same
    # project. Clear the dispatching card first so only the consult is
    # in flight.
    store.update(starting["id"], {"link_state": "live",
                                  "session_id": "already-bound"})
    other = _make(store, title="other ask")
    ok, detail = await d.ask_card(other["id"], "first helper")
    assert ok, detail
    starting2 = _make(store, title="start later", column_name="backlog")
    ok, detail = await d.dispatch_card(starting2["id"], allow_unplanned=True)
    assert not ok
    # The work queue absorbs the per-project bound as a transient, so the
    # card is queued rather than started — no second session in the project.
    assert store.get(starting2["id"])["link_state"] != "dispatching"


def test_bind_consults_binds_by_elimination_and_expiry_writes_the_note(daemon):
    d, store = daemon
    card = _make(store)
    now = time.time()
    d._consults[card["id"]] = {
        "card_id": card["id"], "project": "bob", "root": "/tmp",
        "tool": "claude", "baseline": {"already-running"},
        "started": now, "session_id": "",
    }
    snapshot = {"running": [
        {"session_id": "already-running", "provider": "claude",
         "kind": "interactive",
         "project": "bob", "cwd": "/tmp", "started_at": now + 1},
        {"session_id": "the-helper", "provider": "claude",
         "kind": "interactive",
         "project": "bob", "cwd": "/tmp", "started_at": now + 2},
    ]}
    assert d._bind_consults(snapshot, now + 3)
    assert d._consults[card["id"]]["session_id"] == "the-helper"
    got = store.get(card["id"])
    assert got["session_id"] == "" and got["link_state"] == ""
    assert got["column_name"] == "prep"

    lonely = _make(store, title="never bound")
    started = time.time() - dispatch.DISPATCH_BIND_WINDOW - 5
    d._consults[lonely["id"]] = {
        "card_id": lonely["id"], "project": "bob", "root": "/tmp",
        "tool": "claude", "baseline": set(),
        "started": started, "session_id": "",
    }
    assert d._bind_consults({"running": []}, time.time())
    assert lonely["id"] not in d._consults
    notes = store.messages(lonely["id"])
    assert notes and notes[0]["kind"] == "note"
    assert "no helper session appeared" in notes[0]["text"]
    assert notes[0]["author"] == "bob"


@pytest.mark.asyncio
async def test_answer_from_a_bound_session_lands_via_session(daemon):
    d, store = daemon
    card = _make(store)
    store.update(card["id"], {"session_id": "sess-live",
                              "link_state": "live"})
    got, detail = await d.answer_card_by_session("sess-live", "yes it is")
    assert got is not None, detail
    rows = store.messages(card["id"])
    assert rows[-1]["kind"] == "answer"
    assert rows[-1]["via"] == "session"
    assert rows[-1]["author"] == "sess-live"
    assert rows[-1]["text"] == "yes it is"
    assert store.get(card["id"])["session_id"] == "sess-live"


@pytest.mark.asyncio
async def test_answer_from_a_consult_lands_via_consultant_and_never_touches_the_card(
        daemon):
    d, store = daemon
    card = _make(store)
    before = _card_fields(store, card["id"])
    d._consults[card["id"]] = {
        "card_id": card["id"], "project": "bob", "root": "/tmp",
        "tool": "claude", "baseline": set(),
        "started": time.time(), "session_id": "helper-1",
    }
    got, detail = await d.answer_card_by_session("helper-1", "from the helper")
    assert got is not None, detail
    assert card["id"] not in d._consults
    rows = store.messages(card["id"])
    assert rows[-1]["via"] == "consultant"
    assert rows[-1]["author"] == "helper-1"
    assert _card_fields(store, card["id"]) == before


@pytest.mark.asyncio
async def test_a_consult_session_cannot_close_the_card(daemon):
    d, store = daemon
    card = _make(store)
    d._consults[card["id"]] = {
        "card_id": card["id"], "project": "bob", "root": "/tmp",
        "tool": "claude", "baseline": set(),
        "started": time.time(), "session_id": "helper-1",
    }
    got, detail = await d.close_card_by_session("helper-1", "done")
    assert got is None
    assert "no card on Dark Army's board names this session" in detail
    assert store.get(card["id"])["column_name"] == "prep"


@pytest.mark.asyncio
async def test_answer_ambiguity_is_refused(daemon):
    d, store = daemon
    a = _make(store, title="one")
    b = _make(store, title="two")
    store.update(a["id"], {"session_id": "shared", "link_state": "live"})
    store.update(b["id"], {"session_id": "shared", "link_state": "live"})
    got, detail = await d.answer_card_by_session("shared", "which?")
    assert got is None
    assert "more than one card" in detail
    assert store.messages(a["id"]) == []
    assert store.messages(b["id"]) == []


@pytest.mark.asyncio
async def test_a_failed_direct_push_stores_nothing(daemon, monkeypatch):
    d, store = daemon
    card = _live_claude(d, store)
    monkeypatch.setattr(d, "_channel_for_session",
                        lambda sid: {"port": 1})

    async def miss(session_id, content, meta=None):
        return False

    monkeypatch.setattr(d, "push_channel_event", miss)
    ok, detail = await d.ask_card(card["id"], "hello")
    assert not ok
    assert store.messages(card["id"]) == []


@pytest.mark.asyncio
async def test_launch_inflight_omits_a_bound_consult(daemon, monkeypatch):
    """A helper that has bound no longer occupies the launch bound, so a
    Start in the same project is allowed. The per-card Ask block stays."""
    d, store = daemon
    asking = _make(store, title="asked")
    _arm_spawn(d, monkeypatch)
    ok, detail = await d.ask_card(asking["id"], "hello")
    assert ok, detail
    d._consults[asking["id"]]["session_id"] = "helper-1"
    inflight = d._launch_inflight(store.cards())
    assert not any(
        c.get("id") == f"consult:{asking['id']}" for c in inflight)
    starting = _make(store, title="start later", column_name="backlog")
    ok, detail = await d.dispatch_card(starting["id"], allow_unplanned=True)
    assert ok, detail
    assert store.get(starting["id"])["link_state"] == "dispatching"
    ok, detail = await d.ask_card(asking["id"], "again")
    assert not ok
    assert "already looking" in detail


@pytest.mark.asyncio
async def test_delete_card_pops_consults(daemon):
    d, store = daemon
    card = _make(store)
    d._consults[card["id"]] = {
        "card_id": card["id"], "project": "bob", "root": "/tmp",
        "tool": "claude", "baseline": set(),
        "started": time.time(), "session_id": "helper-1",
    }
    d._consult_attempts[card["id"]] = time.time()
    ok, detail = await d.delete_card(card["id"])
    assert ok, detail
    assert card["id"] not in d._consults
    assert card["id"] not in d._consult_attempts


def test_bind_consult_uses_shell_pid_on_the_entry(daemon):
    d, store = daemon
    card = _make(store)
    now = time.time()
    d._consults[card["id"]] = {
        "card_id": card["id"], "project": "bob", "root": "/tmp",
        "tool": "claude", "baseline": set(),
        "started": now, "session_id": "", "shell_pid": 100,
    }
    snapshot = {"running": [
        {"session_id": "wrong-pid", "provider": "claude",
         "kind": "interactive", "project": "bob", "cwd": "/tmp",
         "started_at": now + 1, "pid": 200},
        {"session_id": "no-pid-yet", "provider": "claude",
         "kind": "interactive", "project": "bob", "cwd": "/tmp",
         "started_at": now + 1},
        {"session_id": "the-helper", "provider": "claude",
         "kind": "interactive", "project": "bob", "cwd": "/tmp",
         "started_at": now + 2, "pid": 100},
    ]}
    assert d._bind_consults(snapshot, now + 3)
    assert d._consults[card["id"]]["session_id"] == "the-helper"


@pytest.mark.asyncio
async def test_answer_from_helper_before_bind_lands_via_consultant(daemon):
    d, store = daemon
    card = _make(store)
    now = time.time()
    d._consults[card["id"]] = {
        "card_id": card["id"], "project": "bob", "root": "/tmp",
        "tool": "claude", "baseline": set(),
        "started": now, "session_id": "",
    }
    d._agents_snapshot_cache = {"running": [
        {"session_id": "helper-early", "provider": "claude",
         "kind": "interactive", "project": "bob", "cwd": "/tmp",
         "started_at": now + 1},
    ]}
    got, detail = await d.answer_card_by_session("helper-early", "early")
    assert got is not None, detail
    assert card["id"] not in d._consults
    rows = store.messages(card["id"])
    assert rows[-1]["via"] == "consultant"
    assert rows[-1]["author"] == "helper-early"
    assert store.get(card["id"])["session_id"] == ""


@pytest.mark.asyncio
async def test_add_message_failure_after_spawn_pops_the_consult(
        daemon, monkeypatch):
    d, store = daemon
    card = _make(store)
    _arm_spawn(d, monkeypatch)
    monkeypatch.setattr(store, "add_message",
                        lambda *a, **k: (None, "full"))
    ok, detail = await d.ask_card(card["id"], "hello")
    assert not ok
    assert "full" in detail
    assert card["id"] not in d._consults


def test_bound_consult_is_dropped_when_its_session_leaves(daemon):
    d, store = daemon
    card = _make(store)
    d._consults[card["id"]] = {
        "card_id": card["id"], "project": "bob", "root": "/tmp",
        "tool": "claude", "baseline": set(),
        "started": time.time(), "session_id": "helper-1",
    }
    empty = {"running": [], "waiting": [], "sleeping": [], "finished": []}
    assert d._bind_consults(empty, time.time())
    assert card["id"] not in d._consults
    notes = store.messages(card["id"])
    assert notes and notes[-1]["kind"] == "note"
    assert "left without answering" in notes[-1]["text"]


@pytest.mark.asyncio
async def test_answer_pops_consult_when_the_card_is_gone(daemon):
    d, store = daemon
    card = _make(store)
    cid = card["id"]
    d._consults[cid] = {
        "card_id": cid, "project": "bob", "root": "/tmp",
        "tool": "claude", "baseline": set(),
        "started": time.time(), "session_id": "helper-1",
    }
    store.delete(cid)
    got, detail = await d.answer_card_by_session("helper-1", "too late")
    assert got is None
    assert "no such card" in detail
    assert cid not in d._consults
