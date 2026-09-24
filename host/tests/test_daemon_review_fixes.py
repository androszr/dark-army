# host/tests/test_daemon_review_fixes.py
"""Pinning tests for the 1 Sep 2026 review sweep over the daemon and its
stores: iteration safety on the executor, the dead Grok tombstone branch, the
subagent parking bounds, the durable JSON writer, project/cwd persistence, and
the pid-file identity check."""

import json
import os
import time

import pytest

from dark_army_daemon import daemon as daemon_mod
from dark_army_daemon import paths as paths_mod
from dark_army_daemon.daemon import BobDaemon, SUBAGENT_PARK_MAX_SECONDS
from dark_army_daemon.grok_roster import GrokRecord


# ── comprehensions over _session_states survive a mid-walk arrival ───────────


def test_claiming_session_ids_tolerates_a_session_arriving_mid_walk(monkeypatch):
    """`_claiming_session_ids` runs on the executor while the loop mutates
    `_session_states`. Simulated by growing the dict from inside the walk's
    own predicate: a live dict iterator raises "dictionary changed size during
    iteration" there, the `list(...)` snapshot does not."""
    d = BobDaemon()
    d._session_states["a"] = {"state": "working", "last_event": time.time()}
    d._session_states["b"] = {"state": "working", "last_event": time.time()}

    orig = d._parent_of_child

    def growing(sid):
        if "newcomer" not in d._session_states:
            d._session_states["newcomer"] = {
                "state": "working", "last_event": time.time()}
        return orig(sid)

    monkeypatch.setattr(d, "_parent_of_child", growing)
    ids = d._claiming_session_ids()
    assert {"a", "b"} <= ids


# ── a stopped roster-only Grok session reaches Recently finished ─────────────


def test_a_stopped_roster_only_grok_session_reaches_recently_finished():
    """`_forget_session` pops `_grok_records` itself, so `_forget_stopped`
    reading the record *afterwards* found nothing — the Grok tombstone branch
    was dead and a stopped Grok run simply vanished. The record is captured
    before the forget."""
    d = BobDaemon()
    d._grok_records["g1"] = GrokRecord(session_id="g1", pid=4242,
                                       cwd="/tmp/proj")

    d._forget_stopped("g1")

    assert "g1" in d._finished, "the stopped Grok run must leave a tombstone"
    stub = d._finished["g1"]["stub"]
    assert stub["provider"] == "grok"
    assert d._finished["g1"]["end_reason"] == "stopped"
    assert "g1" not in d._grok_records


# ── subagent parking: cleared by a fresh prompt, bounded by age ───────────────


@pytest.mark.asyncio
async def test_a_fresh_prompt_clears_the_previous_turns_subagents():
    """The harness does not take a prompt while a Task subagent still runs, so
    a `UserPromptSubmit` means the previous turn's children are done — and a
    `SubagentStop` dropped on the wire must not park the session for life.
    The append-only `subagents_seen` record stays."""
    d = BobDaemon()
    await d._handle_message({"event": "subagent_start", "session_id": "s1",
                             "agent_id": "child-1"})
    assert d._session_states["s1"]["subagents"] == {"child-1"}

    await d._handle_message({"event": "dismiss", "hook": "UserPromptSubmit",
                             "session_id": "s1"})

    st = d._session_states["s1"]
    assert st["subagents"] == set()
    assert st.get("subagents_seen") == ["child-1"], \
        "the record of what ran is append-only and survives the clear"
    assert d._parked_reason(st) == ""


@pytest.mark.asyncio
async def test_a_session_start_leaves_no_previous_subagents():
    d = BobDaemon()
    await d._handle_message({"event": "subagent_start", "session_id": "s1",
                             "agent_id": "child-1"})
    await d._handle_message({"event": "session_start", "session_id": "s1"})
    assert not d._session_states["s1"].get("subagents")


def test_a_subagent_set_with_no_traffic_for_the_bound_stops_parking():
    """Hook delivery is best-effort: a set with no start/stop traffic for
    SUBAGENT_PARK_MAX_SECONDS no longer parks its parent, so the card the
    human is actually needed for finally shows."""
    stale = {"state": "idle", "last_event": time.time(),
             "subagents": {"child"},
             "subagent_event_at": time.time() - SUBAGENT_PARK_MAX_SECONDS - 1}
    assert BobDaemon._parked_reason(stale) == ""

    fresh = dict(stale, subagent_event_at=time.time())
    assert "active subagent" in BobDaemon._parked_reason(fresh)


def test_an_unstamped_subagent_set_still_parks():
    """Grok's `_sync_grok_subagents` replaces the set without a stamp, and
    sessions restored from before the stamp existed carry none — both keep
    the old behaviour: the bound is for the stream that stamps."""
    state = {"state": "idle", "last_event": time.time(),
             "subagents": {"child"}}
    assert "active subagent" in BobDaemon._parked_reason(state)


# ── the durable JSON writer ───────────────────────────────────────────────────


def test_atomic_write_json_writes_a_complete_target_and_no_temp(tmp_path):
    target = tmp_path / "x.json"
    paths_mod.atomic_write_json(target, {"a": 1}, mode=0o600)
    assert json.loads(target.read_text()) == {"a": 1}
    assert sorted(p.name for p in tmp_path.iterdir()) == ["x.json"], \
        "the temp sibling must never survive"
    assert (target.stat().st_mode & 0o777) == 0o600


