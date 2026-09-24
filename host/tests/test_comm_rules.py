# host/tests/test_comm_rules.py
"""The Comm tab's shared rules and its phone view.

`CommRules` is one Foundation-only enum, byte-pinned Mac/phone from the
`enum CommRules {` marker down (`test_run_figures.py`'s idiom), and its two
functions are run under `swiftc` rather than read (`test_cast.py`'s idiom).
The phone view is checked by grep for the rules `docs/phone-contract.md`
already pins on every other screen: no clipped prose, no text-size ceiling,
no spinner, every glyph a labelled button.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PANEL_FILE = REPO / "panel" / "Sources" / "BobPanel" / "CommRules.swift"
PHONE_FILE = REPO / "ios" / "BobPhone" / "CommRules.swift"
COMM_VIEW = REPO / "ios" / "BobPhone" / "CommView.swift"
APP = REPO / "ios" / "BobPhone" / "BobPhoneApp.swift"
PROJECT = REPO / "ios" / "BobPhone.xcodeproj" / "project.pbxproj"
MARKER = "enum CommRules {"


def _shared(path: Path) -> str:
    lines = path.read_text().splitlines(keepends=True)
    starts = [i for i, line in enumerate(lines) if line.rstrip("\n") == MARKER]
    assert len(starts) == 1, f"expected one {MARKER!r} line in {path}"
    return "".join(lines[starts[0]:])


# --- the byte-pinned pair ------------------------------------------------------


def test_the_panel_and_phone_share_one_rule():
    panel, phone = _shared(PANEL_FILE), _shared(PHONE_FILE)
    for name, region in (("panel", panel), ("phone", phone)):
        assert len(region) >= 800, f"parsed too little from the {name} copy"
        assert "static func line(from text: String) -> String" in region
        assert "static func status(" in region
        assert "static func canAsk(status: String) -> Bool" in region
        assert "static func showsEnd(status: String) -> Bool" in region
        assert "What is everyone working on?" in region
        assert "Any pitfalls?" in region
    assert panel == phone, (
        "ios/BobPhone/CommRules.swift has drifted from the panel's copy — "
        "mirror the edit onto the other side; never re-baseline one side.")
    for path in (PANEL_FILE, PHONE_FILE):
        text = path.read_text()
        assert "import Foundation" in text
        assert "SwiftUI" not in _shared(path)


def test_the_phone_project_compiles_both_new_files():
    project = PROJECT.read_text()
    assert "CommRules.swift in Sources" in project
    assert "CommView.swift in Sources" in project


# --- the rules, run ------------------------------------------------------------


HARNESS = r'''
struct In: Decodable {
    let mode: String
    let text: String?
    let available: Bool?
    let alive: Bool?
    let exited: Bool?
    let sessionId: String?
    let rowState: String?
    let originBy: String?
    let missionSessionId: String?
}
let input = try! JSONDecoder().decode(In.self, from: FileHandle.standardInput.readDataToEndOfFile())
var out: [String: Any] = [:]
if input.mode == "line" {
    out["line"] = CommRules.line(from: input.text!)
} else if input.mode == "mission_row" {
    out["isMissionRow"] = CommRules.isMissionRow(
        sessionId: input.sessionId!, originBy: input.originBy!,
        missionSessionId: input.missionSessionId!)
} else {
    let status = CommRules.status(available: input.available!, alive: input.alive!,
                                  exited: input.exited!, sessionId: input.sessionId!,
                                  rowState: input.rowState!)
    out["status"] = status
    out["canAsk"] = CommRules.canAsk(status: status)
    out["showsEnd"] = CommRules.showsEnd(status: status)
    out["showsOpen"] = CommRules.showsOpen(status: status)
    out["chips"] = CommRules.chips.map { [$0.label, $0.question] }
}
let data = try! JSONSerialization.data(withJSONObject: out, options: [.sortedKeys])
FileHandle.standardOutput.write(data)
'''


@pytest.fixture(scope="module")
def probe(tmp_path_factory):
    swiftc = shutil.which("swiftc")
    if not swiftc:
        pytest.skip("Swift toolchain unavailable")
    tmp = tmp_path_factory.mktemp("comm-probe")
    path = tmp / "CommProbe.swift"
    path.write_text(PANEL_FILE.read_text() + "\n" + HARNESS)
    executable = tmp / "CommProbe"
    built = subprocess.run([swiftc, str(path), "-o", str(executable)],
                           capture_output=True, text=True, timeout=180)
    assert built.returncode == 0, built.stderr

    def run(payload: dict) -> dict:
        ran = subprocess.run([str(executable)], input=json.dumps(payload),
                             capture_output=True, text=True, timeout=30)
        assert ran.returncode == 0, ran.stderr
        return json.loads(ran.stdout)

    return run


def test_line_collapses_whitespace_clamps_and_empties(probe):
    assert probe({"mode": "line", "text": "a\n\nb  c"})["line"] == "a b c"
    assert probe({"mode": "line", "text": "   \n\t "})["line"] == ""
    assert len(probe({"mode": "line", "text": "x" * 2001})["line"]) == 2000
    assert probe({"mode": "line", "text": " What's the timeline? "})["line"] \
        == "What's the timeline?"


def test_the_clamp_counts_code_points_as_the_daemon_does(probe):
    """`terminal_input` refuses a line over `TERMINAL_MAX_INPUT_CHARS` by
    Python's `len()` — code points. A Swift `Character` can be several
    scalars (`e` + combining acute is one Character, two code points), so
    a Character clamp could still hand the daemon a line it refuses."""
    from dark_army_daemon.daemon import TERMINAL_MAX_INPUT_CHARS
    assert TERMINAL_MAX_INPUT_CHARS == 2000
    grapheme = "e\u0301"
    assert len(grapheme) == 2
    out = probe({"mode": "line", "text": grapheme * 1500})["line"]
    assert len(out) == 2000, "clamped on code points, not Characters"
    assert out.startswith(grapheme) and out.endswith(grapheme)
    flag = "\U0001F1F5\U0001F1F1"  # one flag, two scalars
    out = probe({"mode": "line", "text": flag * 1001})["line"]
    assert len(out) == 2000


_STATUS_SHAPES = [
    ({"available": False, "alive": False, "exited": False, "sessionId": "", "rowState": ""},
     "older Mac", False, False, False),
    ({"available": True, "alive": False, "exited": False, "sessionId": "", "rowState": ""},
     "off", False, False, True),
    ({"available": True, "alive": False, "exited": True, "sessionId": "s", "rowState": ""},
     "ended", False, False, True),
    ({"available": True, "alive": True, "exited": False, "sessionId": "", "rowState": ""},
     "starting", False, True, False),
    ({"available": True, "alive": True, "exited": False, "sessionId": "s", "rowState": "running"},
     "thinking", True, True, False),
    ({"available": True, "alive": True, "exited": False, "sessionId": "s", "rowState": "sleeping"},
     "ready", True, True, False),
]


@pytest.mark.parametrize("shape,expected,ask,end,shows_open", _STATUS_SHAPES)
def test_status_over_the_six_shapes(probe, shape, expected, ask, end, shows_open):
    out = probe({"mode": "status", **shape})
    assert out["status"] == expected
    assert out["canAsk"] is ask
    assert out["showsEnd"] is end
    assert out["showsOpen"] is shows_open


@pytest.mark.parametrize("shape,expected,ask,end,shows_open", _STATUS_SHAPES)
def test_shows_open_over_the_six_shapes(probe, shape, expected, ask, end, shows_open):
    """`showsOpen` is executed, not only byte-pinned: off and ended, nothing else."""
    del expected, ask, end
    assert probe({"mode": "status", **shape})["showsOpen"] is shows_open


def test_the_four_chips(probe):
    out = probe({"mode": "status", "available": True, "alive": True,
                 "exited": False, "sessionId": "s", "rowState": ""})
    chips = out["chips"]
    assert len(chips) == 4
    assert [c[1] for c in chips][0] == "What is everyone working on?"
    assert "What's the timeline?" in [c[1] for c in chips]
    assert "Any pitfalls?" in [c[1] for c in chips]
    assert any(c[1].startswith("Find a card") for c in chips)
    for label, question in chips:
        assert label.strip() and question.strip()


@pytest.mark.parametrize("sid,by,mission,expected", [
    ("s1", "", "s1", True),        # the record's last-bound id
    ("s2", "mission", "", True),   # the stamp, before the record learns the id
    ("s2", "mission", "s1", True),
    ("s3", "", "s1", False),       # another session
    ("", "", "", False),           # nothing known — never everything
    ("s4", "card-start", "", False),
])
def test_a_mission_row_is_known_by_id_or_stamp(probe, sid, by, mission, expected):
    out = probe({"mode": "mission_row", "sessionId": sid, "originBy": by,
                 "missionSessionId": mission})
    assert out["isMissionRow"] is expected


FLEET_VIEW = REPO / "ios" / "BobPhone" / "FleetView.swift"


def test_the_phone_fleet_keeps_mission_control_out_of_the_project_lists():
    """Mission Control lives in Dark Army's checkout, so without the rule it
    is listed as work on that project; its home is the Comm tab."""
    text = FLEET_VIEW.read_text()
    start = text.index("private var allRows: [Row] {")
    body = text[start:text.index("private var allLive", start)]
    assert "CommRules.isMissionRow(" in body
    assert "missionSessionId: mission" in body
    assert "snapshot.mission.sessionId" in body
    # The needs-you chip reads the same filtered rows, not the raw bucket.
    start = text.index("private var waitingProjects: Set<String> {")
    body = text[start:text.index("\n    }", start)]
    assert "allRows" in body and "snapshot.agents.waiting" not in body


PANEL_VIEW = REPO / "panel" / "Sources" / "BobPanel" / "PanelView.swift"


def test_the_mac_table_keeps_mission_control_out_of_the_project_lists():
    """The same rule on the Mac: every bucket the process table draws goes
    through one `rows(_:)` that drops Mission Control's session, so the
    project tabs, the ACTIVE count and the badges never list it as work on
    Dark Army's checkout (Cipher under dark-army, 21 Sep 2026)."""
    text = PANEL_VIEW.read_text()
    start = text.index("private func rows(_ category: Category) -> [SectionRow] {")
    body = text[start:text.index("\n    }", start)]
    assert "CommRules.isMissionRow(" in body
    assert "missionSessionId: mission" in body
    assert "snapshot.mission.sessionId" in body
    # No bucket is read raw: every `.rows(snapshot.agents)` outside the helper
    # would be a list that still shows Mission Control.
    rest = text[:start] + text[start + len(body):]
    assert ".rows(snapshot.agents)" not in rest
    for name in ("liveRows", "finishedRows", "abandonedRows"):
        s = text.index(f"private var {name}: [SectionRow] {{")
        assert "rows(" in text[s:text.index("}", s) + 1]


