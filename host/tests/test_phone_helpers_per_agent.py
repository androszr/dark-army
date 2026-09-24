"""The phone shows helpers on each live agent, from the Mac's per-entry count.

Source pins over the Swift files, `test_phone_context_trend.py`'s idiom —
`ios/` has no test target by explicit decision, and this suite is the one
that actually runs.

The displayed number is the per-entry `subagents` int the daemon already
publishes. The phone does not re-sum `Counts.subagents`, does not use
`subagentRows.count`, and does not invent a nested list of helper rows.
A finished or abandoned row never grows the line.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

PHONE = ROOT / "ios" / "BobPhone"
MODELS = PHONE / "Models.swift"
PROCESS = PHONE / "ProcessTable.swift"
DETAIL = PHONE / "AgentDetailView.swift"
FLEET = PHONE / "FleetView.swift"
BRAND = PHONE / "BrandBar.swift"
CAST = PHONE / "Cast.swift"
PANEL_MODELS = ROOT / "panel" / "Sources" / "BobPanel" / "Models.swift"
DAEMON_PY = ROOT / "host" / "dark_army_daemon" / "daemon.py"
WIDGET_SUMMARY = ROOT / "ios" / "Shared" / "FleetSummary.swift"
WIDGET_DIR = ROOT / "ios" / "BobPhoneWidget"


def _read(path: Path) -> str:
    assert path.is_file(), f"missing file: {path}"
    return path.read_text()


def _struct(text: str, name: str) -> str:
    """A top-level `struct <name>` declaration, up to the next top-level one."""
    match = re.search(rf"\bstruct {name}\b", text)
    assert match, f"no struct {name}"
    rest = text[match.start():]
    ends = [i for i in (rest.find("\nstruct ", 1),
                        rest.find("\nfinal class ", 1),
                        rest.find("\nextension ", 1),
                        rest.find("\nenum ", 1)) if i > 0]
    return rest[:min(ends)] if ends else rest


def _member(text: str, signature: str) -> str:
    """A member's body: from its signature to the next member at the same
    four-space indent (a `var`, `func` or `static`)."""
    start = text.index(signature)
    rest = text[start + len(signature):]
    ends = [i for i in (rest.find("\n    var "),
                        rest.find("\n    private var "),
                        rest.find("\n    func "),
                        rest.find("\n    private func "),
                        rest.find("\n    static "),
                        rest.find("\n    enum "),
                        rest.find("\n}")) if i >= 0]
    return rest[:min(ends)] if ends else rest


def _raw_values(struct_text: str) -> set[str]:
    return set(re.findall(r'case \w+ = "([^"]+)"', struct_text))


# --- the wire ------------------------------------------------------------------

def test_the_daemon_publishes_both_per_entry_keys():
    daemon = _read(DAEMON_PY)
    assert '"subagents":' in daemon
    assert 'entry["subagent_rows"]' in daemon


def test_the_phone_agent_decodes_the_per_entry_fields():
    agent = _struct(_read(MODELS), "Agent")
    assert "subagents = c.value(.subagents, 0)" in agent
    assert "subagentRows = c.value(.subagentRows, [])" in agent
    keys = agent[agent.index("enum CodingKeys"):agent.index("init(from decoder")]
    assert "case subagents" in keys
    assert 'case subagentRows = "subagent_rows"' in keys
    assert "subagent_ids" not in agent
    assert "subagentIds" not in agent


def test_subagent_row_wire_keys_match_the_panel():
    phone = _raw_values(_struct(_read(MODELS), "SubagentRow"))
    panel = _raw_values(_struct(_read(PANEL_MODELS), "SubagentRow"))
    assert "agent_id" in phone
    assert "subagent_type" in phone
    assert phone == panel


def test_subagent_row_label_prefers_type_then_description_then_id():
    label = _member(_struct(_read(MODELS), "SubagentRow"), "var label: String {")
    assert "if !subagentType.isEmpty { return subagentType }" in label
    assert "if !description.isEmpty { return description }" in label
    assert "agentId.prefix(8)" in label


def test_subagent_summary_matches_the_panel():
    phone = _member(_struct(_read(MODELS), "Agent"), "var subagentSummary: String {")
    panel = _member(_struct(_read(PANEL_MODELS), "Agent"), "var subagentSummary: String {")
    for needle in ("guard subagents > 0",
                   # `qualified`, never `label`: the bare label is the key
                   # `liveStageNames` matches against a card's workflow.
                   "subagentRows.last?.qualified",
                   r"+\(subagents) sub"):
        assert needle in phone, needle
        assert needle in panel, needle
    assert phone.strip().splitlines()[0:4] == panel.strip().splitlines()[0:4]


def test_an_older_mac_decodes_to_no_helpers():
    agent = _struct(_read(MODELS), "Agent")
    assert "var subagents = 0" in agent
    assert "var subagentRows: [SubagentRow] = []" in agent


# --- the row -------------------------------------------------------------------

def test_the_row_draws_the_summary_on_live_agents_only():
    process = _read(PROCESS)
    assert process.count("agent.subagentSummary") == 1
    visible = _member(process, "private var helpersVisible: Bool {")
    assert ".sleeping" in visible
    assert "subagentSummary" in process[process.index("if helpersVisible"):]
    assert ".finished" not in visible
    assert ".abandoned" not in visible


def test_state_label_is_unchanged():
    label = _member(_read(PROCESS), "private var stateLabel: String {")
    for word in ('"wait"', '"run"', '"slp"', '"done"', '"dead"'):
        assert word in label
    assert "subagents" not in label


def test_context_trend_marker_is_not_reverted():
    process = _read(PROCESS)
    assert "Text(ctxLabel)" in process
    assert "agent.trend.pace?.marker" in process
    header = re.search(r'Text\("CTX"\)\.frame\(width: (\d+)', process)
    assert header, "no CTX heading width"
    after_label = process[process.index("Text(ctxLabel)"):]
    row = re.search(r"\.frame\(width: (\d+)", after_label)
    assert row, "no CTX cell width"
    assert int(header.group(1)) == int(row.group(1)) == 44


def test_helpers_are_not_a_nested_list():
    for path in (PROCESS, FLEET, DETAIL):
        text = _read(path)
        assert "ForEach(agent.subagentRows" not in text, path.name


# --- the detail screen ---------------------------------------------------------

def test_the_detail_screen_lists_every_helper_name():
    facts = _member(_read(DETAIL), "private var factLines: [String] {")
    assert "helpers   " in facts
    assert "agent.subagents" in facts
    assert "subagentRows.count" not in facts
    assert "Self.contextLine" in facts


# --- not a second tally, not a widget, not a face ------------------------------

def test_the_fleet_tally_is_not_drawn_per_row():
    for path in (PROCESS, FLEET, DETAIL, BRAND):
        text = _read(path)
        assert "counts.subagents" not in text, path.name
        assert "snapshot.counts.subagents" not in text, path.name


def test_the_widget_is_untouched():
    assert "subagent" not in _read(WIDGET_SUMMARY)
    files = sorted(WIDGET_DIR.rglob("*.swift"))
    assert files, "no widget sources found"
    for path in files:
        assert "subagent" not in path.read_text(), path.name


def test_cast_state_is_still_category_only():
    func = _member(_read(CAST),
                   "static func state(for agent: Agent, category: Category) -> State {")
    assert "switch category" in func
    assert "subagent" not in func


def test_fleet_view_does_not_mention_subagent():
    assert "subagent" not in _read(FLEET)
