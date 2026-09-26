# host/tests/test_phone_card_read_and_edit.py
"""The phone's card screen — source pins over the Swift.

`ios/` has no test target by decision; this suite reads the Swift as text.
What it pins: the card read is asked for as the sealed `card` kind on both
legs and defined exactly once, and no background path touches it; the screen
draws the *real* instructions rather than the frame's preview, so the two
"cut short" apologies are gone; Save is held with the panel's own wording;
Approve is drawn behind the Mac's version marker so an older Mac hides it
rather than offering one that does nothing; and the changed-plan refusal has
its own matcher and its own armed label.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PHONE = ROOT / "ios" / "BobPhone"
PANEL = ROOT / "panel" / "Sources" / "BobPanel"
MODELS = PHONE / "Models.swift"
CLIENT = PHONE / "Client.swift"
ACTIONS = PHONE / "Actions.swift"
DETAIL = PHONE / "CardDetailView.swift"
REFRESH = PHONE / "BackgroundRefresh.swift"
PUSH = PHONE / "Push.swift"
WIDGET_DIR = ROOT / "ios" / "BobPhoneWidget"
PBXPROJ = ROOT / "ios" / "BobPhone.xcodeproj" / "project.pbxproj"


def _read(path: Path) -> str:
    assert path.is_file(), f"missing file: {path}"
    return path.read_text()


# --- the read ------------------------------------------------------------------


def test_the_card_read_rides_both_legs_and_is_defined_once():
    src = _read(CLIENT)
    assert src.count("func fetchCard(") == 1, (
        "fetchCard must have exactly one definition")
    assert src.count('kind: "card"') == 2, (
        "the card read needs both legs — the relay one and the home one")


#: `fetchCard(` with the paren, so a differently-named sibling (`fetchCardSync`)
#: is not counted as this verb. The bare name would make every pin below drift
#: the moment another read is added beside it.
_CALL = re.compile(r"\bfetchCard\(")


def test_no_background_path_reads_a_card():
    """A screen nobody is looking at has no card to read, and the widget does
    no network at all."""
    for path in (REFRESH, PUSH):
        assert not _CALL.search(_read(path)), path.name
    for path in WIDGET_DIR.rglob("*.swift"):
        assert not _CALL.search(_read(path)), path.name


def test_the_card_screen_is_the_caller():
    """Two callers, and both are screens somebody opened: the card screen,
    and the agent sheet's Main tab, whose journey rail times the card the
    agent is on from the same read (`loadJourney`)."""
    assert "client.fetchCard(" in _read(DETAIL)
    callers = [q.name for q in PHONE.rglob("*.swift")
               if _CALL.search(_read(q))]
    assert sorted(callers) == ["AgentDetailView.swift", "CardDetailView.swift",
                               "Client.swift"]
    agent = _read(PHONE / "AgentDetailView.swift")
    assert len(_CALL.findall(agent)) == 1
    load = agent[agent.index("private func loadJourney("):]
    load = load[:load.index("\n    }\n")]
    assert "client.fetchCard(" in load


def test_the_screen_no_longer_apologises_for_a_shortened_prompt():
    """Replaced by the real text, and by the held Save while it has not
    arrived — an apology is not an answer."""
    assert "cut short" not in _read(DETAIL)


def test_the_screen_draws_the_plan_with_the_shared_renderer():
    src = _read(DETAIL)
    assert "planSection" in src
    # The plan is split at the template's `## Technical detail` heading
    # (`PlanSplit`, pinned by `test_phone_plan_fold.py`); both halves still
    # go through the shared renderer.
    assert "PlanSplit.split(plan.text)" in src
    assert "MarkdownText(source: split.summary" in src
    assert "MarkdownText(source: detail" in src
    # An unavailable plan says why, in the Mac's own words.
    assert "plan.reason" in src


def test_the_plan_is_drawn_by_the_shared_renderer_not_a_new_one():
    """The renderer body is byte-pinned across the two surfaces by
    `test_phone_theme_drift.py`; what this pins is that the plan reuses
    `MarkdownText` rather than growing a phone-only one beside it. Making a
    plan look better on a phone by editing the renderer breaks both surfaces
    at once, so the temptation has to land somewhere visible."""
    src = _read(DETAIL)
    # The plan is split at the template's `## Technical detail` heading
    # (`PlanSplit`, pinned by `test_phone_plan_fold.py`); both halves still
    # go through the shared renderer.
    assert "PlanSplit.split(plan.text)" in src
    assert "MarkdownText(source: split.summary" in src
    assert "MarkdownText(source: detail" in src
    assert "struct " not in src.split("planSection")[1].split("private func loadFull")[0]


# --- the editors ---------------------------------------------------------------


def test_the_screen_saves_only_through_board_update():
    src = _read(DETAIL)
    assert "PhoneActions.boardUpdate" in src
    assert '"title": draftTitle' in src
    assert '"summary": draftSummary' in src
    assert '"prompt": draftPrompt' in src


def test_saves_hold_reason_is_the_panels_own_words():
    """Byte-identical to `BoardState.saveHoldReason`, so the two surfaces
    cannot start saying different things about the same not-yet."""
    src = _read(DETAIL)
    panel = _read(PANEL / "BoardState.swift")
    for words in ('"Name it first"',
                  '"Waiting for the full instructions"',
                  '"Waiting for the full description"'):
        assert words in src, words
        assert words in panel, words


def test_the_editors_are_seeded_once_and_never_over_a_typed_field():
    src = _read(DETAIL)
    assert "draftTouched" in src
    assert "guard let row = answer?.card, row.id == id, !draftTouched" in src
    # And a different card drops the previous read, or the guard below would
    # answer "already fetched" and the screen would never leave the preview.
    assert "cardFull = nil" in src


# --- the approval --------------------------------------------------------------


def test_approve_is_absent_rather_than_inert_on_an_older_mac():
    src = _read(DETAIL)
    assert "board.planApprovalSupported" in src
    assert "PhoneActions.boardApprovePlan" in src
    assert '"plan_digest": plan.digest' in src


def test_the_screen_posts_no_verb_beyond_the_approval():
    """Every action this screen sends is one somebody chose: the board verbs
    it already had, plus `board_approve_plan` and `board_message`, plus the
    two acknowledgements (Mark checked / Mark reviewed, each chosen on both
    phone tuples and armed here), plus `board_promote` — chosen by a person
    on a Done scout card — plus `close_terminal` with `by_person`, the
    DONE & CLOSE press on an ended run (the fleet's own Close, which
    finishes the card after the tab closes), plus `board_manual_outcome` —
    Passed / Failed on a card flagged with a check file, chosen on both
    phone tuples and armed here — and nothing else."""
    import re
    src = _read(DETAIL)
    named = set(re.findall(r"PhoneActions\.([A-Za-z]+)", src))
    assert "boardApprovePlan" in named
    assert named <= {"boardUpdate", "boardReset", "boardDelete",
                     "boardDispatch", "boardRefine", "boardApprovePlan",
                     "boardMessage", "boardManualClear", "boardReview",
                     "boardPromote", "closeTerminal",
                     "boardManualOutcome"}, named


def test_the_changed_plan_refusal_has_its_own_matcher_and_label():
    actions = _read(ACTIONS)
    assert "isPlanChangedRefusal" in actions
    detail = _read(DETAIL)
    assert "result.isPlanChangedRefusal" in detail
    assert '"Start with changed plan?"' in detail


# --- decoding ------------------------------------------------------------------


def test_the_three_new_keys_decode_with_defaults():
    src = _read(MODELS)
    for key, default in (('planApproved = c.value(.planApproved, "")', None),
                         ("planApprovedAt = c.value(.planApprovedAt, 0)", None),
                         ("planApprovalSupported = "
                          "c.value(.planApprovalSupported, false)", None)):
        assert key in src, key
    for wire in ('case planApproved = "plan_approved"',
                 'case planApprovedAt = "plan_approved_at"',
                 'case planApprovalSupported = "plan_approval_supported"'):
        assert wire in src, wire


def test_the_card_read_decodes_through_the_tolerant_helpers():
    src = _read(MODELS)
    assert "struct CardFull: Decodable" in src
    assert "struct CardPlan: Decodable" in src
    # Never a synthesized `Decodable` — it throws on a missing key even with a
    # default, which would blank the card screen against an older Mac.
    for line in ("available = c.value(.available, false)",
                 "digest = c.value(.digest, \"\")",
                 "reason = c.value(.reason, \"\")"):
        assert line in src, line


def test_no_new_phone_file_was_added():
    """Everything lands in files the project already builds, so the four
    project slots are untouched."""
    pbx = _read(PBXPROJ)
    assert "CardFull.swift" not in pbx
    assert "PlanApproval" not in pbx


# --- the importance number ------------------------------------------------------


def test_the_phone_decodes_the_number_and_the_marker_tolerantly():
    """Absent is `""` and `false`: an older Mac sends neither key, and the box
    must be **absent, never inert**."""
    src = _read(MODELS)
    assert "case priority" in src
    assert 'priority = c.value(.priority, "")' in src
    assert 'case prioritySupported = "priority_supported"' in src
    assert "prioritySupported = c.value(.prioritySupported, false)" in src


def test_the_number_box_is_drawn_behind_the_marker():
    src = _read(DETAIL)
    assert "@StateObject private var retained: PhoneCardDraftState" in src
    assert "TextField(\"0–100\", text: $retained.draftPriority)" in src
    assert "if board.prioritySupported {" in src
    assert ".keyboardType(.numberPad)" in src


def test_the_number_rides_the_one_existing_save():
    """Inside the same `send(PhoneActions.boardUpdate, …, as: .save)` with the
    same `expected_revision`, so the conflict section, the outbox and the
    receipts cover it with no new code. Always a **string**: a JSON `0` would
    reach the Mac's `str(... or "")` coercion and store `""`."""
    src = _read(DETAIL)
    assert '"priority": draftPriority,' in src
    assert src.count("as: .save)") == 1
    assert '"expected_revision": String(expecting ?? shownRevision)' in src


