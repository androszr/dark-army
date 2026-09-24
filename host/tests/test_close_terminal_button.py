"""The panel wrap-up button closes the terminal tab rather than typing /clear.

A new verb, a new flag, a new route. wrap_up_session is left typing /clear as
the fallback behind `wrap_up_or_close_session`, which is what the `wrap_up` API
action — sent only by stale close-out.sh copies — now reaches.
"""
import asyncio
import inspect
from pathlib import Path

import pytest

from dark_army_daemon.codex_rollouts import CodexProcessIdentity, CodexRecord
from dark_army_daemon.daemon import BobDaemon


def _daemon(monkeypatch, closed=None, result=None):
    from dark_army_daemon import vscode_reveal as vr

    async def _close(pid, tty):
        if closed is not None:
            closed.append((pid, tty))
        return result

    monkeypatch.setattr(vr, "close_terminal", _close)
    daemon = BobDaemon(headless=True)
    daemon._session_states["s1"] = {"pid": 4242, "last_event": 0}
    return daemon


def _codex_record(thread_id, *, match_kind="explicit_resume",
                  parent_thread_id="", pid=4242):
    identity = None
    if match_kind:
        identity = CodexProcessIdentity(
            pid=pid, executable="/usr/local/bin/codex", cwd="/code/bob",
            create_time=1.0, argv=("codex", "resume", thread_id),
            match_kind=match_kind,
        )
    return CodexRecord(
        session_id=f"codex:{thread_id}", thread_id=thread_id,
        path=Path(f"{thread_id}.jsonl"), cwd="/code/bob",
        pid=pid, process_identity=identity,
        parent_thread_id=parent_thread_id,
        originator="codex-tui", source_kind="cli", thread_source="user",
        revision=(10, 20), last_event=1.0,
    )


def test_a_permission_prompt_blocks_the_close(monkeypatch):
    closed = []
    daemon = _daemon(monkeypatch, closed, {"matched": True, "closed": True})
    monkeypatch.setattr(daemon, "_prompts_by_session",
                        lambda: {"s1": {"request_id": "r1"}})
    ok, detail = asyncio.run(daemon.close_session_terminal("s1"))
    assert ok is False and "permission prompt" in detail
    assert closed == []


def test_no_pid_comes_back_as_a_person_readable_refusal(monkeypatch):
    daemon = _daemon(monkeypatch, result={"matched": True, "closed": True})
    daemon._session_states["s2"] = {"last_event": 0}
    ok, detail = asyncio.run(daemon.close_session_terminal("s2"))
    assert ok is False
    assert detail == "Dark Army has no process on record for this session."
    assert "no pid" not in detail


def test_no_matching_window_comes_back_as_a_person_readable_refusal(monkeypatch):
    daemon = _daemon(monkeypatch, result=None)
    ok, detail = asyncio.run(daemon.close_session_terminal("s1"))
    assert ok is False
    assert detail == ("No VS Code window owns this session's terminal, "
                      "so Dark Army cannot close it.")
    assert "0.1.9" not in detail


def test_a_claude_row_is_dismissed_and_forgotten_as_closed(monkeypatch):
    closed = []
    daemon = _daemon(monkeypatch, closed, {"matched": True, "closed": True,
                                           "terminalName": "claude"})
    daemon._active_notifications["s1"] = {"session_id": "s1",
                                          "message": "waiting for you"}
    ok, detail = asyncio.run(daemon.close_session_terminal("s1"))
    assert (ok, detail) == (True, "")
    assert closed == [(4242, "")]
    assert "s1" not in daemon._session_states
    assert "s1" not in daemon._active_notifications
    assert daemon._finished["s1"]["end_reason"] == "closed"


def test_a_grok_leader_pid_is_refused_and_nothing_is_closed(monkeypatch):
    from dark_army_daemon import daemon as daemon_mod

    closed = []
    daemon = _daemon(monkeypatch, closed, {"matched": True, "closed": True})
    daemon._session_states["grok-1"] = {
        "pid": 111, "last_event": 0, "provider": "grok", "state": "idle",
    }
    monkeypatch.setattr(daemon, "_ensure_session_pid", lambda sid, st: 111)
    monkeypatch.setattr(daemon_mod, "_grok_session_pid_ok", lambda pid: False)
    ok, detail = asyncio.run(daemon.close_session_terminal("grok-1"))
    assert ok is False
    assert detail == "Dark Army has no process on record for this session."
    assert closed == []
    assert "grok-1" in daemon._session_states