# --- the phone view ------------------------------------------------------------


def _code(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return "\n".join(line.split("//")[0] for line in text.splitlines())


def test_the_closed_tab_leads_with_one_open_button():
    """Off or ended, one Open leads the column, before the reply, and the
    old second press is gone."""
    raw = COMM_VIEW.read_text()
    assert "Try again" not in raw
    code = _code(raw)
    assert code.count('Text("Open Mission Control")') == 1
    body = code[code.index("var body: some View"):code.index("static let olderMacSentence")]
    gate = body.index("CommRules.showsOpen")
    label = body.index('Text("Open Mission Control")')
    reply_at = body.index("\n                    reply\n")
    assert gate < label < reply_at
    controls = code[code.index("private var controls"):code.index("private var terminalScreen")]
    assert 'Text("Open Mission Control")' not in controls


def test_the_comm_view_clips_nothing_and_caps_no_text_size():
    code = _code(COMM_VIEW.read_text())
    caps = [m for m in re.findall(r"\.lineLimit\([^)]*\)", code)]
    assert caps == [".lineLimit(5...)"], caps
    assert ".dynamicTypeSize(" not in code
    assert "ProgressView" not in code
    assert ".fixedSize(horizontal: false, vertical: true)" in code
    assert ".textSelection(.enabled)" in code
    assert "MarkdownText(source: text, base: 13, mono: true)" in code


def test_every_glyph_in_the_comm_view_is_a_labelled_button():
    lines = COMM_VIEW.read_text().splitlines()
    for i, line in enumerate(lines):
        if "Image(systemName:" not in line:
            continue
        window = "\n".join(lines[i:i + 15])
        assert ".accessibilityLabel(" in window or ".accessibilityHidden(true)" in window, i + 1


def test_the_comm_view_writes_through_the_chosen_verbs_and_the_text_route():
    code = _code(COMM_VIEW.read_text())
    assert "PhoneActions.missionOpen" in code
    assert "PhoneActions.missionEnd" in code
    assert "PhoneActions.terminalInput" in code
    assert '"text": line' in code and '"bytes"' not in code
    assert "client.watchTerminal(mission.sessionId)" in code
    assert "client.watchTerminal(nil)" in code
    assert "PhoneTerminalPane(agent: missionAgent, client: client)" in code
    assert 'MicButton(id: "comm.ask", text: $draft)' in code
    assert "arm.confirm(.missionEnd)" in code
    assert "openedThisVisit" in code, "one open per appearance"
    assert "Timer" not in code and ".task(" not in code, "never retry on a timer"


def test_the_mac_rail_says_starting_only_while_opening():
    """Send and End keep the snapshot's status word; only Open rewrites it
    to "starting" — the phone's `opening` rule, on the Mac."""
    rail = REPO / "panel" / "Sources" / "BobPanel" / "CommRail.swift"
    code = _code(rail.read_text())
    assert '@State private var opening = false' in code
    assert 'working ? "starting"' not in code
    assert code.count('opening ? "starting" : status') >= 2, \
        "the rail's status line and the column's header"
    phone = _code(COMM_VIEW.read_text())
    assert "@State private var opening = false" in phone
    assert 'if opening { return "starting" }' in phone


def test_the_app_has_five_tab_roots_and_the_comm_label():
    app = APP.read_text()
    assert app.count("PhoneTabRoot(path:") == 5
    assert app.count('Label("Comm"') == 1
    assert 'PhoneTabRoot(path: "~/comm"' in app
