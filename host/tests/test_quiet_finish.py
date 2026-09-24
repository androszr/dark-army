"""A turn that ends on a work report is waiting on nobody.

`session_stats.finished_quietly` reads the closing message of a transcript;
the daemon's `add` path asks it once per Stop / Notification and raises no
card for a finished turn, so the row sleeps instead of asking. Anything the
reader is unsure of is False — the old behaviour exactly.
"""
import asyncio
import json
import time

from dark_army_daemon import session_stats as ss
from dark_army_daemon.daemon import BobDaemon

REPORT = ("Here is what I did.\n\n## Work done\n**Asked:** x\n**Changed:** y\n"
          "**Verified:** z\n**Unchecked:** Nothing - every check above ran.")


def _assistant(*blocks):
    return json.dumps({"type": "assistant", "message": {
        "role": "assistant", "content": list(blocks)}})


def _text(text):
    return {"type": "text", "text": text}


def _tool(name="Bash"):
    return {"type": "tool_use", "id": "toolu_1", "name": name, "input": {}}


def _user(text):
    return json.dumps({"type": "user", "message": {
        "role": "user", "content": text}})


def _transcript(tmp_path, *records):
    p = tmp_path / "s.jsonl"
    p.write_text("\n".join(records) + "\n")
    return str(p)


# --- the reading -------------------------------------------------------------

def test_a_report_with_no_summary_is_quiet(tmp_path):
    tp = _transcript(tmp_path, _user("do x"), _assistant(_text(REPORT)))
    assert ss.finished_quietly(tp)


def test_a_report_with_a_tldr_is_not_quiet(tmp_path):
    tp = _transcript(tmp_path, _assistant(_text(
        REPORT + "\n<!-- bob-tldr: pick one -->")))
    assert not ss.finished_quietly(tp)


def test_a_report_with_offered_actions_is_not_quiet(tmp_path):
    tp = _transcript(tmp_path, _assistant(_text(
        REPORT + "\n<!-- bob-actions: Accept | Iterate -->")))
    assert not ss.finished_quietly(tp)


def test_a_plain_answer_is_not_quiet(tmp_path):
    tp = _transcript(tmp_path, _assistant(_text("The file is at host/x.py.")))
    assert not ss.finished_quietly(tp)


def test_the_closing_message_is_the_last_one_with_text(tmp_path):
    """A trailing tool-only assistant record is the same turn still running;
    the last *spoken* message decides."""
    tp = _transcript(tmp_path,
                     _assistant(_text(REPORT)),
                     _assistant(_tool()))
    assert ss.finished_quietly(tp)
    tp = _transcript(tmp_path,
                     _assistant(_text(REPORT)),
                     _user("and now do y"),
                     _assistant(_text("Sure, which y?")))
    assert not ss.finished_quietly(tp)


def test_a_report_in_a_user_message_is_not_read(tmp_path):
    tp = _transcript(tmp_path, _user(REPORT), _assistant(_text("ok?")))
    assert not ss.finished_quietly(tp)


def test_a_missing_or_empty_transcript_is_not_quiet(tmp_path):
    assert not ss.finished_quietly("")
    assert not ss.finished_quietly(str(tmp_path / "missing.jsonl"))
    tp = _transcript(tmp_path, "")
    assert not ss.finished_quietly(tp)


def test_a_half_written_last_line_is_skipped(tmp_path):
    tp = _transcript(tmp_path, _assistant(_text(REPORT)))
    with open(tp, "ab") as fh:
        fh.write(b'{"type": "assistant", "message": {"content": [{"type": "te')
    assert ss.finished_quietly(tp)


# --- the daemon rung ---------------------------------------------------------

def _hook(session_id, hook="Stop"):
    return {"event": "add", "hook": hook, "session_id": session_id,
            "message": "Claude is waiting for your input"}


def _state(tp, state="working"):
    return {"state": state, "last_event": time.time(),
            "subagents": set(), "transcript_path": tp}


def test_a_stop_on_a_report_raises_no_card_and_sleeps(tmp_path):
    tp = _transcript(tmp_path, _assistant(_text(REPORT)))
    d = BobDaemon()
    d._session_states["s"] = _state(tp)
    asyncio.run(d._handle_message(_hook("s")))
    assert "s" not in d._active_notifications
    assert d._session_states["s"]["state"] == "idle"
    assert d._session_states["s"].get("async_park_at") is None