@pytest.mark.asyncio
async def test_a_codex_explicit_resume_row_closes_the_matched_pid(monkeypatch):
    """The snapshot pid is not aimed at — Stop's live process is. Hide waits
    until that pid is gone, or a shutdown flush restores the row."""
    from types import SimpleNamespace

    from dark_army_daemon import vscode_reveal as vr
    from dark_army_daemon import codex_rollouts

    closed = []
    confirmed = []
    matched = []

    async def _close(pid, tty):
        closed.append((pid, tty))
        return {"matched": True, "closed": True, "terminalName": "codex"}

    async def _confirm(session_id, identity, reason="stopped"):
        confirmed.append((session_id, identity, reason))

    def _match(record, identity, destructive=False, records=None):
        matched.append({"destructive": destructive, "pid": identity.pid})
        return SimpleNamespace(pid=5555)

    monkeypatch.setattr(vr, "close_terminal", _close)
    monkeypatch.setattr(codex_rollouts, "matching_process_identity", _match)
    daemon = BobDaemon(headless=True)
    monkeypatch.setattr(daemon, "_confirm_codex_stop", _confirm)
    record = _codex_record("abc", match_kind="explicit_resume", pid=4242)
    daemon._codex_records[record.session_id] = record
    daemon._active_notifications[record.session_id] = {
        "session_id": record.session_id, "message": "waiting for you",
    }
    ok, detail = await daemon.close_session_terminal(record.session_id)
    await asyncio.sleep(0)
    assert (ok, detail) == (True, "")
    assert closed == [(5555, "")]
    assert matched == [{"destructive": True, "pid": 4242}]
    assert confirmed == [(record.session_id, record.process_identity, "closed")]
    assert daemon._closed_by_bob(record.session_id)
    assert record.session_id in daemon._codex_records
    assert record.session_id not in daemon._hidden_codex
    assert record.session_id not in daemon._active_notifications


@pytest.mark.asyncio
async def test_a_codex_close_settles_once_the_pid_is_gone(monkeypatch):
    from types import SimpleNamespace

    from dark_army_daemon import daemon as daemon_mod
    from dark_army_daemon import vscode_reveal as vr
    from dark_army_daemon import codex_rollouts

    monkeypatch.setattr(codex_rollouts, "load_recent", lambda: [])
    monkeypatch.setattr(
        codex_rollouts, "matching_process_identity",
        lambda *a, **k: SimpleNamespace(pid=5555))

    async def _close(pid, tty):
        return {"matched": True, "closed": True, "terminalName": "codex"}

    monkeypatch.setattr(vr, "close_terminal", _close)
    daemon = BobDaemon(headless=True)
    record = _codex_record("abc", match_kind="explicit_resume", pid=4242)
    daemon._codex_records[record.session_id] = record
    real_confirm = daemon._confirm_codex_stop
    scheduled = []

    async def capture(session_id, identity, reason="stopped"):
        scheduled.append((session_id, identity, reason))

    monkeypatch.setattr(daemon, "_confirm_codex_stop", capture)
    ok, detail = await daemon.close_session_terminal(record.session_id)
    await asyncio.sleep(0)
    assert (ok, detail) == (True, "")
    assert scheduled == [(record.session_id, record.process_identity, "closed")]
    assert record.session_id in daemon._codex_records

    async def instant(_delay):
        return None

    monkeypatch.setattr(daemon_mod.asyncio, "sleep", instant)
    monkeypatch.setattr(daemon_mod.psutil, "pid_exists", lambda pid: False)
    await real_confirm(record.session_id, record.process_identity, reason="closed")
    assert record.session_id not in daemon._codex_records
    assert record.session_id in daemon._hidden_codex
    assert daemon._closed_by_bob(record.session_id)
    assert daemon._finished[record.session_id]["end_reason"] == "closed"


@pytest.mark.asyncio
async def test_a_codex_identity_mismatch_refuses_and_closes_nothing(monkeypatch):
    from dark_army_daemon import daemon as daemon_mod
    from dark_army_daemon import vscode_reveal as vr
    from dark_army_daemon import codex_rollouts

    closed = []

    async def _close(pid, tty):
        closed.append((pid, tty))
        return {"matched": True, "closed": True}

    monkeypatch.setattr(vr, "close_terminal", _close)
    monkeypatch.setattr(
        codex_rollouts, "matching_process_identity",
        lambda *a, **k: None)
    monkeypatch.setattr(daemon_mod.psutil, "pid_exists", lambda pid: True)
    daemon = BobDaemon(headless=True)
    record = _codex_record("abc", match_kind="explicit_resume", pid=4242)
    daemon._codex_records[record.session_id] = record
    ok, detail = await daemon.close_session_terminal(record.session_id)
    assert ok is False
    assert "identity changed" in detail
    assert "codex identity changed" not in detail
    assert closed == []
    assert record.session_id in daemon._codex_records
    assert record.session_id not in daemon._hidden_codex


@pytest.mark.asyncio
async def test_a_codex_pid_already_gone_settles_without_closing(monkeypatch):
    from dark_army_daemon import daemon as daemon_mod
    from dark_army_daemon import vscode_reveal as vr
    from dark_army_daemon import codex_rollouts

    closed = []

    async def _close(pid, tty):
        closed.append((pid, tty))
        return {"matched": True, "closed": True}

    monkeypatch.setattr(vr, "close_terminal", _close)
    monkeypatch.setattr(
        codex_rollouts, "matching_process_identity",
        lambda *a, **k: None)
    monkeypatch.setattr(daemon_mod.psutil, "pid_exists", lambda pid: False)
    monkeypatch.setattr(codex_rollouts, "load_recent", lambda: [])
    daemon = BobDaemon(headless=True)
    record = _codex_record("abc", match_kind="explicit_resume", pid=4242)
    daemon._codex_records[record.session_id] = record
    daemon._active_notifications[record.session_id] = {
        "session_id": record.session_id, "message": "waiting for you",
    }
    ok, detail = await daemon.close_session_terminal(record.session_id)
    assert (ok, detail) == (True, "")
    assert closed == []
    assert record.session_id not in daemon._codex_records
    assert record.session_id in daemon._hidden_codex
    assert record.session_id not in daemon._active_notifications
    assert daemon._closed_by_bob(record.session_id)
    assert daemon._finished[record.session_id]["end_reason"] == "closed"


