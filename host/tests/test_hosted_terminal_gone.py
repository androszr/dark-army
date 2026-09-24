"""A session in a terminal Dark Army hosts dies with that terminal — and the
daemon must say so, not draw it as a session running in an editor.

The incident (2026-09-12 15:09): a sibling agent `pkill`ed the pty broker
while rebuilding. The broker closed its terminals on the way out, the claude
session inside one of them died, and after the relaunch `sessions.json`
brought the row back in `waiting` with its pending question. The pid does not
survive a restart, the pidless path spares waiters, and the broker's fresh
hello listed nothing — so the row sat under Needs you for the whole staleness
window reading "runs in the editor — find it in the editor yourself", about a
terminal that had never been in an editor.

What must hold: naming a terminal for a session marks the session `hosted`
on its own state, and that mark rides `sessions.json`; after the broker
attach at startup a hosted session the broker no longer lists is forgotten
with the `terminal closed` verdict, its question dropped, before the first
snapshot; `_check_liveness` applies the same rung to a pidless hosted row;
and nothing is judged while the link to the broker is down.
"""

import json
import time

import pytest

from dark_army_daemon import ptyhost, session_store
from dark_army_daemon.daemon import BobDaemon, TERMINAL_CLOSED_REASON
from dark_army_daemon.ptyhost import PtyHost, PtyTerminal, Screen


SID = "a1d2caf4-c7be-43ab-abc4-f441ea5c5995"


def _hosted_daemon(tmp_path, *, persist=False):
    daemon = BobDaemon(sessions_path=tmp_path / "sessions.json")
    daemon._pty = PtyHost(persist=persist, sock_path=str(tmp_path / "pty.sock"))
    return daemon


def _terminal(host, handle="t1", pid=4242):
    term = PtyTerminal(handle=handle, name="agent", root="/p", pid=pid,
                       master=-1, proc=None, screen=Screen(80, 24),
                       cols=80, rows=24)
    host._terms[handle] = term
    host._by_pid[pid] = handle
    return term


def _waiting(daemon, sid=SID, **extra):
    daemon._session_states[sid] = {
        "state": "waiting", "project": "p", "cwd": "/p", "provider": "claude",
        "last_event": time.time(), "last_event_monotonic": time.monotonic(),
        **extra}
    return daemon._session_states[sid]


# --- the mark --------------------------------------------------------------


def test_naming_a_terminal_marks_the_session_hosted(tmp_path):
    daemon = _hosted_daemon(tmp_path)
    _terminal(daemon._pty)
    state = _waiting(daemon, pid=4242)
    assert "hosted" not in state
    row = {"session_id": SID, "kind": "interactive", "pid": 4242,
           "provider": "claude"}
    assert daemon._pty_handle_for(SID, row) == "t1"
    assert state["hosted"] is True


def test_an_already_named_terminal_marks_it_too(tmp_path):
    """The restart path: the broker's hello names the session, so the first
    resolve goes through `for_session` and never binds."""
    daemon = _hosted_daemon(tmp_path)
    _terminal(daemon._pty)
    daemon._pty.bind("t1", SID)
    state = _waiting(daemon)
    assert daemon._pty_handle_for(SID) == "t1"
    assert state["hosted"] is True


def test_the_mark_survives_sessions_json(tmp_path):
    daemon = _hosted_daemon(tmp_path)
    _waiting(daemon, pid=4242, hosted=True)
    daemon._persist_sessions()
    loaded = session_store.load_sessions(tmp_path / "sessions.json")
    assert loaded[SID]["hosted"] is True
    assert "pid" not in loaded[SID]          # the existing rule, unchanged


# --- the verdict -----------------------------------------------------------


def test_a_hosted_session_the_broker_no_longer_lists_is_retired(tmp_path):
    daemon = _hosted_daemon(tmp_path)
    _waiting(daemon, hosted=True)
    daemon._pending_questions[SID] = {"question": "which?", "options": []}
    assert daemon._retire_hostless_sessions() == [SID]
    assert SID not in daemon._session_states
    assert SID not in daemon._pending_questions
    assert daemon._finished[SID]["end_reason"] == TERMINAL_CLOSED_REASON
    on_disk = json.loads((tmp_path / "sessions.json").read_text())
    assert SID not in on_disk["sessions"]


def test_a_hosted_session_whose_terminal_came_back_is_kept(tmp_path):
    daemon = _hosted_daemon(tmp_path)
    _terminal(daemon._pty)
    daemon._pty.bind("t1", SID)
    _waiting(daemon, hosted=True)
    assert daemon._retire_hostless_sessions() == []
    assert SID in daemon._session_states


