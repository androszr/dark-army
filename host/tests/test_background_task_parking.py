"""A session waiting on its own background shell task is not waiting on you.

The bug this pins, measured 20 Sep 2026 on Claude Code 2.1.278: a `Bash`
call with `run_in_background` returns at once, the harness ends the turn —
Stop hook, quiet transcript — and re-invokes the session itself when the
command exits. Dark Army read the Stop as "Waiting for input" and put a session
watching four CI runs under *Needs you* for two and a half minutes; one Stop
in five on this machine ended that way.

Two halves are tested here: `background_watch`'s reading of the transcript
and the task's output file, and the daemon rung that consults it at the one
place a card is raised. The design is `subagent_watch`'s, with one
difference worth pinning: nothing is stored between looks, so a finished
task releases its session at the next card decision with no latch to expire.
"""

import asyncio
import json
import os
import time
from datetime import datetime, timezone

import pytest

from dark_army_daemon import background_watch
from dark_army_daemon.daemon import BobDaemon


def _iso(at):
    return datetime.fromtimestamp(at, timezone.utc).isoformat().replace(
        "+00:00", "Z")


def _launch(task_id, output, at=None, content=None, kind="user"):
    """The harness's own reply to a background `Bash`, as a transcript
    record. `content` overrides the text; a list is the block form."""
    text = (f"Command running in background with ID: {task_id}. Output is "
            f"being written to: {output}. You will be notified when it "
            f"completes.")
    if content is None:
        content = text
    return json.dumps({
        "type": kind, "timestamp": _iso(at or time.time()),
        "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_1",
             "content": content}]}})


def _notification(task_id):
    return json.dumps({
        "type": "user", "timestamp": _iso(time.time()),
        "message": {"role": "user", "content":
                    f"<task-notification>\n<task-id>{task_id}</task-id>\n"
                    f"<status>completed</status>\n</task-notification>"}})


def _stop():
    return json.dumps({"type": "system", "subtype": "stop_hook_summary",
                       "timestamp": _iso(time.time())})


def _transcript(tmp_path, *records, session_id="s"):
    d = tmp_path / ".claude" / "projects" / "-Users-me--work-proj.v2"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{session_id}.jsonl"
    p.write_text("\n".join(records) + "\n")
    return str(p)


def _output(tmp_path, task_id, body=b"", exited=False):
    d = tmp_path / "claude-501" / "-Users-me--work-proj.v2" / "s" / "tasks"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{task_id}.output"
    p.write_bytes(body + (b"\n[exited with code 0]\n" if exited else b""))
    return str(p)


# --- the reading -------------------------------------------------------------

def test_a_task_with_no_exit_marker_is_running(tmp_path):
    out = _output(tmp_path, "bg1", b"watching...\n")
    tp = _transcript(tmp_path, _launch("bg1", out), _stop())
    assert background_watch.running_tasks(tp) == ["bg1"]


def test_the_exit_marker_releases_the_task(tmp_path):
    out = _output(tmp_path, "bg1", b"CI: success\n", exited=True)
    tp = _transcript(tmp_path, _launch("bg1", out), _stop())
    assert background_watch.running_tasks(tp) == []


def test_an_empty_output_is_still_running(tmp_path):
    """`gh run watch > /dev/null` writes nothing until it exits; the file's
    silence is not a finish. (The bound is the launch's age, not the mtime.)"""
    out = _output(tmp_path, "bg1")
    tp = _transcript(tmp_path, _launch("bg1", out), _stop())
    assert background_watch.running_tasks(tp) == ["bg1"]


def test_the_notification_is_the_resumption(tmp_path):
    """Once the `<task-id>` has landed the session is running again, whatever
    the file says — the harness has consumed the result."""
    out = _output(tmp_path, "bg1")
    tp = _transcript(tmp_path, _launch("bg1", out), _stop(),
                     _notification("bg1"))
    assert background_watch.launched(tp) == {}
    assert background_watch.running_tasks(tp) == []


def test_only_the_named_task_is_released_by_a_notification(tmp_path):
    a = _output(tmp_path, "bg1")
    b = _output(tmp_path, "bg2")
    tp = _transcript(tmp_path, _launch("bg1", a), _launch("bg2", b), _stop(),
                     _notification("bg1"), _stop())
    assert background_watch.running_tasks(tp) == ["bg2"]


def test_a_launch_past_the_ceiling_no_longer_holds(tmp_path):
    out = _output(tmp_path, "bg1")
    old = time.time() - background_watch.PARK_MAX_SECONDS - 60
    tp = _transcript(tmp_path, _launch("bg1", out, at=old), _stop())
    assert background_watch.running_tasks(tp) == []


def test_the_ceiling_outlasts_a_testflight_archive(tmp_path):
    out = _output(tmp_path, "bg1")
    tp = _transcript(tmp_path, _launch("bg1", out, at=time.time() - 45 * 60),
                     _stop())
    assert background_watch.running_tasks(tp) == ["bg1"]


def test_a_launch_with_no_timestamp_is_no_proof(tmp_path):
    out = _output(tmp_path, "bg1")
    rec = json.loads(_launch("bg1", out))
    del rec["timestamp"]
    tp = _transcript(tmp_path, json.dumps(rec), _stop())
    assert background_watch.running_tasks(tp) == []


def test_a_quoted_launch_is_not_a_launch(tmp_path):
    """A session grepping another's transcript prints the sentence inside a
    tool result of its own; only a result that *begins* with it counts."""
    out = _output(tmp_path, "bg1")
    quoted = ("06:35:46 user tool_result 'Command running in background "
              f"with ID: bg1. Output is being written to: {out}. You'")
    tp = _transcript(tmp_path, _launch("bg1", out, content=quoted), _stop())
    assert background_watch.launched(tp) == {}