def test_a_codex_unique_cwd_row_is_refused(monkeypatch):
    from dark_army_daemon import codex_rollouts

    closed = []
    daemon = _daemon(monkeypatch, closed, {"matched": True, "closed": True})
    monkeypatch.setattr(
        codex_rollouts, "matching_process_identity",
        lambda *a, **k: pytest.fail("unique_cwd must not reach matching"))
    record = _codex_record("uniq", match_kind="unique_cwd", pid=7002)
    daemon._codex_records[record.session_id] = record
    ok, detail = asyncio.run(daemon.close_session_terminal(record.session_id))
    assert ok is False
    assert detail == "Dark Army has no process on record for this session."
    assert closed == []
    assert record.session_id in daemon._codex_records


def test_a_codex_child_row_is_refused(monkeypatch):
    from dark_army_daemon import codex_rollouts

    closed = []
    daemon = _daemon(monkeypatch, closed, {"matched": True, "closed": True})
    monkeypatch.setattr(
        codex_rollouts, "matching_process_identity",
        lambda *a, **k: pytest.fail("a child row must not reach matching"))
    record = _codex_record("child", match_kind="explicit_resume",
                           parent_thread_id="parent", pid=8001)
    daemon._codex_records[record.session_id] = record
    ok, detail = asyncio.run(daemon.close_session_terminal(record.session_id))
    assert ok is False
    assert detail == "Dark Army has no process on record for this session."
    assert closed == []
    assert record.session_id in daemon._codex_records


def test_wrap_up_session_still_types_clear_and_does_not_close():
    src = inspect.getsource(BobDaemon.wrap_up_session)
    assert "send_text" in src
    assert "close_terminal" not in src


def test_can_close_is_true_for_a_waiting_claude_row(monkeypatch):
    from dark_army_daemon import vscode_reveal as vr

    asked = []

    def _can(pid, tty=""):
        asked.append(pid)
        return True

    monkeypatch.setattr(vr, "can_close_terminal", _can)
    monkeypatch.setattr(vr, "can_send_text", lambda pid, tty="": True)
    daemon = BobDaemon(headless=True)
    stubs = [
        {"session_id": "s1", "pid": 4242, "_category": "waiting", "cwd": ""},
    ]
    out = daemon._enrich_agent_stubs(stubs)
    assert out["waiting"][0]["can_close"] is True
    assert asked == [4242]


def test_can_close_is_false_for_a_running_row_and_is_not_asked(monkeypatch):
    from dark_army_daemon import vscode_reveal as vr

    asked = []

    def _can(pid, tty=""):
        asked.append(pid)
        return True

    monkeypatch.setattr(vr, "can_close_terminal", _can)
    monkeypatch.setattr(vr, "can_send_text", lambda pid, tty="": True)
    daemon = BobDaemon(headless=True)
    stubs = [
        {"session_id": "s2", "pid": 4343, "_category": "running", "cwd": ""},
    ]
    out = daemon._enrich_agent_stubs(stubs)
    assert out["running"][0]["can_close"] is False
    assert asked == []


def test_can_close_is_false_for_codex_without_can_stop(monkeypatch):
    from dark_army_daemon import vscode_reveal as vr

    asked = []

    def _can(pid, tty=""):
        asked.append(pid)
        return True

    monkeypatch.setattr(vr, "can_close_terminal", _can)
    monkeypatch.setattr(vr, "can_send_text", lambda pid, tty="": True)
    daemon = BobDaemon(headless=True)
    stubs = [
        {"session_id": "codex:a", "pid": 4242, "_category": "waiting",
         "cwd": "", "provider": "codex", "can_stop": False},
    ]
    out = daemon._enrich_agent_stubs(stubs)
    assert out["waiting"][0]["can_close"] is False
    assert asked == []


def test_can_close_is_true_for_codex_with_can_stop(monkeypatch):
    from dark_army_daemon import vscode_reveal as vr

    asked = []

    def _can(pid, tty=""):
        asked.append(pid)
        return True

    monkeypatch.setattr(vr, "can_close_terminal", _can)
    monkeypatch.setattr(vr, "can_send_text", lambda pid, tty="": True)
    daemon = BobDaemon(headless=True)
    stubs = [
        {"session_id": "codex:b", "pid": 4242, "_category": "waiting",
         "cwd": "", "provider": "codex", "can_stop": True},
    ]
    out = daemon._enrich_agent_stubs(stubs)
    assert out["waiting"][0]["can_close"] is True
    assert asked == [4242]


def test_can_close_is_false_for_a_background_row(monkeypatch):
    from dark_army_daemon import vscode_reveal as vr

    asked = []

    def _can(pid, tty=""):
        asked.append(pid)
        return True

    monkeypatch.setattr(vr, "can_close_terminal", _can)
    monkeypatch.setattr(vr, "can_send_text", lambda pid, tty="": True)
    daemon = BobDaemon(headless=True)
    stubs = [
        {"session_id": "bg", "pid": 4242, "_category": "waiting",
         "cwd": "", "kind": "background"},
    ]
    out = daemon._enrich_agent_stubs(stubs)
    assert out["waiting"][0]["can_close"] is False
    assert asked == []


