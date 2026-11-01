"""A delete never reaches a live run.

`BoardStore.delete` and `clear_done` refuse a card with an assistant on it,
starting up or planning it (`board.has_live_run`), under the store lock and
before any row goes; `prune_done` skips such a card. The daemon verb
`delete_card` keeps the run's bookkeeping when the store refuses, and the
loopback API answers 409 with the sentence both clients draw verbatim.
Contract: `docs/card-worktrees.md`, *A delete never reaches a live run*.
"""

import asyncio
import itertools
import json
import time
import urllib.error
import urllib.request

import pytest
import pytest_asyncio

from dark_army_daemon import api_server as api_mod
from dark_army_daemon import board
from dark_army_daemon.api_server import ApiServer
from dark_army_daemon.board import BoardStore
from dark_army_daemon.daemon import BobDaemon
from tests.free_ports import free_port

API_PORT = free_port()


@pytest.fixture
def store(tmp_path):
    s = BoardStore(tmp_path / "board.db")
    s.connect()
    yield s
    s.close()


@pytest.fixture
def daemon_pair(tmp_path):
    d = BobDaemon(headless=True)
    s = BoardStore(tmp_path / "board.db")
    s.connect()
    d._board = s
    yield d, s
    s.close()


def _card(store, **kw):
    fields = {"title": "a card", "project": "bob", "root": "/tmp",
              "prompt": "go", "tool": "claude", "column_name": "backlog"}
    fields.update(kw)
    card, detail = store.create(fields)
    assert card is not None, detail
    return card


def _set(store, card, **fields):
    got, detail = store.update(card["id"], fields, bump=False)
    assert got is not None, detail
    return got


# --- the pure rule -----------------------------------------------------------


@pytest.mark.parametrize("link,refine", list(itertools.product(
    board.LINK_STATES, board.REFINE_STATES)))
def test_has_live_run_truth_table(link, refine):
    live = {"dispatching", "live"}
    expected = link in live or refine in live
    assert board.has_live_run(
        {"link_state": link, "refine_state": refine}) is expected


def test_has_live_run_tolerates_absent_keys():
    assert board.has_live_run({}) is False
    assert board.has_live_run(None) is False


def test_the_refusal_sentences_name_the_run_and_the_next_step():
    assert board.LIVE_RUN_DELETE_REFUSAL == (
        "This card still has a live run — stop it first, then delete the card.")
    assert board.live_run_clear_refusal(1) == (
        "1 card in Done still has a live run — stop it first; "
        "nothing was cleared.")
    assert board.live_run_clear_refusal(3) == (
        "3 cards in Done still have a live run — stop them first; "
        "nothing was cleared.")


# --- the store ---------------------------------------------------------------


@pytest.mark.parametrize("field,state", [
    ("link_state", "dispatching"), ("link_state", "live"),
    ("refine_state", "dispatching"), ("refine_state", "live")])
def test_store_delete_refuses_each_live_state_and_loses_nothing(
        store, field, state):
    card = _card(store)
    store.add_message(card["id"], "user", "keep me")
    _set(store, card, **{field: state})
    assert store.delete(card["id"]) == (False, board.LIVE_RUN_DELETE_REFUSAL)
    assert store.get(card["id"]) is not None
    assert store.messages(card["id"])


@pytest.mark.parametrize("field", ("link_state", "refine_state"))
@pytest.mark.parametrize("state", ("", "ended"))
def test_store_delete_goes_through_when_nothing_is_live(store, field, state):
    card = _card(store)
    _set(store, card, **{field: state})
    ok, detail = store.delete(card["id"])
    assert ok and detail == "deleted"
    assert store.get(card["id"]) is None


def test_store_delete_refuses_a_live_card_in_done_too(store):
    card = _card(store, column_name="done")
    _set(store, card, link_state="live", session_id="s1")
    assert store.delete(card["id"]) == (False, board.LIVE_RUN_DELETE_REFUSAL)
    assert store.get(card["id"]) is not None


def test_store_delete_of_a_missing_card_still_says_so(store):
    assert store.delete("nope") == (False, "no such card")


def _done_scope(store):
    return store.done_scope()


