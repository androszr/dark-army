# host/tests/test_agent_report_surface.py
"""The wire agreement between the daemon and its two clients.

`AgentReportView.swift` is deliberately **not** joined to the byte-pinned set
(`Theme.swift`, `Markdown.swift`, `Specialists.swift`, `AgentChatter.swift`,
`OutcomeViews.swift`, `DetailTab`): the desk draws a wide table and the phone
draws a stacked list, so a byte-identical pair would be a lie. What is pinned
here instead is the drift that actually breaks a client — **a key one side
names and the other never sends**, which decodes to a default for ever and
shows a confident zero nobody can trace.

The direction matters. A client may legitimately decline to decode a field it
does not draw (the phone shows no token counts), so the assertion is that
every key a client *names* is a key the daemon *sends*, plus a small core set
both must carry.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

import pytest

from dark_army_daemon import agent_report
from dark_army_daemon.history import HistoryStore

REPO = Path(__file__).resolve().parents[2]
PANEL = REPO / "panel/Sources/BobPanel/History.swift"
PHONE = REPO / "ios/BobPhone/AgentReportView.swift"


# --- what the daemon actually emits -----------------------------------------


@pytest.fixture
def payload(tmp_path):
    store = HistoryStore(tmp_path / "history.db")
    store.connect()
    try:
        store.add_turn("s1", time.time(), message_id="m1",
                       model="claude-sonnet-4-6", input_tokens=100,
                       output_tokens=50, cache_read=100, cache_creation=10,
                       duration_ms=900, tool_name="Bash", agent_id="a1",
                       attr_agent="bc-implementer")
        agents = store.by_agent(30)
        dispatches = store.top_dispatches(30)
        efficiency = store.session_efficiency(["s1"])
    finally:
        store.close()
    runs = [{"card_id": "c1", "provider": "claude", "session_id": "s1",
             "phase": "implementation", "root": "/p", "bound_at": 1.0}]
    joined = agent_report.join(
        runs, agents, efficiency,
        {"accepted_outcomes": 1, "submitted_cards": 2, "reworked_cards": 1,
         "rework_rate": 0.5, "observed_card_hours": 1.5},
        {"c1": {"title": "A card", "accepted": True, "rework_count": 1,
                "manual_steps": "1. Open it."}})
    return {
        "agent": set(joined["agents"][0]),
        "dispatch": set(dispatches[0]),
        "card": set(joined["cards"][0]),
        "summary": set(joined["summary"]),
        "envelope": {"supported", "available", "reason", "outcomes_available",
                     "outcomes_reason", "sessions_truncated", "range_days",
                     "agents", "cards", "summary", "root", "from", "to",
                     "generated_at", "measurements_available", "coverage",
                     "next_offset", "offset", "agent_scope",
                     "agents_truncated", "cards_truncated",
                     "codex_history_partial"},
    }


def _coding_keys(source: str, struct: str) -> set:
    """The wire names one Swift struct's `CodingKeys` declares.

    `case a, b` names a key spelled the same on both sides; `case a = "b"`
    names `b`. Both forms count, because both reach the decoder.
    """
    block = source.split(f"struct {struct}:", 1)
    assert len(block) == 2, f"{struct} is not in this file"
    body = block[1].split("enum CodingKeys", 1)[1].split("}", 1)[0]
    names = set()
    for line in body.splitlines():
        line = line.strip()
        if not line.startswith("case "):
            continue
        for part in line[len("case "):].split(","):
            part = part.strip()
            if "=" in part:
                names.add(part.split("=", 1)[1].strip().strip('"'))
            elif part:
                names.add(part)
    return names


# --- the panel ---------------------------------------------------------------


@pytest.mark.parametrize("struct,section", [
    ("HistoryAgent", "agent"),
    ("HistoryDispatch", "dispatch"),
    ("HistoryOutcomeCard", "card"),
    ("HistoryEffectivenessSummary", "summary"),
])
def test_the_panel_names_no_key_the_daemon_never_sends(payload, struct, section):
    named = _coding_keys(PANEL.read_text(), struct)
    assert named <= payload[section], sorted(named - payload[section])


def test_the_panel_effectiveness_envelope_is_served(payload):
    named = _coding_keys(PANEL.read_text(), "HistoryEffectiveness")
    assert named <= payload["envelope"], sorted(named - payload["envelope"])


# --- the phone ---------------------------------------------------------------


@pytest.mark.parametrize("struct,section", [
    ("AgentReportRow", "agent"),
    ("AgentReportCard", "card"),
    ("AgentReportSummary", "summary"),
])
def test_the_phone_names_no_key_the_daemon_never_sends(payload, struct, section):
    named = _coding_keys(PHONE.read_text(), struct)
    assert named <= payload[section], sorted(named - payload[section])


def test_the_phone_envelope_is_served(payload):
    named = _coding_keys(PHONE.read_text(), "AgentReport")
    assert named <= payload["envelope"], sorted(named - payload["envelope"])


# --- the core both must carry ------------------------------------------------


def test_both_clients_name_the_figures_the_report_exists_for():
    panel = PANEL.read_text()
    phone = PHONE.read_text()
    for key in ("dispatches", "cost_usd", "cache_hit_ratio", "duration_ms",
                "accepted_outcomes", "rework_rate",
                "manual_checks_outstanding"):
        assert key in panel, f"the desk does not name {key}"
        assert key in phone, f"the phone does not name {key}"


# --- the reversal is recorded, not deleted -----------------------------------


def test_history_swift_records_the_reversed_judgment():
    text = PANEL.read_text()
    header = text.split("struct HistoryReport", 1)[0]
    # The old reasoning is quoted rather than deleted…
    assert "curiosities nobody acted on twice" in header
    assert "honest minimum" in header
    # …and recorded as reversed on purpose, with the reason.
    assert re.search(r"reversed", header, re.I)
    assert re.search(r"no longer holds|superseded", header, re.I)
    assert "goal" in header
    # It must not still read as current policy.
    assert "What survives is the honest minimum" not in header


def test_the_panel_decodes_the_sections_it_used_to_withhold():
    named = _coding_keys(PANEL.read_text(), "HistoryReport")
    assert named >= {"by_model", "waiting", "limits", "hourly", "context",
                     "by_agent", "top_dispatches", "effectiveness"}


# --- what each client does with the report's own hedges -----------------------

PANEL_VIEW = REPO / "panel/Sources/BobPanel/HistoryView.swift"
PANEL_FETCH = REPO / "panel/Sources/BobPanel/Fetchers.swift"


def test_the_desk_gates_acceptance_on_whether_it_was_measured():
    """`available` says a report was produced; `outcomes_available` says
    somebody computed acceptance. Gating the acceptance sentence on the
    first is how "0 accepted" appears under a period nobody measured."""
    fold = (REPO / "panel/Sources/BobPanel/LedgerFold.swift").read_text()
    body = fold.split("static func acceptanceSentence", 1)[1]
    body = body.split("\n    static func ", 1)[0]
    assert "outcomesAvailable" in body
    assert "if effectiveness.available" not in body
    assert "not computed" in body


def test_the_desk_can_ask_about_one_project():
    """Acceptance is per project, so the desk needs a way to name one — the
    half of the report that turns spend into value-per-spend is otherwise
    reachable from the phone alone."""
    view = PANEL_VIEW.read_text()
    assert "projectPicker" in view
    assert "client.historyRoot" in view
    fetchers = PANEL_FETCH.read_text()
    assert "func loadHistory(range: String, root: String" in fetchers
    assert "&root=" in fetchers


def test_the_phone_says_when_the_report_was_cut_short():
    """A trimmed list that says nothing reads as a complete one — and a
    complete list that says it was trimmed is the same lie the other way up.

    The offset is shared between `agents` and `cards`; `cards` is routinely
    the long one and this screen never draws it, so the note is keyed on
    whether the **helper list** was reached."""
    phone = PHONE.read_text()
    assert "agents_truncated" in phone
    assert "report.agentsTruncated" in phone
    assert "report.nextOffset != nil" not in phone, (
        "a shared offset does not say the helper list was cut")


def test_the_desk_does_not_draw_the_helper_table():
    """The bug this retires: a machine-wide helper list under a project
    caption. The ledger does not draw that table from either list. The
    decoder still carries `agents`, so an older payload still loads."""
    view = PANEL_VIEW.read_text()
    assert "helperRows" not in view
    assert "agentTable" not in view
    assert "report.byAgent" not in view
    assert "agents" in _coding_keys(PANEL.read_text(), "HistoryEffectiveness")


def test_the_desk_does_not_hide_acceptance_behind_the_helper_table():
    """Acceptance is a sentence of its own. There is no helper table for it
    to be the last row of, and it is still gated on outcomes, not on
    whether a report merely loaded."""
    view = PANEL_VIEW.read_text()
    assert "agentTable" not in view
    assert "helperRows" not in view
    fold = (REPO / "panel/Sources/BobPanel/LedgerFold.swift").read_text()
    assert "outcomesAvailable" in fold
    assert "picture.acceptance" in view or "acceptanceSentence" in fold




def test_a_history_row_opens_its_run_directly_beneath_itself():
    """Clicking a wide history row opens the run's details under that row,
    not in the dock at the foot of the whole list; the bottom dock is drawn
    only when no visible row carries the open run."""
    view = PANEL_VIEW.read_text()
    row = view.split("private func wideRow(", 1)[1].split("\n    private func ", 1)[0]
    assert "wideRowButton(card" in row
    assert "run == card.id, opensUnderRow(picture)" in row
    assert "dock(picture)" in row
    assert "if !opensUnderRow(picture) {\n                        dock(picture)" in view