@pytest.mark.asyncio
async def test_the_api_exposes_close_terminal_and_reports_its_refusal():
    from dark_army_daemon import api_server

    class _Daemon:
        def __init__(self):
            self.close_calls = []
            self.wrap_calls = []

        async def close_session_terminal(self, session_id, *, by_person=False):
            self.close_calls.append((session_id, by_person))
            return False, "No VS Code window owns this session's terminal."

        async def wrap_up_session(self, session_id):
            self.wrap_calls.append(session_id)
            return True, ""

    daemon = _Daemon()
    server = api_server.ApiServer(daemon)
    server.token = "t"
    request = api_server._Request(
        "POST", "/api/action", "",
        {"x-bob-token": "t"},
        b'{"action": "close_terminal", "session_id": "s1"}')
    # `(session_id, by_person)`; the flag is absent here, which is
    # `close-out.sh`'s own route and therefore today's behaviour.
    assert server._close_terminal_request(request) == ("s1", False)
    status, _ctype, body = await server._close_terminal("s1")
    assert status == 409 and b"VS Code" in body
    assert daemon.close_calls == [("s1", False)]
    assert daemon.wrap_calls == []


def test_an_unauthorised_close_terminal_is_not_routed():
    from dark_army_daemon import api_server

    server = api_server.ApiServer(object())
    server.token = "t"
    request = api_server._Request(
        "POST", "/api/action", "", {},
        b'{"action": "close_terminal", "session_id": "s1"}')
    assert server._close_terminal_request(request) is None


@pytest.mark.asyncio
async def test_wrap_up_reaches_wrap_up_or_close():
    """The route re-pointed: neither daemon verb is called directly."""
    from dark_army_daemon import api_server

    class _Daemon:
        def __init__(self):
            self.either_calls = []
            self.wrap_calls = []
            self.close_calls = []

        async def wrap_up_or_close_session(self, session_id):
            self.either_calls.append(session_id)
            return True, ""

        async def wrap_up_session(self, session_id):
            self.wrap_calls.append(session_id)
            return True, ""

        async def close_session_terminal(self, session_id):
            self.close_calls.append(session_id)
            return True, ""

    daemon = _Daemon()
    server = api_server.ApiServer(daemon)
    server.token = "t"
    request = api_server._Request(
        "POST", "/api/action", "",
        {"x-bob-token": "t"},
        b'{"action": "wrap_up", "session_id": "s1"}')
    assert server._wrap_up_request(request) == "s1"
    status, _ctype, _body = await server._wrap_up("s1")
    assert status == 200
    assert daemon.either_calls == ["s1"]
    assert daemon.wrap_calls == []
    assert daemon.close_calls == []


# ── wrap_up_or_close_session: close first, /clear only as a fallback ─────────


def _stubbed(monkeypatch, close_result, wrap_result=(True, "")):
    """The daemon with both verbs replaced by recorders."""
    daemon = _daemon(monkeypatch)
    calls = {"close": [], "wrap": []}

    async def _close(session_id):
        calls["close"].append(session_id)
        return close_result

    async def _wrap(session_id):
        calls["wrap"].append(session_id)
        return wrap_result

    monkeypatch.setattr(daemon, "close_session_terminal", _close)
    monkeypatch.setattr(daemon, "wrap_up_session", _wrap)
    return daemon, calls


def test_wrap_up_with_the_gate_off_only_clears(monkeypatch):
    daemon, calls = _stubbed(monkeypatch, (True, ""), (True, ""))
    daemon.board_close_terminal_enabled = False
    assert asyncio.run(daemon.wrap_up_or_close_session("s1")) == (True, "")
    assert calls == {"close": [], "wrap": ["s1"]}


def test_wrap_up_with_the_gate_on_closes_and_says_so(monkeypatch, caplog):
    daemon, calls = _stubbed(monkeypatch, (True, ""))
    daemon.board_close_terminal_enabled = True
    with caplog.at_level("INFO", logger="dark-army"):
        assert asyncio.run(daemon.wrap_up_or_close_session("s1")) == (True, "")
    assert calls == {"close": ["s1"], "wrap": []}
    assert "old close-out script" in caplog.text


def test_wrap_up_never_clears_a_prompt_blocked_session(monkeypatch):
    from dark_army_daemon.daemon import PROMPT_BLOCKED_REFUSAL

    daemon, calls = _stubbed(monkeypatch, (False, PROMPT_BLOCKED_REFUSAL))
    daemon.board_close_terminal_enabled = True
    ok, detail = asyncio.run(daemon.wrap_up_or_close_session("s1"))
    assert (ok, detail) == (False, PROMPT_BLOCKED_REFUSAL)
    assert calls == {"close": ["s1"], "wrap": []}


def test_wrap_up_never_clears_when_codex_identity_changed(monkeypatch):
    from dark_army_daemon.daemon import CODEX_IDENTITY_REFUSAL

    daemon, calls = _stubbed(monkeypatch, (False, CODEX_IDENTITY_REFUSAL))
    daemon.board_close_terminal_enabled = True
    ok, detail = asyncio.run(daemon.wrap_up_or_close_session("s1"))
    assert (ok, detail) == (False, CODEX_IDENTITY_REFUSAL)
    assert calls == {"close": ["s1"], "wrap": []}