def test_clear_done_refuses_the_whole_set_for_one_live_card(store):
    ended = _card(store, column_name="done")
    live = _card(store, column_name="done")
    _set(store, live, link_state="live", session_id="s1")
    count, token = _done_scope(store)
    assert store.clear_done(count, token) == (
        False, 0, board.live_run_clear_refusal(1))
    assert store.get(ended["id"]) is not None
    assert store.get(live["id"]) is not None


def test_clear_done_counts_every_live_card_in_the_plural_sentence(store):
    for sid, field in (("s1", "link_state"), ("s2", "refine_state")):
        card = _card(store, column_name="done")
        _set(store, card, **{field: "live"})
    count, token = _done_scope(store)
    assert store.clear_done(count, token) == (
        False, 0, board.live_run_clear_refusal(2))
    assert store.counts()["done"] == 2


def test_clear_done_clears_once_every_link_has_ended(store):
    card = _card(store, column_name="done")
    _set(store, card, link_state="live", session_id="s1")
    count, token = _done_scope(store)
    assert store.clear_done(count, token)[0] is False
    store.mark_ended(card["id"])
    count, token = _done_scope(store)
    assert store.clear_done(count, token) == (True, 1, "deleted")
    assert store.get(card["id"]) is None


def test_prune_done_skips_a_live_old_card_and_collects_an_ended_one(store):
    old = time.time() - 86400 * 90
    live = _card(store, column_name="done")
    _set(store, live, link_state="live", session_id="s1")
    planning = _card(store, column_name="done")
    _set(store, planning, refine_state="dispatching")
    ended = _card(store, column_name="done")
    _set(store, ended, link_state="ended")
    for card in (live, planning, ended):
        store._conn.execute(
            "UPDATE cards SET done_at = ?, updated_at = ? WHERE id = ?",
            (old, old, card["id"]))
    store._conn.commit()
    assert store.prune_done(time.time() - 86400 * 30) == 1
    assert store.get(live["id"]) is not None
    assert store.get(planning["id"]) is not None
    assert store.get(ended["id"]) is None


# --- the daemon verb ----------------------------------------------------------


def _close_spy(d, monkeypatch):
    calls = []

    async def _close(session_id):
        calls.append(session_id)
        return True, ""

    monkeypatch.setattr(d, "_close_session_terminal", _close)
    return calls


@pytest.mark.asyncio
async def test_daemon_delete_of_a_live_card_is_refused_and_closes_nothing(
        daemon_pair, monkeypatch):
    d, store = daemon_pair
    card = _card(store)
    store.bind_session(card["id"], "sess-1")
    closes = _close_spy(d, monkeypatch)
    ok, detail = await d.delete_card(card["id"])
    assert (ok, detail) == (False, board.LIVE_RUN_DELETE_REFUSAL)
    assert store.get(card["id"]) is not None
    assert closes == []


@pytest.mark.asyncio
async def test_daemon_delete_of_a_dispatching_card_keeps_its_bookkeeping(
        daemon_pair, monkeypatch):
    d, store = daemon_pair
    card = _card(store)
    cid = str(card["id"])
    _set(store, card, link_state="dispatching")
    d._spawn_shell_pids[cid] = 4242
    d._dispatch_baseline[cid] = {"a"}
    d._dispatch_attempts[cid] = 1.0
    ok, detail = await d.delete_card(cid)
    assert (ok, detail) == (False, board.LIVE_RUN_DELETE_REFUSAL)
    assert d._spawn_shell_pids[cid] == 4242
    assert d._dispatch_baseline[cid] == {"a"}
    assert d._dispatch_attempts[cid] == 1.0


@pytest.mark.asyncio
async def test_a_delete_confirmed_off_a_stale_picture_is_refused_once_the_card_started(
        daemon_pair):
    """The success criterion. The delete waits on `_dispatch_lock` with an
    idle card; the start verb, holding the lock, moves the card to
    `dispatching` meanwhile. The delete reads the card as it is now."""
    d, store = daemon_pair
    card = _card(store)
    await d._dispatch_lock.acquire()
    try:
        task = asyncio.ensure_future(d.delete_card(card["id"]))
        await asyncio.sleep(0.05)
        assert not task.done()
        _set(store, card, link_state="dispatching")
    finally:
        d._dispatch_lock.release()
    ok, detail = await task
    assert (ok, detail) == (False, board.LIVE_RUN_DELETE_REFUSAL)
    assert store.get(card["id"]) is not None


