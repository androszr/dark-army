# host/tests/test_terminal_stream.py
"""The terminal stream on every door, and the write action's refusals.

The sealed `terminal` **kind** is served at home and away, needs no lease,
writes no `remote_activity`, and is on neither action tuple; the
`terminal_input` **action** is on both tuples, types a leading slash the
same way the desk does, refuses an open permission prompt in its own
words, and replays a recorded receipt. Loopback: `GET /api/terminal` is
token-gated and `_loopback_host` still gates it.

A real pty over `/bin/cat` stands in for the agent — the cheapest process
that echoes what it is typed.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import signal

import pytest

from dark_army_daemon import daemon as dm
from dark_army_daemon import ptyhost, relay
from dark_army_daemon.api_server import ApiServer

# The sealed-door fixtures already exist; reuse them rather than a second copy.
from tests.test_lan_access import (  # noqa: F401  (fixtures)
    _home_post, _pair_plain, _raw, fetch, ledger, machine, ports, server,
    token_path, _no_fleet_snapshot)


async def _wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return predicate()


async def _cat_session(daemon, tmp_path, sid="s1"):
    """A Dark Army-owned `/bin/cat` bound to session `sid`, with the row Dark Army would
    hold for it. Returns the handle."""
    ok, detail, pid = await daemon._pty.start(str(tmp_path), ["/bin/cat"], "cat")
    assert ok, detail
    handle = daemon._pty.owns(pid)
    daemon._pty.bind(handle, sid)
    daemon._session_states[sid] = {"pid": pid, "last_event": 0, "state": "idle",
                                   "provider": "claude"}
    return handle


@pytest.fixture(autouse=True)
def _reap_ptys():
    yield
    host = ptyhost.current()
    if host is not None:
        for term in host.terminals():
            try:
                os.killpg(term.pid, signal.SIGKILL)
            except OSError:
                pass


# --- the tuples ---------------------------------------------------------------


def test_the_read_is_on_neither_tuple_and_the_write_is_on_both():
    assert "terminal" not in ApiServer.LAN_ACTIONS
    assert "terminal" not in ApiServer.REMOTE_ACTIONS
    assert "terminal_input" in ApiServer.LAN_ACTIONS
    assert "terminal_input" in ApiServer.REMOTE_ACTIONS
    assert set(ApiServer.REMOTE_ACTIONS) <= set(ApiServer.LAN_ACTIONS)


def test_terminal_supported_is_stated_on_the_board_snapshot():
    d = dm.BobDaemon(headless=True)
    flags = d._pipeline_writable()
    assert flags["terminal_supported"] is True
    assert flags["own_terminal_enabled"] is False
    assert flags["own_terminal_spawn_supported"] is True
    d.board_own_terminal_enabled = True
    assert d._pipeline_writable()["own_terminal_enabled"] is True


# --- the daemon's frame -------------------------------------------------------


def test_a_session_bob_does_not_host_says_so():
    d = dm.BobDaemon(headless=True)
    frame = d.terminal_frame("nobody", -1)
    assert frame["available"] is False
    assert frame["rows_changed"] == []
    assert frame["unchanged"] is False
    assert frame["reason"] == dm.TERMINAL_NOT_OWNED_REFUSAL


@pytest.mark.asyncio
async def test_frames_are_incremental_and_unchanged_is_a_present_key(tmp_path):
    d = dm.BobDaemon(headless=True)
    handle = await _cat_session(d, tmp_path)
    term = d._pty.get(handle)
    d._pty.write(handle, "hello\r")
    assert await _wait_for(lambda: "hello" in "".join(term.screen.text()))
    first = d.terminal_frame("s1", -1)
    assert first["available"] is True
    assert first["cols"] == ptyhost.DEFAULT_COLS
    texts = ["".join(r[0] for r in runs) for _y, runs in first["rows_changed"]]
    assert any("hello" in t for t in texts)
    assert first["unchanged"] is False
    again = d.terminal_frame("s1", first["revision"])
    assert again["rows_changed"] == []
    assert again["unchanged"] is True
    assert again["revision"] == first["revision"]
    assert "cursor" in again and again["exited"] is False
    assert "history" in first and first["history"] == []
    assert "history" not in again
    await d._pty.close(handle)


@pytest.mark.asyncio
async def test_a_frame_carries_scrollback_as_history(tmp_path):
    d = dm.BobDaemon(headless=True)
    handle = await _cat_session(d, tmp_path)
    term = d._pty.get(handle)
    # One more line than the live screen, so the first one scrolls off.
    blob = "".join(f"line{i}\r\n" for i in range(term.rows + 2))
    d._pty.write(handle, blob)
    assert await _wait_for(lambda: len(term.screen.scrollback) >= 1)
    frame = d.terminal_frame("s1", -1)
    assert frame["unchanged"] is False
    texts = ["".join(r[0] for r in row) for row in frame["history"]]
    assert any(t.startswith("line") for t in texts), texts
    await d._pty.close(handle)


@pytest.mark.asyncio
async def test_a_frame_is_bounded_and_more_is_stated(tmp_path, monkeypatch):
    d = dm.BobDaemon(headless=True)
    handle = await _cat_session(d, tmp_path)
    term = d._pty.get(handle)
    # Dirty many rows across many revisions.
    for i in range(12):
        term.screen.feed(f"\x1b[{i + 1};1Hrow{i}".encode())
    monkeypatch.setattr(dm, "TERMINAL_MAX_BYTES", 120)
    # `since=0`: only the dirtied rows. A first frame (`-1`) is whole on
    # purpose, and its never-dirtied rows share one revision group.
    frame = d.terminal_frame("s1", 0)
    assert frame["more"] is True
    assert 0 < len(frame["rows_changed"]) < 12
    # Quoting the returned revision back continues from the cut, until the
    # frame says there is no more.
    ys = {y for y, _ in frame["rows_changed"]}
    hops = 0
    while frame["more"] and hops < 20:
        frame = d.terminal_frame("s1", frame["revision"])
        ys |= {y for y, _ in frame["rows_changed"]}
        hops += 1
    assert frame["more"] is False
    assert set(range(12)) <= ys
    await d._pty.close(handle)


@pytest.mark.asyncio
async def test_a_stale_revision_after_a_resize_overflows_into_a_full_frame(tmp_path):
    d = dm.BobDaemon(headless=True)
    handle = await _cat_session(d, tmp_path)
    frame = d.terminal_frame("s1", -1)
    d.terminal_frame("s1", frame["revision"], 60, 12, resize=True)
    again = d.terminal_frame("s1", frame["revision"])
    assert again["overflowed"] is True
    assert (again["cols"], again["rows"]) == (60, 12)
    assert len(again["rows_changed"]) == 12
    # Without `resize`, the size is left alone — the phone never sizes.
    d.terminal_frame("s1", -1, 80, 24, resize=False)
    assert (d._pty.get(handle).cols, d._pty.get(handle).rows) == (60, 12)
    await d._pty.close(handle)


# --- the write, through the daemon -------------------------------------------


@pytest.mark.asyncio
async def test_terminal_input_types_a_line_and_enter(tmp_path):
    d = dm.BobDaemon(headless=True)
    handle = await _cat_session(d, tmp_path)
    term = d._pty.get(handle)
    ok, detail = await d.terminal_input("s1", "hello there", from_phone=True)
    assert (ok, detail) == (True, "")
    assert await _wait_for(lambda: b"hello there\r" in term.ring)
    await d._pty.close(handle)


@pytest.mark.asyncio
async def test_the_phone_types_a_slash_the_same_way_the_desk_does(tmp_path):
    d = dm.BobDaemon(headless=True)
    handle = await _cat_session(d, tmp_path)
    term = d._pty.get(handle)
    ok, detail = await d.terminal_input("s1", "/low-priority", from_phone=True)
    assert (ok, detail) == (True, "")
    assert await _wait_for(lambda: b"/low-priority\r" in term.ring)
    ok, detail = await d.terminal_input("s1", "/clear", from_phone=True)
    assert (ok, detail) == (True, "")
    assert await _wait_for(lambda: b"/clear\r" in term.ring)
    ok, detail = await d.terminal_input("s1", "  /clear", from_phone=True)
    assert (ok, detail) == (True, "")
    assert await _wait_for(lambda: b"  /clear\r" in term.ring)
    ok, detail = await d.terminal_input("s1", "/clear", from_phone=False)
    assert (ok, detail) == (True, "")
    assert await _wait_for(lambda: term.ring.count(b"/clear\r") >= 2)
    await d._pty.close(handle)


@pytest.mark.asyncio
async def test_an_open_permission_prompt_refuses_on_both_doors(tmp_path, monkeypatch):
    d = dm.BobDaemon(headless=True)
    handle = await _cat_session(d, tmp_path)
    monkeypatch.setattr(d, "_prompts_by_session", lambda: {"s1": {"request_id": "r"}})
    for phone in (True, False):
        ok, detail = await d.terminal_input("s1", "yes", from_phone=phone)
        assert (ok, detail) == (False, dm.TERMINAL_PROMPT_REFUSAL)
    assert b"yes" not in d._pty.get(handle).ring
    await d._pty.close(handle)


@pytest.mark.asyncio
async def test_the_other_refusals_are_their_own_sentences(tmp_path):
    d = dm.BobDaemon(headless=True)
    assert await d.terminal_input("nobody", "x") == (False, dm.TERMINAL_NOT_OWNED_REFUSAL)
    handle = await _cat_session(d, tmp_path)
    assert await d.terminal_input("s1", "", from_phone=True) == (False, dm.TERMINAL_EMPTY_REFUSAL)
    assert await d.terminal_input("s1", "a\nb") == (False, dm.TERMINAL_ONE_LINE_REFUSAL)
    assert await d.terminal_input("s1", "x" * (dm.TERMINAL_MAX_INPUT_CHARS + 1)) == (
        False, dm.TERMINAL_TOO_LONG_REFUSAL)
    # A trailing newline is Enter, not a second line.
    ok, _ = await d.terminal_input("s1", "fine\n")
    assert ok
    await d._pty.close(handle)
    assert await d.terminal_input("s1", "x") == (False, dm.TERMINAL_NOT_OWNED_REFUSAL)
    sentences = {dm.TERMINAL_NOT_OWNED_REFUSAL, dm.TERMINAL_EXITED_REFUSAL,
                 dm.TERMINAL_PROMPT_REFUSAL,
                 dm.TERMINAL_EMPTY_REFUSAL, dm.TERMINAL_ONE_LINE_REFUSAL,
                 dm.TERMINAL_TOO_LONG_REFUSAL, dm.TERMINAL_CONTROL_REFUSAL,
                 dm.TERMINAL_BUSY_REFUSAL}
    assert len(sentences) == 8
    assert all("No VS Code window owns" not in s for s in sentences)
    # The busy sentence never claims the process has ended.
    assert "ended" not in dm.TERMINAL_BUSY_REFUSAL


@pytest.mark.asyncio
async def test_the_phone_is_refused_every_control_character(tmp_path):
    """Not just the two newlines: two Ctrl-Cs quit the CLI, Escape cancels a
    turn and an arrow sequence recalls history. `message_card`'s own test."""
    d = dm.BobDaemon(headless=True)
    handle = await _cat_session(d, tmp_path)
    term = d._pty.get(handle)
    for bad in ("\x03\x03", "quit\x1b", "\x1b[A", "tab\tbed", "del\x7f"):
        ok, detail = await d.terminal_input("s1", bad, from_phone=True)
        assert (ok, detail) == (False, dm.TERMINAL_CONTROL_REFUSAL), repr(bad)
    assert term.ring == b"" or b"\x1b[A" not in term.ring
    # Plain text with a trailing Enter is still fine from the phone.
    ok, _ = await d.terminal_input("s1", "plain words\n", from_phone=True)
    assert ok
    await d._pty.close(handle)


