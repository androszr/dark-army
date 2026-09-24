"""The last completion report survives the chatter that follows it.

`last_text` is the *latest* assistant message and nothing else, so a one-line
follow-up ("that was the background job, nothing new") used to be all the panel
had of a session that had just reported a day's work — and the transcript is
not on the wire, so there was no route back to it. `last_report` is kept beside
the tail, survives every later message, and is cleared by the person's next
prompt.
"""
import json
from pathlib import Path

import pytest

from dark_army_daemon.session_stats import (MAX_LAST_REPORT_CHARS,
                                                parse_transcript)

PANEL = Path(__file__).resolve().parents[2] / "panel" / "Sources" / "BobPanel"

REPORT = (
    "Root cause found.\n\n"
    "## Work done\n"
    "**Asked:** why refines open in the editor.\n"
    "**Changed:** the spawner now follows the preference.\n"
    "**Verified:** the suite passed.\n"
    "**Unchecked:** Nothing - every check above ran.\n"
)


def _write(tmp_path, lines):
    p = tmp_path / "transcript.jsonl"
    p.write_text("\n".join(json.dumps(o) for o in lines) + "\n", encoding="utf-8")
    return str(p)


def _assistant(ts, text):
    return {"type": "assistant", "timestamp": ts,
            "message": {"model": "claude-opus-5", "usage": {},
                        "content": [{"type": "text", "text": text}]}}


def _user(ts, text):
    return {"type": "user", "timestamp": ts,
            "message": {"role": "user", "content": text}}


def test_the_report_survives_the_follow_up_that_replaces_last_text(tmp_path):
    stats = parse_transcript(_write(tmp_path, [
        _user("2026-09-06T10:00:00.000Z", "fix the refines"),
        _assistant("2026-09-06T10:05:00.000Z", REPORT),
        _assistant("2026-09-06T10:06:00.000Z",
                   "That was the leftover watcher - nothing new."),
    ]))
    assert stats.last_text.startswith("That was the leftover watcher")
    assert stats.last_report.startswith("## Work done")
    assert "**Unchecked:**" in stats.last_report
    # The prose before the heading belongs to the message, not the report.
    assert "Root cause found" not in stats.last_report


def test_the_next_prompt_clears_it_and_a_tool_result_does_not(tmp_path):
    after_prompt = parse_transcript(_write(tmp_path, [
        _assistant("2026-09-06T10:05:00.000Z", REPORT),
        _user("2026-09-06T10:07:00.000Z", "now do the next thing"),
    ]))
    assert after_prompt.last_report == ""

    tool_result = {"type": "user", "timestamp": "2026-09-06T10:07:00.000Z",
                   "message": {"role": "user", "content": [
                       {"type": "tool_result", "tool_use_id": "t1",
                        "content": "ok"}]}}
    mid_turn = parse_transcript(_write(tmp_path, [
        _assistant("2026-09-06T10:05:00.000Z", REPORT), tool_result]))
    assert mid_turn.last_report.startswith("## Work done")


def test_the_latest_report_wins_and_a_long_one_is_marked_as_cut(tmp_path):
    second = REPORT.replace("why refines open", "why the phone was quiet")
    stats = parse_transcript(_write(tmp_path, [
        _assistant("2026-09-06T10:05:00.000Z", REPORT),
        _assistant("2026-09-06T10:09:00.000Z", second),
    ]))
    assert "why the phone was quiet" in stats.last_report
    assert "why refines open" not in stats.last_report

    long = "## Work done\n" + "**Changed:** a line of the report.\n" * 400
    cut = parse_transcript(_write(tmp_path, [
        _assistant("2026-09-06T10:05:00.000Z", long)]))
    assert len(cut.last_report) <= MAX_LAST_REPORT_CHARS
    assert cut.last_report.startswith("…")


def test_a_session_that_finished_nothing_publishes_no_report(tmp_path):
    stats = parse_transcript(_write(tmp_path, [
        _assistant("2026-09-06T10:05:00.000Z", "still working on it")]))
    assert stats.last_report == ""


@pytest.mark.skipif(not PANEL.exists(), reason="no panel sources")
def test_the_panel_decodes_the_field_and_draws_it_under_the_latest_words():
    models = (PANEL / "Models.swift").read_text(encoding="utf-8")
    assert 'case lastReport = "last_report"' in models
    # Absent must decode empty, never blank the pane.
    assert "lastReport = c.value(.lastReport, \"\")" in models

    pane = (PANEL / "ProcessTable.swift").read_text(encoding="utf-8")
    assert "agent.lastReport" in pane
    assert "static func reportToShow(" in pane
    assert '"# work done"' in pane
