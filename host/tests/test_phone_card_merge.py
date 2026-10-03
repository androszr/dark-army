# host/tests/test_phone_card_merge.py
"""The phone's half of review and merge — source pins and the drift floor.

`plans/2026-10-03-review-and-merge-done-card.md`. The daemon's halves are
pinned by `test_merge_card.py`, `test_card_changes_api.py` and
`test_lan_access.py`; the rule both clients read by `test_card_merge_rule.py`.
What this file pins is the wire: that every key the daemon's Changes page and
card decoration publish has a Swift counterpart on the panel *and* the phone
(a key added in `merges.py` without one fails here rather than going undrawn),
that the three markers decode, that the verbs and their receipts are wired,
and that the files this plan touched still parse under `swiftc`.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from dark_army_daemon import merges

ROOT = Path(__file__).resolve().parents[2]
PANEL = ROOT / "panel" / "Sources" / "BobPanel"
PHONE = ROOT / "ios" / "BobPhone"


def _read(path: Path) -> str:
    assert path.is_file(), f"missing file: {path}"
    return path.read_text()


def test_every_changes_key_is_decoded_on_both_clients():
    for path in (PANEL / "BoardModels.swift", PHONE / "Models.swift"):
        text = _read(path)
        report = text[text.index("struct CardChangesReport"):]
        report = report[:report.index("\n}\n")]
        for key in merges.CHANGES_KEYS:
            assert f'"{key}"' in report or f"case {key}" in report \
                or f", {key}" in report or f" {key}," in report, (path.name, key)
        for name, keys in (("CardChangeCommit", merges.COMMIT_KEYS),
                           ("CardChangeFile", merges.FILE_ROW_KEYS),
                           ("CardChangeDiff", merges.DIFF_KEYS),
                           ("CardChangeReview", merges.REVIEW_KEYS)):
            block = text[text.index(f"struct {name}"):]
            block = block[:block.index("\n}\n")]
            for key in keys:
                assert key in block, (path.name, name, key)


def test_the_card_keys_the_daemon_publishes_are_decoded_on_both_clients():
    for path in (PANEL / "BoardModels.swift", PHONE / "Models.swift"):
        text = _read(path)
        for key in ("merge_state", "merge_line", "review_verdict",
                    "review_running", "worktree_branch", "merge_offered"):
            assert f'"{key}"' in text, (path.name, key)
        # The stored bookkeeping never rides a snapshot, so no client reads it.
        for key in ("merge_note", "review_tip"):
            card = text[text.index("struct BoardCard"):]
            assert f'"{key}"' not in card[:card.index("\n}\n")], (path.name, key)


def test_the_three_markers_decode_on_the_phone():
    text = _read(PHONE / "Models.swift")
    for key in ("card_changes_supported", "merge_writable", "review_run_writable"):
        assert f'"{key}"' in text, key
    for name in ("cardChangesSupported", "mergeWritable", "reviewRunWritable"):
        assert f"var {name} = false" in text, name
        assert f"{name} = c.value(.{name}, false)" in text, name


def test_the_verbs_have_receipts_and_notes():
    actions = _read(PHONE / "Actions.swift")
    for verb, wire in (("boardMerge", "board_merge"),
                       ("boardMergeFix", "board_merge_fix"),
                       ("boardReviewRun", "board_review_run")):
        assert f'static let {verb} = "{wire}"' in actions, verb
    receipts = _read(PHONE / "Receipts.swift")
    assert "case cardMergeState(cardId: String, before: String, line: String)" in receipts
    assert "case cardReviewRunning(cardId: String, verdict: String)" in receipts
    # A merge that ends where it began still lands (its line moved), and a
    # review that answered before the next poll still lands (its verdict did).
    assert "card.mergeState != before || card.mergeLine != line" in receipts
    assert "card.reviewRunning || card.reviewVerdict != verdict" in receipts
    assert "case PhoneActions.boardMerge:" in receipts
    for verb in ("boardMerge", "boardMergeFix", "boardReviewRun"):
        assert f"case PhoneActions.{verb}: return" in receipts, verb
    client = _read(PHONE / "Client.swift")
    # MERGE is judged against the merge state the person was looking at.
    assert "effect = Self.reviewAndMergeBaseline(effect, in: snapshot)" in client
    assert "before: card.mergeState," in client and "line: card.mergeLine)" in client
    assert "verdict: card.reviewVerdict)" in client
    arm = _read(PHONE / "Arm.swift")
    assert "case merge\n" in arm and "case mergeFix\n" in arm
    assert "case reviewRun" not in arm  # one press, never armed
    ack = actions[actions.index("enum PhoneCardAck {"):]
    assert 'fields["expected_tip"] = tip' in ack


def test_merge_and_review_presses_are_never_dropped_unsent():
    """`landed` reads the card's line and verdict, which move on their own;
    only `evidenceBeforeSending` decides whether a press is dropped before it
    goes, and for these two effects it must say no."""
    receipts = _read(PHONE / "Receipts.swift")
    gate = receipts[receipts.index("static func evidenceBeforeSending"):]
    gate = gate[:gate.index("static func landed")]
    false_arm = gate[:gate.index("case .botAccess")]
    assert "case .cardMergeState, .cardReviewRunning:" in false_arm
    assert false_arm.index("case .cardMergeState, .cardReviewRunning:") \
        < false_arm.index("return false", false_arm.index(
            "case .cardMergeState, .cardReviewRunning:"))
    assert "default:\n            return landed" in gate


def test_the_changes_read_is_a_sealed_kind_with_the_card_file_and_tip_in_the_body():
    client = _read(PHONE / "Client.swift")
    body = client[client.index("func fetchCardChanges("):]
    body = body[:body.index("    /// Many cards in full")]
    assert '["card": cardId]' in body
    assert '["card": cardId, "file": file, "tip": tip]' in body
    assert 'kind: "card_changes"' in body
    assert "knowsItIsAway" in body and "homeChannel" in body
    assert "!backgroundRun" in body


@pytest.mark.parametrize("name", ["CardDetailView", "Receipts", "Client",
                                  "Actions", "Arm", "Models", "CardMerge"])
def test_the_touched_phone_files_parse_under_swiftc(name):
    swiftc = shutil.which("swiftc")
    if not swiftc:
        pytest.skip("Swift toolchain unavailable")
    done = subprocess.run([swiftc, "-parse", str(PHONE / f"{name}.swift")],
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