@pytest.mark.asyncio
async def test_a_partial_write_is_busy_never_ended(tmp_path, monkeypatch):
    """Darwin's pty input queue is ~1 KB. A line the pty takes only part of
    is queued and drained on the loop; a drain that runs out of time is
    `TERMINAL_BUSY_REFUSAL`, never `TERMINAL_EXITED_REFUSAL`. Over a pipe
    nobody reads, so the queue fills deterministically."""
    from dark_army_daemon.ptyhost import PtyTerminal
    from dark_army_daemon.vtgrid import Screen
    d = dm.BobDaemon(headless=True)
    r, w = os.pipe()
    os.set_blocking(w, False)
    host = d._pty
    host._loop = asyncio.get_running_loop()
    term = PtyTerminal(handle="pty-pipe", name="pipe", root=str(tmp_path), pid=0,
                       master=w, proc=None, screen=Screen(80, 24), cols=80, rows=24)
    host._terms[term.handle] = term
    assert host.bind(term.handle, "pp")
    d._session_states["pp"] = {"pid": 0, "last_event": 0, "state": "idle",
                               "provider": "claude"}
    monkeypatch.setattr(dm, "TERMINAL_DRAIN_SECONDS", 0.1)
    try:
        # Fill the pipe so the next line cannot land at once.
        assert host.write(term.handle, "y" * 300_000)
        assert host.pending(term.handle) > 0
        ok, detail = await d.terminal_input("pp", "hello", from_phone=True)
        assert (ok, detail) == (False, dm.TERMINAL_BUSY_REFUSAL)
        assert detail != dm.TERMINAL_EXITED_REFUSAL
        # The line was queued, not dropped: once somebody reads, it lands.
        os.set_blocking(r, False)
        got = bytearray()
        for _ in range(400):
            try:
                got += os.read(r, 65536)
            except BlockingIOError:
                pass
            await asyncio.sleep(0.005)
            if host.pending(term.handle) == 0:
                break
        assert await host.drain(term.handle, 1.0)
        while True:
            try:
                got += os.read(r, 65536)
            except BlockingIOError:
                break
        assert got.endswith(b"hello\r")
    finally:
        host._detach(term)
        host._terms.pop(term.handle, None)
        host._by_session.pop("pp", None)
        os.close(r)
        os.close(w)