def test_wrap_up_falls_back_to_clear_on_another_refusal(monkeypatch):
    daemon, calls = _stubbed(
        monkeypatch,
        (False, "Dark Army has no process on record for this session."),
        (False, "No PID on record for this session."))
    daemon.board_close_terminal_enabled = True
    ok, detail = asyncio.run(daemon.wrap_up_or_close_session("s1"))
    assert (ok, detail) == (False, "No PID on record for this session.")
    assert calls == {"close": ["s1"], "wrap": ["s1"]}


def test_wrap_up_on_a_real_prompt_refuses_in_the_shared_words(monkeypatch):
    """Unstubbed: the daemon's own prompt gate, the shared constant, and no
    keystroke or close anywhere near the session."""
    from dark_army_daemon import vscode_reveal as vr
    from dark_army_daemon.daemon import PROMPT_BLOCKED_REFUSAL

    closed = []
    typed = []
    daemon = _daemon(monkeypatch, closed, {"matched": True, "closed": True})

    async def _send(pid, tty, text):
        typed.append((pid, text))
        return {"sent": True}

    monkeypatch.setattr(vr, "send_text", _send)
    monkeypatch.setattr(daemon, "_prompts_by_session",
                        lambda: {"s1": {"request_id": "r1"}})
    daemon.board_close_terminal_enabled = True
    ok, detail = asyncio.run(daemon.wrap_up_or_close_session("s1"))
    assert (ok, detail) == (False, PROMPT_BLOCKED_REFUSAL)
    assert closed == []
    assert typed == []


@pytest.mark.asyncio
async def test_codex_navigation_proof_never_grants_stop_close_or_typing(tmp_path, monkeypatch):
    from tests.test_agents_poll import _navigation_daemon
    from unittest.mock import AsyncMock, Mock
    from dark_army_daemon import vscode_reveal
    daemon, roots, _processes = _navigation_daemon(tmp_path, monkeypatch)
    close, send, signal = AsyncMock(), AsyncMock(), Mock()
    monkeypatch.setattr(vscode_reveal, "close_terminal", close)
    monkeypatch.setattr(vscode_reveal, "send_text", send)
    monkeypatch.setattr("os.kill", signal)
    root = roots[0]
    assert not daemon.stop_codex_session(root.session_id)[0]
    assert not (await daemon.close_session_terminal(root.session_id))[0]
    assert not (await daemon.wrap_up_session(root.session_id))[0]
    close.assert_not_called()
    send.assert_not_called()
    signal.assert_not_called()


from tests.test_board_refine import refinement_handoff  # noqa: E402,F401 — shared pytest fixture


@pytest.mark.asyncio
async def test_refinement_closes_once_and_never_schedules_signals(refinement_handoff, monkeypatch):
    d, store, card, plan, records, processes, posts = refinement_handoff
    sid = records[0].session_id
    for name in ('_confirm_codex_stop', 'stop_codex_session', 'wrap_up_session'):
        monkeypatch.setattr(d, name, lambda *a, **k: pytest.fail('signal/clear escalation'))
    attached, _ = await d.attach_plan_by_session(sid, str(plan))
    assert attached
    results = await asyncio.gather(d.close_refinement_terminal(sid), d.close_refinement_terminal(sid))
    assert sorted(ok for ok, _ in results) == [False, True]
    assert len(posts) == 1 and posts[0]['pid'] == 701
    assert d._closed_ids[sid][1:] == (701, 100.0)
    assert d._finished[sid]['end_reason'] == 'closed'
    d._refresh_codex_records()  # exiting process/journal flush cannot resurrect it
    assert sid not in d._codex_records and records[1].session_id in d._codex_records
    assert store.get(card['id'])['column_name'] == 'backlog'


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', [
    'missing_receipt', 'expiry', 'restart', 'move', 'move_back', 'reset', 'delete',
    'start', 'started', 'plan_missing', 'plan_replace', 'root', 'journal_replace',
    'question', 'permission', 'subagent', 'new_turn', 'unknown_turn', 'shared_tty',
    'holder_gone', 'scan_failed', 'argv', 'created', 'cwd', 'tty',
])
async def test_refinement_refusals_close_nothing(refinement_handoff, monkeypatch, fault):
    import json
    from dataclasses import replace
    from dark_army_daemon import codex_rollouts
    d, store, card, plan, records, processes, posts = refinement_handoff
    sid = records[0].session_id
    assert (await d.attach_plan_by_session(sid, str(plan)))[0]
    if fault in ('missing_receipt', 'restart'):
        d._refinement_receipts.clear()
    elif fault == 'expiry':
        d._refinement_receipts[sid] = replace(d._refinement_receipts[sid], expires=0)
    elif fault in ('move', 'move_back'):
        store.update(card['id'], {'column_name': 'done'})
        if fault == 'move_back': store.update(card['id'], {'column_name': 'backlog'})
    elif fault == 'reset':
        await d.reset_card(card['id'])
    elif fault == 'delete':
        store.delete(card['id'])
    elif fault in ('start', 'started'):
        store.update(card['id'], {'link_state': 'dispatching' if fault == 'start' else 'live',
                                  'session_id': '' if fault == 'start' else 'implementation'})
    elif fault == 'plan_missing':
        plan.unlink()
    elif fault == 'plan_replace':
        plan.unlink(); plan.symlink_to('/etc/hosts')
    elif fault == 'root':
        store.update(card['id'], {'root': '/else'})
    elif fault == 'journal_replace':
        old = records[0].path.read_bytes(); records[0].path.unlink(); records[0].path.write_bytes(old)
    elif fault == 'question':
        records[0].stats.question = {'id': 'q'}
    elif fault == 'permission':
        monkeypatch.setattr(d, '_prompts_by_session', lambda: {sid: {'request_id': 'q'}})
    elif fault == 'subagent':
        records[0].stats.agents['child'] = codex_rollouts.AgentInfo(agent_id='child', activity='running')
    elif fault in ('new_turn', 'unknown_turn'):
        with records[0].path.open('a') as f:
            f.write(json.dumps({'type': 'event_msg', 'payload': {'type': 'task_started',
                         'turn_id': 'turn-2' if fault == 'new_turn' else ''}}) + '\n')
    elif fault == 'shared_tty':
        processes[1].info['terminal'] = processes[1].fresh['terminal'] = 'ttys040'
    elif fault == 'holder_gone':
        processes.pop(0)
    elif fault == 'scan_failed':
        monkeypatch.setattr(codex_rollouts, '_process_snapshot', lambda: None)
    else:
        key, value = {'argv': ('cmdline', ['codex', 'resume', 'other']),
                      'created': ('create_time', 222), 'cwd': ('cwd', '/else'),
                      'tty': ('terminal', 'ttys099')}[fault]
        processes[0].fresh[key] = value
    ok, detail = await d.close_refinement_terminal(sid)
    assert not ok and detail
    assert posts == [] and sid not in d._closed_ids


