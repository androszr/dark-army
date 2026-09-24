"""A person's press on Close terminal finishes that session's board card.

The discriminator is a payload field, `by_person`, and never the action name:
`.claude/skills/ship/close-out.sh --close` posts the *same* `close_terminal`
action when the person asks an agent in words to close its own tab. Keying
the Done move off the action would finish a card on every such request —
which, since most such turns already closed their card, would look fine in
testing and be wrong exactly on the turns where the agent chose not to close
it.
"""
import pytest

from dark_army_daemon import daemon as daemon_mod
from dark_army_daemon.api_server import ApiServer
from dark_army_daemon.board import BoardStore
from dark_army_daemon.daemon import BobDaemon


@pytest.fixture
def daemon(tmp_path, monkeypatch):
    """`test_close_terminal_button.py`'s stubbed `vscode_reveal.close_terminal`
    plus `test_dispatch.py`'s `BoardStore` on a temp path. Nothing here reaches
    a real VS Code window or the running fleet's board."""
    from dark_army_daemon import vscode_reveal as vr

    closed = []

    async def _close(pid, tty):
        closed.append((pid, tty))
        return {"matched": True, "closed": True, "terminalName": "claude"}

    monkeypatch.setattr(vr, "close_terminal", _close)
    d = BobDaemon(headless=True, sessions_path=tmp_path / "sessions.json")
    d._session_states["sess-1"] = {"pid": 4242, "last_event": 0}
    store = BoardStore(tmp_path / "board.db")
    store.connect()
    d._board = store
    try:
        yield d, store, closed
    finally:
        store.close()


def _make(store, **kw):
    fields = {"title": "do the thing", "project": "bob", "root": "/tmp",
              "prompt": "go", "tool": "claude", "column_name": "backlog"}
    fields.update(kw)
    card, detail = store.create(fields)
    assert card is not None, detail
    return card


def _live(store, session="sess-1", **kw):
    card = _make(store, column_name="in_progress", **kw)
    store.bind_session(card["id"], session)
    store.mark_live(card["id"])
    return card


# --- the person's press -------------------------------------------------------


@pytest.mark.asyncio
async def test_a_persons_press_finishes_the_bound_card(daemon):
    d, store, closed = daemon
    card = _live(store)
    ok, detail = await d.close_session_terminal("sess-1", by_person=True)
    assert (ok, detail) == (True, "")
    assert closed == [(4242, "")]
    got = store.get(card["id"])
    assert got["column_name"] == "done"
    assert got["close_note"] == daemon_mod.CLOSE_TERMINAL_DONE_NOTE
    # The *store* column is the session's own id; `declare_done` stays its
    # single writer and the diary's `closed_by` is a different word.
    assert got["closed_by"] == "sess-1"
    # The press is the person's own review: a close *they* made must not
    # wear "FINISHED · REVIEW" and sit in Needs you on the Mac and the
    # phone waiting for the same person to press Reviewed.
    assert got["reviewed_at"] is not None


@pytest.mark.asyncio
async def test_a_persons_close_leaves_its_manual_check_standing(daemon):
    """Reviewing is not doing the chore: the steps the session flagged stay
    on the card, so the phone and the Mac still list it under Manual check
    until somebody presses Mark checked."""
    d, store, _ = daemon
    card = _live(store)
    flagged, detail = store.flag_manual(card["id"], "sess-1", "1. Open it.")
    assert flagged is not None, detail
    ok, _ = await d.close_session_terminal("sess-1", by_person=True)
    assert ok
    got = store.get(card["id"])
    assert got["column_name"] == "done"
    assert got["reviewed_at"] is not None
    assert got["manual_steps"] == "1. Open it."


@pytest.mark.asyncio
async def test_an_agents_own_close_is_not_reviewed(daemon):
    """`bob_close_card` is the assistant's statement, and only a person's
    press reviews it — the auto-review rides `_finish_card_for_closed_session`
    alone, never `close_card_by_session`."""
    d, store, _ = daemon
    card = _live(store)
    closed, detail = await d.close_card_by_session("sess-1", "done it")
    assert closed is not None, detail
    assert store.get(card["id"])["reviewed_at"] is None


@pytest.mark.asyncio
async def test_the_default_finishes_nothing(daemon):
    """`close-out.sh --close`'s route, byte-identical apart from the missing key."""
    d, store, closed = daemon
    card = _live(store)
    ok, detail = await d.close_session_terminal("sess-1")
    assert (ok, detail) == (True, "")
    assert closed == [(4242, "")]
    assert store.get(card["id"])["column_name"] == "in_progress"


@pytest.mark.asyncio
async def test_the_wrap_up_action_finishes_nothing(daemon):
    """Driven through the real method rather than by reading its source: an
    old installed `close-out.sh` reaches `wrap_up_or_close_session`."""
    d, store, _ = daemon
    d.board_close_terminal_enabled = True
    card = _live(store)
    await d.wrap_up_or_close_session("sess-1")
    assert store.get(card["id"])["column_name"] == "in_progress"


# --- what it must not reach ---------------------------------------------------


@pytest.mark.asyncio
async def test_a_refining_session_is_untouched(daemon, tmp_path):
    """A card whose plan is being written is held under `refine_session_id`,
    which `by_session` does not match — the exclusion is inherited, not
    re-implemented."""
    d, store, _ = daemon
    card = _make(store, column_name="prep")
    store.update(card["id"], {"refine_session_id": "sess-1",
                              "refine_state": "live"})
    ok, detail = await d.close_session_terminal("sess-1", by_person=True)
    assert (ok, detail) == (True, "")
    got = store.get(card["id"])
    assert got["column_name"] == "prep"
    assert got["close_note"] == ""