@pytest.mark.asyncio
async def test_a_second_descendant_row_does_not_steal_the_terminal(tmp_path):
    """A background agent the dispatched session spawned descends from the
    same child pid. It must neither name an unnamed terminal nor re-point a
    named one — or the pane flaps between the two rows every frame."""
    d = dm.BobDaemon(headless=True)
    ok, detail, pid = await d._pty.start(str(tmp_path), ["/bin/cat"], "cat")
    assert ok, detail
    handle = d._pty.owns(pid)
    term = d._pty.get(handle)
    for sid in ("main", "bg", "later"):
        d._session_states[sid] = {"pid": pid, "last_event": 0, "state": "idle",
                                  "provider": "claude"}
    background = {"session_id": "bg", "pid": pid, "kind": "background"}
    # An unnamed terminal is not named after a background agent…
    assert d._pty_handle_for("bg", background) is None
    assert term.session_id == ""
    # …and without a row it is resolved but not remembered.
    assert d._pty_handle_for("later") == handle
    assert term.session_id == ""
    # A pipeline main agent names it.
    main = {"session_id": "main", "pid": pid, "kind": "interactive"}
    assert d._pty_handle_for("main", main) == handle
    assert term.session_id == "main"
    # And nothing re-points it afterwards, whatever its pid says.
    assert d._pty_handle_for("bg", background) is None
    assert d._pty_handle_for("later", {"session_id": "later", "pid": pid,
                                       "kind": "interactive"}) is None
    assert d._pty_handle_for("later") is None
    assert term.session_id == "main"
    assert d._pty.for_session("main") == handle
    await d._pty.close(handle)


@pytest.mark.asyncio
async def test_forgetting_a_session_lets_its_successor_take_the_terminal(tmp_path):
    """`/clear` fires `SessionEnd` then `SessionStart` with a fresh id in the
    same process. Once the old id is forgotten, the next enrich pass names
    the successor — still only a bindable row, so a background agent that
    descends from the same pid gets nothing."""
    d = dm.BobDaemon(headless=True)
    handle = await _cat_session(d, tmp_path, sid="old-sid")
    pid = d._session_states["old-sid"]["pid"]
    term = d._pty.get(handle)
    d._session_states["new-sid"] = {"pid": pid, "last_event": 0, "state": "idle",
                                    "provider": "claude"}
    successor = {"session_id": "new-sid", "pid": pid, "kind": "interactive"}
    # While Dark Army still holds the old id the terminal is not re-pointed.
    assert d._pty_handle_for("new-sid", successor) is None
    assert term.session_id == "old-sid"
    d._forget_session("old-sid", "clear")
    assert d._pty.for_session("old-sid") is None
    assert term.session_id == ""
    # A background agent still cannot take the freed name…
    background = {"session_id": "new-sid", "pid": pid, "kind": "background"}
    assert d._pty_handle_for("new-sid", background) is None
    assert term.session_id == ""
    # …and the successor's own row can.
    assert d._pty_handle_for("new-sid", successor) == handle
    assert term.session_id == "new-sid"
    assert d._pty.for_session("new-sid") == handle
    assert d.terminal_frame("new-sid")["available"] is True
    await d._pty.close(handle)


@pytest.mark.asyncio
async def test_an_exited_terminal_is_still_closable_by_its_session(tmp_path, monkeypatch):
    """After the child exits `owns(pid)` is None while Dark Army still holds the
    master for `EXITED_RETENTION_SECONDS`: `can_close` stays true and the
    close resolves the terminal by session name, never falling to VS Code's
    refusal."""
    from dark_army_daemon import vscode_reveal as vr
    monkeypatch.setattr(vr, "can_send_text", lambda pid: False)
    monkeypatch.setattr(vr, "can_close_terminal", lambda pid, tty="": False)

    async def never(*a, **k):
        raise AssertionError("fell to VS Code")
    monkeypatch.setattr(vr, "close_terminal", never)
    d = dm.BobDaemon(headless=True)
    handle = await _cat_session(d, tmp_path)
    term = d._pty.get(handle)
    os.kill(term.pid, signal.SIGKILL)
    assert await _wait_for(lambda: term.exited)
    assert d._pty.owns(term.pid) is None
    assert d._pty.get(handle) is not None
    # The ordinary exit path forgets the session within a tick; the name
    # must survive that, or the retention window has nothing to show.
    d._forget_session("s1", "ended")
    assert d._pty.for_session("s1") == handle
    assert d.terminal_frame("s1")["available"] is True
    stub = {"session_id": "s1", "pid": term.pid, "_category": "waiting", "cwd": ""}
    out = d._enrich_agent_stubs([dict(stub)])
    row = [r for b in out.values() for r in b if r["session_id"] == "s1"][0]
    assert row["own_terminal"] is True
    assert row["can_close"] is True
    assert row["can_terminal_input"] is False
    ok, detail = await d._close_session_terminal("s1")
    assert (ok, detail) == (True, "")
    assert d._pty.get(handle) is None
    assert d._pty.for_session("s1") is None
    assert "s1" not in d._session_states


def test_a_repeated_terminal_query_key_is_refused_in_words(server):
    srv, _daemon, _ports = server
    status, _ctype, body = srv._terminal_report_for(
        "session=a&session=b", resize=False)
    assert status == 400
    assert "repeat" in json.loads(body)["error"]
    status, _ctype, body = srv._terminal_report_for(
        "session=a&since=1&since=2", resize=False)
    assert status == 400


@pytest.mark.asyncio
async def test_a_row_publishes_own_terminal_and_can_terminal_input(tmp_path, monkeypatch):
    from dark_army_daemon import vscode_reveal as vr
    monkeypatch.setattr(vr, "can_send_text", lambda pid: False)
    monkeypatch.setattr(vr, "can_close_terminal", lambda pid, tty="": False)
    d = dm.BobDaemon(headless=True)
    handle = await _cat_session(d, tmp_path)
    pid = d._pty.get(handle).pid
    stub = {"session_id": "s1", "pid": pid, "_category": "running", "cwd": ""}
    out = d._enrich_agent_stubs([dict(stub)])
    row = [r for b in out.values() for r in b if r["session_id"] == "s1"][0]
    assert row["own_terminal"] is True
    assert row["can_terminal_input"] is True
    assert row["can_type"] is False        # still the stopped-category gate
    monkeypatch.setattr(d, "_prompts_by_session", lambda: {"s1": {}})
    out = d._enrich_agent_stubs([dict(stub)])
    row = [r for b in out.values() for r in b if r["session_id"] == "s1"][0]
    assert row["can_terminal_input"] is False
    assert row["own_terminal"] is True
    await d._pty.close(handle)


# --- the doors ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_kind_answers_at_home_and_away_with_no_lease(server, tmp_path):
    srv, daemon, (_loop_port, lan_port) = server
    daemon.lan_access_enabled = True
    daemon.remote_access_enabled = True
    await srv.start_lan()
    handle = await _cat_session(daemon, tmp_path)
    try:
        paired = await _pair_plain(lan_port)
        status, body = await _home_post(
            lan_port, paired["key"], "terminal", {"query": "session=s1&since=-1"}, 1)
        assert status == 200
        frame = json.loads(body)
        assert frame["available"] is True and frame["session"] == "s1"
        relay.set_lease_days(paired["device_id"], 0)
        assert relay.lease_valid(paired["device_id"]) is False
        status, _ctype, out = await srv._remote_run(
            "terminal", {"query": f"session=s1&since={frame['revision']}"},
            paired["device_id"])
        assert status == 200
        assert json.loads(out)["unchanged"] is True
        # Bad queries are 400 in words.
        status, body = await _home_post(
            lan_port, paired["key"], "terminal", {"query": "since=1"}, 2)
        assert status == 400 and "session" in body
        status, body = await _home_post(
            lan_port, paired["key"], "terminal", {"query": "session=s1&since=x"}, 3)
        assert status == 400
    finally:
        await daemon._pty.close(handle)


