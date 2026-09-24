"""A Grok session that lost its tab mid-turn is still a session.

The turn runs under the shared `grok agent leader`, which outlives the
terminal: when VS Code's main process ran out of memory on 21 Sep 2026 and
every tab died, the session on the board-search card kept implementing,
spawned a verifier and flagged its hand-check — while Dark Army showed its
card as ended and its row as finished, because Grok had dropped the id from
`active_sessions.json` with the tab and the hook grace (20 s) ran out on
every wait for a subagent.

The witness is the session directory: Grok appends to `events.jsonl`,
`updates.jsonl` and `chat_history.jsonl` while the turn runs, and a parent
blocked on a child writes nothing itself while the child's own directory
moves. Both liveness paths — the roster pass and the pidless eviction —
accept that witness for a mid-turn row, and only for a mid-turn row: a tab
that closed after Stop still ends at the roster grace as before.
"""
import json
import os
import time
from urllib.parse import quote

from dark_army_daemon import grok_roster
from dark_army_daemon.daemon import (
    BobDaemon, GROK_HEADLESS_GRACE_SECONDS, GROK_ROSTER_GRACE_SECONDS,
    PIDLESS_GRACE_SECONDS,
)

CWD = "/tmp/headless-proj"


def _session_dir(tmp_path, sid):
    directory = tmp_path / "sessions" / quote(CWD, safe="") / sid
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _touch(directory, name, age):
    path = directory / name
    path.write_text(json.dumps({"type": "tool_started"}) + "\n")
    when = time.time() - age
    os.utime(path, (when, when))