def test_a_later_notification_on_the_same_report_stays_idle(tmp_path):
    """Claude's own idle-prompt Notification would bank the finished row
    under `confused`; the report still says nobody is waited on."""
    tp = _transcript(tmp_path, _assistant(_text(REPORT)))
    d = BobDaemon()
    d._session_states["s"] = _state(tp, state="idle")
    asyncio.run(d._handle_message(_hook("s", hook="Notification")))
    assert "s" not in d._active_notifications
    assert d._session_states["s"]["state"] == "idle"


def test_a_report_supersedes_an_earlier_card(tmp_path):
    tp = _transcript(tmp_path, _assistant(_text(REPORT)))
    d = BobDaemon()
    d._session_states["s"] = _state(tp)
    d._active_notifications["s"] = {"message": "earlier"}
    asyncio.run(d._handle_message(_hook("s")))
    assert "s" not in d._active_notifications


def test_a_stop_with_a_tldr_still_asks(tmp_path):
    tp = _transcript(tmp_path, _assistant(_text(
        REPORT + "\n<!-- bob-tldr: choose -->")))
    d = BobDaemon()
    d._session_states["s"] = _state(tp)
    asyncio.run(d._handle_message(_hook("s")))
    assert "s" in d._active_notifications
    assert d._session_states["s"]["state"] == "idle"


def test_a_stop_failure_is_never_quiet(tmp_path):
    tp = _transcript(tmp_path, _assistant(_text(REPORT)))
    d = BobDaemon()
    d._session_states["s"] = _state(tp)
    asyncio.run(d._handle_message({
        "event": "add", "hook": "StopFailure", "session_id": "s",
        "message": "rate limited", "error": "rate_limit"}))
    assert "s" in d._active_notifications
    assert d._session_states["s"]["state"] == "error"


def test_a_session_with_no_transcript_is_untouched(tmp_path):
    d = BobDaemon()
    d._session_states["s"] = _state("")
    asyncio.run(d._handle_message(_hook("s")))
    assert "s" in d._active_notifications


# --- the close-out "left open" line -----------------------------------------
#
# A plan or scout run ends its last message on the line close-out.sh prints
# when it leaves the terminal open. That is the second way a turn says it is
# waiting on nobody; the markers still veto it.

LEFT_OPEN = ("Filed the card in Backlog; the plan is on it.\n\n"
             "close-out: terminal left open; close it in Dark Army when you "
             "have read it.")


def test_a_left_open_line_is_quiet(tmp_path):
    tp = _transcript(tmp_path, _user("plan x"), _assistant(_text(LEFT_OPEN)))
    assert ss.finished_quietly(tp)
    assert ss.quiet_close_out(LEFT_OPEN)
    # Quoted in a markdown blockquote is still the line.
    assert ss.quiet_close_out("> " + ss.CLOSE_OUT_LEFT_OPEN + "; x")
    # Mentioned mid-sentence is not.
    assert not ss.quiet_close_out("it printed " + ss.CLOSE_OUT_LEFT_OPEN)
    assert not ss.quiet_close_out("")


def test_a_left_open_line_followed_by_a_question_is_not_quiet(tmp_path):
    text = LEFT_OPEN + "\n\nShould I also file the follow-up?"
    tp = _transcript(tmp_path, _assistant(_text(text)))
    assert not ss.finished_quietly(tp)
    assert not ss.quiet_close_out(text)


def test_a_codex_work_report_ending_on_the_line_still_asks():
    """Needs you is the only place an unchecked Codex build is flagged, so
    the line does not quiet a `## Work done` report on the Codex path."""
    text = "## Work done\n**Unchecked:** 1. Open it.\n\n" + ss.CLOSE_OUT_LEFT_OPEN
    assert not ss.quiet_close_out(text)


def test_a_left_open_line_with_a_tldr_is_not_quiet(tmp_path):
    text = LEFT_OPEN + "\n<!-- bob-tldr: pick one -->"
    tp = _transcript(tmp_path, _assistant(_text(text)))
    assert not ss.finished_quietly(tp)
    assert not ss.quiet_close_out(text)


def test_a_left_open_line_with_actions_is_not_quiet(tmp_path):
    text = LEFT_OPEN + "\n<!-- bob-actions: Accept | Iterate -->"
    tp = _transcript(tmp_path, _assistant(_text(text)))
    assert not ss.finished_quietly(tp)
    assert not ss.quiet_close_out(text)


def test_a_stop_on_the_left_open_line_raises_no_card_and_sleeps(tmp_path):
    tp = _transcript(tmp_path, _assistant(_text(LEFT_OPEN)))
    d = BobDaemon()
    d._session_states["s"] = _state(tp)
    asyncio.run(d._handle_message(_hook("s")))
    assert "s" not in d._active_notifications
    assert d._session_states["s"]["state"] == "idle"
    assert d._activity_counts().get("attention", 0) == 0
