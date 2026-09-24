"""Regressions found reviewing the panel-rebuild branch against origin/main.

Each of these is a fault the 1442-test suite ran green over, so each test is
here to say what the code must *not* go back to doing. Grouped by the thing
that breaks rather than by module — the failure is the point.
"""
import json
import time

import pytest

from dark_army_daemon import grok_roster, grok_scan, vscode_reveal
from dark_army_daemon.daemon import BobDaemon


def _daemon() -> BobDaemon:
    return BobDaemon.__new__(BobDaemon)


# ── a quiet Grok tab is not a closed one ─────────────────────────────────────

def _grok_daemon():
    d = _daemon()
    d._session_states = {"g1": {"provider": "grok", "state": "idle"}}
    d._grok_records = {}
    d._grok_ended = set()
    d._low_priority_sent = {}
    d._refinement_receipts = {}
    d._grok_origin_probed = {}
    d._active_notifications = {}
    d._ai_title_cache = {}
    d._last_metric_sample = {}
    d._pending_questions = {}
    d._permission_requests = {}
    d._finished = {}
    d._event_log = None
    d._logged_starts = set()
    # The tombstone needs half the daemon's metric caches and is not what any
    # of these are about.
    d._record_finished = lambda *a, **k: None
    return d


def test_staleness_eviction_does_not_blacklist_a_live_grok_tab():
    """The failure: a Grok tab left alone for five minutes went permanently
    untrackable. `_grok_ended` is what `_live_grok_records` filters on, and it
    was stamped by *every* removal — including wall-clock staleness, which a
    live tab reaches simply by being quiet."""
    d = _grok_daemon()
    d._forget_session("g1", "evicted")
    assert "g1" not in d._grok_ended


@pytest.mark.parametrize("reason", ["ended", "stopped", "cleared", "no process"])
def test_a_real_ending_still_blacklists(reason):
    """The suppress-list still does its job: a stale active_sessions.json entry
    must not resurrect a session that genuinely finished."""
    d = _grok_daemon()
    d._forget_session("g1", reason)
    assert "g1" in d._grok_ended


# ── a permission prompt that nobody can answer must not pin a row ────────────

def _prompt_daemon(asked_at, port=51000, state="waiting"):
    d = _daemon()
    d._permission_requests = {
        "r1": {"request_id": "r1", "session_id": "s1", "port": port,
               "asked_at": asked_at, "tool_name": "Bash"},
    }
    d._channels = {51000: {"session_id": "s1", "is_channel": True,
                           "last_seen": time.time()}}
    d._session_states = {"s1": {"state": state, "last_event": asked_at}}
    d.CHANNEL_STALE_SECONDS = 95.0
    d._event_log = None
    d._logged_starts = set()
    return d


def test_a_prompt_answered_in_the_terminal_stops_pinning_the_row():
    """The commonest ending of all, and the one nothing used to notice: the
    user answers the dialog in their own terminal. A permission row is not
    dismissible by design, so a leaked one holds its session in "Needs you"
    for the life of the daemon."""
    now = time.time()
    d = _prompt_daemon(now)
    # The session is working again, on an event later than the ask.
    d._session_states["s1"] = {"state": "working", "last_event": now + 1}
    d._reap_permissions()
    assert d._permission_requests == {}


def test_a_prompt_still_blocking_is_kept():
    """A session sitting on the dialog is exactly what the row is for."""
    d = _prompt_daemon(time.time())
    d._reap_permissions()
    assert "r1" in d._permission_requests


def test_a_notification_while_blocked_does_not_clear_the_prompt():
    """`Notification` fires at a session that is sitting on a prompt, so a bare
    "any newer event" test would drop a dialog that is still up. The session
    has to be *running* again."""
    now = time.time()
    d = _prompt_daemon(now)
    d._session_states["s1"] = {"state": "waiting", "last_event": now + 30}
    d._reap_permissions()
    assert "r1" in d._permission_requests


def test_a_prompt_whose_channel_went_away_is_dropped():
    """Nothing can carry the answer back, so the row offers a button that
    cannot work."""
    d = _prompt_daemon(time.time())
    d._channels = {}
    d._reap_permissions()
    assert d._permission_requests == {}


def test_a_forgotten_session_takes_its_prompts_with_it():
    d = _grok_daemon()
    d._permission_requests = {"r1": {"request_id": "r1", "session_id": "g1",
                                     "port": 1, "asked_at": time.time()}}
    d._forget_session("g1", "ended")
    assert d._permission_requests == {}


# ── the reaper must not kill a git editor ────────────────────────────────────

