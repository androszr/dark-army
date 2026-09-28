"""The typed loading line is one file carried by two apps, and no spinner survives.

`AgentChatter.swift` is the fourth marker-pinned Swift pair (beside
`Markdown.swift` and `Specialists.swift`, under `test_phone_theme_drift.py`'s
marker-line discipline; `OutcomeViews.swift` is pinned whole by
`test_phone_outcomes.py`). Each copy keeps its own header comment above the
marker line `enum AgentChatter {`; everything below is byte-identical.

Three contracts live here:

- **the byte pin** below the marker, with the anti-vacuous floor and a
  doctored-copy self-check so the comparison cannot rot into two empty strings;
- **the cross-platform and accessibility sweep** over both copies — nothing
  one-platform, nothing that clamps type, nothing random, and the reduce-motion
  / single-element / `TimelineView(AgentChatter.Schedule(` / `.task(id:)` /
  `agentChatterRunning` shape actually present;
- **the scope**: `ProgressView` occurs zero times across both Swift trees, the
  seven panel and nineteen phone call sites are counted, and the phone project
  file names the new source four times, so it is really in the target.

The phone's nineteen are also sorted by kind: every one of the five *action*
waits the panel draws (`starting`, `refining`, `deleting`, `sending`,
`closing`) has a phone caller, every screen with a `.refreshable` is tinted
phosphor and says `.refreshing` while the pull is out, and the flag it reads
is raised by `refreshNow()` alone — never by the refresh a write is dimmed
behind.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PANEL_DIR = ROOT / "panel" / "Sources" / "BobPanel"
PHONE_DIR = ROOT / "ios" / "BobPhone"
PANEL_CHATTER = PANEL_DIR / "AgentChatter.swift"
PHONE_CHATTER = PHONE_DIR / "AgentChatter.swift"
PBXPROJ = ROOT / "ios" / "BobPhone.xcodeproj" / "project.pbxproj"

MARKER = "enum AgentChatter {"

PANEL_CALL_SITES = 7
#: 4 non-action waits (BoardView `.clearingDone`, BrandBar `.connecting`,
#: CatchUpView `.readingHistory` — the same call speaks `.refreshing` during
#: a pull — and `.opening`), 6 pull-to-refresh lines (BoardView `unavailable`
#: and `page(for:)`, PipelineView, NeedsYouView, FleetView, UsageView), and 9
#: action waits (BoardView `.starting` / `.refining` / `.deleting`,
#: CardDetailView `.starting` / `.refining` / `.deleting`, AgentDetailView
#: `.closing`, AnswerBox `.sending`). AgentDetailView's `.sending` left with
#: the terminal's one-line send box: the phone's terminal is SwiftTerm now
#: (`TerminalPane.swift`) and a keystroke into it waits on nothing drawn.
PHONE_CALL_SITES = 4  # BoardView `.clearingDone` plus the three waits this plan inherited
PHONE_CALL_SITES += 16  # concurrent action chatter + pull-to-refresh sites in this tree
PHONE_CALL_SITES += 1  # the Comm tab's "thinking" line under Mission Control's reply (CommView)
                          # (Usage report screens gained .refreshable chatter)
PHONE_CALL_SITES += 1  # ConversationScreen catching-up caret
PHONE_CALL_SITES += 1  # Comm's helper tab: the caret beside a helper's activity (CommView)
PHONE_CALL_SITES += 1  # ConversationScreen's "now doing" line while the agent works

#: The panel's action waits; each must have a phone caller outside the shared
#: file, or the phone's buttons fall silent again.
ACTION_WAITS = ("starting", "refining", "deleting", "sending", "closing")

#: One-platform API, type clamps, measurement and randomness — none of it
#: belongs in a file both targets compile.
FORBIDDEN = (
    ".help(",
    ".clickable(",
    "import AppKit",
    "import UIKit",
    ".lineLimit(",
    ".system(size:",
    ".dynamicTypeSize(",
    "GeometryReader",
    "Int.random",
    "UIAccessibility",
)

#: The shape the plan states, present by name on both sides.
REQUIRED = (
    "accessibilityReduceMotion",
    "accessibilityElement(children: .ignore)",
    "TimelineView(AgentChatter.Schedule(",
    "Theme.mono",
    ".task(id: seed)",
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _chatter_code(path: Path) -> str:
    """The marker line through end of file — `_specialists_code`'s shape."""
    text = _read(path)
    lines = text.splitlines(keepends=True)
    starts = [i for i, line in enumerate(lines) if line.rstrip("\n") == MARKER]
    assert len(starts) == 1, f"expected one {MARKER!r} line in {path}, got {len(starts)}"
    return "".join(lines[starts[0]:])


