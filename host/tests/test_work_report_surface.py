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


# --- the parsed shape beside it (`work_report`) --------------------------------

REPO = Path(__file__).resolve().parents[2]
PHONE = REPO / "ios" / "BobPhone"


@pytest.mark.parametrize("root", [PANEL, PHONE])
def test_both_models_decode_the_parts_tolerantly(root):
    models = (root / "Models.swift").read_text(encoding="utf-8")
    assert 'case workReport = "work_report"' in models
    # Absent, empty or wrong-typed must decode, never blank the row.
    assert "workReport = c.maybe(.workReport)" in models


@pytest.mark.parametrize("root", [PANEL, PHONE])
def test_neither_row_puts_the_headline_before_the_title(root):
    # The headline in front pushed the title off the row's lines, so the row
    # read "Changed: …" over a detail titled something else (25 Sep 2026).
    table = (root / "ProcessTable.swift").read_text(encoding="utf-8")
    assert "WorkReport.rowLead(" not in table
    assert "cardLine" in table


# --- the flagged caption: one rule, the session's own card ---------------------


@pytest.mark.parametrize("root, name", [(PANEL, "BoardModels.swift"),
                                         (PHONE, "Models.swift")])
def test_both_boards_pick_the_flagged_card_through_the_shared_rule(root, name):
    text = (root / name).read_text(encoding="utf-8")
    assert text.count("func manualCheckCard(forSession") == 1
    body = text[text.index("func manualCheckCard(forSession"):]
    body = body[:body.index("\n    }\n")]
    assert "WorkReport.flagged(" in body
    # Never the refinement rung: a planning session does not own the check.
    assert "refineSessionId" not in body


def test_the_mac_press_opens_the_flagged_card_check():
    view = (PANEL / "PanelView.swift").read_text(encoding="utf-8")
    assert "manualCheckCard: flaggedCard" in view
    assert "manualCheckCard(forSession:" in view
    assert "board.onOpenManualCheck?(" in view
    assert "showCard(card: flagged)" in view


def test_the_phone_press_opens_the_flagged_card():
    text = (PHONE / "AgentDetailView.swift").read_text(encoding="utf-8")
    body = text[text.index("private func workReport(for agent: Agent"):]
    body = body[:body.index("    /// The reply, on the Details tab")]
    assert "manualCheckCard(forSession:" in body
    assert "sheets.show(.card(" in body


@pytest.mark.parametrize("path", [PANEL / "WorkReportBlock.swift",
                                   PHONE / "AgentDetailView.swift"])
def test_the_flag_words_come_from_the_shared_rule_alone(path):
    text = path.read_text(encoding="utf-8")
    assert "flagged on the card" not in text
    assert "WorkReport.uncheckedCaption(" in text
    assert "WorkReport.showsFlag(" in text


@pytest.fixture
def daemon(tmp_path, monkeypatch):
    from dark_army_daemon import codex_rollouts as cr
    from dark_army_daemon.daemon import BobDaemon
    d = BobDaemon(headless=True, sessions_path=tmp_path / "sessions.json")
    monkeypatch.setattr(d, "_refresh_codex_records", lambda: None)
    monkeypatch.setattr(d, "_refresh_grok_records", lambda: None)
    monkeypatch.setattr(d, "_session_reachable", lambda sid: False)
    monkeypatch.setattr(d, "_schedule_agents_push", lambda: None)
    monkeypatch.setattr(d, "_known_project_roots", lambda: {str(tmp_path)})
    monkeypatch.setattr(cr, "attach_process_ids", lambda records: None)
    return d


def _codex_record(tmp_path, text):
    """A Codex rollout ending on `text` — the row seam of
    `test_codex_session_parity.py`, whose rows reach `_enrich_agent_stubs`
    without a transcript on disk."""
    from dark_army_daemon import codex_rollouts as cr

    def row(kind, **payload):
        return {"timestamp": "2026-09-12T16:52:57Z", "type": kind, "payload": payload}

    path = tmp_path / "2026/09/12" / "rollout-root.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [row("session_meta", id="root", cwd=str(tmp_path), originator="codex-tui",
                source="cli", thread_source="user"),
            row("event_msg", type="task_started", turn_id="turn1"),
            row("response_item", type="message", role="assistant",
                content=[{"type": "output_text", "text": text}]),
            row("event_msg", type="task_complete", turn_id="turn1")]
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return cr.parse_rollout(path)


def _publish(d, record):
    d._codex_records = {record.session_id: record}
    snapshot = d._enrich_agent_stubs(d._collect_agent_stubs())
    return next(e for entries in snapshot.values() for e in entries
                if e["session_id"] == record.session_id)


def test_the_row_carries_the_parsed_report_beside_the_raw_one(tmp_path, daemon):
    entry = _publish(daemon, _codex_record(tmp_path, REPORT))
    assert entry["last_report"].startswith("## Work done")
    parsed = entry["work_report"]
    assert parsed["labelled"] is True
    assert parsed["headline"] == (
        "Changed: the spawner now follows the preference · 1 verified"
        " · nothing unchecked")
    assert parsed["nothing_unchecked"] is True


def test_a_row_that_finished_nothing_carries_no_parsed_report(tmp_path, daemon):
    entry = _publish(daemon, _codex_record(tmp_path, "still working on it"))
    assert entry["last_report"] == ""
    assert "work_report" not in entry


def test_the_finished_row_keeps_the_parsed_report(tmp_path, daemon):
    record = _codex_record(tmp_path, REPORT)
    _publish(daemon, record)
    daemon._record_finished(record.session_id,
                            daemon._codex_finished_state(record), "no process")
    daemon._codex_records = {}
    snapshot = daemon._enrich_agent_stubs(daemon._collect_agent_stubs())
    row = next(r for r in snapshot["finished"] if r["session_id"] == record.session_id)
    assert row["work_report"]["headline"].startswith("Changed:")


def test_the_daemons_alert_policy_knows_when_the_daemon_started(tmp_path, daemon):
    """A restart forgets which reports were announced; the policy seeds a
    report from before the start as delivered, so it needs the start."""
    _publish(daemon, _codex_record(tmp_path, REPORT))
    assert daemon._started_wall > 0
    assert daemon._alert_policy.started_at == daemon._started_wall