def test_the_conflict_view_shows_and_restores_the_macs_number():
    """It rides the 409 body for a reason: **USE THE MAC'S** applies it, so a
    number that is never drawn would replace a typed one the person never
    saw."""
    assert 'priority = json["priority"] as? String ?? ""' in _read(ACTIONS)
    src = _read(DETAIL)
    assert 'conflictField("priority", current.priority)' in src
    assert "draftPriority = current.priority" in src


def test_the_phone_still_sorts_no_cards():
    """It inherits the store's order for free, and a second ordering rule here
    is how the two surfaces would come to disagree."""
    src = _read(MODELS)
    body = re.search(r"func cards\(in column: String\) -> \[BoardCard\] \{"
                     r"(.*?)\n    \}", src, re.S)
    assert body is not None
    assert "sorted" not in body.group(1)
    assert "filter" in body.group(1)


# --- the scout's verdict above the report ---------------------------------------


def _phone_board_card() -> str:
    """`struct BoardCard` through the next top-level `struct` in Models."""
    text = _read(MODELS)
    start = text.find("struct BoardCard")
    assert start >= 0
    rest = text[start:]
    return rest[:rest.find("\nstruct ", 1)]


def _report_section() -> str:
    """`CardDetailView.reportSection`, up to the next member."""
    text = _read(DETAIL)
    start = text.index("private var reportSection")
    rest = text[start:]
    stops = [i for i in (rest.find("\n    @ViewBuilder", 1),
                         rest.find("\n    private ", 1),
                         rest.find("\n    func ", 1)) if i > 0]
    return rest[:min(stops)] if stops else rest


def test_the_verdict_pair_decodes_tolerantly():
    card = _phone_board_card()
    assert card.count('c.value(.reportVerdict, "")') == 1
    assert card.count('c.value(.reportRecommendation, "")') == 1
    assert "decode(.reportVerdict" not in card
    assert "decode(.reportRecommendation" not in card


def test_the_report_section_draws_the_verdict_in_full():
    section = _report_section()
    assert section.count("ScoutVerdictLine.text(") == 1
    assert "ScoutVerdictLine.spoken(" in section
    assert ".lineLimit(" not in section


def test_the_phone_carries_the_shared_verdict_rule_once():
    shared = _read(PHONE / "ScoutReports.swift")
    assert shared.count("enum ScoutVerdictLine") == 1