@pytest.mark.asyncio
async def test_a_mark_live_racing_the_delete_in_the_store_window_is_refused(
        daemon_pair, monkeypatch):
    """The reconcile's `mark_live` takes only the store lock. It lands just
    before the `delete` reaches the store, after the daemon's own read; the
    store's check, under its lock, is what refuses."""
    d, store = daemon_pair
    card = _card(store)
    store.bind_session(card["id"], "sess-1")
    store.mark_ended(card["id"])
    orig = d._board_call

    async def racing(method, *a, **k):
        if method == "delete":
            store.mark_live(card["id"])
        return await orig(method, *a, **k)

    monkeypatch.setattr(d, "_board_call", racing)
    ok, detail = await d.delete_card(card["id"])
    assert (ok, detail) == (False, board.LIVE_RUN_DELETE_REFUSAL)
    assert store.get(card["id"]) is not None


@pytest.mark.asyncio
async def test_a_card_being_prepared_is_not_a_live_run(daemon_pair):
    """The preparing window has no link yet; Delete is the only way to
    cancel it, so it still goes through."""
    d, store = daemon_pair
    card = _card(store)
    d._worktree_preparing = getattr(d, "_worktree_preparing", set())
    ok, detail = await d.delete_card(card["id"])
    assert ok, detail
    assert store.get(card["id"]) is None


@pytest.mark.asyncio
async def test_daemon_clear_done_with_a_live_card_refuses_and_publishes_nothing(
        daemon_pair, monkeypatch):
    d, store = daemon_pair
    card = _card(store, column_name="done")
    _set(store, card, link_state="live", session_id="s1")
    count, token = store.done_scope()
    published = []

    async def publish():
        published.append(True)

    monkeypatch.setattr(d, "_publish_board", publish)
    result = await d.clear_done_cards(count, token)
    assert result == (False, 0, board.live_run_clear_refusal(1))
    assert published == []
    assert store.get(card["id"]) is not None


# --- the loopback API ----------------------------------------------------------


@pytest_asyncio.fixture
async def api_board(tmp_path, monkeypatch):
    path = tmp_path / "api-token"
    monkeypatch.setattr(api_mod, "API_TOKEN_PATH", path)
    monkeypatch.setattr(api_mod, "ensure_state_dir", lambda: tmp_path)
    monkeypatch.setattr(BobDaemon, "_schedule_agents_push", lambda self: None)
    d = BobDaemon(sessions_path=tmp_path / "sessions.json")
    store = BoardStore(tmp_path / "board.db")
    store.connect()
    d._board = store
    d._refresh_board_state()
    srv = ApiServer(d, port=API_PORT)
    await srv.start()
    try:
        yield srv, d, store
    finally:
        await srv.stop()
        store.close()


def _post(body, token):
    req = urllib.request.Request(
        f"http://127.0.0.1:{API_PORT}/api/action",
        data=json.dumps(body).encode(), headers={"X-Bob-Token": token},
        method="POST")
    try:
        resp = urllib.request.urlopen(req, timeout=5)
        return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


async def _api(body, token):
    return await asyncio.to_thread(_post, body, token)


@pytest.mark.asyncio
async def test_api_board_delete_of_a_bound_card_is_409_with_the_sentence(
        api_board):
    srv, _d, store = api_board
    card = _card(store)
    store.bind_session(card["id"], "sess-1")
    status, body = await _api(
        {"action": "board_delete", "card_id": card["id"]}, srv.token)
    assert status == 409
    assert body == {"ok": False, "detail": board.LIVE_RUN_DELETE_REFUSAL}
    assert store.get(card["id"]) is not None


@pytest.mark.asyncio
async def test_api_board_clear_done_with_a_live_card_is_409_with_the_sentence(
        api_board):
    srv, _d, store = api_board
    card = _card(store, column_name="done")
    _set(store, card, link_state="live", session_id="s1")
    count, token = store.done_scope()
    status, body = await _api(
        {"action": "board_clear_done", "expected_count": str(count),
         "expected_done_token": token}, srv.token)
    assert status == 409
    assert body["detail"] == board.live_run_clear_refusal(1)
    assert store.get(card["id"]) is not None