def test_atomic_write_json_fsyncs_before_the_replace(tmp_path, monkeypatch):
    """The fsync is the point of the helper: without it a crash between the
    rename and writeback leaves a truncated file wearing the real name."""
    order: list = []
    real_fsync, real_replace = os.fsync, os.replace
    monkeypatch.setattr(paths_mod.os, "fsync",
                        lambda fd: (order.append("fsync"), real_fsync(fd))[1])
    monkeypatch.setattr(paths_mod.os, "replace",
                        lambda a, b: (order.append("replace"),
                                      real_replace(a, b))[1])
    paths_mod.atomic_write_json(tmp_path / "y.json", [1, 2])
    assert order == ["fsync", "replace"]


def test_atomic_write_json_cleans_up_and_raises_on_failure(tmp_path, monkeypatch):
    def boom(a, b):
        raise OSError("disk says no")

    monkeypatch.setattr(paths_mod.os, "replace", boom)
    with pytest.raises(OSError):
        paths_mod.atomic_write_json(tmp_path / "z.json", {"a": 1})
    assert list(tmp_path.iterdir()) == [], "no temp file left behind"


def test_save_sessions_goes_through_the_durable_writer(tmp_path, monkeypatch):
    from dark_army_daemon import session_store

    synced: list = []
    real_fsync = os.fsync
    monkeypatch.setattr("dark_army_daemon.paths.os.fsync",
                        lambda fd: (synced.append(fd), real_fsync(fd))[1])
    path = tmp_path / "sessions.json"
    session_store.save_sessions(
        {"s1": {"state": "idle", "last_event": 1.0}}, path)
    assert synced, "sessions.json must be fsynced before the replace"
    assert "s1" in json.loads(path.read_text())["sessions"]


def test_enrollment_save_goes_through_the_durable_writer(monkeypatch):
    from dark_army_daemon import enrollment

    synced: list = []
    real_fsync = os.fsync
    monkeypatch.setattr("dark_army_daemon.paths.os.fsync",
                        lambda fd: (synced.append(fd), real_fsync(fd))[1])
    assert enrollment.save({"projects": []}) is True
    assert synced, "the ledger must be fsynced before the replace"
    assert json.loads(paths_mod.ENROLLMENT_PATH.read_text()) == {"projects": []}


# ── a project/cwd change alone is persisted ───────────────────────────────────


@pytest.mark.asyncio
async def test_a_message_that_changes_only_the_cwd_is_persisted(monkeypatch):
    """`changed` gated the persist, and neither the project nor the cwd write
    set it — so a session that moved folders kept the old pair in
    sessions.json until something else happened to change state."""
    d = BobDaemon()
    saves: list = []
    monkeypatch.setattr(d, "_persist_sessions", lambda: saves.append(1))

    await d._handle_message({"event": "tool_use", "session_id": "s1",
                             "project": "p", "cwd": "/tmp/one"})
    saves.clear()
    await d._handle_message({"event": "tool_use", "session_id": "s1",
                             "project": "p", "cwd": "/tmp/two"})
    assert saves, "a cwd change alone must trigger a save"
    assert d._session_states["s1"]["cwd"] == "/tmp/two"

    saves.clear()
    await d._handle_message({"event": "tool_use", "session_id": "s1",
                             "project": "renamed", "cwd": "/tmp/two"})
    assert saves, "a project change alone must trigger a save"

    saves.clear()
    await d._handle_message({"event": "tool_use", "session_id": "s1",
                             "project": "renamed", "cwd": "/tmp/two"})
    assert not saves, "an unchanged pair must not buy a write"


# ── the pid file's number is checked for identity before any signal ──────────


class _FakeProc:
    def __init__(self, cmdline, exe="", raise_on_identity=False):
        self._cmdline = cmdline
        self._exe = exe
        self.terminated = False
        self._raise = raise_on_identity

    def cmdline(self):
        if self._raise:
            import psutil
            raise psutil.AccessDenied(123)
        return list(self._cmdline)

    def exe(self):
        return self._exe

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0


def _with_pid_file(pid=54321):
    paths_mod.ensure_state_dir()
    daemon_mod.PID_PATH.write_text(str(pid))


def test_stop_existing_daemon_never_signals_a_non_bob_process(monkeypatch):
    """The pid file is just a number, and after a crash the number is
    routinely somebody else's. A recycled pid pointing at an unrelated
    process gets the stale file removed and no signal."""
    _with_pid_file()
    proc = _FakeProc(("/usr/bin/sleep", "60"), exe="/usr/bin/sleep")
    monkeypatch.setattr(daemon_mod.psutil, "Process", lambda pid: proc)

    assert daemon_mod._stop_existing_daemon() is False
    assert proc.terminated is False, "a stranger's process must not be signalled"
    assert not daemon_mod.PID_PATH.exists(), "the stale pid file is removed"


def test_stop_existing_daemon_still_terminates_a_real_daemon(monkeypatch):
    _with_pid_file()
    proc = _FakeProc(("/usr/bin/python3", "-m", "dark_army_daemon"),
                     exe="/usr/bin/python3")
    monkeypatch.setattr(daemon_mod.psutil, "Process", lambda pid: proc)

    assert daemon_mod._stop_existing_daemon() is True
    assert proc.terminated is True


def test_stop_existing_daemon_fails_closed_on_unreadable_identity(monkeypatch):
    """Identity we may not read is not identity we may signal — and not a pid
    file we may remove either."""
    _with_pid_file()
    proc = _FakeProc((), raise_on_identity=True)
    monkeypatch.setattr(daemon_mod.psutil, "Process", lambda pid: proc)

    assert daemon_mod._stop_existing_daemon() is False
    assert proc.terminated is False
    assert daemon_mod.PID_PATH.exists(), \
        "an unreadable process is not proof the file is stale"
