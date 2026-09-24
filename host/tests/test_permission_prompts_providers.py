"""Hermetic provider permission rows; no assistant process is invoked."""

import asyncio
import time
from pathlib import Path

import pytest

from dark_army_daemon import grok_events, grok_roster
from dark_army_daemon.codex_rollouts import CodexRecord
from dark_army_daemon.daemon import BobDaemon


def _grok(monkeypatch, pending=True, detail=None):
    daemon = BobDaemon(headless=True)
    monkeypatch.setattr(grok_roster, "session_dir", lambda *a: Path("/tmp/grok-test"))
    monkeypatch.setattr(grok_events, "permission_pending", lambda *a, **k: pending)
    monkeypatch.setattr(grok_events, "permission_request_detail",
                        lambda *a: detail or {})
    monkeypatch.setattr(daemon, "_pty_handle_for", lambda *a: None)
    return daemon


def _grok_ask(daemon, description="touch /tmp/probe"):
    daemon._register_grok_permission("grok-one", {
        "provider": "grok", "tool_name": "run_terminal_command",
        "description": description})


def test_grok_waiting_row_names_the_command(monkeypatch):
    daemon = _grok(monkeypatch)
    _grok_ask(daemon)
    row = daemon._permission_snapshot()[0]
    assert row["description"] == "touch /tmp/probe"
    assert row["provider"] == "grok"
    assert row["answerable"] is False
    assert "Answer this in its terminal" in row["answer_note"]


def test_grok_needs_you_row_keeps_private_path_off_wire(monkeypatch):
    daemon = _grok(monkeypatch)
    _grok_ask(daemon)
    assert "events_path" not in daemon._permission_snapshot()[0]


@pytest.mark.parametrize("pending", [False, None])
def test_grok_not_open_mints_no_row(monkeypatch, pending):
    daemon = _grok(monkeypatch, pending=pending)
    _grok_ask(daemon)
    assert daemon._permission_snapshot() == []


def test_grok_detail_fallback_carries_a_command_when_present(monkeypatch):
    daemon = _grok(monkeypatch, detail={"command": "touch /tmp/from-log"})
    _grok_ask(daemon, description="")
    assert daemon._permission_snapshot()[0]["description"] == "touch /tmp/from-log"


def test_grok_second_ask_replaces_first(monkeypatch):
    daemon = _grok(monkeypatch)
    _grok_ask(daemon, "first")
    _grok_ask(daemon, "second")
    rows = daemon._permission_snapshot()
    assert len(rows) == 1 and rows[0]["description"] == "second"


def test_grok_reap_drops_a_resolved_row(monkeypatch):
    daemon = _grok(monkeypatch)
    _grok_ask(daemon)
    monkeypatch.setattr(grok_events, "permission_pending", lambda *a, **k: False)
    assert daemon._permission_snapshot() == []


def test_grok_unhosted_answer_refuses_before_typing(monkeypatch):
    daemon = _grok(monkeypatch)
    _grok_ask(daemon)
    rid = daemon._permission_snapshot()[0]["request_id"]
    assert asyncio.run(daemon.answer_permission(rid, "allow")) == (
        False, "Answer this in its terminal — Dark Army did not open that terminal.")


@pytest.mark.parametrize("behavior,digit", [("allow", "1"), ("deny", "2")])
def test_grok_hosted_answer_writes_only_the_once_digit(monkeypatch, behavior, digit):
    daemon = _grok(monkeypatch)
    _grok_ask(daemon)
    pending = [True]
    monkeypatch.setattr(grok_events, "permission_pending", lambda *a, **k: pending[0])
    monkeypatch.setattr(daemon, "_pty_handle_for", lambda *a: "hosted")
    monkeypatch.setattr(daemon, "_pty_exited", lambda *a: False)

    class _Screen:
        def text(self):
            # `vtgrid.Screen.text()` is a list of rows, never one string.
            return ["Permission required", "1. Allow once", "2. Reject once",
                    "3. Always allow", ""]

    class _Pty:
        written = []

        def screen(self, handle):
            assert handle == "hosted"
            return _Screen()

        def write(self, handle, value):
            self.written.append(value)
            pending[0] = False
            return True

        async def drain(self, handle, timeout):
            return True

    fake = _Pty()
    daemon._pty = fake
    rid = daemon._permission_snapshot()[0]["request_id"]
    assert asyncio.run(daemon.answer_permission(rid, behavior)) == (True, "")
    assert fake.written == [digit]
    assert daemon._permission_snapshot() == []


def _codex(daemon, thread="one", **extra):
    record = CodexRecord(session_id="codex:" + thread, thread_id=thread,
                         path=Path("/tmp/" + thread), current_tool="shell_command",
                         turn_active=True, **extra)
    daemon._codex_records[record.session_id] = record
    return record


def _codex_ask(daemon, thread="one", **extra):
    msg = {"provider": "codex", "session_id": thread,
           "request_id": "hook-" + thread, "claim": "secret",
           "tool_name": "shell_command", "description": "touch /tmp/probe"}
    msg.update(extra)
    return daemon._decide_permission_ask(msg)


def test_codex_waiting_row_names_command_and_refuses_answer():
    daemon = BobDaemon(headless=True)
    _codex(daemon)
    assert _codex_ask(daemon) == {"hold": 0}
    row = daemon._permission_snapshot()[0]
    assert row["description"] == "touch /tmp/probe"
    assert row["answerable"] is False
    assert "Codex terminal" in row["answer_note"]
    assert "claim" not in row


def test_codex_unknown_root_is_refused():
    daemon = BobDaemon(headless=True)
    assert _codex_ask(daemon) == {"hold": 0}
    assert daemon._permission_snapshot() == []


def test_codex_helper_ask_is_refused():
    daemon = BobDaemon(headless=True)
    _codex(daemon)
    assert _codex_ask(daemon, agent_type="worker") == {"hold": 0}
    assert daemon._permission_snapshot() == []


def test_codex_record_ending_reaps_ask():
    daemon = BobDaemon(headless=True)
    record = _codex(daemon)
    _codex_ask(daemon)
    record.last_event = time.time() + 1
    record.turn_active = False
    assert daemon._permission_snapshot() == []


def test_codex_closed_session_reaps_ask():
    """A closed Codex session drops its record and leaves a tombstone; its
    ask goes with it rather than pinning Needs you until the longstop."""
    daemon = BobDaemon(headless=True)
    record = _codex(daemon)
    _codex_ask(daemon)
    daemon._codex_records.pop(record.session_id)
    daemon._finished[record.session_id] = {"state": "finished"}
    assert daemon._permission_snapshot() == []


def test_codex_record_not_yet_read_keeps_ask():
    daemon = BobDaemon(headless=True)
    record = _codex(daemon)
    _codex_ask(daemon)
    daemon._codex_records.pop(record.session_id)
    assert len(daemon._permission_snapshot()) == 1