@pytest.mark.asyncio
async def test_the_write_on_the_home_door_lands_a_slash_and_replays_a_receipt(
        server, tmp_path, monkeypatch):
    srv, daemon, (_loop_port, lan_port) = server
    daemon.lan_access_enabled = True
    await srv.start_lan()
    handle = await _cat_session(daemon, tmp_path)
    try:
        paired = await _pair_plain(lan_port)
        writes = []
        real = daemon._pty.write
        monkeypatch.setattr(daemon._pty, "write",
                            lambda h, t: writes.append(t) or real(h, t))
        press = {"action": "terminal_input", "session_id": "s1",
                 "text": "/low-priority", "command_token": "tok-slash-1"}
        status, body = await _home_post(lan_port, paired["key"], "action", press, 1)
        assert status == 200 and json.loads(body)["ok"] is True
        status, body2 = await _home_post(lan_port, paired["key"], "action", press, 2)
        assert status == 200 and body2 == body
        assert writes == ["/low-priority\r"]
    finally:
        await daemon._pty.close(handle)


@pytest.mark.asyncio
async def test_the_write_away_rides_the_lease(server, tmp_path, monkeypatch):
    srv, daemon, (_loop_port, lan_port) = server
    daemon.lan_access_enabled = True
    daemon.remote_access_enabled = True
    await srv.start_lan()
    handle = await _cat_session(daemon, tmp_path)
    try:
        paired = await _pair_plain(lan_port)
        await _home_post(lan_port, paired["key"], "state", {}, 1)
        assert relay.lease_valid(paired["device_id"])
        body = {"action": "terminal_input", "session_id": "s1", "text": "from away"}
        status, _ctype, out = await srv._remote_run("action", dict(body), paired["device_id"])
        assert status == 200, out
        monkeypatch.setattr(relay, "lease_valid", lambda _d: False)
        status, _ctype, out = await srv._remote_run("action", dict(body), paired["device_id"])
        assert status == 403
        assert json.loads(out)["error"] == relay.LEASE_REFUSAL
    finally:
        await daemon._pty.close(handle)


@pytest.mark.asyncio
async def test_loopback_get_is_token_gated_and_host_checked(server, tmp_path):
    srv, daemon, (loop_port, lan_port) = server
    handle = await _cat_session(daemon, tmp_path)
    try:
        status, out = await fetch("/api/terminal?session=s1&since=-1", port=loop_port)
        assert status == 403
        status, out = await fetch("/api/terminal?session=s1&since=-1&cols=64&rows=16",
                                  port=loop_port, headers={"X-Bob-Token": srv.token})
        assert status == 200
        frame = json.loads(out)
        assert frame["available"] is True
        assert (frame["cols"], frame["rows"]) == (64, 16)
        status, out = await fetch("/api/terminal?session=s1&cols=1", port=loop_port,
                                  headers={"X-Bob-Token": srv.token})
        assert status == 400
        head = await _raw(
            b"GET /api/terminal?session=s1 HTTP/1.1\r\nHost: evil.example\r\n"
            + b"X-Bob-Token: " + srv.token.encode() + b"\r\n\r\n", loop_port)
        assert b"403" in head
        # Plaintext on the LAN door is the same 426 the other reads give.
        daemon.lan_access_enabled = True
        await srv.start_lan()
        status, _out = await fetch("/api/terminal?session=s1", port=lan_port,
                                   headers={"X-Bob-Token": srv.token})
        assert status == 426
    finally:
        await daemon._pty.close(handle)


@pytest.mark.asyncio
async def test_the_loopback_action_lets_the_desk_type_a_slash(server, tmp_path):
    srv, daemon, (loop_port, _lan_port) = server
    handle = await _cat_session(daemon, tmp_path)
    term = daemon._pty.get(handle)
    try:
        body = json.dumps({"action": "terminal_input", "session_id": "s1",
                           "text": "/status"}).encode()
        status, out = await fetch("/api/action", port=loop_port, data=body,
                                  headers={"X-Bob-Token": srv.token,
                                           "Content-Type": "application/json"})
        assert status == 200, out
        assert await _wait_for(lambda: b"/status\r" in term.ring)
        status, _out = await fetch("/api/action", port=loop_port, data=body)
        assert status == 403
    finally:
        await daemon._pty.close(handle)


# --- what was rebuilt ---------------------------------------------------------


def test_the_pane_heartbeat_owns_the_width_and_does_not_mute_a_banner():
    """The pane's own heartbeat is the whole fact — for the **width**.

    `_panel_focused_sessions()` used to be anded with `panel_visibility`,
    and since the panel's visibility heartbeat was removed nothing ever
    sends that verb `True`, so the width rule was inert. The pane states
    its session id only while it can be seen, so the heartbeat carries the
    visibility itself and the conjunct is gone from the predicate.

    It is **kept at the alert call site**, and that is the point of the
    second half of this test. Suppressing an alert is a far stronger claim
    than owning a width: the same drained list feeds the Mac's banner *and*
    the phone's push, and `client.visible` is true of a panel open on a
    monitor the person walked away from. Nothing asked for that, so alert
    behaviour is exactly what it was — inert, because production sends no
    `panel_visibility: True`.
    """
    import time
    d = dm.BobDaemon(headless=True)
    assert d._panel_focused_sessions() == set()
    d.note_panel_terminal("s1")
    # The width owner is live…
    assert d._panel_focused_sessions() == {"s1"}
    # …and the banner is not muted by it on its own.
    assert "s1" not in d._alert_suppressed()
    # No `panel_visibility: True` was sent — production never sends one.
    assert d._panel_visible is False
    # A panel that says it is on screen does suppress, as it always did.
    d.note_panel_visible(True)
    assert "s1" in d._alert_suppressed()
    d.note_panel_visible(False)
    assert "s1" not in d._alert_suppressed()
    # Stale is empty: a panel that died stops heartbeating.
    d._panel_terminal_at = time.time() - (dm.FRONTMOST_TRUST_SECONDS + 1)
    assert d._panel_focused_sessions() == set()
    # The pane moving off the session clears it at once (its `onDisappear`
    # and its `client.visible` change both send `""`).
    d.note_panel_terminal("s1")
    assert d._panel_focused_sessions() == {"s1"}
    d.note_panel_terminal("")
    assert d._panel_focused_sessions() == set()
    # And the EOF clear — the one `panel_visibility` production still sends
    # — takes nothing away that the heartbeat's own expiry does not.
    d.note_panel_terminal("s1")
    d.note_panel_visible(False)
    assert d._panel_focused_sessions() == {"s1"}