@pytest.mark.asyncio
async def test_a_refused_close_finishes_nothing(daemon, monkeypatch):
    """The move comes after the close returned ok. A card finished by a close
    that then refused would be the worst of both."""
    from dark_army_daemon import vscode_reveal as vr

    d, store, _ = daemon

    async def _no_match(pid, tty):
        return None

    monkeypatch.setattr(vr, "close_terminal", _no_match)
    card = _live(store)
    ok, _ = await d.close_session_terminal("sess-1", by_person=True)
    assert ok is False
    assert store.get(card["id"])["column_name"] == "in_progress"


@pytest.mark.asyncio
async def test_a_card_already_done_leaves_the_answer_alone(daemon):
    """The ordinary case: the agent called `bob_close_card` during its own
    turn, then the person closed the tab. Silent, and the close still says
    it worked — the tab really is gone."""
    d, store, _ = daemon
    card = _live(store)
    got, detail = store.declare_done(card["id"], "sess-1", "all green")
    assert got is not None, detail
    ok, detail = await d.close_session_terminal("sess-1", by_person=True)
    assert (ok, detail) == (True, "")
    assert store.get(card["id"])["close_note"] == "all green"


@pytest.mark.asyncio
async def test_two_open_cards_move_neither_and_still_succeed(daemon):
    """Ambiguity fails closed, inherited from `close_card_by_session`."""
    d, store, _ = daemon
    one = _live(store)
    two = _live(store)
    ok, detail = await d.close_session_terminal("sess-1", by_person=True)
    assert (ok, detail) == (True, "")
    assert store.get(one["id"])["column_name"] == "in_progress"
    assert store.get(two["id"])["column_name"] == "in_progress"


@pytest.mark.asyncio
async def test_the_done_arrival_hook_cannot_fire(daemon, monkeypatch):
    """The no-loop property, proven by execution rather than by comment:
    `declare_done` never enters `BoardStore.update` and this route never calls
    `update_card`, so `_after_board_write` — and therefore `_wrap_up_for_done`
    — is structurally unreachable."""
    d, store, _ = daemon
    d.board_close_terminal_enabled = True

    async def boom(*a, **k):
        raise AssertionError("the Done-arrival hook fired")

    monkeypatch.setattr(d, "_wrap_up_for_done", boom)
    card = _live(store)
    ok, detail = await d.close_session_terminal("sess-1", by_person=True)
    assert (ok, detail) == (True, "")
    assert store.get(card["id"])["column_name"] == "done"


@pytest.mark.asyncio
async def test_a_permission_prompt_refuses_above_everything(daemon, monkeypatch):
    d, store, closed = daemon
    card = _live(store)
    monkeypatch.setattr(d, "_prompts_by_session",
                        lambda: {"sess-1": {"request_id": "r1"}})
    ok, detail = await d.close_session_terminal("sess-1", by_person=True)
    assert ok is False and "permission prompt" in detail
    assert closed == []
    assert store.get(card["id"])["column_name"] == "in_progress"


@pytest.mark.asyncio
async def test_a_codex_card_is_moved_too(daemon):
    """Deliberately *not* excluded, unlike in `_wrap_up_for_done` and
    `_close_for_deleted_card`. Those skip Codex because Dark Army cannot type at
    it; this writes a board row and types nothing."""
    d, store, _ = daemon
    card = _live(store, tool="codex")
    ok, detail = await d.close_session_terminal("sess-1", by_person=True)
    assert (ok, detail) == (True, "")
    assert store.get(card["id"])["column_name"] == "done"


# --- the diary ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_diary_says_the_terminal_was_closed(daemon, monkeypatch):
    from dark_army_daemon import event_log

    d, store, _ = daemon
    lines = []
    # `_log_card_event` returns early with no diary open; the stub below
    # replaces the append, so the object only has to be non-None.
    d._event_log = object()
    monkeypatch.setattr(d, "_log_event",
                        lambda kind, **fields: lines.append((kind, fields)))
    card = _live(store)
    await d.close_session_terminal("sess-1", by_person=True)
    done = [f for kind, f in lines if kind == "card_done"]
    assert len(done) == 1, lines
    assert done[0]["detail"]["closed_by"] == "terminal_close"
    assert event_log.sentence("card_done", **done[0]) == (
        f"{card['title']} was finished when its terminal was closed")


# --- the wire -----------------------------------------------------------------


def test_the_loopback_route_parses_the_flag_and_defaults_false():
    import inspect
    src = inspect.getsource(ApiServer._close_terminal_request)
    assert "by_person" in src
    handler = inspect.signature(ApiServer._close_terminal)
    assert handler.parameters["by_person"].default is False


def test_the_sealed_branch_reads_the_same_key():
    import inspect
    src = inspect.getsource(ApiServer._lan_run)
    branch = src[src.find('if action == "close_terminal"'):]
    assert "by_person" in branch[:400], (
        "the sealed door drops the flag, so a phone press finishes nothing")


def test_neither_action_tuple_widened():
    assert len(ApiServer.LAN_ACTIONS) == len(set(ApiServer.LAN_ACTIONS))
    assert "close_terminal" in ApiServer.LAN_ACTIONS
    assert "close_terminal" in ApiServer.REMOTE_ACTIONS
    assert set(ApiServer.REMOTE_ACTIONS) <= set(ApiServer.LAN_ACTIONS)
