# host/tests/test_phone_place.py
"""The phone comes back where you left it.

`PhonePlaceStore` (`ios/BobPhone/PhonePlace.swift`) keeps the tab, the Menu
section, the sheet trail and the text typed into it in a protected file
written just before the lock, read behind the Face ID gate, stamped with the
pairing and dropped on an un-pair. The rules — which rungs a saved trail
keeps against the current picture, what outranks a saved place, which agent
page a restore lands on — are Foundation-only and run here under `swiftc`
(`test_phone_background_grace.py`'s seam); the wiring is pinned by source
greps. `ios/BobPhoneTests/PhonePlaceTests.swift` holds the store's file
cases. Contract: `docs/phone-contract.md`, *The phone comes back where you
left it*.
"""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
PHONE = ROOT / "ios" / "BobPhone"
PLACE = PHONE / "PhonePlace.swift"
APP = PHONE / "BobPhoneApp.swift"
CLIENT = PHONE / "Client.swift"
HOST = PHONE / "PhoneSheetHost.swift"
DETAIL = PHONE / "AgentDetailView.swift"
PBXPROJ = ROOT / "ios" / "BobPhone.xcodeproj" / "project.pbxproj"
XCTEST = ROOT / "ios" / "BobPhoneTests" / "PhonePlaceTests.swift"

HARNESS = r'''
import Foundation

struct Rung: Decodable { var kind: String; var key: String }
struct CutCase: Decodable { var trail: [Rung]; var agents: [String]; var cards: [String] }
struct Input: Decodable {
    var cut: [CutCase]
    var wins: [[Bool]]
    var screens: [String]
}

let input = try! JSONDecoder().decode(Input.self,
                                      from: FileHandle.standardInput.readDataToEndOfFile())
var out: [String: Any] = [:]
out["cut"] = input.cut.map { c -> [String: Any] in
    let trail = c.trail.map { PhonePlace.Entry(kind: $0.kind, key: $0.key) }
    let result = PhonePlaceRules.cut(trail, agents: Set(c.agents), cards: Set(c.cards))
    return ["kept": result.kept.map(\.id),
            "dropped": result.dropped.map { [$0.0.id, $0.1] }]
}
out["wins"] = input.wins.map {
    PhonePlaceRules.wins(pendingTab: $0[0], pendingReceipt: $0[1], composerResume: $0[2])
}
out["screens"] = input.screens.map { PhonePlaceRules.restoredScreen($0) }
out["restorable"] = PhonePlaceRules.restorableKinds.sorted()
out["depth"] = PhonePlaceRules.maxDepth
let data = try! JSONSerialization.data(withJSONObject: out, options: [.sortedKeys])
print(String(data: data, encoding: .utf8)!)
'''


def _read(path: pathlib.Path) -> str:
    return path.read_text()


def _code(text: str) -> str:
    return "\n".join(line for line in text.splitlines()
                     if not line.strip().startswith("//"))


def _block(text: str, header: str) -> str:
    """The brace-balanced body following `header`."""
    i = text.index(header)
    depth = 0
    for j in range(i, len(text)):
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                return text[i:j + 1]
    raise AssertionError(header)


@pytest.fixture(scope="module")
def place_bin(tmp_path_factory):
    swiftc = shutil.which("swiftc")
    if not swiftc:
        pytest.fail("swiftc is required on this macOS repository; it was not found")
    folder = tmp_path_factory.mktemp("phone-place")
    main = folder / "main.swift"
    main.write_text(HARNESS)
    binary = folder / "phone-place"
    built = subprocess.run(
        [swiftc, str(PLACE), str(main), "-o", str(binary)],
        capture_output=True, text=True, timeout=180)
    assert built.returncode == 0, built.stderr
    return binary


def _run(binary: pathlib.Path, *, cut=(), wins=(), screens=()) -> dict:
    payload = {"cut": list(cut), "wins": list(wins), "screens": list(screens)}
    proc = subprocess.run([str(binary)], input=json.dumps(payload),
                          capture_output=True, text=True, timeout=30, check=False)
    assert proc.returncode == 0, proc.stderr or proc.stdout
    return json.loads(proc.stdout)


def _rung(kind: str, key: str) -> dict:
    return {"kind": kind, "key": key}


# --- the rules, run ---------------------------------------------------------------