@pytest.mark.asyncio
@pytest.mark.parametrize('reply', [None, {}, {'matched': True}, {'matched': 1, 'closed': True},
                                  {'matched': True, 'closed': 1}, {'matched': False, 'closed': True}])
async def test_refinement_unconfirmed_response_consumes_without_retry(refinement_handoff, monkeypatch, reply):
    from dark_army_daemon import vscode_reveal
    d, store, card, plan, records, processes, posts = refinement_handoff
    sid = records[0].session_id
    assert (await d.attach_plan_by_session(sid, str(plan)))[0]
    async def post(*args, **kwargs):
        if kwargs.get('before_write') and not await kwargs['before_write']():
            return None
        posts.append(args)
        return reply
    monkeypatch.setattr(vscode_reveal, '_post_json', post)
    assert not (await d.close_refinement_terminal(sid))[0]
    assert not (await d.close_refinement_terminal(sid))[0]
    assert len(posts) == 1 and sid not in d._closed_ids and sid in d._codex_records


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['record_generation', 'new_question', 'new_child', 'new_turn', 'store_edit'])
async def test_refinement_rechecks_after_editor_preparation(refinement_handoff, monkeypatch, fault):
    from copy import deepcopy
    from dark_army_daemon import vscode_reveal, codex_rollouts
    d, store, card, plan, records, processes, posts = refinement_handoff
    sid = records[0].session_id
    assert (await d.attach_plan_by_session(sid, str(plan)))[0]
    original = vscode_reveal._session_in_vscode
    def prepare(pid):
        if fault == 'record_generation': d._codex_records[sid] = deepcopy(records[0])
        elif fault == 'new_question': records[0].stats.question = {'id': 'q'}
        elif fault == 'new_child':
            d._codex_records['codex:child'] = codex_rollouts.CodexRecord(
                session_id='codex:child', thread_id='child', path=plan,
                parent_thread_id=records[0].thread_id, turn_active=True)
        elif fault == 'new_turn': records[0].turn_id = 'turn-2'
        else: store.update(card['id'], {'title': 'Human changed scope'})
        return original(pid)
    monkeypatch.setattr(vscode_reveal, '_session_in_vscode', prepare)
    assert not (await d.close_refinement_terminal(sid))[0]
    assert posts == []