def _swift_sources(folder: Path) -> list[Path]:
    files = sorted(folder.glob("*.swift"))
    assert files, f"no Swift sources under {folder}"
    return files


# --- the byte pin -----------------------------------------------------------


def test_both_copies_exist():
    assert PANEL_CHATTER.is_file()
    assert PHONE_CHATTER.is_file()


def test_the_words_are_the_same_words():
    panel = _chatter_code(PANEL_CHATTER)
    phone = _chatter_code(PHONE_CHATTER)
    for name, region in (("panel", panel), ("phone", phone)):
        assert len(region) >= 1500, f"parsed too little from the {name} chatter"
        assert "struct AgentChatterView" in region, f"no AgentChatterView in the {name} region"
    assert panel == phone, (
        "the phone's AgentChatter.swift has drifted from the panel's. Mirror "
        "the edit onto the other side in this same commit — never re-baseline "
        "one side and never loosen this comparison.")


def test_a_doctored_chatter_file_would_not_compare_equal():
    """A one-literal change on one side must mismatch."""
    panel = _chatter_code(PANEL_CHATTER)
    assert '"hello, friend"' in panel
    doctored = panel.replace('"hello, friend"', '"goodbye, friend"', 1)
    assert doctored != panel
    assert doctored != _chatter_code(PHONE_CHATTER)


def test_the_headers_differ_only_above_the_marker():
    """Each copy names the other one; the marker splits the two halves."""
    panel = _read(PANEL_CHATTER)
    phone = _read(PHONE_CHATTER)
    assert "ios/BobPhone/AgentChatter.swift" in panel.split(MARKER, 1)[0]
    assert "panel/Sources/BobPanel/AgentChatter.swift" in phone.split(MARKER, 1)[0]


# --- the cross-platform and accessibility sweep ----------------------------


@pytest.mark.parametrize("path", [PANEL_CHATTER, PHONE_CHATTER])
def test_the_shared_file_stays_cross_platform(path):
    text = _read(path)
    for needle in FORBIDDEN:
        assert needle not in text, f"{path.name} uses {needle!r}, which one platform lacks or a contract forbids"
    assert not re.search(r"^import (?!SwiftUI$)", text, re.M), (
        f"{path.name} imports something other than SwiftUI")


@pytest.mark.parametrize("path", [PANEL_CHATTER, PHONE_CHATTER])
def test_the_shared_file_carries_the_stated_shape(path):
    text = _read(path)
    for needle in REQUIRED:
        assert needle in text, f"{path.name} lacks {needle!r}"
    # One clock and one identity seam: a second of either is a second source
    # of truth for where the animation is.
    assert text.count("TimelineView(AgentChatter.Schedule(") == 1
    assert text.count(".task(id: seed)") == 1
    # The clock pauses on an environment value the window sets; the shared
    # file reads the key and never reaches for the panel's client.
    assert "agentChatterRunning" in text, f"{path.name} does not read the running key"
    assert "client.visible" not in text, f"{path.name} reaches for the panel's client"


def test_the_spoken_sentence_is_required():
    """No default for `spoken:` — a call site cannot be silent by omission."""
    code = _chatter_code(PANEL_CHATTER)
    init = re.search(r"init\(_ style: Style = \.line, wait: AgentChatter\.Wait, seed: String,\s*spoken: String\)", code)
    assert init, "the initialiser's shape moved; `spoken:` must stay required"
    assert "spoken: String =" not in code


def test_the_pool_is_total_and_bounded():
    """Every `Wait` case answers in the switch; every line is under the cap.

    The Swift tests prove this properly; this is the grep that fails first when
    someone adds a case and forgets the pool.
    """
    code = _chatter_code(PANEL_CHATTER)
    cases = re.findall(r"^\s+case (\w+)$", code.split("static let maxLineChars", 1)[0], re.M)
    assert len(cases) == 10, cases
    switch = code.split("static func pool(for wait: Wait)", 1)[1].split("static func hash", 1)[0]
    for case in cases:
        assert f"case .{case}:" in switch, f"pool has no arm for .{case}"
    for line in re.findall(r'"([^"\\]*)"', switch):
        assert 0 < len(line) <= 48, f"pool line out of bounds: {line!r}"
        assert line == line.strip(), f"pool line carries stray whitespace: {line!r}"