def test_the_width_owner_is_live_on_the_panels_own_message_path():
    """The wiring, not the arithmetic.

    Every other case here calls `note_panel_terminal` directly. This one
    goes the way production does — the panel's stdout line arriving at
    `BobCompanionApp._on_panel_action` — so removing the producer, or
    re-gating the predicate on a verb nobody sends, fails a test rather
    than silently handing the width to the phone.
    """
    import re
    from pathlib import Path

    from dark_army_menubar import app as A

    instance = object.__new__(A.BobCompanionApp)
    d = dm.BobDaemon(headless=True)
    instance._daemon = d
    # No `panel_visibility` at any point: the pane's heartbeat is all the
    # daemon hears, and it is enough.
    instance._on_panel_action("panel_terminal", "s1")
    assert d._panel_focused_sessions() == {"s1"}
    assert d.terminal_phone_resize("pty-x", "s1", 60, 20) is False
    instance._on_panel_action("panel_terminal", "")
    assert d._panel_focused_sessions() == set()
    assert d.terminal_phone_resize("pty-x", "s1", 60, 20) is True

    # And the producer at the other end of that pipe: the pane states the
    # session id only while the panel can be seen.
    pane = (Path(__file__).resolve().parents[2] / "panel" / "Sources"
            / "BobPanel" / "TerminalPane.swift").read_text()
    assert re.search(r'if client\.visible \{\s*\n\s*Panel\.send\(action: "panel_terminal", '
                     r'value: agent\.sessionId\)', pane), \
        "the heartbeat must stay gated on client.visible — it is the visibility"
    assert 'Panel.send(action: "panel_terminal", value: "")' in pane


def test_evaluate_unions_the_panel_focused_set():
    from dark_army_daemon.alerts import AlertPolicy
    policy = AlertPolicy()
    snapshot = {"waiting": [{"session_id": "s1", "nickname": "Gil", "project": "p",
                             "branch": "", "signals": []}],
                "running": [], "sleeping": [], "finished": []}
    cards = {"s1": {"session_id": "s1", "message": "?", "hook": "Notification"}}
    raised = policy.evaluate(snapshot, cards, 1000.0, panel_focused={"s1"})
    assert raised == []
    raised = policy.evaluate(snapshot, cards, 1000.0)
    assert [a.session_id for a in raised] == ["s1"]


def test_titles_skip_a_terminal_bob_owns():
    from dark_army_daemon.terminal_title import TitleWriter
    written = []
    w = TitleWriter(resolver=lambda pid: f"/dev/ttys{pid}", owned=lambda pid: pid == 2)
    w._write = lambda tty, title: written.append(tty) or True
    snap = {"running": [
        {"session_id": "a", "pid": 1, "nickname": "Ann", "name": "x", "project": "p",
         "idle_seconds": 0},
        {"session_id": "b", "pid": 2, "nickname": "Vex", "name": "y", "project": "p",
         "idle_seconds": 0},
    ], "waiting": [], "sleeping": []}
    w.apply(snap, None)
    assert written == ["/dev/ttys1"]
    d = dm.BobDaemon(headless=True)
    assert d._titles.owned == d._pty.owns


@pytest.mark.asyncio
async def test_reveal_of_a_pty_row_never_asks_vscode(tmp_path, monkeypatch):
    from dark_army_daemon import vscode_reveal as vr

    async def boom(pid):
        raise AssertionError("vscode reveal was asked for a pty row")
    monkeypatch.setattr(vr, "reveal", boom)
    d = dm.BobDaemon(headless=True)
    handle = await _cat_session(d, tmp_path)
    pid = d._pty.get(handle).pid
    monkeypatch.setattr(d, "_legacy_navigation_pid", lambda _inp: pid)
    ok, detail = await d.reveal_in_vscode("s1")
    assert ok is True and "panel" in detail
    await d._pty.close(handle)


def test_the_menu_bar_feeds_the_preference_and_the_focus_verb(monkeypatch):
    from dark_army_menubar import app as A
    from dark_army_menubar.preferences import DEFAULTS
    assert DEFAULTS["board_own_terminal"] is False
    assert "set_board_own_terminal" in A.BobCompanionApp.PANEL_ACTIONS
    src = __import__("inspect").getsource(A.BobCompanionApp._on_panel_action)
    assert 'name == "panel_terminal"' in src
    assert "note_panel_terminal" in src


# --- the away phone's feed: bounded raw bytes, a paint on first contact -------


def test_terminal_stream_supported_is_stated_on_the_board_snapshot():
    """The ninth marker beside `terminal_supported`: an older Mac sends no
    key, and the phone draws the terminal absent rather than a blank
    emulator waiting on a route that is not there."""
    d = dm.BobDaemon(headless=True)
    flags = d._pipeline_writable()
    assert flags["terminal_stream_supported"] is True
    assert flags["terminal_supported"] is True


@pytest.mark.asyncio
async def test_since_bytes_minus_one_is_a_paint_and_grid_zero_skips_the_rows(
        server, tmp_path):
    from dark_army_daemon.vtgrid import Screen
    srv, daemon, (_loop_port, lan_port) = server
    daemon.lan_access_enabled = True
    await srv.start_lan()
    handle = await _cat_session(daemon, tmp_path)
    term = daemon._pty.get(handle)
    try:
        daemon._pty.write(handle, "first contact\r")
        assert await _wait_for(lambda: b"first contact\r\n" in term.ring)
        paired = await _pair_plain(lan_port)
        status, body = await _home_post(
            lan_port, paired["key"], "terminal",
            {"query": "session=s1&grid=0&since_bytes=-1"}, 1)
        assert status == 200, body
        frame = json.loads(body)
        assert frame["available"] is True
        assert frame["painted"] is True
        assert frame["ring_overflowed"] is False
        assert frame["data_more"] is False
        assert frame["bytes_read"] == term.bytes_read
        assert frame["rows_changed"] == []
        assert "history" not in frame
        assert frame["unchanged"] is False
        data = base64.b64decode(frame["data"])
        assert data.startswith(b"\x1b[0m")           # a paint, never the ring
        t = Screen(term.cols, term.rows)
        t.feed(data)
        assert "first contact" in "".join(t.text())
        # `grid` is 0 or 1 and nothing else; the default keeps the rows.
        status, body = await _home_post(
            lan_port, paired["key"], "terminal", {"query": "session=s1&grid=2"}, 2)
        assert status == 400
        status, body = await _home_post(
            lan_port, paired["key"], "terminal", {"query": "session=s1&since=-1"}, 3)
        assert json.loads(body)["rows_changed"] != []
        status, body = await _home_post(
            lan_port, paired["key"], "terminal",
            {"query": "session=s1&since_bytes=-2"}, 4)
        assert status == 400
    finally:
        await daemon._pty.close(handle)