@pytest.mark.parametrize("line", [
    "123 10:00 /Applications/VS.app/app/bin/code --wait /tmp/COMMIT_EDITMSG",
    "123 10:00 /Applications/VS.app/app/bin/code -w /tmp/COMMIT_EDITMSG",
    "123 10:00 /Applications/VS.app/app/bin/code -nw /tmp/COMMIT_EDITMSG",
    "123 10:00 /Applications/VS.app/app/bin/code -rw /tmp/COMMIT_EDITMSG",
])
def test_a_blocking_code_editor_is_never_reaped(line):
    """`-w` is the documented short form and the common spelling in
    `core.editor`. Matched as a substring, "--wait" missed all of these, and
    the reaper SIGKILLed the editor of any `git commit` open past two minutes
    — losing the message."""
    assert vscode_reveal._keeps_cli_alive(line) is True


@pytest.mark.parametrize("line", [
    "123 10:00 /Applications/VS.app/app/out/cli.js --status",
    "123 10:00 /Applications/VS.app/app/bin/code --goto /some/where/README.md",
    "123 10:00 /Applications/VS.app/app/bin/code /path/with/w/in/it",
])
def test_an_ordinary_wedged_cli_is_still_reapable(line):
    assert vscode_reveal._keeps_cli_alive(line) is False


# ── a half-written line is not a record ──────────────────────────────────────

def test_a_torn_final_line_is_left_for_the_next_pass(tmp_path):
    """`updates.jsonl` is appended to while it is read. The torn line was
    dropped on the decode error but *counted* as read, so the rest of that
    record arrived past the scan position and the turn's cost was lost."""
    path = tmp_path / "updates.jsonl"
    good = json.dumps({"type": "turn", "usage": {"input_tokens": 1}})
    path.write_text(good + "\n" + '{"type": "tur', encoding="utf-8")

    seen = {}

    class _Store:
        def add_turn(self, *a, **k):
            return True

        def scan_position(self, key):
            return seen.get(key, (0.0, 0))

        def set_scan_position(self, key, mtime, lines):
            seen[key] = (mtime, lines)

    grok_scan._scan_file(_Store(), path, "s1")
    # One complete line consumed; the torn one is still ahead of the position.
    # Positions are byte offsets stored negated (see grok_scan._scan_file), so
    # the stored value ends exactly at the good line's newline.
    assert seen[str(path)][1] == -(len(good) + 1)


# ── the roster read is incremental ───────────────────────────────────────────

def test_the_chat_history_scan_resumes_rather_than_re_reading(tmp_path, monkeypatch):
    """The file reaches megabytes and its mtime moves on every assistant
    chunk, so an mtime-keyed whole-file read missed on nearly every call — on
    the asyncio loop, several times a second."""
    directory = tmp_path / "sess"
    directory.mkdir()
    path = directory / "chat_history.jsonl"
    spawn = json.dumps({"content": "Subagent started in background. "
                        "subagent_id: "
                        "01a00c78-1111-2222-3333-444455556666"})
    path.write_text(spawn + "\n", encoding="utf-8")
    monkeypatch.setattr(grok_roster, "session_dir", lambda *a, **k: directory)
    grok_roster._live_cache.clear()

    first = grok_roster.live_subagent_ids("s1")
    assert first == ["01a00c78-1111-2222-3333-444455556666"]

    reads = []
    real = grok_roster._read_new_lines

    def _counting(p, offset):
        reads.append(offset)
        return real(p, offset)

    monkeypatch.setattr(grok_roster, "_read_new_lines", _counting)
    done = json.dumps({"content": "=== Task "
                       "01a00c78-1111-2222-3333-444455556666 === Status: completed"})
    with path.open("a", encoding="utf-8") as fh:
        fh.write(done + "\n")

    assert grok_roster.live_subagent_ids("s1") == []
    # Resumed at the end of the first line rather than starting over.
    assert reads == [len(spawn) + 1]


def test_a_truncated_chat_history_is_re_read_whole(tmp_path, monkeypatch):
    """`/clear` replaces the file. Everything accumulated is about a file that
    no longer exists under this name."""
    directory = tmp_path / "sess"
    directory.mkdir()
    path = directory / "chat_history.jsonl"
    spawn = json.dumps({"content": "Subagent started in background. "
                        "subagent_id: "
                        "01a00c78-1111-2222-3333-444455556666"})
    path.write_text(spawn + "\n", encoding="utf-8")
    monkeypatch.setattr(grok_roster, "session_dir", lambda *a, **k: directory)
    grok_roster._live_cache.clear()
    assert grok_roster.live_subagent_ids("s1")

    path.write_text("", encoding="utf-8")
    assert grok_roster.live_subagent_ids("s1") is None