def test_an_agent_gone_at_rung_two_keeps_rung_one_only(place_bin):
    out = _run(place_bin, cut=[{
        "trail": [_rung("card", "c1"), _rung("agent", "s-gone"), _rung("card", "c2")],
        "agents": ["s1"], "cards": ["c1", "c2"],
    }])
    result = out["cut"][0]
    assert result["kept"] == ["card/c1"]
    assert result["dropped"][0] == ["agent/s-gone", "agent gone"]
    # Nothing above the gap is shown over it.
    assert [d[0] for d in result["dropped"]] == ["agent/s-gone", "card/c2"]


def test_a_decision_at_the_bottom_keeps_nothing(place_bin):
    out = _run(place_bin, cut=[{
        "trail": [_rung("decision", "d1"), _rung("agent", "s1")],
        "agents": ["s1"], "cards": [],
    }])
    result = out["cut"][0]
    assert result["kept"] == []
    assert result["dropped"][0] == ["decision/d1", "kind not restorable"]


def test_four_rungs_cap_at_three(place_bin):
    out = _run(place_bin, cut=[{
        "trail": [_rung("agent", "s1"), _rung("card", "c1"),
                  _rung("agent", "s2"), _rung("card", "c2")],
        "agents": ["s1", "s2"], "cards": ["c1", "c2"],
    }])
    result = out["cut"][0]
    assert result["kept"] == ["agent/s1", "card/c1", "agent/s2"]
    assert result["dropped"] == [["card/c2", "trail depth"]]
    assert out["depth"] == 3


def test_catch_up_is_kept_only_whole(place_bin):
    out = _run(place_bin, cut=[
        {"trail": [_rung("catchUp", "all")], "agents": [], "cards": []},
        {"trail": [_rung("catchUp", "r1/r2")], "agents": [], "cards": []},
    ])
    assert out["cut"][0]["kept"] == ["catchUp/all"]
    assert out["cut"][1]["kept"] == []
    assert out["cut"][1]["dropped"][0][1] == "kind not restorable"


def test_a_card_present_is_kept_and_a_card_gone_is_not(place_bin):
    out = _run(place_bin, cut=[
        {"trail": [_rung("card", "c1"), _rung("agent", "s1")],
         "agents": ["s1"], "cards": ["c1"]},
        {"trail": [_rung("card", "c-gone")], "agents": [], "cards": ["c1"]},
    ])
    assert out["cut"][0]["kept"] == ["card/c1", "agent/s1"]
    assert out["cut"][0]["dropped"] == []
    assert out["cut"][1]["dropped"] == [["card/c-gone", "card gone"]]


def test_the_restorable_kinds_are_three_and_never_a_fetched_page(place_bin):
    out = _run(place_bin)
    assert out["restorable"] == ["agent", "card", "catchUp"]
    for kind in ("decision", "workFile", "notification"):
        result = _run(place_bin, cut=[{"trail": [_rung(kind, "x")],
                                       "agents": ["x"], "cards": ["x"]}])
        assert result["cut"][0]["kept"] == [], kind


def test_anything_waiting_wins_over_the_place(place_bin):
    combos = [[a, b, c] for a in (False, True) for b in (False, True) for c in (False, True)]
    out = _run(place_bin, wins=combos)
    assert len(out["wins"]) == 8
    assert out["wins"] == [any(combo) for combo in combos]


def test_terminal_comes_back_as_details(place_bin):
    out = _run(place_bin, screens=["Terminal", "Conversation", "Details", "Main",
                                   "bogus", ""])
    assert out["screens"] == ["Details", "Conversation", "Details", "Main",
                              "Main", "Main"]


# --- the file -----------------------------------------------------------------------


def test_the_rule_file_is_foundation_only_and_protected():
    text = _read(PLACE)
    assert "import SwiftUI" not in text
    assert "import CryptoKit" not in text
    assert "import Foundation" in text
    assert "completeFileProtection" in text
    assert 'static let fileName = "place.json"' in text
    assert "@MainActor" in text
    # The pairing is a hash handed in, never the token.
    code = _code(text)
    assert "token" not in code.lower()
    # The file holds the person's words: it builds no URL and reaches no
    # transport.
    for stranger in ("URLSession", "RelayChannel", "PhoneClient", "UserDefaults"):
        assert stranger not in code, stranger


# --- the wiring -----------------------------------------------------------------------


def test_the_place_is_flushed_before_each_lock():
    app = _read(APP)
    assert app.count("place.flushNow()") == 2
    inactive = app.split("case .inactive:")[1].split("case .background:")[0]
    background = app.split("case .background:")[1].split("@unknown default:")[0]
    for branch in (inactive, background):
        assert branch.count("place.flushNow()") == 1
        assert branch.index("outbox.flushDraftNow()") < branch.index("place.flushNow()")
        assert branch.index("place.flushNow()") < branch.index("lock.lock()")