def _daemon(tmp_path, monkeypatch):
    roster = tmp_path / "active.json"
    roster.write_text("[]")
    monkeypatch.setattr(grok_roster, "ACTIVE_SESSIONS_PATH", roster)
    monkeypatch.setattr(grok_roster, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(
        "dark_army_daemon.daemon.enrollment.root_enrolled",
        lambda root: True)
    return BobDaemon()


def _state(age, state="working", subagents=()):
    return {
        "provider": "grok", "state": state, "cwd": CWD, "pid": None,
        "last_event": time.time() - age,
        "last_event_monotonic": time.monotonic() - age,
        "subagents": set(subagents),
    }


def test_last_activity_is_the_newest_write_across_parent_and_children(
        tmp_path, monkeypatch):
    monkeypatch.setattr(grok_roster, "SESSIONS_DIR", tmp_path / "sessions")
    parent = _session_dir(tmp_path, "p1")
    child = _session_dir(tmp_path, "c1")
    _touch(parent, "events.jsonl", 600)
    _touch(child, "events.jsonl", 30)
    last = grok_roster.last_activity("p1", CWD, ("c1",))
    assert last is not None and time.time() - last < 60
    assert grok_roster.last_activity("nobody", CWD) is None


def test_roster_pass_keeps_a_headless_turn_whose_files_move(
        tmp_path, monkeypatch):
    """Absent from the roster, silent past the hook grace, mid-turn, and
    the session directory was written a minute ago: the row stays."""
    d = _daemon(tmp_path, monkeypatch)
    _touch(_session_dir(tmp_path, "g-head"), "events.jsonl", 60)
    d._session_states["g-head"] = _state(GROK_ROSTER_GRACE_SECONDS + 5)

    d._refresh_grok_records()

    assert "g-head" in d._session_states
    assert d._session_states["g-head"]["tab_gone"] is True


def test_roster_pass_keeps_a_parent_whose_child_is_the_one_writing(
        tmp_path, monkeypatch):
    """The parent waits on `get_command_or_subagent_output` and writes
    nothing; the verifier child's directory is what moves."""
    d = _daemon(tmp_path, monkeypatch)
    _touch(_session_dir(tmp_path, "g-parent"), "events.jsonl", 900)
    _touch(_session_dir(tmp_path, "g-child"), "updates.jsonl", 20)
    d._session_states["g-parent"] = _state(
        GROK_ROSTER_GRACE_SECONDS + 5, subagents=("g-child",))

    d._refresh_grok_records()

    assert "g-parent" in d._session_states


def test_roster_pass_ends_a_headless_turn_gone_quiet(tmp_path, monkeypatch):
    """Past the headless window with nothing written, it is over."""
    d = _daemon(tmp_path, monkeypatch)
    _touch(_session_dir(tmp_path, "g-quiet"), "events.jsonl",
           GROK_HEADLESS_GRACE_SECONDS + 5)
    d._session_states["g-quiet"] = _state(GROK_ROSTER_GRACE_SECONDS + 5)

    d._refresh_grok_records()

    assert "g-quiet" not in d._session_states
    assert d._finished["g-quiet"]["end_reason"] == "ended"


def test_roster_pass_ends_a_closed_tab_after_stop(tmp_path, monkeypatch):
    """A tab closed once the turn was over: the files are fresh (the Stop
    just landed) but the row is idle, so the witness does not apply and the
    session ends at the roster grace as before."""
    d = _daemon(tmp_path, monkeypatch)
    _touch(_session_dir(tmp_path, "g-idle"), "chat_history.jsonl", 5)
    d._session_states["g-idle"] = _state(GROK_ROSTER_GRACE_SECONDS + 5,
                                         state="idle")

    d._refresh_grok_records()

    assert "g-idle" not in d._session_states


def test_pidless_eviction_spares_a_headless_turn(tmp_path, monkeypatch):
    """The second path: no pid, silent past the pidless grace on the hook
    side, busy on disk — not a ghost."""
    d = _daemon(tmp_path, monkeypatch)
    _touch(_session_dir(tmp_path, "g-live"), "events.jsonl", 45)
    d._session_states["g-live"] = _state(PIDLESS_GRACE_SECONDS + 1)
    monkeypatch.setattr(d, "_refresh_grok_records", lambda: None)

    evicted = d._check_liveness()

    assert "g-live" not in evicted
    assert "g-live" in d._session_states


def test_pidless_eviction_still_takes_a_quiet_headless_ghost(
        tmp_path, monkeypatch):
    d = _daemon(tmp_path, monkeypatch)
    _touch(_session_dir(tmp_path, "g-ghost"), "events.jsonl",
           GROK_HEADLESS_GRACE_SECONDS + 5)
    d._session_states["g-ghost"] = _state(PIDLESS_GRACE_SECONDS + 1)
    monkeypatch.setattr(d, "_refresh_grok_records", lambda: None)

    evicted = d._check_liveness()

    assert "g-ghost" in evicted


def _live(monkeypatch, sid, pid=4242):
    roster = grok_roster.ACTIVE_SESSIONS_PATH
    roster.write_text(json.dumps([{
        "session_id": sid,
        "pid": pid,
        "cwd": CWD,
        "opened_at": "2026-09-22T12:00:00Z",
    }]))
    monkeypatch.setattr(
        "dark_army_daemon.daemon._still_our_process",
        lambda pid, provider=None: True,
    )
    monkeypatch.setattr(
        "dark_army_daemon.daemon._process_cwd",
        lambda pid: CWD,
    )


def test_roster_grace_does_not_stamp_a_missing_tab(tmp_path, monkeypatch):
    """A brand-new id missing from the file is lag. The 20s grace keeps
    the row and does not call it a lost tab."""
    d = _daemon(tmp_path, monkeypatch)
    _touch(_session_dir(tmp_path, "g-new"), "events.jsonl", 1)
    d._session_states["g-new"] = _state(5)

    d._refresh_grok_records()

    assert "g-new" in d._session_states
    assert not d._session_states["g-new"].get("tab_gone")


def _listed(d, sid):
    """The id was on the previous roster pass. Hooks have not aged it."""
    d._grok_records[sid] = grok_roster.GrokRecord(
        session_id=sid, pid=4242, cwd=CWD,
        opened_at=time.time() - 60,
    )


def test_a_listed_working_row_with_a_fresh_hook_is_a_lost_tab(
        tmp_path, monkeypatch):
    """Hooks rewrite `last_event`, so the 20s grace stays open for the
    whole turn. A row the previous roster listed, now absent, is a lost
    tab anyway — and it stays."""
    d = _daemon(tmp_path, monkeypatch)
    _touch(_session_dir(tmp_path, "g-fresh"), "events.jsonl", 1)
    state = _state(0)
    state["busy_since"] = time.time()
    d._session_states["g-fresh"] = state
    _listed(d, "g-fresh")

    d._refresh_grok_records()

    assert "g-fresh" in d._session_states
    assert d._session_states["g-fresh"]["tab_gone"] is True


def test_a_live_grok_pid_absent_from_the_roster_is_a_lost_tab(
        tmp_path, monkeypatch):
    """The live-pid stay returns before the old stamp. A pid that still
    looks like grok, on a row older than the roster lag, is a lost tab
    even while hooks keep `last_event` fresh."""
    d = _daemon(tmp_path, monkeypatch)
    state = _state(0)
    state["pid"] = 4242
    state["busy_since"] = time.time() - (GROK_ROSTER_GRACE_SECONDS + 5)
    d._session_states["g-pid"] = state
    monkeypatch.setattr(
        "dark_army_daemon.daemon._still_our_process",
        lambda pid, provider=None: True,
    )

    d._refresh_grok_records()

    assert "g-pid" in d._session_states
    assert d._session_states["g-pid"]["tab_gone"] is True


def test_a_listed_waiting_row_absent_from_the_roster_is_a_lost_tab(
        tmp_path, monkeypatch):
    """Waiting returns before the stamp used to run. A listed row that
    is now absent still waits, and says the tab is gone."""
    d = _daemon(tmp_path, monkeypatch)
    state = _state(0, state="waiting")
    state["busy_since"] = time.time()
    d._session_states["g-wait"] = state
    _listed(d, "g-wait")

    d._refresh_grok_records()

    assert "g-wait" in d._session_states
    assert d._session_states["g-wait"]["tab_gone"] is True


def test_a_pid_taken_by_another_live_session_is_not_stamped(
        tmp_path, monkeypatch):
    """`/clear` leaves the old id's pid on the new session. That row
    ends, and it is not called a lost tab."""
    d = _daemon(tmp_path, monkeypatch)
    state = _state(0)
    state["pid"] = 4242
    state["busy_since"] = time.time() - (GROK_ROSTER_GRACE_SECONDS + 5)
    d._session_states["g-old"] = state
    _listed(d, "g-old")
    _live(monkeypatch, "g-new", pid=4242)

    d._refresh_grok_records()

    assert "g-old" not in d._session_states
    tomb = d._finished.get("g-old") or {}
    assert not (tomb.get("stub") or {}).get("tab_gone")


def test_a_live_roster_row_is_not_stamped_and_a_return_clears_it(
        tmp_path, monkeypatch):
    d = _daemon(tmp_path, monkeypatch)
    _touch(_session_dir(tmp_path, "g-back"), "events.jsonl", 30)
    d._session_states["g-back"] = _state(GROK_ROSTER_GRACE_SECONDS + 5)
    _live(monkeypatch, "g-back")

    d._refresh_grok_records()

    assert "g-back" in d._session_states
    assert not d._session_states["g-back"].get("tab_gone")

    grok_roster.ACTIVE_SESSIONS_PATH.write_text("[]")
    d._refresh_grok_records()
    assert d._session_states["g-back"]["tab_gone"] is True

    _live(monkeypatch, "g-back")
    d._refresh_grok_records()
    assert d._session_states["g-back"]["tab_gone"] is False


def test_a_stamped_row_keeps_tab_gone_on_the_tombstone(tmp_path, monkeypatch):
    d = _daemon(tmp_path, monkeypatch)
    directory = _session_dir(tmp_path, "g-tomb")
    _touch(directory, "events.jsonl", 30)
    d._session_states["g-tomb"] = _state(GROK_ROSTER_GRACE_SECONDS + 5)

    d._refresh_grok_records()
    assert d._session_states["g-tomb"]["tab_gone"] is True
    stub = next(s for s in d._collect_agent_stubs()
                if s["session_id"] == "g-tomb")
    assert stub["tab_gone"] is True

    _touch(directory, "events.jsonl", GROK_HEADLESS_GRACE_SECONDS + 5)
    d._refresh_grok_records()

    assert "g-tomb" not in d._session_states
    assert d._finished["g-tomb"]["stub"]["tab_gone"] is True


def test_tab_gone_withholds_jump_even_when_a_pid_is_present():
    d = BobDaemon()
    gone = d._session_capabilities({
        "provider": "grok", "pid": 42, "alive": True,
        "kind": "interactive", "tab_gone": True,
    })
    kept = d._session_capabilities({
        "provider": "grok", "pid": 42, "alive": True,
        "kind": "interactive",
    })
    assert gone["can_jump"] is False
    assert kept["can_jump"] is True


def _report(tmp_path, sid):
    directory = _session_dir(tmp_path, sid)
    (directory / "chat_history.jsonl").write_text(json.dumps({
        "type": "assistant",
        "content": (
            "## Work done\n"
            "**Asked:** say the tab is gone.\n"
            "**Changed:** the row.\n"
        ),
    }) + "\n")


def _rows(snap):
    found = []
    for rows in snap.values():
        if isinstance(rows, list):
            found.extend(rows)
    return found


def test_a_lost_tab_publishes_no_jump_no_terminal_and_the_report(
        tmp_path, monkeypatch):
    d = _daemon(tmp_path, monkeypatch)
    _report(tmp_path, "g-lost")
    monkeypatch.setattr(
        "dark_army_daemon.session_io.can_send_text",
        lambda pid, tty="": False)
    monkeypatch.setattr(
        "dark_army_daemon.ptyhost.PtyHost.__len__",
        lambda self: 1)
    monkeypatch.setattr(d, "_pty_handle_for", lambda sid, row=None: "held")
    monkeypatch.setattr(d, "_pty_exited", lambda handle: False)
    stub = {
        "session_id": "g-lost", "project": "proj", "state": "working",
        "subagents": 0, "subagent_ids": [], "idle_seconds": 0.0,
        "current_tool": "", "cwd": CWD, "provider": "grok",
        "pid": 4242, "_category": "running", "kind": "interactive",
        "tab_gone": True, "can_jump": True,
    }
    row = next(r for r in _rows(d._enrich_agent_stubs([stub]))
               if r["session_id"] == "g-lost")
    assert row["tab_gone"] is True
    assert row["can_jump"] is False
    assert row["own_terminal"] is False
    assert row["can_terminal_input"] is False
    assert row["last_report"].startswith("## Work done")


def test_enrich_without_tab_gone_does_not_force_can_jump_false(
        tmp_path, monkeypatch):
    d = _daemon(tmp_path, monkeypatch)
    _report(tmp_path, "g-here")
    monkeypatch.setattr(
        "dark_army_daemon.session_io.can_send_text",
        lambda pid, tty="": False)
    monkeypatch.setattr(
        "dark_army_daemon.ptyhost.PtyHost.__len__",
        lambda self: 1)
    monkeypatch.setattr(d, "_pty_handle_for", lambda sid, row=None: "held")
    monkeypatch.setattr(d, "_pty_exited", lambda handle: False)
    d._session_states["g-here"] = {
        "state": "idle", "last_event": 0, "provider": "grok",
    }
    stub = {
        "session_id": "g-here", "project": "proj", "state": "idle",
        "subagents": 0, "subagent_ids": [], "idle_seconds": 0.0,
        "current_tool": "", "cwd": CWD, "provider": "grok",
        "pid": 4242, "_category": "waiting", "kind": "interactive",
        "can_jump": True,
    }
    row = next(r for r in _rows(d._enrich_agent_stubs([stub]))
               if r["session_id"] == "g-here")
    assert row["can_jump"] is True
    assert row["own_terminal"] is True
    assert row.get("tab_gone") is False