def test_a_launch_on_an_assistant_record_is_not_a_launch(tmp_path):
    out = _output(tmp_path, "bg1")
    tp = _transcript(tmp_path, _launch("bg1", out, kind="assistant"), _stop())
    assert background_watch.launched(tp) == {}


def test_the_block_form_of_a_tool_result_is_read(tmp_path):
    out = _output(tmp_path, "bg1")
    text = (f"Command running in background with ID: bg1. Output is being "
            f"written to: {out}. You will be notified when it completes.")
    tp = _transcript(tmp_path, _launch("bg1", out, content=[
        {"type": "text", "text": text}]), _stop())
    assert background_watch.running_tasks(tp) == ["bg1"]


@pytest.mark.parametrize("claimed", [
    "relative/tasks/bg1.output",
    "/tmp/x/tasks/../tasks/bg1.output",
    "/tmp/x/other/bg1.output",
    "/tmp/x/tasks/bg2.output",
    "/tmp/x/tasks/bg1.output/",
    "/tmp/x/tasks/bg1.log",
    "",
])
def test_only_the_harnesss_own_output_name_is_opened(claimed):
    assert background_watch.output_path("bg1", claimed) is None


def test_the_harnesss_own_output_name_is_accepted():
    p = "/private/tmp/claude-501/-Users-me-proj/s/tasks/bg1.output"
    assert background_watch.output_path("bg1", p) == p


def test_a_missing_output_is_not_running(tmp_path):
    missing = str(tmp_path / "tasks" / "bg1.output")
    tp = _transcript(tmp_path, _launch("bg1", missing), _stop())
    assert background_watch.running_tasks(tp) == []


def test_a_fifo_at_that_name_cannot_hang_the_daemon(tmp_path):
    d = tmp_path / "tasks"
    d.mkdir()
    fifo = d / "bg1.output"
    os.mkfifo(fifo)
    tp = _transcript(tmp_path, _launch("bg1", str(fifo)), _stop())
    started = time.monotonic()
    assert background_watch.running_tasks(tp) == []
    assert time.monotonic() - started < 2


def test_a_missing_or_foreign_transcript_launches_nothing(tmp_path):
    assert background_watch.running_tasks("") == []
    assert background_watch.running_tasks(str(tmp_path / "none.jsonl")) == []
    # Grok and Codex write nothing in this shape.
    tp = _transcript(tmp_path, json.dumps({"type": "message",
                                           "content": "hello"}))
    assert background_watch.running_tasks(tp) == []


def test_a_half_written_last_line_is_ignored(tmp_path):
    out = _output(tmp_path, "bg1")
    tp = _transcript(tmp_path, _launch("bg1", out),
                     '{"type": "user", "message": {"content": "Command run')
    assert background_watch.running_tasks(tp) == ["bg1"]


# --- the daemon rung ---------------------------------------------------------

def _daemon():
    return BobDaemon()


def _stop_hook(session_id):
    return {"event": "add", "hook": "Stop", "session_id": session_id,
            "message": "Claude is waiting for your input"}


def _state(tp):
    return {"state": "working", "last_event": time.time(),
            "subagents": set(), "transcript_path": tp}


def test_a_running_background_task_parks_the_stop(tmp_path):
    out = _output(tmp_path, "bg1")
    tp = _transcript(tmp_path, _launch("bg1", out), _stop())
    d = _daemon()
    d._session_states["s"] = _state(tp)
    asyncio.run(d._handle_message(_stop_hook("s")))
    assert "s" not in d._active_notifications
    assert d._session_states["s"]["state"] != "idle"
    assert d._session_states["s"].get("async_park_at") is not None


def test_a_finished_task_lets_the_card_through(tmp_path):
    out = _output(tmp_path, "bg1", exited=True)
    tp = _transcript(tmp_path, _launch("bg1", out), _stop())
    d = _daemon()
    d._session_states["s"] = _state(tp)
    asyncio.run(d._handle_message(_stop_hook("s")))
    assert "s" in d._active_notifications
    assert d._session_states["s"].get("async_park_at") is None


def test_the_park_is_not_a_latch(tmp_path):
    """The task exits between two Stops: the second one is a real ask, and
    nothing stored from the first can hold it back."""
    out = _output(tmp_path, "bg1")
    tp = _transcript(tmp_path, _launch("bg1", out), _stop())
    d = _daemon()
    d._session_states["s"] = _state(tp)
    asyncio.run(d._handle_message(_stop_hook("s")))
    assert "s" not in d._active_notifications
    with open(out, "ab") as fh:
        fh.write(b"\n[exited with code 0]\n")
    asyncio.run(d._handle_message(_stop_hook("s")))
    assert "s" in d._active_notifications


def test_a_session_with_no_transcript_path_is_untouched(tmp_path):
    d = _daemon()
    d._session_states["s"] = _state("")
    asyncio.run(d._handle_message(_stop_hook("s")))
    assert "s" in d._active_notifications


def test_a_parked_session_is_not_evicted_mid_task(tmp_path):
    """Forty minutes of `gh run watch` emits nothing; the 300-second evictor
    would otherwise retire the session with its report still to come."""
    out = _output(tmp_path, "bg1")
    tp = _transcript(tmp_path, _launch("bg1", out), _stop())
    d = _daemon()
    d._session_states["s"] = dict(
        _state(tp), last_event_monotonic=time.monotonic() - 10 * 60)
    asyncio.run(d._handle_message(_stop_hook("s")))
    d._evict_stale_sessions()
    assert "s" in d._session_states