def test_the_gate_loads_adopts_then_arms():
    app = _read(APP)
    gate = app[app.index(".onChange(of: lock.unlocked)"):]
    gate = gate[:gate.index(".onChange(of: pairing.record)")]
    assert gate.index("client.notificationLog.load()") < gate.index("place.load()")
    assert gate.index("place.load()") < gate.index("outbox.armResumeIfNeeded()")
    pair = gate.index("PhoneRouter.shared.pair(token: record.token)")
    adopt = gate.index("place.adopt(identity: pairingIdentity(token: record.token))")
    arm = gate.index("place.arm()")
    assert pair < adopt < arm
    assert gate.index("outbox.armResumeIfNeeded()") < arm
    # No record: nothing of an old pairing's place survives.
    unpaired = gate[gate.index("} else {"):]
    assert "place.forget()" in unpaired
    # The held picture's pin still sees its load early in the gate.
    assert "client.heldPicture.load()" in gate[:1500]
    # The identity is the router's: SHA-256 hex of the token.
    helper = _block(app, "private func pairingIdentity(token: String) -> String")
    assert "SHA256.hash(data: Data(token.utf8))" in helper
    assert "import CryptoKit" in app
    # A pairing made while unlocked is adopted too.
    handler = _block(app, ".onChange(of: pairing.record) { old, record in")
    assert handler.index("PhoneRouter.shared.pair(token: record.token)") < handler.index(
        "place.adopt(identity: pairingIdentity(token: record.token))")


def test_forget_drops_the_place():
    client = _read(CLIENT)
    assert client.count("PhonePlaceStore.shared.forget()") == 1
    forget = _block(client, "private func forgetPairing()")
    assert forget.index("PhoneRouter.shared.forget()") < forget.index(
        "PhonePlaceStore.shared.forget()")


def test_the_place_applies_first_and_steps_aside_for_a_tap_or_a_draft():
    app = _read(APP)
    content = app[app.index("struct ContentView: View {"):app.index("struct PhoneTabRoot")]
    arrival = _block(content, ".onAppear {")
    assert arrival.index("applyPlace()") < arrival.index("applyResumeTab()") \
        < arrival.index("applyPendingTab()")
    assert "place.compose = { currentPlace() }" in arrival
    assert content.count(".onAppear {") == 1
    assert ".onDisappear { place.compose = nil }" in content
    assert "place.setAside()" in _block(content, "private func applyPendingTab()")
    assert "place.setAside()" in _block(content, "private func applyResumeTab()")
    apply = _block(content, "private func applyPlace()")
    assert "PhonePlaceRules.wins(" in apply
    assert apply.index("PhonePlaceRules.wins(") < apply.index("place.take()")
    # The trail waits for a picture and is resolved, never fetched.
    trail = _block(content, "private func applyPlaceTrailIfReady()")
    assert "client.snapshot.generatedAt != 0" in trail
    assert "PhonePlaceRules.cut(" in trail
    assert "PhoneSheet.fromPlace(" in trail
    assert "sheets.restore(" in trail
    assert "fetch" not in _code(trail)
    assert ".onChange(of: client.snapshot.generatedAt) { applyPlaceTrailIfReady() }" in content
    for moved in ("selectedTab", "menuOpen", "sheets.stack"):
        line = content[content.index(f".onChange(of: {moved}) {{"):]
        assert "stashPlace()" in line[:line.index("\n")], moved


def test_the_sheet_router_restores_in_one_assignment():
    host = _code(_read(HOST))
    restore = _block(host, "func restore(_ trail: [(PhoneSheet, PhonePlace.Entry)])")
    assert "show(" not in restore
    assert restore.count("entryStates = ") == 1 and restore.count("stack = ") == 1
    assert "PhoneSheetEntryState(restoring:" in restore
    assert "Self.MAX_DEPTH" in restore
    state = _block(host, "final class PhoneSheetEntryState")
    assert "var agentScreen: AgentScreen = AgentScreen.defaultScreen" in state
    assert "PhonePlaceRules.restoredScreen(" in state
    assert "PhonePlaceStore.shared.takeOrphanDraft(for: entry.id)" in host
    # The pins this plan must keep.
    assert "static let MAX_DEPTH = 3" in host
    assert "let id = UUID()" in state


