# host/tests/test_review_rules.py
"""The Review section's shared rules, `test_comm_rules.py`'s shape.

`ReviewRules` is one Foundation-only enum, byte-pinned Mac/phone from the
`enum ReviewRules {` marker down, and its functions are run under `swiftc`
rather than read. The fingerprint material is also checked against the
daemon's own spelling (`inbox_ack.review_picks_material`), because Dismiss is
refused when the two disagree.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from dark_army_daemon import inbox_ack, review_run

REPO = Path(__file__).resolve().parents[2]
PANEL_FILE = REPO / "panel" / "Sources" / "BobPanel" / "ReviewRules.swift"
PHONE_FILE = REPO / "ios" / "BobPhone" / "ReviewRules.swift"
MARKER = "enum ReviewRules {"


def _shared(path: Path) -> str:
    lines = path.read_text().splitlines(keepends=True)
    starts = [i for i, line in enumerate(lines) if line.rstrip("\n") == MARKER]
    assert len(starts) == 1, f"expected one {MARKER!r} line in {path}"
    return "".join(lines[starts[0]:])


def test_the_panel_and_phone_share_one_rule():
    panel, phone = _shared(PANEL_FILE), _shared(PHONE_FILE)
    for name, region in (("panel", panel), ("phone", phone)):
        assert len(region) >= 1500, f"parsed too little from the {name} copy"
        assert "static func stateWord(_ state: String) -> String" in region
        assert "static func canStart(" in region
        assert "static func canContinue(state: String) -> Bool" in region
        assert "static func picksEntry(" in region
        assert "static func isReviewRow(" in region
        assert 'static let defaultProvider = "claude"' in region
    assert panel == phone, (
        "ios/BobPhone/ReviewRules.swift has drifted from the panel's copy — "
        "mirror the edit onto the other side; never re-baseline one side.")
    for path in (PANEL_FILE, PHONE_FILE):
        assert "import Foundation" in path.read_text()
        assert "SwiftUI" not in _shared(path)


def test_the_state_words_cover_every_state_the_daemon_publishes():
    region = _shared(PANEL_FILE)
    for state in review_run.STATES:
        assert f'case "{state}"' in region, state


HARNESS = r'''
struct In: Decodable {
    let mode: String
    let state: String?
    let root: String?
    let tool: String?
    let supported: Bool?
    let dispatchEnabled: Bool?
    let runId: String?
    let scopeLine: String?
    let count: Int?
    let findingsAt: Double?
    let sessionId: String?
    let runSessions: [String]?
    let picks: [Int]?
}
let input = try! JSONDecoder().decode(In.self, from: FileHandle.standardInput.readDataToEndOfFile())
var out: [String: Any] = [:]
switch input.mode {
case "state":
    let s = input.state!
    out["word"] = ReviewRules.stateWord(s)
    out["canContinue"] = ReviewRules.canContinue(state: s)
    out["showsEnd"] = ReviewRules.showsEnd(state: s)
    out["editable"] = ReviewRules.picksEditable(state: s)
case "start":
    out["canStart"] = ReviewRules.canStart(root: input.root!, tool: input.tool!,
        supported: input.supported!, dispatchEnabled: input.dispatchEnabled!)
case "entry":
    if let e = ReviewRules.picksEntry(runId: input.runId!, state: input.state!,
        scopeLine: input.scopeLine!, findingCount: input.count!) {
        out["key"] = e.key
        out["detail"] = e.detail
    }
    out["material"] = ReviewRules.fingerprintMaterial(runId: input.runId!,
        findingsAt: input.findingsAt!)
case "row":
    out["isReviewRow"] = ReviewRules.isReviewRow(sessionId: input.sessionId!,
        runSessions: input.runSessions!)
default:
    out["fix"] = ReviewRules.fixList(Set(input.picks!))
}
let data = try! JSONSerialization.data(withJSONObject: out, options: [.sortedKeys])
FileHandle.standardOutput.write(data)
'''


@pytest.fixture(scope="module")
def probe(tmp_path_factory):
    swiftc = shutil.which("swiftc")
    if not swiftc:
        pytest.skip("Swift toolchain unavailable")
    tmp = tmp_path_factory.mktemp("review-probe")
    path = tmp / "ReviewProbe.swift"
    path.write_text(PANEL_FILE.read_text() + "\n" + HARNESS)
    executable = tmp / "ReviewProbe"
    built = subprocess.run([swiftc, str(path), "-o", str(executable)],
                           capture_output=True, text=True, timeout=180)
    assert built.returncode == 0, built.stderr

    def run(payload: dict) -> dict:
        ran = subprocess.run([str(executable)], input=json.dumps(payload),
                             capture_output=True, text=True, timeout=30)
        assert ran.returncode == 0, ran.stderr
        return json.loads(ran.stdout)

    return run


@pytest.mark.parametrize("state,word,cont,end,editable", [
    ("reviewing", "reviewing", False, True, False),
    ("picks", "pick the fixes", True, True, True),
    ("fixing", "fixing and shipping", False, True, False),
    ("done", "finished", False, False, False),
    ("exited", "terminal ended", False, False, False),
    ("ended", "ended", False, False, False),
    ("from-the-future", "from-the-future", False, False, False),
])
def test_the_state_over_every_shape(probe, state, word, cont, end, editable):
    out = probe({"mode": "state", "state": state})
    assert out == {"word": word, "canContinue": cont, "showsEnd": end,
                   "editable": editable}


@pytest.mark.parametrize("root,tool,supported,enabled,expected", [
    ("/p", "claude", True, True, True),
    ("", "claude", True, True, False),
    ("/p", "", True, True, False),
    ("/p", "claude", False, True, False),
    ("/p", "claude", True, False, False),
])
def test_start_is_enabled_only_when_every_term_holds(
        probe, root, tool, supported, enabled, expected):
    out = probe({"mode": "start", "root": root, "tool": tool,
                 "supported": supported, "dispatchEnabled": enabled})
    assert out["canStart"] is expected


def test_the_entry_is_made_only_for_a_run_waiting_on_picks(probe):
    out = probe({"mode": "entry", "runId": "ab12cd34", "state": "picks",
                 "scopeLine": "no remote branch, uncommitted changes only",
                 "count": 1, "findingsAt": 1700.9})
    assert out["key"] == "r:ab12cd34"
    assert out["detail"] == ("no remote branch, uncommitted changes only"
                             " — 1 finding — pick the fixes")
    out = probe({"mode": "entry", "runId": "ab12cd34", "state": "fixing",
                 "scopeLine": "", "count": 3, "findingsAt": 5})
    assert "key" not in out


def test_the_fingerprint_material_is_the_daemons_spelling(probe):
    for stamp in (0.0, 1700.9, 1700.0, 99999999.5):
        out = probe({"mode": "entry", "runId": "ab12cd34", "state": "picks",
                     "scopeLine": "", "count": 0, "findingsAt": stamp})
        assert out["material"] == inbox_ack.review_picks_material(
            "ab12cd34", stamp), stamp


def test_a_review_row_is_known_by_its_session(probe):
    assert probe({"mode": "row", "sessionId": "s1",
                  "runSessions": ["s1", "s2"]})["isReviewRow"] is True
    assert probe({"mode": "row", "sessionId": "s3",
                  "runSessions": ["s1"]})["isReviewRow"] is False
    assert probe({"mode": "row", "sessionId": "",
                  "runSessions": [""]})["isReviewRow"] is False


def test_the_picks_go_on_the_wire_ascending(probe):
    assert probe({"mode": "fix", "picks": [3, 1, 2]})["fix"] == [1, 2, 3]


def test_only_picks_and_done_cover_a_plain_wait():
    """While a run is fixing, a wait on its terminal is real news."""
    for path in (PANEL_FILE, PHONE_FILE):
        assert 'coveredStates: Set<String> = ["picks", "done"]' in path.read_text()