# --- the scope --------------------------------------------------------------


def _count(needle: str, files: list[Path]) -> int:
    return sum(_read(p).count(needle) for p in files)


def test_no_spinner_survives_on_either_surface():
    panel = _swift_sources(PANEL_DIR)
    phone = _swift_sources(PHONE_DIR)
    assert _count("ProgressView", panel) == 0, "a ProgressView survives in the panel"
    assert _count("ProgressView", phone) == 0, "a ProgressView survives on the phone"


def test_the_spinner_needle_still_bites():
    """A doctored source must be caught, so the count cannot rot into a no-op."""
    doctored = _read(PANEL_DIR / "BoardView.swift") + "\n    ProgressView()\n"
    assert doctored.count("ProgressView") == 1


def test_every_wait_is_drawn_by_the_chatter_view():
    panel = _count("AgentChatterView(", _swift_sources(PANEL_DIR))
    phone = _count("AgentChatterView(", _swift_sources(PHONE_DIR))
    panel_decl = _read(PANEL_CHATTER).count("AgentChatterView(")
    phone_decl = _read(PHONE_CHATTER).count("AgentChatterView(")
    assert panel == PANEL_CALL_SITES + panel_decl
    assert phone == PHONE_CALL_SITES + phone_decl, (
        f"phone AgentChatterView( count is {phone}, PHONE_CALL_SITES "
        f"expects {PHONE_CALL_SITES + phone_decl}. Bump PHONE_CALL_SITES "
        "in this file in the same change as the new (or removed) call."
    )
    # The shared file declares the struct and never calls it.
    assert _read(PANEL_CHATTER).count("AgentChatterView(") == 0
    # History's wait keeps its own caption beside the typed line; the generic
    # filler it once drew is the one caption removed on purpose.
    assert '"A wide range takes a few seconds."' in _read(PANEL_DIR / "HistoryView.swift")
    assert '"Reading history…"' not in _read(PANEL_DIR / "HistoryView.swift")


def test_every_call_site_pins_its_identity_to_the_wait():
    """`.id(seed)` beside each call, so the wait, not the position, is the view."""
    for folder in (PANEL_DIR, PHONE_DIR):
        for path in _swift_sources(folder):
            if path.name == "AgentChatter.swift":
                continue
            text = _read(path)
            for match in re.finditer(r"AgentChatterView\((?:.|\n){0,240}?seed: ([^,\n]+),", text):
                seed = match.group(1).strip()
                tail = text[match.end():match.end() + 200]
                assert f".id({seed})" in tail, f"{path.name}: the call seeded {seed} does not pin .id({seed})"


def test_every_call_site_speaks():
    for folder in (PANEL_DIR, PHONE_DIR):
        for path in _swift_sources(folder):
            if path.name == "AgentChatter.swift":
                continue
            text = _read(path)
            calls = text.count("AgentChatterView(")
            spoken = len(re.findall(r'spoken: "[^"]+"', text))
            # A rail button's own `spoken:` is its accessibility label, not a
            # chatter sentence (`ScoutReportsRail.swift`, 25 Sep 2026).
            spoken -= len(re.findall(r'railButton\(label: "[^"]*", spoken: "', text))
            assert calls == spoken, f"{path.name}: {calls} chatter calls, {spoken} spoken sentences"


def test_the_phone_project_file_carries_the_source_four_times():
    """PBXBuildFile, PBXFileReference, the group, and the Sources phase —
    the `Specialists.swift` shape. Three means the file exists and is not
    compiled, which TestFlight would be the first to notice."""
    text = _read(PBXPROJ)
    lines = text.splitlines()
    # Line counts, `grep -c`'s figure: each entry line names the file twice.
    assert sum("AgentChatter.swift" in line for line in lines) == 4
    assert sum("Specialists.swift" in line for line in lines) == 4, "the template entry moved"
    assert "/* AgentChatter.swift in Sources */ = {isa = PBXBuildFile;" in text
    assert "path = AgentChatter.swift;" in text