def test_the_agent_page_is_remembered_and_terminal_never_is():
    detail = _code(_read(DETAIL))
    assert "@State private var hostedTab: DetailTab = DetailTab.defaultTab" in detail
    assert "screen = sheetEntry?.agentScreen ?? AgentScreen.defaultScreen(" in detail
    assert "sheetEntry?.agentScreen = next == .terminal ? .details : next" in detail
    reset = _block(detail, ".onChange(of: client.snapshot.board.conversationSupported)")
    assert "if screen == AgentScreen.defaultScreen {" in reset


def test_both_swift_files_are_registered_in_the_project():
    pbx = _read(PBXPROJ)
    lines = pbx.splitlines()
    # App file: a build file, a file reference, a group child and a Sources
    # entry; the test file: the same four, the last two inline in the test
    # target's lists.
    assert sum("/* PhonePlace.swift" in line for line in lines) == 4
    assert sum("/* PhonePlaceTests.swift" in line for line in lines) == 4
    assert "path = BobPhone/PhonePlace.swift" in pbx
    assert "path = BobPhoneTests/PhonePlaceTests.swift" in pbx
    assert XCTEST.exists()


def test_the_widget_and_the_background_run_never_name_the_place():
    for folder in ("BobPhoneWidget", "BobPhoneNotification"):
        for path in (ROOT / "ios" / folder).glob("*.swift"):
            assert "PhonePlace" not in path.read_text(), path.name
    assert "PhonePlace" not in _read(PHONE / "BackgroundRefresh.swift")


# --- audit repairs (dispatch 2) ---------------------------------------------------


def test_a_restored_edit_quotes_the_revision_it_was_typed_against():
    """A touched card edit restored after a lock must still be refused when
    the Mac changed the card meanwhile: the saved draft carries the
    revision, the rung hands it back as `editRevision`, and the card
    screen's Save quotes it while touched rather than the fresh fetch's."""
    place = _code(_read(PLACE))
    draft = _block(place, "struct CardDraft: Codable, Equatable")
    assert "var baseRevision: Int?" in draft
    assert "case touched, baseRevision, editing, messageOpen, messageText" in draft
    host = _code(_read(HOST))
    assert "@Published var editRevision: Int?" in host
    held = _block(host, "init?(held: PhoneCardDraftState)")
    assert "held.draftTouched ? held.editRevision : nil" in held
    fill = _block(host, "func fill(_ held: PhoneCardDraftState)")
    assert "guard touched, let baseRevision else { return }" in fill
    # The editor reopens only with its own text (audit, dispatch 3).
    assert fill.index("guard touched, let baseRevision") < fill.index("held.editing = editing")
    assert fill.index("held.editRevision = baseRevision") < fill.index("held.draftTouched = true")
    card = _code(_read(PHONE / "CardDetailView.swift"))
    shown = _block(card, "private var shownRevision: Int")
    assert "if draftTouched, let base = retained.editRevision { return base }" in shown
    touched = _block(card, "private var draftTouched: Bool")
    assert "retained.editRevision = seedRevision" in touched
    assert "if !newValue { retained.editRevision = nil }" in touched
    save = _block(card, "private func saveCard(expecting: Int? = nil) async")
    assert '"expected_revision": String(expecting ?? shownRevision)' in save


def test_the_status_note_is_the_macs_words_and_never_saved():
    place = _code(_read(PLACE))
    draft = _block(place, "struct CardDraft: Codable, Equatable")
    assert "note" not in draft
    host = _code(_read(HOST))
    for header in ("init?(held: PhoneCardDraftState)", "func fill(_ held: PhoneCardDraftState)"):
        assert "note" not in _block(host, header), header


def test_a_waiting_trail_is_dropped_when_the_person_moves_first():
    app = _read(APP)
    content = app[app.index("struct ContentView: View {"):app.index("struct PhoneTabRoot")]
    assert ".onChange(of: selectedTab) { dropWaitingTrailIfMoved(); stashPlace() }" in content
    assert ".onChange(of: menuOpen) { dropWaitingTrailIfMoved(); stashPlace() }" in content
    drop = _block(content, "private func dropWaitingTrailIfMoved()")
    assert "place.keepDrafts(of: trail)" in drop
    assert "pendingTrail = nil" in drop
    apply = _block(content, "private func applyPlace()")
    assert apply.index("placeLanding = PlaceLanding(") < apply.index("applyPlaceTrailIfReady()")
