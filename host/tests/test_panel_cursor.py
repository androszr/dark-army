"""Static guard: every drawn tap target in the panel carries the pointing hand.

The panel draws nearly all of its controls itself, and a drawn control tells
AppKit nothing, so the arrow stays an arrow unless `.clickable()` is attached
(see `panel/Sources/BobPanel/Cursor.swift`). Two sweeps closed the gaps by
hand; this is the standing check so a third one is never needed.

It is a *lint*, not a behavioural test — cursor state cannot be asserted at
runtime from a test process. It therefore cannot catch: a `.clickable()`
attached to the wrong view in a stack (the text window is satisfied, the wrong
pixels track), a wrong gate (`.clickable(true)` on a permanently disabled
button), or a drawn control built with no marker line at all (a bare
`.gesture(DragGesture…)`). The manual hover checks in the plan are the other
half of the story.
"""

from pathlib import Path

PANEL = Path(__file__).resolve().parents[2] / "panel" / "Sources" / "BobPanel"

# A line carrying one of these is a *drawn* tap target: a button style the
# panel paints itself, a borderless menu, or a raw tap gesture. Native
# controls (segmented tabs, bordered buttons, text fields, pickers) keep the
# arrow deliberately and carry none of these markers.
MARKERS = (
    ".buttonStyle(.plain)",
    ".buttonStyle(.link)",
    ".buttonStyle(.borderless)",
    ".buttonStyle(AlarmOutline",
    ".buttonStyle(JumpRailStyle",
    ".menuStyle(.borderlessButton)",
    ".onTapGesture",
)

# How far after the marker the hand may sit. House style puts `.clickable()`
# after the gesture/style it applies to; the farthest existing site is 21
# lines. A future site that puts it *before* its marker will false-fail — the
# fix is to move the modifier after it, not to widen this.
WINDOW = 25

HAND = (".clickable(", "TapToToggle")

# Deliberate exclusions, keyed by (filename, stripped line text) rather than
# line number, because line numbers drift. Cost of that key: a *second*
# identical line in the same file would ride the exception unseen. Accepted.
ALLOWED = {
    # The column background's full-surface cancel tap. Not a control — a hand
    # over the empty part of a column advertises a button that does not exist.
    ("BoardView.swift", ".onTapGesture { state.disarm() }"),
    # The typed loading line's skip. A status message, not a control: a hand
    # over it would promise a destination it does not have. Shared with the
    # phone byte-for-byte, where `.clickable()` does not exist anyway.
    ("AgentChatter.swift", ".onTapGesture { skipped = true }"),
}


def _sites():
    """Every marker site in the panel, as (file, lineno, text, has_hand)."""
    out = []
    for path in sorted(PANEL.glob("*.swift")):
        if path.name == "Cursor.swift":  # defines the machinery
            continue
        lines = path.read_text().splitlines()
        for i, line in enumerate(lines):
            if not any(m in line for m in MARKERS):
                continue
            window = "\n".join(lines[i : i + WINDOW])
            out.append(
                (path.name, i + 1, line.strip(), any(h in window for h in HAND))
            )
    return out


def test_every_drawn_tap_target_carries_the_pointing_hand():
    missing = [
        f"{name}:{lineno}: {text}"
        for name, lineno, text, has_hand in _sites()
        if not has_hand and (name, text) not in ALLOWED
    ]
    assert not missing, (
        "Drawn tap targets with no pointing hand — add `.clickable(…)` within "
        f"{WINDOW} lines, or record the exclusion in ALLOWED:\n"
        + "\n".join(missing)
    )


def test_allow_list_entries_still_exist():
    """A removed site must take its exception with it, not leave it to rot."""
    seen = {(name, text) for name, _, text, _ in _sites()}
    stale = sorted(ALLOWED - seen)
    assert not stale, f"ALLOWED entries no longer match any line: {stale}"


def test_scanner_still_sees_markers():
    """A rename of `.clickable` or a marker drift must fail loudly, not go
    vacuous."""
    sites = _sites()
    assert len(sites) >= 25, f"only {len(sites)} marker sites found"
    assert sum(1 for s in sites if s[3]) >= 25


def test_cursor_modifier_still_exists():
    assert "func clickable(" in (PANEL / "Cursor.swift").read_text()


def test_covered_board_turns_cursor_affordances_off():
    # History covers the board the same way a detail does, so the affordance
    # follows `boardCovered`, not only an open detail.
    text = (PANEL / "PanelView.swift").read_text()
    assert ".cursorAffordances(!boardCovered)" in text
    assert "case .detail, .history: return true" in text


def _method_body(src, signature):
    start = src.index(signature)
    brace = src.index("{", start)
    depth = 0
    for i, ch in enumerate(src[brace:], brace):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[start : i + 1]
    raise AssertionError(f"unclosed method {signature!r}")


def test_relayout_decides_whether_to_rebuild_the_tracking_area():
    src = (PANEL / "Cursor.swift").read_text()
    assert "CursorAffordance.live(requested:" in src
    assert "CursorTracking.onRelayout(" in src
    body = _method_body(src, "override func updateTrackingAreas()")
    assert "CursorTracking.onRelayout" in body
    assert "case .rebuild:" in body
    assert body.index("CursorTracking.onRelayout") < body.index("rebuildTrackingArea()")


def test_clickable_call_sites_stay_inside_the_budget():
    count = 0
    for path in sorted(PANEL.glob("*.swift")):
        if path.name == "Cursor.swift":
            continue
        count += path.read_text().count(".clickable(")
    # 2026-09-06 the tree held 91; the dirty checkout this plan landed on
    # already sits at 97 (BoardCardSheet, LifecycleReportView and others
    # this plan does not touch). 2026-09-14 the access-log window and the
    # Inbox's two alert buttons took it past 110. 2026-09-25 the Prep row's
    # batch refine added four drawn buttons (the card's tick box, SELECT,
    # the batch verb and CANCEL — each gated, each owed its hand by
    # `test_every_drawn_tap_target_carries_the_pointing_hand`) to a tree
    # already at 120. 2026-09-25 again, the usability pass drew Stop and
    # Delete as buttons (`StopBar`, `DeleteBar`) and the brand bar's keys
    # chip, taking a tree at 124 to 127; the plan measured 130 and set 150
    # for the concurrent sessions building beside it
    # (`plans/2026-09-25-usability-accessibility-pass.md`). The ceiling is a
    # budget, not a target.
    assert 80 <= count <= 150, (
        f"{count} .clickable( call sites outside Cursor.swift; "
        "the ceiling is a budget, not a target"
    )


def test_board_presentations_turn_cursor_affordances_back_on():
    board = (PANEL / "BoardView.swift").read_text()
    # The plan-gate dialog's two buttons and the drafts sheet: the rename
    # alert and the folder sheet went with the folders.
    assert board.count(".cursorAffordances(true)") >= 3
    drafts = (PANEL / "DraftsSheet.swift").read_text()
    assert drafts.count(".cursorAffordances(true)") >= 1

