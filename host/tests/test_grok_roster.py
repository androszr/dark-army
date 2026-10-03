"""Grok roster / summary / signals readers."""
import inspect
import json
import os
from urllib.parse import quote

from dark_army_daemon import grok_roster
from dark_army_daemon.grok_usage import USD_TICKS
from tests.test_grok_usage import _turn


def test_read_active_parses_roster(tmp_path):
    path = tmp_path / "active_sessions.json"
    path.write_text(json.dumps([
        {
            "session_id": "abc",
            "pid": 42,
            "cwd": "/Users/me/proj",
            "opened_at": "2026-08-14T21:59:01.453374Z",
        },
        {"session_id": "", "pid": 1},
        {"not": "a session"},
    ]))
    recs = grok_roster.read_active(path)
    assert len(recs) == 1
    assert recs[0].session_id == "abc"
    assert recs[0].pid == 42
    assert recs[0].project == "proj"
    assert recs[0].opened_at is not None


def test_read_active_missing_file(tmp_path):
    assert grok_roster.read_active(tmp_path / "nope.json") == []


def test_load_active_none_on_unreadable(tmp_path):
    path = tmp_path / "active.json"
    path.write_text("{nope")
    assert grok_roster.load_active(path) is None
    path.write_text("{}")
    assert grok_roster.load_active(path) is None
    assert grok_roster.read_active(path) == []
    assert grok_roster.load_active(tmp_path / "nope.json") is None


def test_session_dir_uses_urlencoded_cwd(tmp_path):
    cwd = "/Users/me/proj"
    directory = tmp_path / quote(cwd, safe="") / "sid-1"
    directory.mkdir(parents=True)
    (directory / "summary.json").write_text("{}")
    found = grok_roster.session_dir("sid-1", cwd, tmp_path)
    assert found == directory


def test_folder_for_session_prefers_summary_cwd(tmp_path):
    cwd = "/Users/me/finance-demo"
    directory = tmp_path / quote(cwd, safe="") / "sid-1"
    directory.mkdir(parents=True)
    (directory / "summary.json").write_text(json.dumps({
        "info": {"cwd": cwd},
    }))
    assert grok_roster.folder_for_session("sid-1", sessions_dir=tmp_path) == cwd


def test_folder_for_session_unquotes_parent_when_summary_has_no_cwd(tmp_path):
    cwd = "/Users/me/finance-demo"
    directory = tmp_path / quote(cwd, safe="") / "sid-1"
    directory.mkdir(parents=True)
    (directory / "summary.json").write_text("{}")
    assert grok_roster.folder_for_session("sid-1", sessions_dir=tmp_path) == cwd


def test_folder_for_session_missing_is_empty(tmp_path):
    assert grok_roster.folder_for_session("nope", sessions_dir=tmp_path) == ""


def test_first_prompt_skips_synthetic_user_lines(tmp_path):
    cwd = "/Users/me/proj"
    directory = tmp_path / quote(cwd, safe="") / "sid-1"
    directory.mkdir(parents=True)
    (directory / "chat_history.jsonl").write_text("\n".join([
        json.dumps({"type": "user", "synthetic_reason": "system_reminder",
                    "content": "<system-reminder>skills…"}),
        json.dumps({"type": "user", "content": [
            {"type": "text", "text": "<user_info>\nOS Version: macos\n</user_info>"},
        ]}),
        json.dumps({"type": "user", "content": [
            {"type": "text",
             "text": "<user_query>\nczemu grok nie nazywa sesji\n</user_query>"},
        ]}),
    ]) + "\n")
    assert grok_roster.first_prompt("sid-1", cwd, tmp_path) == (
        "czemu grok nie nazywa sesji"
    )


def test_title_and_stats_from_summary_and_signals(tmp_path):
    cwd = "/Users/me/proj"
    directory = tmp_path / quote(cwd, safe="") / "sid-1"
    directory.mkdir(parents=True)
    (directory / "summary.json").write_text(json.dumps({
        "info": {"id": "sid-1", "cwd": cwd},
        "generated_title": "Pixel art redesign",
        "current_model_id": "grok-4.6",
        "head_branch": "main",
        "reasoning_effort": "high",
    }))
    (directory / "signals.json").write_text(json.dumps({
        "contextWindowUsage": 22,
        "contextTokensUsed": 113768,
        "contextWindowTokens": 500000,
        "toolCallCount": 64,
        "userMessageCount": 3,
        "assistantMessageCount": 18,
        "sessionDurationSeconds": 412,
        "primaryModelId": "grok-4.6",
        "modelsUsed": ["grok-4.6"],
        "totalFilesTouched": 4,
    }))
    summary = grok_roster.read_summary("sid-1", cwd, tmp_path)
    signals = grok_roster.read_signals("sid-1", cwd, tmp_path)
    assert grok_roster.title_of(summary) == "Pixel art redesign"
    assert grok_roster.branch_of(summary) == "main"
    stats = grok_roster.stats_from(signals, summary)
    assert stats.model == "grok-4.6"
    assert stats.effort == "high"
    assert stats.user_prompts == 3
    assert stats.total_tool_calls == 64
    assert stats.files_touched == 4
    assert abs(stats.duration_seconds - 412) < 2
    metrics = grok_roster.metrics_from(signals, summary)
    assert metrics["ctx_used_pct"] == 22
    assert metrics["ctx_size"] == 500000
    assert metrics["model_id"] == "grok-4.6"


def test_reconciler_picks_up_a_grok_only_session(tmp_path, monkeypatch):
    """A Grok session the hooks have never seen still gets a row and a face."""
    from dark_army_daemon.daemon import BobDaemon

    roster = tmp_path / "active.json"
    roster.write_text(json.dumps([{
        "session_id": "g-only",
        "pid": 1,
        "cwd": "/tmp/proj",
        "opened_at": "2026-08-14T21:59:01Z",
    }]))
    monkeypatch.setattr(grok_roster, "ACTIVE_SESSIONS_PATH", roster)
    monkeypatch.setattr(
        "dark_army_daemon.daemon._still_our_process",
        lambda pid, provider=None: True,
    )
    monkeypatch.setattr(
        "dark_army_daemon.daemon._process_cwd",
        lambda pid: "/tmp/proj",
    )

    d = BobDaemon()
    assert d._reconciled_categories()["g-only"] == "running"
    stubs = d._collect_agent_stubs()
    extra = [s for s in stubs if s["session_id"] == "g-only"]
    assert extra and extra[0]["provider"] == "grok"


