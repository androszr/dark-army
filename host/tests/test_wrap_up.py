"""Acknowledging a finished turn and clearing the session for the next one.

The verb is two acts that only make sense together — `/clear` typed onto the
session's own input line, then the card dropped — and its whole risk is that
half of it can land. These tests pin the order, the guards and the refusals.
"""
import asyncio

import pytest


def _daemon(monkeypatch, sent=None, result=None):
    from dark_army_daemon.daemon import BobDaemon
    from dark_army_daemon import vscode_reveal as vr

    async def _send(pid, tty, text, newline=True):
        if sent is not None:
            sent.append((pid, text))
        return result

    monkeypatch.setattr(vr, "send_text", _send)
    daemon = BobDaemon(headless=True)
    daemon._session_states["s1"] = {"pid": 4242, "last_event": 0}
    return daemon


def test_the_command_is_a_bare_slash_clear_behind_a_kill_line(monkeypatch):
    """Ctrl-U first, for the reason auto-compact pays the same cost: the input
    line may hold a half-typed draft, and appending to it would submit
    something the user never wrote."""
    from dark_army_daemon import daemon as dm

    sent = []
    daemon = _daemon(monkeypatch, sent, {"matched": True, "sent": True,
                                         "terminalName": "zsh"})
    ok, detail = asyncio.run(daemon.wrap_up_session("s1"))
    assert (ok, detail) == (True, "")
    assert sent == [(4242, "\x15/clear")]
    assert dm.WRAP_UP_COMMAND == "/clear"


def test_the_card_is_dropped_only_after_the_command_lands(monkeypatch):
    daemon = _daemon(monkeypatch, result={"matched": True, "sent": True})
    daemon._active_notifications["s1"] = {"session_id": "s1",
                                          "message": "waiting for you"}
    assert asyncio.run(daemon.wrap_up_session("s1"))[0] is True
    assert "s1" not in daemon._active_notifications
    # Claude (or an unstamped session) still waits for SessionEnd to
    # forget the row. Grok is refused before `/clear` — see the next test.
    assert "s1" in daemon._session_states


def test_wrap_up_refuses_a_grok_session_and_does_not_clear(monkeypatch):
    """Grok `/clear` starts a new id and never SessionEnds the old one.
    Forgetting that leftover vanished the row; refusing leaves it to Close."""
    from dark_army_daemon import daemon as dm

    sent = []
    daemon = _daemon(monkeypatch, sent, {"matched": True, "sent": True})
    daemon._session_states["s1"] = {
        "pid": 4242, "last_event": 0, "provider": "grok", "state": "idle",
    }
    daemon._active_notifications["s1"] = {"session_id": "s1",
                                          "message": "waiting for you"}
    ok, detail = asyncio.run(daemon.wrap_up_session("s1"))
    assert (ok, detail) == (False, dm.GROK_WRAP_UP_REFUSAL)
    assert sent == []
    assert "s1" in daemon._session_states
    assert "s1" in daemon._active_notifications
    assert "s1" not in daemon._finished


def test_wrap_up_or_close_refuses_grok_before_closing(monkeypatch, tmp_path):
    """A stale wrap_up action must not close hosted Grok mid-tool."""
    from dark_army_daemon import daemon as dm
    from dark_army_daemon import vscode_reveal as vr
    from dark_army_daemon.board import BoardStore
    from dark_army_daemon.daemon import BobDaemon

    closed = []
    sent = []

    async def _close(pid, tty):
        closed.append((pid, tty))
        return {"matched": True, "closed": True, "terminalName": "grok"}

    async def _send(pid, tty, text, newline=True):
        sent.append((pid, text))
        return {"matched": True, "sent": True}

    monkeypatch.setattr(vr, "close_terminal", _close)
    monkeypatch.setattr(vr, "send_text", _send)
    daemon = BobDaemon(headless=True)
    daemon.board_close_terminal_enabled = True
    daemon._session_states["s1"] = {
        "pid": 4242, "last_event": 0, "provider": "grok", "state": "working",
    }
    store = BoardStore(tmp_path / "board.db")
    store.connect()
    daemon._board = store
    try:
        card, detail = store.create({
            "title": "t", "project": "bob", "root": "/tmp",
            "prompt": "go", "tool": "grok", "column_name": "in_progress",
        })
        assert card is not None, detail
        store.bind_session(card["id"], "s1")
        store.mark_live(card["id"])
        ok, detail = asyncio.run(daemon.wrap_up_or_close_session("s1"))
        assert (ok, detail) == (False, dm.GROK_WRAP_UP_REFUSAL)
        assert closed == []
        assert sent == []
        assert "s1" in daemon._session_states
        assert store.get(card["id"])["column_name"] == "in_progress"
        assert "s1" not in daemon._finished
    finally:
        store.close()


def test_a_send_that_does_not_land_leaves_the_card_alone(monkeypatch):
    """The half that must never happen on its own: dismissing first and then
    failing to type takes the row out of Needs you with the session still
    full, which is the one outcome nobody asked for."""
    daemon = _daemon(monkeypatch, result=None)
    daemon._active_notifications["s1"] = {"session_id": "s1",
                                          "message": "waiting for you"}
    ok, detail = asyncio.run(daemon.wrap_up_session("s1"))
    assert ok is False and "VS Code" in detail
    assert "s1" in daemon._active_notifications


