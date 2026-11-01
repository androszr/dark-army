"""The phone's card screen says which batch a refining card belongs to.

A refinement batch writes `batch_rank` beside `refine_state` on every member;
the daemon's `batch` mark is deliberately absent on a refining Prep card, so
the phone draws "card n of a batch planning session" off the raw rank, only
while the card is refining in Prep. The words are the first clause of the
daemon's `BATCH_UNPLANNED_NOTE`, which a member the session never reached
keeps verbatim as `dispatch_error`, with Refine offered beneath.

Contract: `docs/phone-contract.md`, *Several Prep cards are refined from the
phone at once*. The `swiftc` harness decodes a snapshot over `SOURCES`.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess

import pytest

from dark_army_daemon.daemon_board import BATCH_UNPLANNED_NOTE, BoardVerbsMixin
from tests.test_phone_action_queue import SOURCES

ROOT = pathlib.Path(__file__).resolve().parents[2]
PHONE = ROOT / "ios" / "BobPhone"
MODELS = PHONE / "Models.swift"
CARD = PHONE / "CardDetailView.swift"
XCTEST = ROOT / "ios" / "BobPhoneTests" / "RowSelectionTests.swift"
CONTRACT = ROOT / "docs" / "phone-contract.md"
CONTEXT_PANEL = ROOT / "docs" / "context-panel.md"
PANEL = ROOT / "panel" / "Sources" / "BobPanel"

LINE = "of a batch planning session"

HARNESS = r'''
import Foundation

enum PhoneClient {
    static let autostartSettlingKey = "board_autostart"
    static func parallelSettlingKey(_ root: String) -> String { "parallel:\(root)" }
}
enum PrepareRoute: Equatable { case mac, phone }
enum OutboxStore {
    static let backoffStart: TimeInterval = 10
    static let backoffCap: TimeInterval = 300
}

@main
enum Runner {
    @MainActor
    static func main() throws {
        let data = FileHandle.standardInput.readDataToEndOfFile()
        let snap = try JSONDecoder().decode(Snapshot.self, from: data)
        for card in snap.board.cards {
            print("PLANNING \(card.id) \(card.batchPlanningLine)")
            print("BATCHLINE \(card.id) \(card.batchLine)")
            print("ERROR \(card.id) \(card.dispatchError)")
            print("RANK \(card.id) \(card.batchRank)")
        }
    }
}
'''

_PER_CARD = ("PLANNING", "BATCHLINE", "ERROR", "RANK")


def _read(path: pathlib.Path) -> str:
    return path.read_text()


@pytest.fixture(scope="module")
def rank_bin(tmp_path_factory):
    swiftc = shutil.which("swiftc")
    if not swiftc:
        pytest.fail("swiftc is required on this macOS repository; it was not found")
    folder = tmp_path_factory.mktemp("phone-card-batch-rank")
    main = folder / "rank_main.swift"
    main.write_text(HARNESS)
    binary = folder / "phone-card-batch-rank"
    sources = [str(PHONE / name) for name in SOURCES]
    built = subprocess.run(
        [swiftc] + sources + [str(main), "-o", str(binary)],
        capture_output=True, text=True, timeout=240)
    assert built.returncode == 0, built.stderr
    return binary


def _card(**fields) -> dict:
    card = {"id": "c1", "column_name": "prep"}
    card.update(fields)
    return card


def _run(binary: pathlib.Path, cards: list[dict]) -> dict:
    home = binary.parent / "home"
    home.mkdir(exist_ok=True)
    payload = json.dumps({"board": {"available": True, "cards": cards}})
    proc = subprocess.run([str(binary)], input=payload, capture_output=True,
                          text=True, timeout=30, check=False,
                          env={**os.environ, "HOME": str(home)})
    assert proc.returncode == 0, proc.stderr or proc.stdout
    out: dict = {key: {} for key in _PER_CARD}
    for line in proc.stdout.splitlines():
        key, _, rest = line.partition(" ")
        if key in out:
            card_id, _, value = rest.partition(" ")
            out[key][card_id] = value
    return out


# --- the rule, run ------------------------------------------------------------

def test_the_line_is_drawn_off_batch_rank_only_while_refining_in_prep(rank_bin):
    cards = [
        _card(id="live", refine_state="live", batch_rank="2"),
        _card(id="disp", refine_state="dispatching", batch_rank="1"),
        _card(id="idle", refine_state="", batch_rank="2"),
        _card(id="norank", refine_state="live", batch_rank=""),
        _card(id="zero", refine_state="live", batch_rank="0"),
        _card(id="junk", refine_state="live", batch_rank="x"),
        _card(id="absent", refine_state="live"),
        _card(id="backlog", column_name="backlog", refine_state="live",
              batch_rank="2"),
        _card(id="impl", batch_rank="2",
              batch={"rank": 2, "size": 3, "state": "left"}),
    ]
    full = _run(rank_bin, cards)
    planning = full["PLANNING"]
    assert planning["live"] == "card 2 of a batch planning session"
    assert planning["disp"] == "card 1 of a batch planning session"
    for quiet in ("idle", "norank", "zero", "junk", "absent", "backlog", "impl"):
        assert planning[quiet] == "", quiet
    assert full["BATCHLINE"]["impl"] == "BATCH 2/3 · left the line"
    assert full["BATCHLINE"]["live"] == ""


def test_the_frame_survives_a_non_string_rank(rank_bin):
    out = _run(rank_bin, [_card(id="int", refine_state="live", batch_rank=2)])
    assert out["PLANNING"]["int"] == ""
    assert "int" in out["RANK"]


def test_the_line_is_the_first_clause_of_the_macs_unplanned_note():
    assert BATCH_UNPLANNED_NOTE.startswith("card {rank} " + LINE)
    assert _read(MODELS).count('"card \\(rank) ' + LINE + '"') == 1
    for name in ("BoardCardView.swift", "BoardModels.swift"):
        assert LINE not in _read(PANEL / name), name


def test_the_unplanned_note_reaches_the_card_screen_in_the_macs_words(rank_bin):
    note = BATCH_UNPLANNED_NOTE.format(rank="2")
    out = _run(rank_bin, [_card(id="ended", refine_state="ended", batch_rank="",
                                dispatch_error=note)])
    assert out["ERROR"]["ended"] == note
    assert out["PLANNING"]["ended"] == ""
    card = _read(CARD)
    status = card.split("private var statusSection: some View {", 1)[1].split(
        "\n    }\n", 1)[0]
    assert "card.dispatchError" in status
    can_refine = card.split("private var canRefine: Bool {", 1)[1].split(
        "\n    }\n", 1)[0]
    assert 'PhoneRowSelection.tickable("prep"' in can_refine


def test_the_rank_and_the_note_ride_the_snapshot_untrimmed():
    card = {"id": "c", "batch_id": "t", "batch_rank": "2",
            "dispatch_error": "x", "prompt": "", "summary": ""}
    kept = BoardVerbsMixin._trim_card_for_snapshot(card)
    for key in ("batch_id", "batch_rank", "dispatch_error"):
        assert key in kept, key


def test_the_card_screen_draws_the_line_under_the_refining_caret():
    card = _read(CARD)
    status = card.split("private var statusSection: some View {", 1)[1]
    live = status.index("liveState")
    line = status.index("card.batchPlanningLine")
    health = status.index("runHealthSupported")
    assert live < line < health
    assert ".lineLimit(" not in status[:health]
    assert card.count("card.batchPlanningLine") == 2


def test_the_decode_is_tolerant_by_source():
    models = _read(MODELS)
    for needle in ('case batchRank = "batch_rank"',
                   'batchRank = c.value(.batchRank, "")',
                   "var batchPlanningLine: String"):
        assert models.count(needle) == 1, needle


def test_xctest_names_the_same_case():
    assert ("func testTheBatchPlanningLineIsDrawnOffBatchRankOnlyWhileRefining()"
            in _read(XCTEST))


def test_the_contract_names_the_line():
    contract = _read(CONTRACT)
    assert "batch_rank" in contract
    assert "batch planning session" in contract
    assert "batch planning session" in _read(CONTEXT_PANEL)