def test_a_live_grok_question_is_waiting_without_hooks(tmp_path, monkeypatch):
    """Grok flushes ask_user_question to chat_history while the dialog is
    up. A row the hooks have never stamped must still land in waiting, or
    the interview draws as captions on a running agent."""
    from dark_army_daemon.daemon import BobDaemon

    cwd = "/tmp/proj"
    sid = "g-ask"
    directory = tmp_path / "sessions" / quote(cwd, safe="") / sid
    directory.mkdir(parents=True)
    arguments = json.dumps({
        "questions": [{
            "question": "Use Postgres or SQLite?",
            "options": [{"label": "Postgres",
                         "description": "Keep the current store."},
                        {"label": "SQLite"}],
        }],
    })
    (directory / "chat_history.jsonl").write_text(json.dumps({
        "type": "assistant",
        "content": "Which store?",
        "tool_calls": [{
            "id": "call-ask-1",
            "name": "ask_user_question",
            "arguments": arguments,
        }],
    }) + "\n")
    roster = tmp_path / "active.json"
    roster.write_text(json.dumps([{
        "session_id": sid,
        "pid": 99,
        "cwd": cwd,
        "opened_at": "2026-08-14T21:59:01Z",
    }]))
    monkeypatch.setattr(grok_roster, "ACTIVE_SESSIONS_PATH", roster)
    monkeypatch.setattr(grok_roster, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(
        "dark_army_daemon.daemon._still_our_process",
        lambda pid, provider=None: True,
    )
    monkeypatch.setattr(
        "dark_army_daemon.daemon._process_cwd",
        lambda pid: cwd,
    )
    monkeypatch.setattr(
        "dark_army_daemon.session_io.can_send_text",
        lambda pid, tty="": True,
    )

    d = BobDaemon()
    d._refresh_grok_records()
    stubs = d._collect_agent_stubs()
    snap = d._enrich_agent_stubs(stubs)
    waiting_ids = [r["session_id"] for r in snap["waiting"]]
    assert sid in waiting_ids
    row = next(r for r in snap["waiting"] if r["session_id"] == sid)
    assert row["state"] == "waiting"
    assert row["question"]["options"] == ["Postgres", "SQLite"]
    assert row["question"]["details"][0] == "Keep the current store."
    assert row["can_type"] is True
    assert d._questions[sid]["id"] == "call-ask-1"
    assert d._reconciled_categories()[sid] == "waiting"


_GROK_REPORT = (
    "Shipped it.\n\n"
    "## Work done\n"
    "**Asked:** finish the card.\n"
    "**Changed:** close-out leaves Grok open.\n"
    "**Verified:** the suite passed.\n"
    "**Unchecked:** Nothing - every check above ran.\n"
)


def _grok_report_stub(tmp_path, monkeypatch, *, sid="g-done", category="waiting"):
    from dark_army_daemon.daemon import BobDaemon
    from dark_army_daemon import session_io

    cwd = "/tmp/proj"
    directory = tmp_path / "sessions" / quote(cwd, safe="") / sid
    directory.mkdir(parents=True)
    (directory / "chat_history.jsonl").write_text(
        json.dumps({"type": "assistant", "content": _GROK_REPORT}) + "\n")
    monkeypatch.setattr(grok_roster, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(session_io, "can_close_terminal",
                        lambda pid, tty="": True)
    monkeypatch.setattr(session_io, "can_send_text",
                        lambda pid, tty="": True)
    d = BobDaemon()
    stub = {
        "session_id": sid, "project": "proj", "state": "idle",
        "subagents": 0, "subagent_ids": [], "idle_seconds": 0.0,
        "current_tool": "", "cwd": cwd, "provider": "grok",
        "pid": 4242, "_category": category, "kind": "interactive",
    }
    return d, stub, sid


def test_a_finished_grok_report_demotes_waiting_to_sleeping(tmp_path, monkeypatch):
    """A leftover Stop card would otherwise bank a finished Grok under
    Needs you. The report means nobody is waiting."""
    d, stub, sid = _grok_report_stub(tmp_path, monkeypatch)
    d._session_states[sid] = {
        "state": "idle", "last_event": 0, "provider": "grok",
    }
    d._active_notifications[sid] = {
        "session_id": sid, "hook": "Stop", "message": "waiting for you",
    }
    snap = d._enrich_agent_stubs([stub])
    sleeping_ids = [r["session_id"] for r in snap["sleeping"]]
    waiting_ids = [r["session_id"] for r in snap["waiting"]]
    assert sid in sleeping_ids
    assert sid not in waiting_ids
    row = next(r for r in snap["sleeping"] if r["session_id"] == sid)
    assert row["state"] == "idle"
    assert row["last_report"].startswith("## Work done")
    assert row["can_close"] is True
    assert row.get("_grok_finished_demote") is True
    # Enrich retargets the snapshot only; hook state and the Stop card
    # wait for the loop-side apply, so categorize still banks waiting.
    assert sid in d._active_notifications
    assert d._session_states[sid]["state"] == "idle"
    assert d._reconciled_categories()[sid] == "waiting"
    d._apply_grok_finished_demote(snap)
    assert sid not in d._active_notifications
    assert d._session_states[sid]["state"] == "idle"
    assert d._reconciled_categories()[sid] == "sleeping"
    assert "_grok_finished_demote" not in row


def test_a_finished_grok_stopfailure_stays_waiting(tmp_path, monkeypatch):
    """StopFailure is a real error. A leftover report must not hide it."""
    d, stub, sid = _grok_report_stub(tmp_path, monkeypatch)
    d._session_states[sid] = {
        "state": "error", "last_event": 0, "provider": "grok",
    }
    d._active_notifications[sid] = {
        "session_id": sid, "hook": "StopFailure",
        "message": "rate limited", "error_kind": "rate_limit",
    }
    snap = d._enrich_agent_stubs([stub])
    waiting_ids = [r["session_id"] for r in snap["waiting"]]
    sleeping_ids = [r["session_id"] for r in snap["sleeping"]]
    assert sid in waiting_ids
    row = next(r for r in snap["waiting"] if r["session_id"] == sid)
    assert row.get("_grok_finished_demote") is not True
    assert sid not in sleeping_ids
    d._apply_grok_finished_demote(snap)
    assert d._active_notifications[sid]["hook"] == "StopFailure"
    assert d._session_states[sid]["state"] == "error"
    assert d._reconciled_categories()[sid] == "waiting"


def test_a_finished_grok_notification_leftover_sleeps_in_categorize(
        tmp_path, monkeypatch):
    """Notification is `confused`; popping the card without idling the hook
    would still bank waiting."""
    d, stub, sid = _grok_report_stub(tmp_path, monkeypatch)
    d._session_states[sid] = {
        "state": "confused", "last_event": 0, "provider": "grok",
    }
    d._active_notifications[sid] = {
        "session_id": sid, "hook": "Notification",
        "message": "waiting for you",
    }
    snap = d._enrich_agent_stubs([stub])
    sleeping_ids = [r["session_id"] for r in snap["sleeping"]]
    waiting_ids = [r["session_id"] for r in snap["waiting"]]
    assert sid in sleeping_ids
    assert sid not in waiting_ids
    assert sid in d._active_notifications
    assert d._session_states[sid]["state"] == "confused"
    assert d._reconciled_categories()[sid] == "waiting"
    d._apply_grok_finished_demote(snap)
    assert sid not in d._active_notifications
    assert d._session_states[sid]["state"] == "idle"
    assert d._reconciled_categories()[sid] == "sleeping"


def test_apply_does_not_idle_a_prompt_that_landed_during_enrich(
        tmp_path, monkeypatch):
    """A UserPromptSubmit during the executor hop must not be overwritten."""
    d, stub, sid = _grok_report_stub(tmp_path, monkeypatch)
    d._session_states[sid] = {
        "state": "idle", "last_event": 10, "provider": "grok",
    }
    d._active_notifications[sid] = {
        "session_id": sid, "hook": "Stop", "message": "waiting for you",
    }
    snap = d._enrich_agent_stubs([stub])
    row = next(r for r in snap["sleeping"] if r["session_id"] == sid)
    assert row.get("_grok_finished_demote") is True
    d._session_states[sid]["state"] = "thinking"
    d._session_states[sid]["last_event"] = 11
    d._apply_grok_finished_demote(snap)
    assert d._session_states[sid]["state"] == "thinking"
    assert d._active_notifications[sid]["hook"] == "Stop"
    assert "_grok_finished_demote" not in row


def test_finished_grok_demote_writes_hook_state_on_the_loop_not_the_executor():
    """The demote's `_session_states` write and card pop belong on the
    loop, next to `_apply_grok_live_subagents`. Enrich may tag."""
    from dark_army_daemon.daemon import BobDaemon

    enrich = inspect.getsource(BobDaemon._enrich_agent_stubs)
    apply = inspect.getsource(BobDaemon._apply_grok_finished_demote)
    push = inspect.getsource(BobDaemon._push_agents_snapshot)
    assert "_grok_finished_demote" in enrich
    assert 'st["state"] = "idle"' not in enrich
    assert "_active_notifications.pop" not in enrich
    assert '["state"] = "idle"' in apply
    assert "_active_notifications.pop" in apply
    assert "_apply_grok_finished_demote" in push


def test_a_running_grok_whose_latest_turn_is_the_report_sleeps_and_can_close(
        tmp_path, monkeypatch):
    """Grok often never fires Stop, so a finished report stayed `running`
    and hid Acknowledge & close. The latest spoken turn *is* the report."""
    d, stub, sid = _grok_report_stub(
        tmp_path, monkeypatch, sid="g-run", category="running")
    stub["state"] = "working"
    d._session_states[sid] = {
        "state": "working", "last_event": 5, "provider": "grok",
    }
    snap = d._enrich_agent_stubs([stub])
    running_ids = [r["session_id"] for r in snap["running"]]
    sleeping_ids = [r["session_id"] for r in snap["sleeping"]]
    assert sid not in running_ids
    assert sid in sleeping_ids
    row = next(r for r in snap["sleeping"] if r["session_id"] == sid)
    assert row["last_report"].startswith("## Work done")
    assert "Work done" in row["last_text"]
    assert row["can_close"] is True
    assert row.get("_grok_finished_demote") is True
    d._apply_grok_finished_demote(snap)
    assert d._session_states[sid]["state"] == "idle"
    assert d._reconciled_categories()[sid] == "sleeping"


def test_a_running_grok_with_stuck_tool_and_helper_still_sleeps(
        tmp_path, monkeypatch):
    """Grok never fires Stop, so the last PreToolUse and a cancelled
    helper stay on the stub. The spoken turn already ended on Work done —
    that used to hide Acknowledge & close."""
    d, stub, sid = _grok_report_stub(
        tmp_path, monkeypatch, sid="g-stuck", category="running")
    stub["state"] = "working"
    stub["current_tool"] = "run_terminal_command"
    stub["subagents"] = 1
    stub["subagent_ids"] = ["01a09c4c"]
    d._session_states[sid] = {
        "state": "working", "last_event": 5, "provider": "grok",
        "tool_name": "run_terminal_command",
    }
    snap = d._enrich_agent_stubs([stub])
    sleeping_ids = [r["session_id"] for r in snap["sleeping"]]
    running_ids = [r["session_id"] for r in snap["running"]]
    assert sid in sleeping_ids
    assert sid not in running_ids
    row = next(r for r in snap["sleeping"] if r["session_id"] == sid)
    assert row["can_close"] is True
    assert row["state"] == "idle"
    assert row.get("current_tool") == ""
    d._apply_grok_finished_demote(snap)
    assert d._session_states[sid]["state"] == "idle"
    assert d._session_states[sid]["tool_name"] == ""


def test_a_running_grok_ship_close_out_sleeps_without_work_done(
        tmp_path, monkeypatch):
    """/ship plan mode never writes ## Work done. The tidy line is the
    finish; leftover tool/helper must not hide Close."""
    from dark_army_daemon.daemon import BobDaemon
    from dark_army_daemon import session_io

    cwd = "/tmp/proj"
    sid = "g-ship"
    directory = tmp_path / "sessions" / quote(cwd, safe="") / sid
    directory.mkdir(parents=True)
    (directory / "chat_history.jsonl").write_text(
        json.dumps({
            "type": "assistant",
            "content": (
                "Karta jest w Backlog.\n"
                "close-out: terminal left open; close it in Dark Army when you have read it.\n"
            ),
        }) + "\n")
    monkeypatch.setattr(grok_roster, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(session_io, "can_close_terminal",
                        lambda pid, tty="": True)
    d = BobDaemon()
    stub = {
        "session_id": sid, "project": "proj", "state": "working",
        "subagents": 1, "subagent_ids": ["01a09c4c"],
        "idle_seconds": 0.0, "current_tool": "run_terminal_command",
        "cwd": cwd, "provider": "grok", "pid": 4242,
        "_category": "running", "kind": "interactive",
    }
    d._session_states[sid] = {
        "state": "working", "last_event": 5, "provider": "grok",
        "tool_name": "run_terminal_command",
    }
    snap = d._enrich_agent_stubs([stub])
    sleeping_ids = [r["session_id"] for r in snap["sleeping"]]
    running_ids = [r["session_id"] for r in snap["running"]]
    assert sid in sleeping_ids
    assert sid not in running_ids
    row = next(r for r in snap["sleeping"] if r["session_id"] == sid)
    assert row["can_close"] is True
    assert "close-out:" in (row.get("last_text") or "")
    assert not (row.get("last_report") or "").startswith("## Work done")
    d._apply_grok_finished_demote(snap)
    assert d._session_states[sid]["state"] == "idle"


def test_a_running_grok_with_later_chatter_keeps_working(
        tmp_path, monkeypatch):
    """A leftover report under a later spoken turn is still a running turn."""
    from dark_army_daemon.daemon import BobDaemon
    from dark_army_daemon import session_io

    cwd = "/tmp/proj"
    sid = "g-chatter"
    directory = tmp_path / "sessions" / quote(cwd, safe="") / sid
    directory.mkdir(parents=True)
    (directory / "chat_history.jsonl").write_text(
        json.dumps({"type": "assistant", "content": _GROK_REPORT}) + "\n"
        + json.dumps({"type": "assistant",
                      "content": "Still going on the next bit."}) + "\n")
    monkeypatch.setattr(grok_roster, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(session_io, "can_close_terminal",
                        lambda pid, tty="": True)
    d = BobDaemon()
    stub = {
        "session_id": sid, "project": "proj", "state": "working",
        "subagents": 0, "subagent_ids": [], "idle_seconds": 0.0,
        "current_tool": "", "cwd": cwd, "provider": "grok",
        "pid": 4242, "_category": "running", "kind": "interactive",
    }
    snap = d._enrich_agent_stubs([stub])
    running_ids = [r["session_id"] for r in snap["running"]]
    sleeping_ids = [r["session_id"] for r in snap["sleeping"]]
    assert sid in running_ids
    assert sid not in sleeping_ids
    row = next(r for r in snap["running"] if r["session_id"] == sid)
    assert row["last_report"].startswith("## Work done")
    assert "Work done" not in row["last_text"]
    assert row["can_close"] is False


def test_a_live_grok_question_wins_over_a_surviving_report(tmp_path, monkeypatch):
    """A follow-up interview after a report is still Needs you."""
    from dark_army_daemon.daemon import BobDaemon
    from dark_army_daemon import session_io

    cwd = "/tmp/proj"
    sid = "g-ask-after"
    directory = tmp_path / "sessions" / quote(cwd, safe="") / sid
    directory.mkdir(parents=True)
    arguments = json.dumps({
        "questions": [{"question": "Ship it?",
                       "options": [{"label": "Yes"}, {"label": "No"}]}],
    })
    (directory / "chat_history.jsonl").write_text(
        json.dumps({"type": "assistant", "content": _GROK_REPORT}) + "\n"
        + json.dumps({
            "type": "assistant",
            "content": "Which way?",
            "tool_calls": [{
                "id": "call-ask-2",
                "name": "ask_user_question",
                "arguments": arguments,
            }],
        }) + "\n")
    monkeypatch.setattr(grok_roster, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(session_io, "can_send_text",
                        lambda pid, tty="": True)
    d = BobDaemon()
    stub = {
        "session_id": sid, "project": "proj", "state": "working",
        "subagents": 0, "subagent_ids": [], "idle_seconds": 0.0,
        "current_tool": "", "cwd": cwd, "provider": "grok",
        "pid": 99, "_category": "running", "kind": "interactive",
    }
    snap = d._enrich_agent_stubs([stub])
    waiting_ids = [r["session_id"] for r in snap["waiting"]]
    assert sid in waiting_ids
    row = next(r for r in snap["waiting"] if r["session_id"] == sid)
    assert row["question"]["options"] == ["Yes", "No"]
    assert row["last_report"].startswith("## Work done")
    assert row["can_type"] is True


def test_hook_tracked_grok_stub_inherits_roster_pid(tmp_path, monkeypatch):
    """A waiting Grok session the hooks never stamped a PID on still jumps.

    The panel gates Jump on `pid != nil`. Grok's notify envelope often omits
    it; ~/.grok/active_sessions.json does not.
    """
    import time
    from dark_army_daemon.daemon import BobDaemon

    roster = tmp_path / "active.json"
    roster.write_text(json.dumps([{
        "session_id": "g-wait",
        "pid": 81898,
        "cwd": "/tmp/proj",
        "opened_at": "2026-08-16T08:30:36Z",
    }]))
    monkeypatch.setattr(grok_roster, "ACTIVE_SESSIONS_PATH", roster)
    monkeypatch.setattr(
        "dark_army_daemon.daemon._still_our_process",
        lambda pid, provider=None: True,
    )
    monkeypatch.setattr(
        "dark_army_daemon.daemon._process_cwd",
        lambda pid: "/tmp/proj",
    )

    d = BobDaemon()
    d._session_states["g-wait"] = {
        "state": "waiting",
        "last_event": time.time(),
        "provider": "grok",
    }
    stubs = d._collect_agent_stubs()
    row = next(s for s in stubs if s["session_id"] == "g-wait")
    assert row["pid"] == 81898
    assert row["_category"] == "waiting"
    assert d._session_states["g-wait"]["pid"] == 81898


def test_liveness_keeps_a_hook_tracked_grok_session_with_only_a_roster_pid(
    tmp_path, monkeypatch,
):
    """Without the backfill, liveness evicted Ledger as 'no PID, silent past grace'."""
    import time
    from dark_army_daemon.daemon import BobDaemon, PIDLESS_GRACE_SECONDS

    roster = tmp_path / "active.json"
    roster.write_text(json.dumps([{
        "session_id": "g-wait",
        "pid": 81898,
        "cwd": "/tmp/proj",
        "opened_at": "2026-08-16T08:30:36Z",
    }]))
    monkeypatch.setattr(grok_roster, "ACTIVE_SESSIONS_PATH", roster)
    monkeypatch.setattr(
        "dark_army_daemon.daemon._still_our_process",
        lambda pid, provider=None: pid == 81898,
    )
    monkeypatch.setattr(
        "dark_army_daemon.daemon._process_cwd",
        lambda pid: "/tmp/proj",
    )

    d = BobDaemon()
    d._refresh_grok_records()
    d._session_states["g-wait"] = {
        "state": "working",
        "last_event": time.time(),
        "last_event_monotonic": time.monotonic() - PIDLESS_GRACE_SECONDS - 1,
        "provider": "grok",
    }
    assert d._check_liveness() == []
    assert d._session_states["g-wait"]["pid"] == 81898


def test_a_hook_leader_pid_is_replaced_by_the_roster_tui(tmp_path, monkeypatch):
    """Hooks under use_leader stamp the shared leader. The roster still
    has the TUI that owns the tab — that is the pid TitleWriter and Stop
    need."""
    import time
    from dark_army_daemon.daemon import BobDaemon

    leader, tui = 9498, 95994
    roster = tmp_path / "active.json"
    roster.write_text(json.dumps([{
        "session_id": "g-tab",
        "pid": tui,
        "cwd": "/tmp/proj",
        "opened_at": "2026-08-16T19:38:45Z",
    }]))
    monkeypatch.setattr(grok_roster, "ACTIVE_SESSIONS_PATH", roster)
    monkeypatch.setattr(
        "dark_army_daemon.daemon._still_our_process",
        lambda pid, provider=None: pid == tui,
    )
    monkeypatch.setattr(
        "dark_army_daemon.daemon._process_cwd",
        lambda pid: "/tmp/proj",
    )

    d = BobDaemon()
    d._refresh_grok_records()
    d._session_states["g-tab"] = {
        "state": "working",
        "last_event": time.time(),
        "provider": "grok",
        "pid": leader,
    }
    stubs = d._collect_agent_stubs()
    row = next(s for s in stubs if s["session_id"] == "g-tab")
    assert row["pid"] == tui
    assert d._session_states["g-tab"]["pid"] == tui


def test_liveness_does_not_evict_a_grok_interview_whose_only_pid_is_the_leader(
    tmp_path, monkeypatch,
):
    """looks_like_grok excludes the shared leader, so `_still_our_process`
    is False for it. That is not "PID gone" — the leader is alive and the
    dialog is up. Evicting here dropped interviews from Needs you every
    30s while Grok was still waiting on an answer."""
    import time
    from dark_army_daemon.daemon import BobDaemon

    leader = 9498
    roster = tmp_path / "active.json"
    roster.write_text("[]")
    monkeypatch.setattr(grok_roster, "ACTIVE_SESSIONS_PATH", roster)
    monkeypatch.setattr(
        "dark_army_daemon.daemon._still_our_process",
        lambda pid, provider=None: False,
    )
    monkeypatch.setattr(
        "dark_army_daemon.daemon._is_live_grok_leader",
        lambda pid: pid == leader,
    )
    monkeypatch.setattr(
        "dark_army_daemon.daemon._process_cwd",
        lambda pid: None,
    )

    d = BobDaemon()
    d._refresh_grok_records()
    d._session_states["g-ask"] = {
        "state": "waiting",
        "last_event": time.time(),
        "last_event_monotonic": time.monotonic(),
        "provider": "grok",
        "pid": leader,
    }
    d._questions["g-ask"] = {
        "id": "call-ask-1",
        "text": "Use Postgres or SQLite?",
        "options": ["Postgres", "SQLite"],
    }
    assert d._check_liveness() == []
    assert "g-ask" in d._session_states
    assert d._session_states["g-ask"].get("pid") in (None, 0)
    assert d._session_waiting_on_question("g-ask") is True


def test_liveness_does_not_treat_a_live_grok_leader_pid_as_death(
    tmp_path, monkeypatch,
):
    """A working Grok tab whose TUI pid has not landed in the roster yet
    still has a hook-stamped leader pid. Immediate eviction as 'PID gone'
    is what put live rows into Recently finished with state still working."""
    import time
    from dark_army_daemon.daemon import BobDaemon

    leader = 9498
    roster = tmp_path / "active.json"
    roster.write_text("[]")
    monkeypatch.setattr(grok_roster, "ACTIVE_SESSIONS_PATH", roster)
    monkeypatch.setattr(
        "dark_army_daemon.daemon._still_our_process",
        lambda pid, provider=None: False,
    )
    monkeypatch.setattr(
        "dark_army_daemon.daemon._is_live_grok_leader",
        lambda pid: pid == leader,
    )
    monkeypatch.setattr(
        "dark_army_daemon.daemon._process_cwd",
        lambda pid: None,
    )

    d = BobDaemon()
    d._refresh_grok_records()
    d._session_states["g-work"] = {
        "state": "working",
        "last_event": time.time(),
        "last_event_monotonic": time.monotonic(),
        "provider": "grok",
        "pid": leader,
    }
    assert d._check_liveness() == []
    assert "g-work" in d._session_states


def test_session_start_on_the_leader_does_not_clear_other_grok_tabs(
    tmp_path, monkeypatch,
):
    """Every Grok hook used to carry the same leader pid, so SessionStart
    looked like `/clear` of every other Grok session."""
    import asyncio
    import time
    from dark_army_daemon.daemon import BobDaemon

    leader, tui_a, tui_b = 9498, 89801, 95994
    roster = tmp_path / "active.json"
    roster.write_text(json.dumps([
        {
            "session_id": "g-a",
            "pid": tui_a,
            "cwd": "/tmp/proj",
            "opened_at": "2026-08-16T19:32:48Z",
        },
        {
            "session_id": "g-b",
            "pid": tui_b,
            "cwd": "/tmp/proj",
            "opened_at": "2026-08-16T19:38:45Z",
        },
    ]))
    monkeypatch.setattr(grok_roster, "ACTIVE_SESSIONS_PATH", roster)
    monkeypatch.setattr(
        "dark_army_daemon.daemon._still_our_process",
        lambda pid, provider=None: pid in (tui_a, tui_b),
    )
    monkeypatch.setattr(
        "dark_army_daemon.daemon._process_cwd",
        lambda pid: "/tmp/proj",
    )

    d = BobDaemon()
    d._refresh_grok_records()
    d._session_states["g-a"] = {
        "state": "working",
        "last_event": time.time(),
        "last_event_monotonic": time.monotonic(),
        "provider": "grok",
        "pid": leader,
    }
    asyncio.run(d._handle_message({
        "event": "session_start",
        "session_id": "g-b",
        "pid": leader,
        "provider": "grok",
        "project": "proj",
    }))
    assert "g-a" in d._session_states
    assert d._session_states["g-b"].get("pid") != leader


def test_a_hook_sibling_tui_pid_is_replaced_by_the_roster_tui(tmp_path, monkeypatch):
    """The leader is parented under one TUI. Walking past it stamps that
    tab's pid on every other Grok session — a live grok process, so
    `_grok_session_pid_ok` used to keep it. The roster is this tab."""
    import time
    from dark_army_daemon.daemon import BobDaemon

    sibling, tui = 58391, 64171
    roster = tmp_path / "active.json"
    roster.write_text(json.dumps([
        {
            "session_id": "g-this",
            "pid": tui,
            "cwd": "/tmp/this",
            "opened_at": "2026-08-29T08:43:05Z",
        },
        {
            "session_id": "g-other",
            "pid": sibling,
            "cwd": "/tmp/other",
            "opened_at": "2026-08-29T08:44:22Z",
        },
    ]))
    monkeypatch.setattr(grok_roster, "ACTIVE_SESSIONS_PATH", roster)
    monkeypatch.setattr(
        "dark_army_daemon.daemon._still_our_process",
        lambda pid, provider=None: pid in (sibling, tui),
    )
    monkeypatch.setattr(
        "dark_army_daemon.daemon._process_cwd",
        lambda pid: "/tmp/this" if pid == tui else "/tmp/other",
    )

    d = BobDaemon()
    d._refresh_grok_records()
    d._session_states["g-this"] = {
        "state": "working",
        "last_event": time.time(),
        "provider": "grok",
        "pid": sibling,
    }
    stubs = d._collect_agent_stubs()
    row = next(s for s in stubs if s["session_id"] == "g-this")
    assert row["pid"] == tui
    assert d._session_states["g-this"]["pid"] == tui


def test_session_start_stamping_a_sibling_tui_does_not_clear_other_tabs(
    tmp_path, monkeypatch,
):
    """The bug that hid every Grok tab: SessionStart arrived with the
    consulting TUI's pid, PID-dedup treated it as `/clear` of every
    other Grok session, and `_grok_ended` kept them dead."""
    import asyncio
    import time
    from dark_army_daemon.daemon import BobDaemon

    sibling, tui_a, tui_b = 58391, 89801, 95994
    roster = tmp_path / "active.json"
    roster.write_text(json.dumps([
        {
            "session_id": "g-a",
            "pid": tui_a,
            "cwd": "/tmp/a",
            "opened_at": "2026-08-16T19:32:48Z",
        },
        {
            "session_id": "g-b",
            "pid": tui_b,
            "cwd": "/tmp/b",
            "opened_at": "2026-08-16T19:38:45Z",
        },
    ]))
    monkeypatch.setattr(grok_roster, "ACTIVE_SESSIONS_PATH", roster)
    monkeypatch.setattr(
        "dark_army_daemon.daemon._still_our_process",
        lambda pid, provider=None: pid in (sibling, tui_a, tui_b),
    )
    monkeypatch.setattr(
        "dark_army_daemon.daemon._process_cwd",
        lambda pid: "/tmp/a" if pid == tui_a else "/tmp/b",
    )

    d = BobDaemon()
    d._refresh_grok_records()
    d._session_states["g-a"] = {
        "state": "working",
        "last_event": time.time(),
        "last_event_monotonic": time.monotonic(),
        "provider": "grok",
        "pid": sibling,
    }
    asyncio.run(d._handle_message({
        "event": "session_start",
        "session_id": "g-b",
        "pid": sibling,
        "provider": "grok",
        "project": "b",
    }))
    assert "g-a" in d._session_states
    assert d._session_states["g-b"].get("pid") == tui_b


def _roster(tmp_path, monkeypatch, rows, *, live_pids=None, cwd_of=None):
    """Write a fake active_sessions.json and stub process inspection."""
    from dark_army_daemon.daemon import BobDaemon

    path = tmp_path / "active.json"
    path.write_text(json.dumps(rows))
    monkeypatch.setattr(grok_roster, "ACTIVE_SESSIONS_PATH", path)
    pids = live_pids if live_pids is not None else {
        r["pid"] for r in rows if isinstance(r.get("pid"), int)
    }
    monkeypatch.setattr(
        "dark_army_daemon.daemon._still_our_process",
        lambda pid, provider=None: pid in pids,
    )
    monkeypatch.setattr(
        "dark_army_daemon.daemon._process_cwd",
        cwd_of if cwd_of is not None else (
            lambda pid: next((r["cwd"] for r in rows if r.get("pid") == pid), None)
        ),
    )
    return BobDaemon(), path


def test_dead_pid_roster_entry_is_not_a_row(tmp_path, monkeypatch):
    """A vanished grok PID used to linger as sleeping until Grok rewrote the
    file. That is the leftover Mira sat in for 1h41m."""
    d, _path = _roster(tmp_path, monkeypatch, [{
        "session_id": "g-dead",
        "pid": 4242,
        "cwd": "/tmp/arpg-web",
        "opened_at": "2026-08-16T15:37:16Z",
    }], live_pids=set())
    assert "g-dead" not in d._reconciled_categories()
    assert not [s for s in d._collect_agent_stubs() if s["session_id"] == "g-dead"
                and s.get("_category") != "finished"]


def test_grok_leaving_the_roster_forgets_hook_state(tmp_path, monkeypatch):
    import time
    d, path = _roster(tmp_path, monkeypatch, [{
        "session_id": "g-gone",
        "pid": 77,
        "cwd": "/tmp/proj",
        "opened_at": "2026-08-16T15:37:16Z",
    }])
    d._session_states["g-gone"] = {
        "state": "working",
        "last_event": time.time() - 60,
        "provider": "grok",
        "pid": 77,
    }
    d._refresh_grok_records()
    assert "g-gone" in d._session_states

    path.write_text("[]")
    monkeypatch.setattr(
        "dark_army_daemon.daemon._still_our_process",
        lambda pid, provider=None: False,
    )
    d._refresh_grok_records()
    assert "g-gone" not in d._session_states
    assert "g-gone" in d._finished


def test_waiting_grok_session_is_not_forgotten_when_off_roster(
    tmp_path, monkeypatch,
):
    import time
    d, path = _roster(tmp_path, monkeypatch, [{
        "session_id": "g-wait",
        "pid": 77,
        "cwd": "/tmp/proj",
        "opened_at": "2026-08-16T15:37:16Z",
    }])
    d._session_states["g-wait"] = {
        "state": "waiting",
        "last_event": time.time() - 60,
        "provider": "grok",
        "pid": 77,
    }
    d._refresh_grok_records()
    path.write_text("[]")
    monkeypatch.setattr(
        "dark_army_daemon.daemon._still_our_process",
        lambda pid, provider=None: False,
    )
    d._refresh_grok_records()
    assert "g-wait" in d._session_states


def test_hook_state_with_a_live_grok_pid_is_not_forgotten(tmp_path, monkeypatch):
    import time
    d, path = _roster(tmp_path, monkeypatch, [{
        "session_id": "g-work",
        "pid": 77,
        "cwd": "/tmp/proj",
        "opened_at": "2026-08-16T15:37:16Z",
    }])
    d._session_states["g-work"] = {
        "state": "working",
        "last_event": time.time() - 60,
        "provider": "grok",
        "pid": 77,
    }
    d._refresh_grok_records()
    path.write_text("[]")
    d._refresh_grok_records()
    assert "g-work" in d._session_states


def test_unreadable_roster_does_not_forget_everyone(tmp_path, monkeypatch):
    d, path = _roster(tmp_path, monkeypatch, [{
        "session_id": "g-live",
        "pid": 77,
        "cwd": "/tmp/proj",
        "opened_at": "2026-08-16T15:37:16Z",
    }])
    d._refresh_grok_records()
    assert "g-live" in d._grok_records
    path.write_text("{not-a-list")
    d._refresh_grok_records()
    assert "g-live" in d._grok_records
    path.write_text("{}")
    d._refresh_grok_records()
    assert "g-live" in d._grok_records


def test_refresh_does_not_connect_the_leader(tmp_path, monkeypatch):
    class Boom:
        connected = False

        async def connect(self):
            raise AssertionError("must not connect from roster refresh")

    d, _path = _roster(tmp_path, monkeypatch, [{
        "session_id": "g-live",
        "pid": 77,
        "cwd": "/tmp/proj",
        "opened_at": "2026-08-16T15:37:16Z",
    }])
    d._leader = Boom()
    d._refresh_grok_records()
    assert getattr(d, "_leader_watch_task", None) is None


def test_cwd_compared_via_realpath(tmp_path, monkeypatch):
    listed = "/tmp/proj"
    d, _path = _roster(
        tmp_path, monkeypatch,
        [{
            "session_id": "g-link",
            "pid": 9,
            "cwd": listed,
            "opened_at": "2026-08-16T15:37:16Z",
        }],
        cwd_of=lambda pid: os.path.realpath(listed),
    )
    assert "g-link" in d._reconciled_categories()


def test_unresolvable_cwd_keeps_the_roster_row(tmp_path, monkeypatch):
    from dark_army_daemon import daemon as daemon_mod

    monkeypatch.setattr(daemon_mod, "_same_cwd", lambda a, b: None)
    d, _path = _roster(
        tmp_path, monkeypatch,
        [{
            "session_id": "g-keep",
            "pid": 9,
            "cwd": "/tmp/proj",
            "opened_at": "2026-08-16T15:37:16Z",
        }],
        cwd_of=lambda pid: "/tmp/other",
    )
    assert "g-keep" in d._reconciled_categories()


def test_roster_only_tombstone_uses_opened_at(tmp_path, monkeypatch):
    from datetime import datetime

    opened = "2026-08-16T15:37:16Z"
    d, _path = _roster(tmp_path, monkeypatch, [{
        "session_id": "g-gone",
        "pid": 77,
        "cwd": "/tmp/proj",
        "opened_at": opened,
    }])
    d._refresh_grok_records()
    assert "g-gone" in d._grok_records
    monkeypatch.setattr(
        "dark_army_daemon.daemon._still_our_process",
        lambda pid, provider=None: False,
    )
    d._refresh_grok_records()
    rec = d._finished["g-gone"]
    expected = datetime.fromisoformat(opened.replace("Z", "+00:00")).timestamp()
    assert rec["finished_at"] == expected


def test_grok_roster_grace_keeps_a_brand_new_session(tmp_path, monkeypatch):
    import time
    d, _path = _roster(tmp_path, monkeypatch, [])
    d._session_states["g-new"] = {
        "state": "registered",
        "last_event": time.time(),
        "provider": "grok",
    }
    d._refresh_grok_records()
    assert "g-new" in d._session_states


def test_cwd_mismatch_drops_stale_roster_row(tmp_path, monkeypatch):
    """Grok left the closed arpg-web session listed against a dark-army
    grok process that was still alive. Same PID, different tree — not ours."""
    d, _path = _roster(
        tmp_path, monkeypatch,
        [{
            "session_id": "g-stale",
            "pid": 57149,
            "cwd": "/tmp/arpg-web",
            "opened_at": "2026-08-16T15:37:16Z",
        }],
        cwd_of=lambda pid: "/tmp/dark-army",
    )
    assert "g-stale" not in d._reconciled_categories()


def test_cwd_mismatch_forgets_waiting_hook_state(tmp_path, monkeypatch):
    """In the file, rejected from live on cwd — departed, even if waiting
    with a still-alive grok at that number (another tab / the leader)."""
    import time
    d, _path = _roster(
        tmp_path, monkeypatch,
        [{
            "session_id": "g-stale",
            "pid": 57149,
            "cwd": "/tmp/arpg-web",
            "opened_at": "2026-08-16T15:37:16Z",
        }],
        live_pids={57149},
        cwd_of=lambda pid: "/tmp/dark-army",
    )
    d._session_states["g-stale"] = {
        "state": "waiting",
        "last_event": time.time(),
        "provider": "grok",
        "pid": 57149,
    }
    d._refresh_grok_records()
    assert "g-stale" not in d._session_states
    assert "g-stale" not in d._grok_records
    assert "g-stale" in d._finished


def test_shared_pid_keeps_the_newest_roster_row(tmp_path, monkeypatch):
    d, _path = _roster(tmp_path, monkeypatch, [
        {
            "session_id": "g-old",
            "pid": 9,
            "cwd": "/tmp/proj",
            "opened_at": "2026-08-16T15:00:00Z",
        },
        {
            "session_id": "g-new",
            "pid": 9,
            "cwd": "/tmp/proj",
            "opened_at": "2026-08-16T17:00:00Z",
        },
    ])
    cats = d._reconciled_categories()
    assert "g-new" in cats
    assert "g-old" not in cats


def test_shared_pid_with_hook_state_keeps_only_the_newest(tmp_path, monkeypatch):
    """Both ids hooked used to keep both — the `/clear` leftover path."""
    import time
    d, _path = _roster(tmp_path, monkeypatch, [
        {
            "session_id": "g-old",
            "pid": 9,
            "cwd": "/tmp/proj",
            "opened_at": "2026-08-16T15:00:00Z",
        },
        {
            "session_id": "g-new",
            "pid": 9,
            "cwd": "/tmp/proj",
            "opened_at": "2026-08-16T17:00:00Z",
        },
    ])
    now = time.time()
    d._session_states["g-old"] = {
        "state": "idle", "last_event": now, "provider": "grok", "pid": 9,
    }
    d._session_states["g-new"] = {
        "state": "registered", "last_event": now, "provider": "grok",
        "pid": 9, "prompted": False,
    }
    d._refresh_grok_records()
    assert "g-new" in d._session_states
    assert "g-old" not in d._session_states
    assert "g-old" in d._finished


def test_clear_leftover_is_forgotten_when_the_tui_now_hosts_a_new_session(
    tmp_path, monkeypatch,
):
    """The wrap-up / `/clear` case from the 17 Aug log: old id gone from
    the roster, TUI pid still a live grok, now listed under the new id.
    Live-pid used to keep the leftover in Needs you."""
    import time
    d, path = _roster(tmp_path, monkeypatch, [{
        "session_id": "g-old",
        "pid": 77,
        "cwd": "/tmp/proj",
        "opened_at": "2026-08-16T15:00:00Z",
    }])
    d._session_states["g-old"] = {
        "state": "idle",
        "last_event": time.time() - 60,
        "provider": "grok",
        "pid": 77,
        "prompted": True,
    }
    d._refresh_grok_records()
    assert "g-old" in d._session_states

    path.write_text(json.dumps([{
        "session_id": "g-new",
        "pid": 77,
        "cwd": "/tmp/proj",
        "opened_at": "2026-08-16T17:00:00Z",
    }]))
    d._refresh_grok_records()
    assert "g-old" not in d._session_states
    assert "g-old" in d._finished
    assert "g-new" in d._grok_records


def test_session_end_is_not_undone_by_a_stale_roster(tmp_path, monkeypatch):
    import time
    d, _path = _roster(tmp_path, monkeypatch, [{
        "session_id": "g-end",
        "pid": 88,
        "cwd": "/tmp/proj",
        "opened_at": "2026-08-16T15:37:16Z",
    }])
    d._session_states["g-end"] = {
        "state": "working",
        "last_event": time.time(),
        "provider": "grok",
        "pid": 88,
    }
    d._update_session_state("dismiss", "SessionEnd", "g-end")
    assert "g-end" not in d._session_states
    d._refresh_grok_records()
    assert "g-end" not in d._grok_records
    assert "g-end" not in d._reconciled_categories()


def _session_dir(tmp_path, cwd="/Users/me/proj", sid="sid-1"):
    directory = tmp_path / quote(cwd, safe="") / sid
    directory.mkdir(parents=True)
    return directory, cwd, sid


def test_enrich_reads_updates_when_signals_is_missing(tmp_path):
    """The live case: a session you are watching often has no signals.json yet."""
    directory, cwd, sid = _session_dir(tmp_path)
    (directory / "summary.json").write_text(json.dumps({
        "info": {"cwd": cwd},
        "current_model_id": "grok-4.6",
        "reasoning_effort": "high",
    }))
    (directory / "updates.jsonl").write_text(
        json.dumps(_turn(1_529_758_600, input_tokens=928_960,
                         output_tokens=32_291, model_calls=14)) + "\n")
    stats, metrics = grok_roster.enrich(sid, cwd, tmp_path)
    assert metrics["cost_usd"] == 1_529_758_600 / USD_TICKS
    assert stats.input_tokens == 928_960
    assert stats.output_tokens == 32_291
    assert stats.model == "grok-4.6"
    assert metrics["ctx_used_pct"] == (928_960 // 14) / 500_000 * 100
    assert metrics["ctx_size"] == 500_000
    assert "cost_usd" in metrics


def test_enrich_reads_live_meta_tokens_on_a_first_turn(tmp_path):
    """A working first-turn session has no signals.json and no turn_completed.

    Cost stays unknown (Grok has not stamped ticks). Context comes from
    the live ``_meta.totalTokens`` on mid-turn updates.
    """
    directory, cwd, sid = _session_dir(tmp_path)
    (directory / "summary.json").write_text(json.dumps({
        "info": {"cwd": cwd},
        "current_model_id": "grok-4.6",
    }))
    (directory / "updates.jsonl").write_text(json.dumps({
        "timestamp": 1,
        "method": "session/update",
        "params": {
            "_meta": {"totalTokens": 98_853},
            "update": {"sessionUpdate": "tool_call_update"},
        },
    }) + "\n")
    stats, metrics = grok_roster.enrich(sid, cwd, tmp_path)
    assert "cost_usd" not in metrics
    assert metrics["ctx_used_pct"] == 100.0 * 98_853 / 500_000
    assert metrics["ctx_input_tokens"] == 98_853
    assert metrics["ctx_size"] == 500_000
    assert stats.input_tokens == 0


def test_enrich_prefers_live_meta_tokens_over_last_turn_mean(tmp_path):
    """Mid-turn, _meta.totalTokens is the current window, not the last bill."""
    directory, cwd, sid = _session_dir(tmp_path)
    (directory / "summary.json").write_text(json.dumps({
        "info": {"cwd": cwd}, "current_model_id": "grok-4.6",
    }))
    (directory / "updates.jsonl").write_text(
        json.dumps(_turn(USD_TICKS, input_tokens=928_960, model_calls=14))
        + "\n"
        + json.dumps({
            "timestamp": 2,
            "method": "session/update",
            "params": {
                "_meta": {"totalTokens": 231_735},
                "update": {"sessionUpdate": "agent_thought_chunk"},
            },
        }) + "\n")
    _stats, metrics = grok_roster.enrich(sid, cwd, tmp_path)
    assert metrics["cost_usd"] == USD_TICKS / USD_TICKS
    assert metrics["ctx_used_pct"] == 100.0 * 231_735 / 500_000
    assert metrics["ctx_input_tokens"] == 231_735


def test_enrich_omits_cost_when_any_turn_is_unpriced(tmp_path):
    directory, cwd, sid = _session_dir(tmp_path)
    (directory / "summary.json").write_text(json.dumps({
        "info": {"cwd": cwd}, "current_model_id": "grok-4.6",
    }))
    (directory / "updates.jsonl").write_text(
        json.dumps(_turn(USD_TICKS)) + "\n" + json.dumps(_turn(None)) + "\n")
    _stats, metrics = grok_roster.enrich(sid, cwd, tmp_path)
    assert "cost_usd" not in metrics
    assert metrics["model_id"] == "grok-4.6"


def test_enrich_prefers_fresher_signals_context(tmp_path):
    directory, cwd, sid = _session_dir(tmp_path)
    (directory / "summary.json").write_text("{}")
    (directory / "signals.json").write_text(json.dumps({
        "contextWindowUsage": 73,
        "contextTokensUsed": 366_336,
        "contextWindowTokens": 500_000,
    }))
    (directory / "updates.jsonl").write_text(
        json.dumps(_turn(USD_TICKS, input_tokens=928_960, model_calls=14)) + "\n")
    os.utime(directory / "updates.jsonl", (1, 1))
    os.utime(directory / "signals.json", (10, 10))
    _stats, metrics = grok_roster.enrich(sid, cwd, tmp_path)
    assert metrics["ctx_used_pct"] == 73

    os.utime(directory / "signals.json", (1, 1))
    os.utime(directory / "updates.jsonl", (10, 10))
    _stats, metrics = grok_roster.enrich(sid, cwd, tmp_path)
    assert metrics["ctx_used_pct"] == (928_960 // 14) / 500_000 * 100


def test_enrich_replaces_the_single_tools_bucket(tmp_path):
    directory, cwd, sid = _session_dir(tmp_path)
    (directory / "summary.json").write_text("{}")
    (directory / "signals.json").write_text(json.dumps({"toolCallCount": 64}))
    (directory / "events.jsonl").write_text("".join(
        json.dumps({"ts": "2026-08-15T00:00:00Z", "type": "tool_started",
                    "tool_name": name}) + "\n"
        for name in ("write", "search_replace", "search_replace", "read_file")
    ))
    stats, _metrics = grok_roster.enrich(sid, cwd, tmp_path)
    assert stats.tool_counts == {"write": 1, "search_replace": 2, "read_file": 1}
    assert stats.total_tool_calls == 4


def test_enrich_maps_line_counts_and_grok_counters(tmp_path):
    directory, cwd, sid = _session_dir(tmp_path)
    (directory / "summary.json").write_text("{}")
    (directory / "signals.json").write_text(json.dumps({
        "agentLinesAdded": 2045,
        "agentLinesRemoved": 306,
        "doomLoopRecoveryAttempts": 2,
        "consecutiveCancellations": 3,
        "editAndRetryCount": 8,
        "hasReverted": True,
    }))
    _stats, metrics = grok_roster.enrich(sid, cwd, tmp_path)
    assert metrics["lines_added"] == 2045
    assert metrics["lines_removed"] == 306
    assert metrics["doom_loop_attempts"] == 2
    assert metrics["consecutive_cancellations"] == 3
    assert metrics["edit_and_retry_count"] == 8
    assert metrics["has_reverted"] is True


def test_enrich_surfaces_the_account_budget(tmp_path):
    directory, cwd, sid = _session_dir(tmp_path)
    (directory / "summary.json").write_text(json.dumps({
        "current_model_id": "grok-4.6",
    }))
    _stats, metrics = grok_roster.enrich(
        sid, cwd, tmp_path,
        billing={"percent": 36.0, "cycle": "weekly", "resets_at": 1_787_000_000.0},
    )
    assert metrics["five_hour_pct"] == 36.0
    assert metrics["budget_cycle"] == "weekly"
    assert metrics["five_hour_resets_at"] == 1_787_000_000.0


def test_enrich_skips_a_stale_budget(tmp_path):
    directory, cwd, sid = _session_dir(tmp_path)
    (directory / "summary.json").write_text("{}")
    _stats, metrics = grok_roster.enrich(
        sid, cwd, tmp_path,
        billing={"percent": 36.0, "cycle": "weekly", "stale": True},
    )
    assert "five_hour_pct" not in metrics


def test_the_snapshot_carries_grok_cost(tmp_path, monkeypatch):
    from dark_army_daemon.daemon import BobDaemon

    directory, cwd, sid = _session_dir(tmp_path / "grok-sessions",
                                       cwd="/tmp/proj", sid="g-live")
    (directory / "summary.json").write_text(json.dumps({
        "info": {"cwd": cwd},
        "generated_title": "Pixel art",
        "current_model_id": "grok-4.6",
    }))
    (directory / "updates.jsonl").write_text(
        json.dumps(_turn(USD_TICKS, input_tokens=1000, output_tokens=20)) + "\n")
    monkeypatch.setattr(grok_roster, "SESSIONS_DIR", tmp_path / "grok-sessions")

    d = BobDaemon()
    stub = {"session_id": sid, "project": "proj", "state": "working",
            "subagents": 0, "subagent_ids": [], "idle_seconds": 0.0,
            "current_tool": "", "cwd": cwd, "provider": "grok",
            "_category": "running"}
    entry = d._enrich_agent_stubs([stub])["running"][0]
    assert entry["metrics"]["cost_usd"] == 1.0
    assert entry["stats"]["input_tokens"] == 1000
    assert entry["stats"]["output_tokens"] == 20
    assert entry["name"]  # titled from summary


def test_grok_cost_is_recorded_as_measured(tmp_path):
    from dark_army_daemon.daemon import BobDaemon
    from dark_army_daemon.history import COST_MEASURED, HistoryStore

    store = HistoryStore(tmp_path / "history.db")
    store.connect()
    d = BobDaemon()
    d._history = store
    d._record_grok_history(
        "g1", {"cost_usd": 0.15, "model_id": "grok-4.6", "cwd": "/tmp/p"},
        "Pixel art")
    rows = store._query("SELECT cost_usd, cost_source, title, provider FROM sessions")
    assert len(rows) == 1
    assert rows[0]["cost_usd"] == 0.15
    assert rows[0]["cost_source"] == COST_MEASURED
    assert rows[0]["title"] == "Pixel art"
    assert rows[0]["provider"] == "grok"
    store.close()


def test_grok_weekly_figure_is_stored_as_grok_and_never_drawn_as_claudes(tmp_path):
    import time

    from dark_army_daemon.daemon import BobDaemon
    from dark_army_daemon.history import HistoryStore

    store = HistoryStore(tmp_path / "history.db")
    store.connect()
    d = BobDaemon()
    d._history = store
    d._record_grok_history(
        "g1", {"five_hour_pct": 80.0, "five_hour_resets_at": time.time() + 400_000,
               "cost_usd": 0.1}, "Pixel art")
    rows = store._query("SELECT provider FROM metric_samples")
    assert [r["provider"] for r in rows] == ["grok"]
    assert store.limits_report(days=7)["series"] == []
    store.close()


def _chat_line(text):
    return json.dumps({"type": "tool_result", "content": text})


def test_live_subagents_none_when_no_spawns(tmp_path):
    directory, cwd, sid = _session_dir(tmp_path)
    (directory / "chat_history.jsonl").write_text(
        json.dumps({"type": "assistant", "content": "hello"}) + "\n")
    assert grok_roster.live_subagent_ids(sid, cwd, tmp_path) is None


def test_live_subagents_follow_spawn_then_completed(tmp_path):
    directory, cwd, sid = _session_dir(tmp_path)
    planner = "01a00c78-2cc4-7683-8471-3c2e6225034f"
    implementer = "01a00c81-43ba-7e62-91e0-41b98ea5378c"
    (directory / "chat_history.jsonl").write_text("\n".join([
        _chat_line(f"Subagent started in background. subagent_id: {planner} type: bc-planner"),
        _chat_line(f"=== Task {planner} === Command: [subagent:bc-planner]\nStatus: completed"),
        _chat_line(f"Subagent started in background. subagent_id: {implementer} type: bc-implementer"),
        _chat_line(f"=== Task {implementer} === Status: running Duration: 12s"),
    ]) + "\n")
    assert grok_roster.live_subagent_ids(sid, cwd, tmp_path) == [implementer]


def test_live_subagents_empty_when_everyone_finished(tmp_path):
    directory, cwd, sid = _session_dir(tmp_path)
    child = "01a00c78-2cc4-7683-8471-3c2e6225034f"
    (directory / "chat_history.jsonl").write_text("\n".join([
        _chat_line(f"Subagent started in background. subagent_id: {child} type: bc-planner"),
        _chat_line(f"=== Task {child} === Status: completed"),
    ]) + "\n")
    assert grok_roster.live_subagent_ids(sid, cwd, tmp_path) == []


def test_live_subagents_last_status_wins(tmp_path):
    directory, cwd, sid = _session_dir(tmp_path)
    child = "01a00c78-2cc4-7683-8471-3c2e6225034f"
    (directory / "chat_history.jsonl").write_text("\n".join([
        _chat_line(f"Subagent started in background. subagent_id: {child}"),
        _chat_line(f"=== Task {child} === Status: running"),
        _chat_line(f"=== Task {child} === Status: completed"),
    ]) + "\n")
    assert grok_roster.live_subagent_ids(sid, cwd, tmp_path) == []


def test_live_subagents_wait_all_completed_drops_them(tmp_path):
    """Grok's parallel wait writes `--- Task <id> [completed] ---`, not
    `=== Task <id> === Status: completed`. The card stayed 'working' after
    both children finished because the old scanner never saw that form."""
    directory, cwd, sid = _session_dir(tmp_path)
    auditor = "01a052d3-a968-7252-8841-c79904d3d981"
    reviewer = "01a052d3-fe65-7460-88ff-fe17f99f7afd"
    (directory / "chat_history.jsonl").write_text("\n".join([
        _chat_line(f"Subagent started in background. subagent_id: {auditor} type: bc-bug-auditor"),
        _chat_line(f"Subagent started in background. subagent_id: {reviewer} type: bc-integration-reviewer"),
        _chat_line(
            "=== Multi-wait (wait_all) ===\n"
            f"--- Task {auditor} [running] ---\n"
            f"--- Task {reviewer} [running] ---\n"
        ),
        _chat_line(
            "=== Multi-wait (wait_all) ===\n"
            f"--- Task {auditor} [completed] ---\n"
            "Command: [subagent:bc-bug-auditor]\n"
            f"--- Task {reviewer} [completed] ---\n"
            "Command: [subagent:bc-integration-reviewer]\n"
        ),
    ]) + "\n")
    assert grok_roster.live_subagent_ids(sid, cwd, tmp_path) == []


def test_live_subagents_wait_all_running_keeps_them(tmp_path):
    directory, cwd, sid = _session_dir(tmp_path)
    auditor = "01a052d3-a968-7252-8841-c79904d3d981"
    reviewer = "01a052d3-fe65-7460-88ff-fe17f99f7afd"
    (directory / "chat_history.jsonl").write_text("\n".join([
        _chat_line(f"Subagent started in background. subagent_id: {auditor}"),
        _chat_line(f"Subagent started in background. subagent_id: {reviewer}"),
        _chat_line(
            "=== Multi-wait (wait_all) ===\n"
            f"--- Task {auditor} [running] ---\n"
            f"--- Task {reviewer} [completed] ---\n"
        ),
    ]) + "\n")
    assert grok_roster.live_subagent_ids(sid, cwd, tmp_path) == [auditor]


def test_live_subagents_wait_all_completion_needs_no_status_word(tmp_path):
    """A wait_all completion has neither `subagent_id` nor `Status:` — the
    line prefilter used to skip it entirely."""
    directory, cwd, sid = _session_dir(tmp_path)
    child = "01a00c78-2cc4-7683-8471-3c2e6225034f"
    (directory / "chat_history.jsonl").write_text("\n".join([
        _chat_line(f"Subagent started in background. subagent_id: {child}"),
        _chat_line(f"--- Task {child} [completed] ---"),
    ]) + "\n")
    assert grok_roster.live_subagent_ids(sid, cwd, tmp_path) == []


def test_a_quoted_source_subagent_id_is_not_a_live_child(tmp_path):
    """Reading grok_roster.py dumps `subagent_id: <example uuid>` into
    chat_history as a tool_result. That is not a spawn."""
    directory, cwd, sid = _session_dir(tmp_path)
    ghost = "01a00c78-2cc4-7683-8471-3c2e6225034f"
    (directory / "chat_history.jsonl").write_text(
        _chat_line(
            '# Spawn result: "subagent_id: '
            f'{ghost} type: bc-planner"'
        ) + "\n")
    assert grok_roster.live_subagent_ids(sid, cwd, tmp_path) is None


def test_read_new_lines_takes_a_finished_record_awaiting_its_newline(tmp_path):
    """`_read_new_lines` routes through `read_jsonl_delta`, gaining the
    `tail_is_complete` half it used to lack: a trailing line that already
    parses as JSON is a finished record, not a torn one."""
    path = tmp_path / "chat_history.jsonl"
    record = json.dumps({"type": "assistant", "content": "done"})
    path.write_bytes(record.encode())          # no trailing newline

    text, offset = grok_roster._read_new_lines(path, 0)
    assert record in text
    assert offset == path.stat().st_size

    # A genuinely torn line is still held for the next pass.
    torn = tmp_path / "torn.jsonl"
    torn.write_bytes(b'{"type": "assis')
    text, offset = grok_roster._read_new_lines(torn, 0)
    assert text == "" and offset == 0


def test_a_leader_handoff_does_not_end_a_running_grok_session(
    tmp_path, monkeypatch,
):
    """Grok hands a starting session to its background `grok agent leader`:
    the first process sends SessionEnd (reason=shutdown), the leader a
    SessionStart (source=load) for the same id seconds later. The end put
    the id on `_grok_ended`, nothing took it off, and the next roster pass
    ended the revived row again — a running planning session shown under
    Finished (3 Oct 2026)."""
    import asyncio
    import time
    from dark_army_daemon.daemon import BobDaemon

    tui = 84629
    roster = tmp_path / "active.json"
    roster.write_text(json.dumps([{
        "session_id": "g-run", "pid": tui, "cwd": "/tmp/run",
        "opened_at": "2026-10-03T06:49:12Z",
    }]))
    monkeypatch.setattr(grok_roster, "ACTIVE_SESSIONS_PATH", roster)
    monkeypatch.setattr("dark_army_daemon.daemon._still_our_process",
                        lambda pid, provider=None: pid == tui)
    monkeypatch.setattr("dark_army_daemon.daemon._process_cwd",
                        lambda pid: "/tmp/run")

    d = BobDaemon()
    msg = {"session_id": "g-run", "provider": "grok", "project": "run"}

    async def replay():
        await d._handle_message({**msg, "event": "session_start", "pid": 84855})
        await d._handle_message({**msg, "event": "dismiss",
                                 "hook": "SessionEnd", "pid": 84855})
        assert "g-run" in d._grok_ended, "a real end still stamps the list"
        await d._handle_message({**msg, "event": "session_start", "pid": 86955})

    asyncio.run(replay())
    assert "g-run" not in d._grok_ended
    d._session_states["g-run"]["last_event"] = time.time()
    d._refresh_grok_records()
    assert "g-run" in d._grok_records, "the live tab must stay on the roster"
    assert "g-run" in d._session_states
    assert "g-run" not in d._finished