@pytest.mark.asyncio
@pytest.mark.parametrize('active', [False, True])
async def test_retained_planner_child_only_blocks_while_turn_active(refinement_handoff, active):
    import json
    from dark_army_daemon import codex_rollouts
    d, store, card, plan, records, processes, posts = refinement_handoff
    parent = records[0]
    path = plan.parent / 'child.jsonl'
    rows = [
        {'type': 'session_meta', 'payload': {'id': 'child', 'cwd': parent.cwd,
         'source': {'subagent': {'thread_spawn': {'parent_thread_id': parent.thread_id}}}}},
        {'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': 'child-turn'}},
    ]
    if not active:
        rows.append({'type': 'event_msg', 'payload': {'type': 'task_complete', 'turn_id': 'child-turn'}})
    path.write_text('\n'.join(json.dumps(r) for r in rows) + '\n')
    child = codex_rollouts.parse_rollout(path)
    assert child.parent_thread_id == parent.thread_id and child.turn_active is active
    records.append(child)
    d._codex_records[child.session_id] = child
    parent.stats.agents['child'] = codex_rollouts.AgentInfo(agent_id='child', activity=child.activity)
    assert (await d.attach_plan_by_session(parent.session_id, str(plan)))[0]
    ok, detail = await d.close_refinement_terminal(parent.session_id)
    assert ok is not active, detail
    assert len(posts) == (0 if active else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation', ['reset', 'delete', 'move', 'start'])
async def test_refinement_disposal_serializes_board_mutations(refinement_handoff, monkeypatch, mutation):
    from dark_army_daemon import dispatch, vscode_reveal
    d, store, card, plan, records, processes, posts = refinement_handoff
    sid = records[0].session_id
    assert (await d.attach_plan_by_session(sid, str(plan)))[0]
    # A valid Start after the close is allowed. Keep it independent of the
    # machine's installed Codex and workspace.windows' cross-test cache, and
    # record spawning separately from the close transport we are testing.
    starts = []
    monkeypatch.setattr(d, '_known_project_roots', lambda: {card['root']})
    monkeypatch.setattr(dispatch, 'resolve_executable', lambda tool: '/fixture/codex')

    async def spawn(root, argv, name, **kwargs):
        assert closing.done(), 'Start reached spawning before the close completed'
        assert [post['op'] for post in posts] == ['close_refinement_terminal']
        starts.append(root)
        return True, 'fixture terminal', None

    monkeypatch.setattr(dispatch, 'spawn', spawn)
    entered, release = asyncio.Event(), asyncio.Event()
    original = vscode_reveal._post_json
    async def post(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)
    monkeypatch.setattr(vscode_reveal, '_post_json', post)
    closing = asyncio.create_task(d.close_refinement_terminal(sid))
    await entered.wait()
    calls = {'reset': lambda: d.reset_card(card['id']), 'delete': lambda: d.delete_card(card['id']),
             'move': lambda: d.update_card(card['id'], {'column_name': 'done'}),
             'start': lambda: d.dispatch_card(card['id'])}
    changing = asyncio.create_task(calls[mutation]())
    await asyncio.sleep(0.02)
    assert not changing.done()
    assert not starts
    assert store.get(card['id'])['column_name'] == 'backlog'
    release.set()
    assert (await closing)[0]
    changed = await changing
    assert len(posts) == 1
    assert starts == ([card['root']] if mutation == 'start' else [])
    if mutation == 'start':
        assert changed[0]


@pytest.mark.asyncio
async def test_timed_out_observation_cannot_close_later(refinement_handoff, monkeypatch):
    import threading
    from dark_army_daemon import codex_rollouts, daemon as daemon_mod
    d, store, card, plan, records, processes, posts = refinement_handoff
    sid = records[0].session_id
    assert (await d.attach_plan_by_session(sid, str(plan)))[0]
    gate = threading.Event()
    original = codex_rollouts.refinement_close_observation
    def slow(*args):
        gate.wait(1)
        return original(*args)
    monkeypatch.setattr(codex_rollouts, 'refinement_close_observation', slow)
    monkeypatch.setattr(daemon_mod, 'REFINEMENT_CLOSE_TIMEOUT', 0.02)
    ok, detail = await d.close_refinement_terminal(sid)
    gate.set()
    await asyncio.sleep(0.05)
    assert not ok and 'timed out' in detail and posts == []


@pytest.mark.asyncio
@pytest.mark.parametrize('target', ['parent', 'child'])
async def test_new_turn_during_final_holder_scan_never_disposes(refinement_handoff, monkeypatch, target):
    import json
    from dark_army_daemon import codex_rollouts
    d, store, card, plan, records, processes, posts = refinement_handoff
    parent = records[0]
    path = parent.path
    if target == 'child':
        path = plan.parent / 'completed-child.jsonl'
        rows = [
            {'type': 'session_meta', 'payload': {'id': 'child', 'cwd': parent.cwd,
             'source': {'subagent': {'thread_spawn': {'parent_thread_id': parent.thread_id}}}}},
            {'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': 'child-turn'}},
            {'type': 'event_msg', 'payload': {'type': 'task_complete', 'turn_id': 'child-turn'}},
        ]
        path.write_text('\n'.join(json.dumps(r) for r in rows) + '\n')
        child = codex_rollouts.parse_rollout(path)
        records.append(child)
        d._codex_records[child.session_id] = child
    assert (await d.attach_plan_by_session(parent.session_id, str(plan)))[0]
    original = codex_rollouts.resolve_navigation_proofs
    calls = 0
    def inspect(*args, **kwargs):
        nonlocal calls
        proof = original(*args, **kwargs)
        calls += 1
        if calls == 2:
            with path.open('a') as journal:
                journal.write(json.dumps({'type': 'event_msg', 'payload': {
                    'type': 'task_started', 'turn_id': 'new-human-turn'}}) + '\n')
        return proof
    monkeypatch.setattr(codex_rollouts, 'resolve_navigation_proofs', inspect)
    ok, detail = await d.close_refinement_terminal(parent.session_id)
    assert calls == 2 and not ok and posts == []
    assert parent.session_id not in d._closed_ids