@pytest.mark.asyncio
async def test_api_board_delete_of_an_idle_card_is_200(api_board):
    srv, _d, store = api_board
    card = _card(store)
    status, body = await _api(
        {"action": "board_delete", "card_id": card["id"]}, srv.token)
    assert status == 200 and body["ok"] is True
    assert store.get(card["id"]) is None


# --- stopping the run, as the refusal says, frees the delete at once ----------


def _live_card(d, store):
    card = _card(store)
    store.bind_session(card["id"], "sess-1")
    d._board_state = d._build_board_state()
    return card


@pytest.mark.asyncio
async def test_a_stop_that_landed_lets_the_next_reconcile_end_the_link_and_delete(
        daemon_pair):
    d, store = daemon_pair
    card = _live_card(d, store)
    assert (await d.delete_card(card["id"]))[0] is False
    d._forget_stopped("sess-1")
    d._reconcile_board({"running": []})
    assert store.get(card["id"])["link_state"] == "ended"
    assert store.get(card["id"])["column_name"] == "in_progress"
    ok, detail = await d.delete_card(card["id"])
    assert ok, detail
    assert store.get(card["id"]) is None


@pytest.mark.asyncio
async def test_a_close_that_landed_frees_the_delete_and_a_refused_one_does_not(
        daemon_pair, monkeypatch):
    """Through the real `_close_session_terminal`; only the editor's
    `close_terminal` is stubbed, so dropping the helper call turns it red."""
    from dark_army_daemon import vscode_reveal as vr
    d, store = daemon_pair
    card = _live_card(d, store)
    d._session_states["sess-1"] = {"pid": 4242, "last_event": 0}
    reply = [None]

    async def close(pid, tty):
        return reply[0]

    monkeypatch.setattr(vr, "close_terminal", close)
    ok, _ = await d.close_session_terminal("sess-1")
    assert ok is False
    d._reconcile_board({"running": []})
    assert (await d.delete_card(card["id"])) == (
        False, board.LIVE_RUN_DELETE_REFUSAL)
    reply[0] = {"matched": True, "closed": True, "terminalName": "claude"}
    ok, _ = await d.close_session_terminal("sess-1")
    assert ok is True
    d._reconcile_board({"running": []})
    ok, detail = await d.delete_card(card["id"])
    assert ok, detail


def _codex_card(d, store):
    from tests.test_close_terminal_button import _codex_record
    record = _codex_record("t-1")
    d._codex_records[record.session_id] = record
    card = _card(store, tool="codex")
    store.bind_session(card["id"], record.session_id)
    d._board_state = d._build_board_state()
    return record, card


@pytest.mark.asyncio
async def test_a_codex_stop_that_landed_frees_the_delete_and_a_refused_one_does_not(
        daemon_pair, monkeypatch):
    from dark_army_daemon import codex_rollouts
    from dark_army_daemon import daemon as daemon_mod
    d, store = daemon_pair
    record, card = _codex_card(d, store)
    monkeypatch.setattr(codex_rollouts.psutil, "process_iter",
                        lambda attrs: [])
    monkeypatch.setattr(codex_rollouts, "load_recent", lambda: [record])
    # The process is still there but its identity no longer matches: refused.
    monkeypatch.setattr(daemon_mod.psutil, "pid_exists", lambda pid: True)
    ok, _ = d.stop_codex_session(record.session_id)
    assert ok is False
    d._reconcile_board({"running": []})
    assert (await d.delete_card(card["id"])) == (
        False, board.LIVE_RUN_DELETE_REFUSAL)
    # The process is gone: the stop lands.
    monkeypatch.setattr(daemon_mod.psutil, "pid_exists", lambda pid: False)
    assert d.stop_codex_session(record.session_id) == (True, "already stopped")
    d._reconcile_board({"running": []})
    assert store.get(card["id"])["link_state"] == "ended"
    ok, detail = await d.delete_card(card["id"])
    assert ok, detail


@pytest.mark.asyncio
async def test_a_stopped_planner_frees_a_refining_card_too(daemon_pair):
    d, store = daemon_pair
    card = _card(store, column_name="prep")
    _set(store, card, refine_state="live", refine_session_id="rs-1")
    d._board_state = d._build_board_state()
    d._forget_stopped("rs-1")
    d._reconcile_board({"running": []})
    assert store.get(card["id"])["refine_state"] == "ended"
    ok, detail = await d.delete_card(card["id"])
    assert ok, detail