def test_an_unmatched_terminal_is_a_refusal_not_a_success(monkeypatch):
    """A window that answered but did not own the terminal reports
    `sent: False`, and that must not read as a clear."""
    daemon = _daemon(monkeypatch, result={"matched": True, "sent": False})
    assert asyncio.run(daemon.wrap_up_session("s1"))[0] is False


def test_a_session_with_no_pid_refuses_in_words(monkeypatch):
    daemon = _daemon(monkeypatch, result={"matched": True, "sent": True})
    daemon._session_states["s2"] = {"last_event": 0}
    ok, detail = asyncio.run(daemon.wrap_up_session("s2"))
    assert ok is False and "PID" in detail


def test_a_permission_prompt_blocks_the_clear(monkeypatch):
    """`sendText` ends in a newline, which an open tool-approval dialog reads
    as "confirm the highlighted choice" — so wrapping up must never approve a
    tool call on the way past. Same trap `_flush_auto_compacts` documents."""
    sent = []
    daemon = _daemon(monkeypatch, sent, {"matched": True, "sent": True})
    monkeypatch.setattr(daemon, "_prompts_by_session",
                        lambda: {"s1": {"request_id": "r1"}})
    ok, detail = asyncio.run(daemon.wrap_up_session("s1"))
    assert ok is False and "permission prompt" in detail
    assert sent == []


def test_the_verb_does_not_reach_for_the_channel():
    """A slash command is expanded by the client on the input line, before a
    request exists. The channel lands in the model's reading, where nothing can
    act on it — this is how auto-compact silently failed first time round."""
    import inspect
    from dark_army_daemon.daemon import BobDaemon

    src = inspect.getsource(BobDaemon.wrap_up_session)
    assert "push_channel_event" not in src
    assert "send_text" in src


def test_a_stopped_row_carries_whether_bob_can_type_at_it(monkeypatch):
    """`can_type` is what the panel's wrap-up button is drawn from, and it is a
    different reach from `channel`: prose to the model versus keystrokes to the
    client. Only stopped rows are asked, because the answer costs a `ps`."""
    from dark_army_daemon.daemon import BobDaemon
    from dark_army_daemon import vscode_reveal as vr

    asked = []

    def _can(pid, tty=""):
        asked.append(pid)
        return True

    monkeypatch.setattr(vr, "can_send_text", _can)
    daemon = BobDaemon(headless=True)
    stubs = [
        {"session_id": "s1", "pid": 4242, "_category": "waiting", "cwd": ""},
        {"session_id": "s2", "pid": 4343, "_category": "running", "cwd": ""},
    ]
    out = daemon._enrich_agent_stubs(stubs)
    waiting = out["waiting"][0]
    running = out["running"][0]
    assert waiting["can_type"] is True
    assert running["can_type"] is False
    assert asked == [4242]


@pytest.mark.asyncio
async def test_the_api_exposes_wrap_up_and_reports_its_refusal():
    """409 rather than a swallowed failure: nothing else on screen moves when a
    clear does not land, so a discarded refusal is indistinguishable from a
    slow success and the press is repeated."""
    from dark_army_daemon import api_server

    class _Daemon:
        def __init__(self):
            self.calls = []

        async def wrap_up_or_close_session(self, session_id):
            self.calls.append(session_id)
            return False, "No VS Code window owns this session's terminal."

    daemon = _Daemon()
    server = api_server.ApiServer(daemon)
    server.token = "t"
    request = api_server._Request(
        "POST", "/api/action", "",
        {"x-bob-token": "t"},
        b'{"action": "wrap_up", "session_id": "s1"}')
    assert server._wrap_up_request(request) == "s1"
    status, _ctype, body = await server._wrap_up("s1")
    assert status == 409 and b"VS Code" in body
    assert daemon.calls == ["s1"]


def test_an_unauthorised_wrap_up_is_not_routed():
    """The token gate is the same one every other write goes through — and a
    read is ungated, so an unauthorised write that fell through to `_route`
    would be answered 403 there rather than performed here."""
    from dark_army_daemon import api_server

    server = api_server.ApiServer(object())
    server.token = "t"
    request = api_server._Request(
        "POST", "/api/action", "", {},
        b'{"action": "wrap_up", "session_id": "s1"}')
    assert server._wrap_up_request(request) is None


def test_the_board_route_does_not_clear_the_conversation(monkeypatch, tmp_path):
    """A card arriving in Done no longer types `/clear`. The row-level Wrap
    up button still does; this is the Done-leg pin that it does not."""
    from dark_army_daemon.board import BoardStore

    sent = []
    daemon = _daemon(monkeypatch, sent, {"matched": True, "sent": True,
                                         "terminalName": "zsh"})
    daemon._active_notifications["s1"] = {"session_id": "s1",
                                          "message": "waiting for you"}

    store = BoardStore(tmp_path / "board.db")
    store.connect()
    daemon._board = store
    try:
        card, detail = store.create({"title": "t", "project": "bob",
                                     "root": "/tmp", "prompt": "go",
                                     "tool": "claude", "column_name": "backlog"})
        assert card is not None, detail
        store.bind_session(card["id"], "s1")
        store.mark_live(card["id"])
        got, detail = asyncio.run(
            daemon.update_card(card["id"], {"column_name": "done"}))
        assert got is not None, detail
        assert store.get(card["id"])["column_name"] == "done"
    finally:
        store.close()

    assert sent == []
    assert "s1" in daemon._active_notifications