@pytest.mark.asyncio
@pytest.mark.parametrize('grandchild_active', [False, True])
@pytest.mark.parametrize('lagging_stats', [False, True])
async def test_refinement_validates_whole_descendant_tree(refinement_handoff, grandchild_active, lagging_stats):
    import json
    from dark_army_daemon import codex_rollouts
    d, store, card, plan, records, processes, posts = refinement_handoff
    parent = records[0]
    for tid, parent_id, active in [('child', parent.thread_id, False),
                                   ('grandchild', 'child', grandchild_active)]:
        path = plan.parent / (tid + '.jsonl')
        rows = [
            {'type': 'session_meta', 'payload': {'id': tid, 'cwd': parent.cwd,
             'source': {'subagent': {'thread_spawn': {'parent_thread_id': parent_id}}}}},
            {'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': tid + '-turn'}},
        ]
        if tid == 'child' and not lagging_stats:
            rows.extend([
                {'type': 'response_item', 'payload': {'type': 'function_call',
                 'name': 'spawn_agent', 'call_id': 'spawn', 'arguments': '{}'}},
                {'type': 'response_item', 'payload': {'type': 'function_call_output',
                 'call_id': 'spawn', 'output': '{"agent_id":"grandchild"}'}},
            ])
        if not active:
            rows.append({'type': 'event_msg', 'payload': {'type': 'task_complete', 'turn_id': tid + '-turn'}})
        path.write_text('\n'.join(json.dumps(r) for r in rows) + '\n')
        child = codex_rollouts.parse_rollout(path)
        records.append(child)
        d._codex_records[child.session_id] = child
    parent.stats.agents['child'] = codex_rollouts.AgentInfo(agent_id='child', activity='running')
    assert (await d.attach_plan_by_session(parent.session_id, str(plan)))[0]
    ok, detail = await d.close_refinement_terminal(parent.session_id)
    assert ok is not grandchild_active, detail
    assert len(posts) == (0 if grandchild_active else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize('phase,when', [
    ('implementation', 'before_attach'), ('implementation', 'after_attach'),
    ('implementation', 'final_scan'), ('refinement', 'after_attach'),
    ('refinement', 'final_scan'),
])
async def test_refinement_refuses_conflicting_card_scope(refinement_handoff, monkeypatch, phase, when):
    from dark_army_daemon import codex_rollouts
    d, store, card, plan, records, processes, posts = refinement_handoff
    sid = records[0].session_id
    other, detail = store.create({'title': 'Other work', 'root': records[0].cwd,
                                 'project': 'bob', 'tool': 'codex'})
    assert other, detail

    def bind():
        if phase == 'implementation':
            linked, reason = store.bind_session(other['id'], sid)
        else:
            linked, reason = store.update(other['id'], {
                'refine_session_id': sid, 'refine_state': 'live'})
        assert linked, reason

    if when == 'before_attach':
        bind()
    assert (await d.attach_plan_by_session(sid, str(plan)))[0]
    if when == 'after_attach':
        bind()
    elif when == 'final_scan':
        original = codex_rollouts.refinement_close_observation
        calls = 0

        def observe(*args, **kwargs):
            nonlocal calls
            proof = original(*args, **kwargs)
            calls += 1
            if calls == 2:
                bind()  # Reconcile can bind from a worker despite loop locks.
            return proof

        monkeypatch.setattr(codex_rollouts, 'refinement_close_observation', observe)
    ok, detail = await d.close_refinement_terminal(sid)
    assert not ok and detail
    assert posts == [] and sid not in d._closed_ids
    assert store.get(card['id'])['column_name'] == 'backlog'
    linked = store.get(other['id'])
    assert linked['session_id' if phase == 'implementation' else 'refine_session_id'] == sid
    if when == 'final_scan':
        assert calls == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('phase', ['implementation', 'refinement'])
@pytest.mark.parametrize('historical', [False, True])
async def test_refinement_other_sessions_and_finished_cards_are_not_conflicts(
        refinement_handoff, phase, historical):
    d, store, card, plan, records, processes, posts = refinement_handoff
    sid = records[0].session_id
    assert (await d.attach_plan_by_session(sid, str(plan)))[0]
    other, detail = store.create({'title': 'Other work', 'root': records[0].cwd,
                                 'project': 'bob', 'tool': 'codex'})
    assert other, detail
    bound_sid = sid if historical else records[1].session_id
    if phase == 'implementation':
        assert store.bind_session(other['id'], bound_sid)[0]
        if historical:
            assert store.update(other['id'], {'column_name': 'done'})[0]
    else:
        assert store.update(other['id'], {
            'refine_session_id': bound_sid,
            'refine_state': 'ended' if historical else 'live'})[0]
    ok, detail = await d.close_refinement_terminal(sid)
    assert ok, detail
    assert len(posts) == 1 and posts[0]['pid'] == 701


def test_a_hook_evicted_idle_claude_still_offers_close(monkeypatch):
    """A finished run now leaves its terminal open for the person to close,
    so Close must stay reachable after five quiet minutes: the hook row is
    evicted, and the interactive session `claude agents --json` still lists
    is re-emitted as an age-only roster row that sleeps and keeps
    `can_close`."""
    from dark_army_daemon import agents_poll, session_io

    asked = []

    def _can(pid, tty=""):
        asked.append(pid)
        return True

    monkeypatch.setattr(session_io, "can_close_terminal", _can)
    daemon = BobDaemon(headless=True)
    daemon._agent_records["s-quiet"] = agents_poll.AgentRecord(
        session_id="s-quiet", kind="interactive", activity="idle", pid=4242)
    assert "s-quiet" not in daemon._session_states
    out = daemon._enrich_agent_stubs(daemon._collect_agent_stubs())
    rows = [r for r in out["sleeping"] if r["session_id"] == "s-quiet"]
    assert len(rows) == 1
    assert rows[0]["can_close"] is True
    assert asked == [4242]