@pytest.mark.asyncio
async def test_the_away_phone_s_own_grid_sizes_the_pty_and_answers_a_paint(
        server, tmp_path):
    """Away the phone has no stream, so until 21 Sep 2026 the pty kept the
    Mac's width and every check-in replayed output laid out for a screen
    three times wider than the phone's — an unreadable smear. The sealed
    `terminal` read's `cols` / `rows` are the phone's own grid, applied
    through `terminal_phone_resize` (so the Mac's own pane still wins),
    and a size that took answers a paint whatever cursor was quoted."""
    srv, daemon, (_loop_port, lan_port) = server
    daemon.lan_access_enabled = True
    await srv.start_lan()
    handle = await _cat_session(daemon, tmp_path)
    term = daemon._pty.get(handle)
    try:
        daemon._pty.write(handle, "hello\r")
        assert await _wait_for(lambda: b"hello\r\n" in term.ring)
        paired = await _pair_plain(lan_port)
        status, body = await _home_post(
            lan_port, paired["key"], "terminal",
            {"query": "session=s1&grid=0&since_bytes=-1"}, 1)
        first = json.loads(body)
        assert status == 200 and first["painted"] is True
        cursor = first["bytes_read"]
        before = (term.cols, term.rows)
        want = (max(before[0] // 2, 20), max(before[1] - 4, 8))
        assert want != before
        # A cursor quoted with a new size: the pty takes it and the answer
        # is a paint, not the ring's bytes drawn for the old width.
        status, body = await _home_post(
            lan_port, paired["key"], "terminal",
            {"query": f"session=s1&grid=0&since_bytes={cursor}"
                      f"&cols={want[0]}&rows={want[1]}"}, 2)
        frame = json.loads(body)
        assert status == 200, body
        assert (term.cols, term.rows) == want
        assert (frame["cols"], frame["rows"]) == want
        assert frame["painted"] is True
        # The same size again is not a change: the ring leg, no paint.
        status, body = await _home_post(
            lan_port, paired["key"], "terminal",
            {"query": f"session=s1&grid=0&since_bytes={frame['bytes_read']}"
                      f"&cols={want[0]}&rows={want[1]}"}, 3)
        assert json.loads(body)["painted"] is False
        # The Mac's own pane showing this session owns the width: ignored,
        # and no paint is forced for a size that did not take.
        daemon.note_panel_terminal("s1")
        status, body = await _home_post(
            lan_port, paired["key"], "terminal",
            {"query": f"session=s1&grid=0&since_bytes={frame['bytes_read']}"
                      f"&cols={before[0]}&rows={before[1]}"}, 4)
        assert (term.cols, term.rows) == want
        assert json.loads(body)["painted"] is False
        # Out of bounds is still 400 in words.
        status, body = await _home_post(
            lan_port, paired["key"], "terminal",
            {"query": "session=s1&grid=0&since_bytes=-1&cols=1&rows=1"}, 5)
        assert status == 400
    finally:
        daemon.note_panel_terminal("")
        await daemon._pty.close(handle)


@pytest.mark.asyncio
async def test_the_raw_leg_is_bounded_and_data_more_says_the_rest_is_waiting(
        tmp_path, monkeypatch):
    d = dm.BobDaemon(headless=True)
    handle = await _cat_session(d, tmp_path)
    term = d._pty.get(handle)
    try:
        monkeypatch.setattr(dm, "TERMINAL_POLL_RAW_BYTES", 16)
        d._pty.write(handle, "0123456789abcdefghijklmnop\r")
        assert await _wait_for(lambda: b"0123456789abcdefghijklmnop\r\n" in term.ring)
        ring = bytes(term.ring)
        first = d.terminal_frame("s1", -1, since_bytes=0, grid=False)
        data = base64.b64decode(first["data"])
        assert len(data) == 16 and data == ring[:16]
        assert first["data_more"] is True
        assert first["painted"] is False and first["ring_overflowed"] is False
        assert first["bytes_read"] == 16
        second = d.terminal_frame("s1", -1, since_bytes=first["bytes_read"], grid=False)
        rest = base64.b64decode(second["data"])
        assert data + rest == ring[:16 + len(rest)]
        assert second["bytes_read"] == 16 + len(rest)
        # Nothing new: empty data, the cursor stands.
        third = d.terminal_frame("s1", -1, since_bytes=term.bytes_read, grid=False)
        assert third["data"] == "" and third["bytes_read"] == term.bytes_read
        assert third["data_more"] is False and third["painted"] is False
    finally:
        await d._pty.close(handle)


@pytest.mark.asyncio
async def test_a_cursor_that_fell_off_the_ring_is_answered_with_a_paint(tmp_path):
    from dark_army_daemon.vtgrid import Screen
    d = dm.BobDaemon(headless=True)
    handle = await _cat_session(d, tmp_path)
    term = d._pty.get(handle)
    try:
        d._pty.write(handle, "gone\r")
        assert await _wait_for(lambda: b"gone\r\n" in term.ring)
        # The ring kept only its tail: a cursor at 0 is behind its start.
        term.ring = bytearray(term.ring[-2:])
        frame = d.terminal_frame("s1", -1, since_bytes=0, grid=False)
        assert frame["painted"] is True and frame["ring_overflowed"] is True
        assert frame["bytes_read"] == term.bytes_read
        data = base64.b64decode(frame["data"])
        assert data != bytes(term.ring)
        t = Screen(term.cols, term.rows)
        t.feed(data)
        assert "gone" in "".join(t.text())
        # A cursor from a previous life of the pty (ahead of what was read)
        # paints too, rather than leaving the phone stuck ahead for ever.
        ahead = d.terminal_frame("s1", -1, since_bytes=term.bytes_read + 100, grid=False)
        assert ahead["painted"] is True and ahead["ring_overflowed"] is True
        assert ahead["bytes_read"] == term.bytes_read
    finally:
        await d._pty.close(handle)


@pytest.mark.asyncio
async def test_raw_bytes_on_the_sealed_write_take_the_desks_rules(server, tmp_path, monkeypatch):
    srv, daemon, (_loop_port, lan_port) = server
    daemon.lan_access_enabled = True
    await srv.start_lan()
    handle = await _cat_session(daemon, tmp_path)
    try:
        paired = await _pair_plain(lan_port)
        writes = []
        real = daemon._pty.write
        monkeypatch.setattr(
            daemon._pty, "write",
            lambda h, text="", data=None: writes.append((text, data)) or real(h, text, data=data))
        # A control byte, raw: typed (the emulator on the phone shows what
        # it does). Escape rather than Ctrl-C, which would end `cat`.
        press = {"action": "terminal_input", "session_id": "s1",
                 "bytes": base64.b64encode(b"\x1b").decode(), "command_token": "tok-raw-1"}
        status, body = await _home_post(lan_port, paired["key"], "action", press, 1)
        assert status == 200 and json.loads(body)["ok"] is True, body
        assert writes == [("", b"\x1b")]
        # The same byte as text: the line route keeps its refusal.
        press = {"action": "terminal_input", "session_id": "s1", "text": "\x1b"}
        status, body = await _home_post(lan_port, paired["key"], "action", press, 2)
        assert status == 409 and json.loads(body)["detail"] == dm.TERMINAL_CONTROL_REFUSAL
        # Bytes that are not base64 are 400 in words, never typed as text.
        press = {"action": "terminal_input", "session_id": "s1", "bytes": "%%%"}
        status, body = await _home_post(lan_port, paired["key"], "action", press, 3)
        assert status == 400 and "base64" in json.loads(body)["error"]
        assert len(writes) == 1
        # Raw bytes are typed during an open permission prompt (the dialog
        # is on the phone's screen); the line route still refuses.
        monkeypatch.setattr(daemon, "_prompts_by_session",
                            lambda: {"s1": {"request_id": "r"}})
        press = {"action": "terminal_input", "session_id": "s1",
                 "bytes": base64.b64encode(b"1").decode()}
        status, body = await _home_post(lan_port, paired["key"], "action", press, 4)
        assert status == 200
        press = {"action": "terminal_input", "session_id": "s1", "text": "1"}
        status, body = await _home_post(lan_port, paired["key"], "action", press, 5)
        assert status == 409 and json.loads(body)["detail"] == dm.TERMINAL_PROMPT_REFUSAL
    finally:
        await daemon._pty.close(handle)


@pytest.mark.asyncio
async def test_the_width_has_one_owner_at_a_time(tmp_path):
    d = dm.BobDaemon(headless=True)
    handle = await _cat_session(d, tmp_path)
    term = d._pty.get(handle)
    try:
        before = (term.cols, term.rows)
        # The panel's pane is showing s1: the phone's size is ignored. The
        # pane's heartbeat is the whole statement — no `panel_visibility`,
        # which production never sends.
        d.note_panel_terminal("s1")
        assert d._panel_focused_sessions() == {"s1"}
        assert d.terminal_phone_resize(handle, "s1", 60, 20) is False
        assert (term.cols, term.rows) == before
        # The pane moved off it: the phone's size lands.
        d.note_panel_terminal("")
        assert d.terminal_phone_resize(handle, "s1", 60, 20) is True
        assert (term.cols, term.rows) == (60, 20)
        # The panel states 140x36 and remembers it; the phone borrows the
        # width while the pane is away; the pane coming back takes it back
        # without restating anything.
        d.note_panel_terminal("s1")
        d.terminal_stream_resize(handle, 140, 36)
        assert (term.cols, term.rows) == (140, 36)
        assert d._panel_pane_size[handle] == (140, 36)
        d.note_panel_terminal("")
        assert d.terminal_phone_resize(handle, "s1", 60, 20) is True
        assert (term.cols, term.rows) == (60, 20)
        d.note_panel_terminal("s1")
        assert (term.cols, term.rows) == (140, 36)
        # A hidden panel is not a window anybody is looking at, and the pane
        # says so itself: `client.visible` going false sends `""`.
        d.note_panel_terminal("")
        assert d.terminal_phone_resize(handle, "s1", 70, 21) is True
        assert (term.cols, term.rows) == (70, 21)
        # A restated focus with no change in the focused set re-applies
        # nothing more than the one reclaim.
        d.note_panel_terminal("s1")
        assert (term.cols, term.rows) == (140, 36)
        d.note_panel_terminal("s1")
        assert (term.cols, term.rows) == (140, 36)
        assert d.terminal_phone_resize(handle, "s1", 70, 21) is False
    finally:
        await d._pty.close(handle)


def test_the_phone_resize_of_an_unknown_handle_is_harmless():
    d = dm.BobDaemon(headless=True)
    assert d.terminal_phone_resize("pty-none", "s1", 80, 24) is True
    assert d.terminal_phone_resize("pty-none", "s1", "x", 24) is False


# --- the away paint, bounded ---------------------------------------------------


@pytest.mark.asyncio
async def test_the_away_paint_is_bounded_and_its_remainder_arrives_on_the_next_polls(
        tmp_path, monkeypatch):
    """A paint over `TERMINAL_POLL_RAW_BYTES` is served in slices: the first
    `painted: true` with `data_more`, every later one `painted: false` under
    the same cursor, and the slices concatenate to `Screen.paint()` byte for
    byte. After the last, the ring leg resumes from the anchor."""
    d = dm.BobDaemon(headless=True)
    handle = await _cat_session(d, tmp_path)
    term = d._pty.get(handle)
    try:
        d._pty.write(handle, "\x1b[31ma picture\x1b[0m of sorts\r")
        assert await _wait_for(lambda: b"of sorts\r\n" in term.ring)
        monkeypatch.setattr(dm, "TERMINAL_POLL_RAW_BYTES", 40)
        paint = term.screen.paint()
        assert len(paint) > 40 * 2
        anchor = term.bytes_read
        first = d.terminal_frame("s1", -1, since_bytes=-1, grid=False)
        assert first["painted"] is True and first["data_more"] is True
        assert first["ring_overflowed"] is False
        assert first["bytes_read"] == anchor
        slices = [base64.b64decode(first["data"])]
        assert len(slices[0]) == 40
        assert d._terminal_paint_tail[(handle, "")][0] == anchor
        assert d._terminal_paint_tail[(handle, "")][2] == paint[40:]
        frame = first
        while frame["data_more"]:
            frame = d.terminal_frame("s1", -1, since_bytes=frame["bytes_read"], grid=False)
            assert frame["painted"] is False and frame["ring_overflowed"] is False
            assert frame["bytes_read"] == anchor
            piece = base64.b64decode(frame["data"])
            assert 0 < len(piece) <= 40
            slices.append(piece)
        assert b"".join(slices) == paint
        assert len(slices) == -(-len(paint) // 40)
        assert (handle, "") not in d._terminal_paint_tail
        # The ring leg resumes from the anchor: nothing new, the cursor stands.
        after = d.terminal_frame("s1", -1, since_bytes=anchor, grid=False)
        assert after["data"] == "" and after["painted"] is False
        assert after["data_more"] is False and after["bytes_read"] == anchor
        # And what the terminal prints next rides the ring from there.
        d._pty.write(handle, "next\r")
        assert await _wait_for(lambda: b"next\r\n" in term.ring)
        moved = d.terminal_frame("s1", -1, since_bytes=anchor, grid=False)
        assert moved["painted"] is False
        assert base64.b64decode(moved["data"]) == bytes(term.ring)[-(term.bytes_read - anchor):]
    finally:
        await d._pty.close(handle)


@pytest.mark.asyncio
async def test_a_busy_terminal_does_not_restart_a_drain_under_way(
        tmp_path, monkeypatch):
    """The tail is served above the ring test: bytes printed between polls
    do not turn the next slice into a fresh paint, and a cursor that would
    have fallen off the ring is answered from the tail first."""
    d = dm.BobDaemon(headless=True)
    handle = await _cat_session(d, tmp_path)
    term = d._pty.get(handle)
    try:
        d._pty.write(handle, "busy\r")
        assert await _wait_for(lambda: b"busy\r\n" in term.ring)
        monkeypatch.setattr(dm, "TERMINAL_POLL_RAW_BYTES", 30)
        paint = term.screen.paint()
        first = d.terminal_frame("s1", -1, since_bytes=-1, grid=False)
        anchor = first["bytes_read"]
        assert first["painted"] is True and first["data_more"] is True
        # The terminal prints more, and the ring even loses its head.
        d._pty.write(handle, "more\r")
        assert await _wait_for(lambda: b"more\r\n" in term.ring)
        term.ring = bytearray(term.ring[-2:])
        second = d.terminal_frame("s1", -1, since_bytes=anchor, grid=False)
        assert second["painted"] is False and second["ring_overflowed"] is False
        assert base64.b64decode(second["data"]) == paint[30:60]
        assert second["bytes_read"] == anchor
    finally:
        await d._pty.close(handle)


@pytest.mark.asyncio
async def test_a_stale_anchor_drops_the_tail_and_a_forgotten_handle_leaves_none(
        tmp_path, monkeypatch):
    """A poll quoting any other cursor takes the ordinary branch and the
    remainder is let go; a handle the pty has forgotten, or a tail older
    than `TERMINAL_PAINT_TAIL_SECONDS`, is pruned on the next pass."""
    d = dm.BobDaemon(headless=True)
    handle = await _cat_session(d, tmp_path)
    term = d._pty.get(handle)
    try:
        d._pty.write(handle, "stale\r")
        assert await _wait_for(lambda: b"stale\r\n" in term.ring)
        monkeypatch.setattr(dm, "TERMINAL_POLL_RAW_BYTES", 30)
        first = d.terminal_frame("s1", -1, since_bytes=-1, grid=False)
        anchor = first["bytes_read"]
        assert first["data_more"] is True and (handle, "") in d._terminal_paint_tail
        # Another cursor: the ring leg, and the tail is gone.
        other = d.terminal_frame("s1", -1, since_bytes=0, grid=False)
        assert other["painted"] is False
        assert base64.b64decode(other["data"]) == bytes(term.ring)[:30]
        assert (handle, "") not in d._terminal_paint_tail
        # Quoting the anchor now is an ordinary ring read, not a slice.
        again = d.terminal_frame("s1", -1, since_bytes=anchor, grid=False)
        assert again["data"] == "" and again["painted"] is False
        # A fresh paint replaces any tail; an old one is pruned by age.
        fresh = d.terminal_frame("s1", -1, since_bytes=-1, grid=False)
        assert fresh["data_more"] is True
        a, stamped, rest = d._terminal_paint_tail[(handle, "")]
        d._terminal_paint_tail[(handle, "")] = (
            a, stamped - dm.TERMINAL_PAINT_TAIL_SECONDS - 1, rest)
        expired = d.terminal_frame("s1", -1, since_bytes=anchor, grid=False)
        assert expired["painted"] is False and expired["data"] == ""
        assert (handle, "") not in d._terminal_paint_tail
        # A -1 without a remainder leaves no entry behind either.
        monkeypatch.setattr(dm, "TERMINAL_POLL_RAW_BYTES", 10 ** 6)
        whole = d.terminal_frame("s1", -1, since_bytes=-1, grid=False)
        assert whole["painted"] is True and whole["data_more"] is False
        assert (handle, "") not in d._terminal_paint_tail
        # A handle the pty no longer knows is dropped on the next pass —
        # every viewer's entry, since the prune is by handle.
        d._terminal_paint_tail[("gone-handle", "")] = (0, 10 ** 12, b"x")
        d._terminal_paint_tail[("gone-handle", "phone-b")] = (0, 10 ** 12, b"y")
        d.terminal_frame("s1", -1, since_bytes=-1, grid=False)
        assert not any(k[0] == "gone-handle" for k in d._terminal_paint_tail)
        assert dm.TERMINAL_PAINT_TAIL_SECONDS == 120.0
    finally:
        await d._pty.close(handle)


@pytest.mark.asyncio
async def test_two_viewers_drain_the_same_paint_independently(
        tmp_path, monkeypatch):
    """Every paired phone polls the same handle, so the remainder is keyed
    per viewer: a second phone's `-1` neither replaces the first's tail nor
    steals its next slice, and each gets the whole paint byte for byte —
    interleaved, and with one starting mid-way through the other's drain."""
    d = dm.BobDaemon(headless=True)
    handle = await _cat_session(d, tmp_path)
    term = d._pty.get(handle)
    try:
        d._pty.write(handle, "\x1b[34mtwo phones\x1b[0m one screen\r")
        assert await _wait_for(lambda: b"one screen\r\n" in term.ring)
        monkeypatch.setattr(dm, "TERMINAL_POLL_RAW_BYTES", 32)
        paint = term.screen.paint()
        assert len(paint) > 32 * 3
        got = {"phone-a": b"", "phone-b": b""}
        frames = {}
        frames["phone-a"] = d.terminal_frame(
            "s1", -1, since_bytes=-1, grid=False, viewer="phone-a")
        got["phone-a"] += base64.b64decode(frames["phone-a"]["data"])
        # A is one slice in; B opens the same terminal now.
        frames["phone-b"] = d.terminal_frame(
            "s1", -1, since_bytes=-1, grid=False, viewer="phone-b")
        got["phone-b"] += base64.b64decode(frames["phone-b"]["data"])
        assert frames["phone-a"]["painted"] and frames["phone-b"]["painted"]
        assert (handle, "phone-a") in d._terminal_paint_tail
        assert (handle, "phone-b") in d._terminal_paint_tail
        assert d._terminal_paint_tail[(handle, "phone-a")][2] == paint[32:]
        # Both drain, turn about, each quoting its own anchor.
        while frames["phone-a"]["data_more"] or frames["phone-b"]["data_more"]:
            for who in ("phone-a", "phone-b"):
                if not frames[who]["data_more"]:
                    continue
                frames[who] = d.terminal_frame(
                    "s1", -1, since_bytes=frames[who]["bytes_read"],
                    grid=False, viewer=who)
                assert frames[who]["painted"] is False
                assert frames[who]["ring_overflowed"] is False
                got[who] += base64.b64decode(frames[who]["data"])
        assert got["phone-a"] == paint
        assert got["phone-b"] == paint
        assert not d._terminal_paint_tail
        # Loopback is its own viewer, `""`, untouched by either phone.
        desk = d.terminal_frame("s1", -1, since_bytes=-1, grid=False)
        assert desk["painted"] is True and desk["data_more"] is True
        assert list(d._terminal_paint_tail) == [(handle, "")]
    finally:
        await d._pty.close(handle)


@pytest.mark.asyncio
async def test_the_bounded_paint_rides_the_sealed_door_whole(server, tmp_path, monkeypatch):
    """The phone's own route: the slices arrive over several sealed `terminal`
    reads, each under the poll bound, and rebuild the screen."""
    from dark_army_daemon.vtgrid import Screen
    srv, daemon, (_loop_port, lan_port) = server
    daemon.lan_access_enabled = True
    await srv.start_lan()
    handle = await _cat_session(daemon, tmp_path)
    term = daemon._pty.get(handle)
    try:
        daemon._pty.write(handle, "\x1b[32mgreen\x1b[0m on the phone\r")
        assert await _wait_for(lambda: b"on the phone\r\n" in term.ring)
        monkeypatch.setattr(dm, "TERMINAL_POLL_RAW_BYTES", 48)
        paint = term.screen.paint()
        paired = await _pair_plain(lan_port)
        ctr = 1
        status, body = await _home_post(
            lan_port, paired["key"], "terminal",
            {"query": "session=s1&grid=0&since_bytes=-1"}, ctr)
        assert status == 200, body
        frame = json.loads(body)
        assert frame["painted"] is True and frame["data_more"] is True
        got = base64.b64decode(frame["data"])
        assert len(got) == 48
        # The remainder is filed under this phone's own device id — the
        # one the seal proved, never the loopback's `""`.
        assert list(daemon._terminal_paint_tail) == [(handle, paired["device_id"])]
        while frame["data_more"]:
            ctr += 1
            status, body = await _home_post(
                lan_port, paired["key"], "terminal",
                {"query": f"session=s1&grid=0&since_bytes={frame['bytes_read']}"}, ctr)
            assert status == 200, body
            frame = json.loads(body)
            assert frame["painted"] is False
            got += base64.b64decode(frame["data"])
        assert got == paint
        t = Screen(term.cols, term.rows)
        t.feed(got)
        assert "green on the phone" in "".join(t.text())
    finally:
        await daemon._pty.close(handle)