def test_the_refreshing_pool_shares_no_line_with_opening():
    """`AgentChatterTests.testRefreshingPoolIsDistinctFromOpening`, as a grep:
    a pull that read "pulling the thread" would claim a buzz was opening."""
    code = _chatter_code(PANEL_CHATTER)
    switch = code.split("static func pool(for wait: Wait)", 1)[1].split("static func hash", 1)[0]

    def arm(name: str) -> set[str]:
        body = switch.split(f"case .{name}:", 1)[1].split("case .", 1)[0]
        return set(re.findall(r'"([^"\\]*)"', body))

    refreshing = arm("refreshing")
    opening = arm("opening")
    assert refreshing, "the .refreshing arm has no lines"
    assert "pulling the thread" in opening, "the .opening arm moved"
    assert refreshing.isdisjoint(opening), refreshing & opening


def test_the_phone_draws_every_action_wait():
    """The five waits the panel's buttons draw each have a phone caller."""
    sources = [p for p in _swift_sources(PHONE_DIR) if p.name != "AgentChatter.swift"]
    for wait in ACTION_WAITS:
        hits = [p.name for p in sources if f"wait: .{wait}" in _read(p)]
        assert hits, f"no phone screen draws the .{wait} wait"


def test_every_pull_to_refresh_talks_and_is_phosphor():
    """Every phone screen with a `.refreshable` tints the indicator and says
    `.refreshing` while the pull is out. Catch up swaps the words on the line
    it already draws while loading rather than adding a second one."""
    found = 0
    for path in _swift_sources(PHONE_DIR):
        text = _read(path)
        if ".refreshable {" not in text:
            continue
        found += 1
        assert ".tint(Theme.phosphor)" in text, f"{path.name}: the pull indicator is not phosphor"
        assert ("wait: .refreshing" in text
                or "refreshing ? .refreshing : .readingHistory" in text), (
            f"{path.name}: nothing says .refreshing while the pull is out")
    # Seven closures across six files: BoardView carries two (`unavailable`
    # and `page(for:)`).
    assert found >= 6, f"only {found} refreshable screens found; the sweep is not looking"


def test_the_refresh_flag_is_published_and_write_refresh_never_raises_it():
    """`refreshNow()` is the one writer. `refreshAfterWrite()` runs after every
    dimmed press; a flag raised there would draw the refresh line on every
    screen for every action."""
    text = _read(PHONE_DIR / "Client.swift")
    assert text.count("@Published private(set) var refreshing") == 1
    assert text.count("refreshesOut += 1") == 1, "refreshNow() is the one place the count rises"
    after_write = text.split("func refreshAfterWrite()", 1)[1].split("func ", 1)[0]
    assert "refreshesOut" not in after_write
    assert "refreshing" not in after_write
    poll = text.split("private func poll(_ record: PairingRecord", 1)[1].split("\n    func ", 1)[0]
    assert "refreshesOut" not in poll, "poll() itself must not count: a join is not a pull"


def test_the_captions_that_are_facts_survive():
    """The chatter replaces the spinner, never Dark Army's words."""
    assert '"Clearing all Done items…"' in _read(PANEL_DIR / "BoardView.swift")
    assert '"starting — waiting for the session to appear"' in _read(PANEL_DIR / "BoardCardView.swift")
    assert '"refining — answer the interview in the terminal"' in _read(PANEL_DIR / "BoardCardView.swift")
    # On the phone the two labels became the spoken sentence.
    catch_up = _read(PHONE_DIR / "CatchUpView.swift")
    assert 'spoken: "Loading decisions"' in catch_up
    assert 'spoken: "Opening notification"' in catch_up
    # The phone's SENDING… swaps and the "Deleting…" word stay beside the
    # new lines; the chatter is added, never substituted.
    # The queue's own mark while the press is in play; the literal only
    # once the Mac accepted the Delete and the card is on its way out.
    card = _read(PHONE_DIR / "CardDetailView.swift")
    assert 'if pressed == .delete { return mark }' in card
    assert 'if settlingHere { return "SENDING…" }' in card
    assert "Text(PhoneClient.cardLeavingLine)" in _read(PHONE_DIR / "BoardView.swift")
    assert "ReconnectBar(sentence: sentence, link: link, phase: phase)" in _read(PHONE_DIR / "BrandBar.swift")