def test_an_exited_terminal_still_held_by_the_broker_is_not_gone(tmp_path):
    """The broker keeps an exited terminal until somebody closes it, and so
    does the row: the existing close and eviction paths own that."""
    daemon = _hosted_daemon(tmp_path)
    term = _terminal(daemon._pty)
    daemon._pty.bind("t1", SID)
    term.exited_at = 1.0
    _waiting(daemon, hosted=True)
    assert daemon._retire_hostless_sessions() == []


def test_an_unhosted_session_is_never_judged_by_the_broker(tmp_path):
    daemon = _hosted_daemon(tmp_path)
    _waiting(daemon)
    assert daemon._retire_hostless_sessions() == []
    assert SID in daemon._session_states


def test_nothing_is_judged_while_the_broker_link_is_down(tmp_path):
    """On the persistent path an empty map between a drop and the reconnect
    is not a list of zero terminals."""
    daemon = _hosted_daemon(tmp_path, persist=True)
    assert daemon._pty.connected is False
    _waiting(daemon, hosted=True)
    assert daemon._hostless_hosted_ids() == []
    assert daemon._retire_hostless_sessions() == []
    assert SID in daemon._session_states


def test_connected_is_the_writer_on_the_persistent_path(tmp_path):
    class _Writer:
        closing = False
        def is_closing(self): return self.closing
    host = PtyHost(persist=True, sock_path=str(tmp_path / "pty.sock"))
    host._writer = _Writer()
    assert host.connected is True
    host._writer.closing = True
    assert host.connected is False
    assert PtyHost(persist=False).connected is True


# --- the liveness rung -----------------------------------------------------


def test_liveness_retires_a_pidless_hosted_waiter_at_once(tmp_path, monkeypatch):
    daemon = _hosted_daemon(tmp_path)
    _waiting(daemon, hosted=True)
    monkeypatch.setattr(daemon, "_refresh_grok_records", lambda: None)
    monkeypatch.setattr(daemon, "_roster_pid", lambda sid: None)
    # A plain waiter is spared for the whole staleness window; a hosted one
    # whose terminal is gone is not.
    assert daemon._check_liveness() == [SID]
    assert daemon._finished[SID]["end_reason"] == TERMINAL_CLOSED_REASON


def test_liveness_still_spares_a_pidless_waiter_that_was_never_hosted(
        tmp_path, monkeypatch):
    daemon = _hosted_daemon(tmp_path)
    _waiting(daemon)
    monkeypatch.setattr(daemon, "_refresh_grok_records", lambda: None)
    monkeypatch.setattr(daemon, "_roster_pid", lambda sid: None)
    assert daemon._check_liveness() == []


def test_liveness_leaves_a_hosted_row_with_a_live_pid_to_the_identity_check(
        tmp_path, monkeypatch):
    """A hosted session resumed in an editor after its terminal went: the pid
    is present and alive, so the broker's list is not consulted — the
    identity check is the judge, as for every other pidful row."""
    daemon = _hosted_daemon(tmp_path)
    _waiting(daemon, hosted=True, pid=4242)
    monkeypatch.setattr(daemon, "_refresh_grok_records", lambda: None)
    monkeypatch.setattr(daemon, "_roster_pid", lambda sid: None)
    monkeypatch.setattr("dark_army_daemon.daemon._still_our_process",
                        lambda pid, provider: True)
    assert daemon._check_liveness() == []
    assert SID in daemon._session_states


# --- the restart, end to end -----------------------------------------------


@pytest.mark.asyncio
async def test_restart_with_an_empty_broker_drops_the_row_before_any_snapshot(
        tmp_path, monkeypatch):
    """`run()`'s own order: attach, then retire, then the pushers start."""
    first = _hosted_daemon(tmp_path)
    _waiting(first, hosted=True, pid=4242)
    first._persist_sessions()

    second = BobDaemon(sessions_path=tmp_path / "sessions.json")
    assert SID in second._session_states           # the prune spares a waiter
    second._pty = PtyHost(persist=False)            # a broker with nothing
    assert second._retire_hostless_sessions() == [SID]
    assert SID not in second._session_states
    assert second._finished[SID]["end_reason"] == TERMINAL_CLOSED_REASON


def test_run_retires_after_the_attach(monkeypatch):
    """Pinned by reading `run()`: the retire follows the attach and precedes
    the first task, so no frame can carry the dead row."""
    import inspect
    src = inspect.getsource(BobDaemon.run)
    attach = src.index("await self._pty.attach()")
    retire = src.index("self._retire_hostless_sessions()")
    first_task = src.index("self._staleness_task = asyncio.create_task(")
    assert attach < retire < first_task
