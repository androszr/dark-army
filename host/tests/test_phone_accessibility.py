"""The phone app honours Dynamic Type, and every control and row speaks.

`ios/` has no test target that runs — the decision, recorded in
`test_phone_theme_drift.py`'s own docstring, is that the repo pins Swift source
with a **Python lint** under `host/tests/`, because `cd host &&
.venv/bin/pytest` is the suite that actually runs on this machine. This file
follows that: it reads `ios/BobPhone/*.swift` and fails when a new screen
brings back a fixed point size, an unnamed icon button, a row that speaks in
fragments, or a clamp on the size the person chose.

The doctrine, restated once so a future edit knows what is load-bearing:

- **One scaling seam.** `Theme.mono` is the only `.system(size:` in the app's
  own views, and it scales through `UIFontMetrics`. Nothing may cap it.
- **The threshold is read, never measured.** A layout reflows off
  `@Environment(\\.dynamicTypeSize)`, not off a screen width.
- **`ios/BobPhone/Markdown.swift` and `Specialists.swift` are byte-pinned** to
  the panel's copies by `test_phone_theme_drift.py`. They need no edit — every
  phone call path through them already routes through `Theme.mono` — and this
  file asserts nobody helpfully edited them anyway.
- **Anti-vacuous.** Every needle that could rot is either paired with a
  positive assertion or with a doctored-string self-check, so a rename cannot
  quietly turn a sweep into a no-op.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PHONE = ROOT / "ios" / "BobPhone"
WIDGET = ROOT / "ios" / "BobPhoneWidget"
PANEL_THEME = ROOT / "panel" / "Sources" / "BobPanel" / "Theme.swift"

SOURCES = sorted(PHONE.glob("*.swift"))
PINNED = {"Markdown.swift", "Specialists.swift"}

#: The one byte-pinned file that may read the text-size threshold.
#:
#: The ban below exists because scaling a pinned copy breaks the pin — a
#: `UIFontMetrics` on this side and not the Mac's is a diff. `@Environment(
#: \.dynamicTypeSize)` is not that: the key exists on **both** platforms, so
#: the same line compiles in `panel/Sources/BobPanel/Specialists.swift` and the
#: two copies stay byte-identical. `CrewBand` needs it because the crew strip
#: **becomes the grid** at an accessibility size — a cap there would be the
#: clipping `docs/phone-contract.md` forbids — and putting the decision at the
#: call sites instead would fork one rule across four screens, which is the
#: drift the shared file exists to prevent.
READS_THE_THRESHOLD = {"Specialists.swift"}
VIEWS = [p for p in SOURCES if p.name not in PINNED]

#: The one file allowed to name a point size of its own: a real terminal is
#: a grid of cells at the terminal's own size (`+` / `−` in its header),
#: not prose that reflows — `docs/phone-contract.md`, "The terminal is a
#: real emulator, and it is the one exception". `test_phone_text_in_full.py`
#: names the same file for the same reason.
TERMINAL_EXCEPTION = "TerminalPane.swift"

#: A **third** byte pin to the panel, and the one this plan learned the hard
#: way: `test_phone_outcomes.py` compares `OutcomeViews.swift` whole against
#: `panel/Sources/BobPanel/OutcomeViews.swift`. `PINNED` above is the pair
#: `test_phone_theme_drift.py` compares *below a marker line*, so it is a
#: separate set with a separate rule — this one admits no edit at all on the
#: phone side, because the Mac side is out of this plan's reach.
BYTE_PINNED_WHOLE = {"OutcomeViews.swift"}

#: The nine files that fork their layout on the system's text size.
REFLOW = (
    "Theme.swift",
    "ProcessTable.swift",
    "UsageView.swift",
    "PipelineView.swift",
    "RecentlyView.swift",
    "BrandBar.swift",
    "FleetView.swift",
    "BoardView.swift",
    "AgentDetailView.swift",
)

#: The modifier that clamps a subtree's text size. Banned outright: a cap is
#: the ceiling the whole change exists to remove.
CLAMP = ".dynamicTypeSize("

#: The environment read, which is a different string — no open paren after the
#: key path.
READ = "@Environment(\\.dynamicTypeSize)"


def _read(path: Path) -> str:
    assert path.is_file(), f"missing file: {path}"
    return path.read_text()


def _brace_chunk(text: str, start: int) -> str:
    brace = text.find("{", start)
    assert brace >= 0, f"no opening brace after offset {start}"
    depth = 0
    for i, ch in enumerate(text[brace:], brace):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    raise AssertionError("unclosed brace")


def _struct(text: str, name: str) -> str:
    match = re.search(rf"(?:private )?struct {name}\b", text)
    assert match, f"no struct {name}"
    return _brace_chunk(text, match.start())


def _member(text: str, needle: str) -> str:
    start = text.find(needle)
    assert start >= 0, f"missing {needle!r}"
    return _brace_chunk(text, start)


# --- the sweep itself ----------------------------------------------------------


def test_the_sweep_sees_the_whole_app():
    """Anti-vacuous guard for every test below: an empty `SOURCES` would make
    each of them pass by reading nothing."""
    assert len(SOURCES) >= 35, f"only {len(SOURCES)} phone sources found"
    names = {p.name for p in SOURCES}
    for expected in ("Theme.swift", "ProcessTable.swift", "UsageView.swift",
                     "PipelineView.swift"):
        assert expected in names, expected
    assert PINNED <= names
    assert len(VIEWS) == len(SOURCES) - len(PINNED)


# --- one scaling seam ----------------------------------------------------------


def test_theme_mono_is_the_only_fixed_point_size():
    needle = ".system(size:"
    for path in VIEWS:
        text = _read(path)
        if path.name == "Theme.swift":
            assert text.count(needle) == 1, (
                "Theme.mono is the one place a point size is named")
            continue
        if path.name == TERMINAL_EXCEPTION:
            # The emulator's own face: `UIFont`, sized in points, never
            # `Theme.mono` and never SwiftUI's `.system(size:`.
            assert needle not in text, (
                "the terminal sizes its UIFont in points; it has no reason "
                "to name a SwiftUI point size")
            assert "UIFont.monospacedSystemFont(ofSize:" in text
            assert "Theme.mono(" in text, "the header chrome still rides the seam"
            continue
        assert needle not in text, (
            f"{path.name} names a point size of its own; use Theme.mono")
    # The two byte-pinned files keep the panel's own body — they are a copy,
    # and every phone call path through them passes `mono: true`, so they
    # inherit the scaling for free and need no edit of their own.
    markdown = _read(PHONE / "Markdown.swift")
    assert needle in markdown, (
        "Markdown.swift's non-mono branch is the panel's copy; unreachable "
        "from the phone, where every call site passes mono: true")
    assert "mono: true" in _read(PHONE / "CardDetailView.swift")
    for name in sorted(PINNED):
        text = _read(PHONE / name)
        assert "Theme.mono" in text, f"{name} no longer rides the one seam"
        assert "UIFontMetrics" not in text, (
            f"{name} is byte-pinned to the panel; scaling it breaks the pin")
        if name in READS_THE_THRESHOLD:
            # Reading the threshold is cross-platform and keeps the pin; see
            # `READS_THE_THRESHOLD`. A *clamp* is still banned everywhere.
            assert CLAMP not in text, name
            panel = _read(ROOT / "panel" / "Sources" / "BobPanel" / name)
            assert READ in panel, (
                f"{name} reads the threshold but its Mac copy does not — the "
                "two are byte-identical below the marker")
            continue
        assert "dynamicTypeSize" not in text, name


def test_theme_mono_scales_from_a_text_style():
    text = _read(PHONE / "Theme.swift")
    region = text[text.index("static func mono("):]
    region = region[:region.index("\n}")]
    assert len(region) >= 120, "the mono/textStyle region is suspiciously short"
    assert "UIFontMetrics(forTextStyle:" in region
    assert ".scaledValue(for: size)" in region
    assert "textStyle(for: size)" in region
    # The no-ceiling rule, in code.
    assert "min(" not in region, "a clamped point size is the ceiling again"
    assert "maximumPointSize" not in region


def test_the_panel_theme_is_untouched():
    """The Mac has no Dynamic Type, so its `mono` keeps a fixed size on
    purpose. `test_phone_theme_drift.py` compares the colour tokens and
    `corner`, never this body — the two are legitimately allowed to differ."""
    text = _read(PANEL_THEME)
    assert ".system(size: size, weight: weight, design: .monospaced)" in text
    assert "UIFontMetrics" not in text
    assert "dynamicTypeSize" not in text


def test_nothing_clamps_the_system_text_size():
    for path in SOURCES:
        text = _read(path)
        assert CLAMP not in text, (
            f"{path.name} clamps the text size; a cap is a ceiling by another "
            "name. A screen that genuinely needs one needs a different helper.")
    # Self-check: a doctored source would be caught, so the needle is real.
    doctored = "some.view\n    .dynamicTypeSize(...DynamicTypeSize.accessibility1)\n"
    assert CLAMP in doctored


# --- the threshold is read from the environment ---------------------------------


@pytest.mark.parametrize("name", REFLOW)
def test_the_reflow_threshold_is_read_from_the_environment(name):
    text = _read(PHONE / name)
    assert READ in text, f"{name} reflows without reading the environment"
    assert ("dynamicTypeSize.isAccessibilitySize" in text
            or "dynamicTypeSize >= .accessibility1" in text), name
    # Never a screen measurement: the size the person chose is the input, not
    # the width it happens to produce.
    assert "UIScreen" not in text, name
    assert "UIFont.preferredFont" not in text, name


# --- the fleet row's columns survive the fork -----------------------------------


def test_the_columnar_row_survives_the_fork():
    """`ProcessTable.swift` is the most heavily pinned file on the phone: five
    assertions across `test_phone_fleet_and_board_legibility.py`,
    `test_phone_helpers_per_agent.py` and `test_phone_context_trend.py` count
    literals in it, and three of them find the *first* occurrence. A stacked
    branch that repeats one of them fails tests with nothing to do with
    accessibility, and reads as an unrelated regression."""
    text = _read(PHONE / "ProcessTable.swift")
    assert "private var columns" in text
    assert "private var stacked" in text
    assert text.index("private var columns") < text.index("private var stacked"), (
        "columns must come first: the pinned regexes read the first match")
    # `lineLimit(3)` was here until the command line stopped being capped at
    # all (`test_phone_text_in_full.py`). What replaced it as the shared,
    # once-only line-2 marker is the `fixedSize` that makes it wrap.
    assert "lineLimit(3)" not in text
    for literal, why in (
        ("Text(ctxLabel)", "test_context_trend_marker_is_not_reverted"),
        ('Text("AGE")', "test_phone_age_ticks_once_a_second"),
        ("agent.subagentSummary",
         "test_the_row_draws_the_summary_on_live_agents_only"),
        ("TimelineView(.periodic(from: .now, by: 1))",
         "test_phone_age_ticks_once_a_second"),
    ):
        assert text.count(literal) == 1, (
            f"{literal!r} appears {text.count(literal)} times; {why} counts it")


# --- every control speaks -------------------------------------------------------


def test_icon_only_controls_speak():
    """A glyph with no name reads out as "button" and nothing else."""
    total = 0
    for path in VIEWS:
        lines = _read(path).splitlines()
        for i, line in enumerate(lines):
            if "Image(systemName:" not in line:
                continue
            total += 1
            window = "\n".join(lines[i:i + 15])
            assert (".accessibilityLabel(" in window
                    or ".accessibilityHidden(true)" in window), (
                f"{path.name}:{i + 1} draws a glyph that says nothing")
    assert total >= 5, f"only {total} system images found; the sweep found none"


#: Anything that takes a press, a tap or a keystroke. A file holding one of
#: these draws a control, and a control with no accessibility surface anywhere
#: in its file is a screen a screen reader cannot work.
_INTERACTIVE = ("Button(", "NavigationLink", "TextField(", "SecureField(",
                "Toggle(", "Picker(", "Menu {")

_SURFACE = ("accessibilityLabel", "accessibilityHidden", "accessibilityElement",
            "accessibilityAddTraits", "accessibilityValue", "accessibilityHint",
            "accessibilityAction")


#: One accessibility touch buys this many control constructs, and no more.
#: Deliberately loose: many `Button("SOME WORDS")` are named by their own text
#: and need nothing, and a menu body is a `Button` per row. Every file today
#: clears it with room — the tightest is `ComposerView.swift` at 5 for 20,
#: where the floor is 3 — so this is a floor against the next silent screen,
#: not a target anybody has to hit.
CONTROLS_PER_TOUCH = 8


def _controls(text: str) -> int:
    return sum(text.count(needle) for needle in _INTERACTIVE)


def _surface(text: str) -> int:
    return sum(text.count(needle) for needle in _SURFACE)


def _floor(controls: int) -> int:
    """At least one touch, then one more per `CONTROLS_PER_TOUCH`."""
    return max(1, -(-controls // CONTROLS_PER_TOUCH))


def _interactive_views():
    out = []
    for path in VIEWS:
        if path.name in BYTE_PINNED_WHOLE:
            continue
        if _controls(_read(path)):
            out.append(path)
    return out


def test_every_interactive_screen_has_a_voice():
    """`test_icon_only_controls_speak` only fires on files that draw an SF
    Symbol, so a whole screen with no glyph and no labels used to pass it in
    silence — which is exactly how `Pairing.swift`, the first screen a new
    user sees, went the whole first pass untouched.

    The floor **scales with the file**. One token per file was satisfiable by
    a single `.accessibilityHidden(true)` on a decorative rule in a
    nine-hundred-line screen, which is the same silence wearing a badge; the
    requirement is `_floor(controls)` instead, so a screen that grows controls
    has to grow a voice with them.
    """
    files = _interactive_views()
    assert len(files) >= 12, f"only {len(files)} interactive files found"
    names = {p.name for p in files}
    # Anti-vacuous: the needles still find the screens they are meant to.
    for expected in ("Pairing.swift", "CatchUpView.swift", "ProfileView.swift",
                     "AgentDetailView.swift", "CardDetailView.swift",
                     "BobPhoneApp.swift", "WorkRecordView.swift",
                     "OutcomeScreens.swift"):
        assert expected in names, f"{expected} draws no control any more?"
    thin = []
    for path in files:
        text = _read(path)
        controls, surface = _controls(text), _surface(text)
        if surface < _floor(controls):
            thin.append(f"{path.name} ({surface} for {controls} controls, "
                        f"needs {_floor(controls)})")
    assert not thin, (
        "these screens draw controls a screen reader cannot work: "
        + "; ".join(sorted(thin)))


def test_the_voice_floor_would_notice_a_silent_screen():
    """The doctored-string self-check: a big screen carrying one decorative
    token is exactly what the one-token version of this rule let through, and
    it must now fail."""
    silent = "Button(\n" * 20 + ".accessibilityHidden(true)\n"
    assert _controls(silent) == 20
    assert _surface(silent) == 1
    assert _surface(silent) < _floor(_controls(silent))
    # And a file with one control and one label still passes: the floor is a
    # floor, not a quota.
    honest = 'Button("Go")\n.accessibilityLabel("Go")\n'
    assert _surface(honest) >= _floor(_controls(honest))


def test_the_files_the_plan_named_were_all_opened():
    """The plan's `## Files to change` named eight screens for "labels and
    traits on their buttons and rows", and six of them were missed on the
    first pass because nothing checked. Named here so the miss cannot be
    silent a second time."""
    for name in ("ProfileView.swift", "CatchUpView.swift",
                 "OutcomeScreens.swift", "Pairing.swift",
                 "BobPhoneApp.swift", "AgentDetailView.swift",
                 "CardDetailView.swift", "WorkRecordView.swift"):
        text = _read(PHONE / name)
        assert any(n in text for n in _SURFACE), name
    # `OutcomeViews.swift` is the exception, and it is not an oversight: it is
    # byte-identical to the panel's copy, so a label on the phone side alone
    # is a failing test rather than a feature. Giving it a voice is a change
    # owed to both surfaces, which is a different plan.
    assert BYTE_PINNED_WHOLE == {"OutcomeViews.swift"}
    phone = _read(PHONE / "OutcomeViews.swift").encode()
    panel = (ROOT / "panel" / "Sources" / "BobPanel"
             / "OutcomeViews.swift").read_bytes()
    assert phone == panel, (
        "OutcomeViews.swift drifted from the panel; this plan may not touch "
        "panel/, so it may not touch this file either")


#: Where `.accessibilityElement(children: .ignore)` is allowed, **how many
#: times**, and why. It mints a **new** element and keeps none of the children
#: — including the one carrying a `Button`'s activation and its enabled state
#: — so it is right only where something *outside* the subtree supplies the
#: press, or where there is no press at all.
#:
#: The count is load-bearing and not decoration. Keyed on `(file, struct)`
#: alone, a *second* `.ignore` added inside an already-rostered struct — on a
#: `Button` this time — produces a key that is already in the map and passes
#: in silence, which is the exact bug the roster exists to catch. Two of the
#: entries below sit in one file, so file granularity was never enough
#: either. Adding a site means adding a reason **and** moving a number.
IGNORE_SITES = {
    ("NeedsYouView.swift", "NeedsYouView"):
        (1, "the orphan permission entry — a published prompt with no agent "
            "row behind it, so nothing to open: a text-only row spoken as "
            "one sentence, with no button or other press to preserve"),
    ("ProcessTable.swift", "PhoneProcessRow"):
        (1, "on the row, drawn as a DecryptButton's label; the button presses"),
    ("BoardView.swift", "PhoneBoardCard"):
        (1, "same — the card is a DecryptButton's label"),
    ("CatchUpView.swift", "CatchUpView"):
        (1, "same — the decision row is a DecryptButton's label"),
    ("WorkRecordView.swift", "PhoneWorkRecordSection"):
        (1, "same — the file row is a DecryptButton's label"),
    ("AgentDetailView.swift", "AgentDetailView"):
        (2, "the `# stdout · name · state` strip and the `runs in the editor` "
            "band; both readings about the session, neither a control — the "
            "band's three lines are one sentence and there is no jump on the "
            "phone"),
    ("TerminalPane.swift", "PhoneTerminalPane"):
        (1, "the emulator — a TUI a screen reader would read as hundreds of "
            "fragments; spoken as one element reading the non-blank rows off "
            "SwiftTerm's own buffer; the size buttons sit in the header, "
            "outside it and unmerged"),
    ("RecentlyView.swift", "RecentlyRow"):
        (1, "the diary line itself; the DecryptButton is around it"),
    ("AgentReportView.swift", "PhoneAgentReport"):
        (1, "one helper's line in the agent report; a reading, not a control — "
            "the name and its figures are one sentence, and the row itself "
            "is not a press"),
    ("UsageView.swift", "UsageView"):
        (1, "the spender line; a reading, not a control"),
    ("UsageView.swift", "UsageWindowRow"):
        (1, "the budget window row; a reading, not a control"),
    ("ComposerView.swift", "ComposerUsageLine"):
        (1, "the usage figures under the assistant tiles; a reading, not a control"),
    ("Specialists.swift", "CrewBand"):
        (2, "byte-pinned to the panel; the crew strip and each grid tile are "
            "readings about the card, not controls — the strip is drawn "
            "inside a card that is already a DecryptButton's label and is "
            "hidden there, and its whole sentence rides `CrewBand.caption`"),
    ("CardDetailView.swift", "PhoneCardDetailView"):
        (1, "the CREW block on the card screen; a reading with no control "
            "inside it, spoken as `CrewBand.caption`'s one sentence rather "
            "than as one fragment per face"),
    ("AgentChatter.swift", "AgentChatterView"):
        (1, "byte-pinned to the panel; a status line with no control inside "
            "it — the tap only finishes the typing, which the spoken sentence "
            "already says in full"),
}


def test_the_element_replacement_sits_where_the_press_survives():
    """A roster, `test_phone_text_in_full.py`'s discipline for `.lineLimit`.

    The bug this exists for is real and was shipped once: `.ignore` on a
    `Button` itself silently drops the press and the disabled state, and it
    looks identical in a diff to the correct use on a DecryptButton's label.
    Nothing about the string says which it is, so the check is a named list
    rather than a pattern, and a new site has to be argued for in writing.
    """
    found: dict = {}
    for path in SOURCES:
        lines = _read(path).splitlines()
        for i, line in enumerate(lines):
            if ".accessibilityElement(children: .ignore)" not in line:
                continue
            owner = None
            for back in lines[:i][::-1]:
                match = re.match(r"(?:private )?struct (\w+)", back)
                if match:
                    owner = match.group(1)
                    break
            key = (path.name, owner)
            assert key in IGNORE_SITES, (
                f"{path.name}:{i + 1} in {owner} replaces an element's "
                "children. On a Button that drops the press and the disabled "
                "state — use .combine, or an explicit .accessibilityAction. "
                "If it is right, add it to IGNORE_SITES with its reason.")
            found[key] = found.get(key, 0) + 1
    for key, (expected, reason) in IGNORE_SITES.items():
        assert key in found, f"IGNORE_SITES names a site that is gone: {key}"
        assert found[key] == expected, (
            f"{key[0]} · {key[1]} replaces its children {found[key]} times, "
            f"and the roster allows {expected} ({reason}). A second one is "
            "how the Button case comes back unseen — argue for it here.")
    assert set(found) == set(IGNORE_SITES), (
        f"unrostered sites: {set(found) - set(IGNORE_SITES)}")


def test_glyph_buttons_speak():
    """▲, ▼ and × are silent symbols. `label` is not optional on purpose."""
    text = _read(PHONE / "PipelineView.swift")
    signature = _member(text, "private func controlButton(")
    assert "label: String" in signature
    assert ".accessibilityLabel(label)" in signature
    calls = re.findall(r'controlButton\("[^"]+",\s*label:\s*"([^"]*)"', text)
    assert len(calls) >= 3, f"only {len(calls)} labelled call sites"
    for label in calls:
        assert label.strip(), "a control button was passed an empty name"


# --- every row speaks as one sentence -------------------------------------------

_ROWS = (
    ("ProcessTable.swift", "PhoneProcessRow"),
    ("BoardView.swift", "PhoneBoardCard"),
    ("RecentlyView.swift", "RecentlyRow"),
    ("UsageView.swift", "UsageWindowRow"),
    ("ComposerView.swift", "ComposerUsageRow"),
)


@pytest.mark.parametrize("name,struct", _ROWS)
def test_rows_speak_as_one_element(name, struct):
    chunk = _struct(_read(PHONE / name), struct)
    assert ".accessibilityElement(children:" in chunk, struct
    assert ".accessibilityLabel(spoken)" in chunk, struct
    assert "private var spoken: String" in chunk, struct


@pytest.mark.parametrize("name,struct", _ROWS)
def test_the_spoken_sentence_reads_only_the_drawn_row(name, struct):
    """A `spoken` that walked the snapshot would be a second source of truth
    for a fact the row is already drawing — and `CLAUDE.md`'s rule is that the
    Mac's own sentences (`queue_reason`, `manual_check_due`) are drawn
    verbatim, never recomposed here."""
    chunk = _struct(_read(PHONE / name), struct)
    spoken = _member(chunk, "private var spoken: String")
    assert "snapshot" not in spoken, f"{struct}.spoken walks the snapshot"
    assert "filter(" not in spoken, f"{struct}.spoken filters a collection"
    assert ".count" not in spoken, f"{struct}.spoken counts a collection"


def test_the_row_sentences_name_the_fields_they_read():
    process = _struct(_read(PHONE / "ProcessTable.swift"), "PhoneProcessRow")
    spoken = _member(process, "private var spoken: String")
    assert "FleetAge.spoken" in spoken
    assert "ctxSpoken" in spoken
    card = _struct(_read(PHONE / "BoardView.swift"), "PhoneBoardCard")
    card_spoken = _member(card, "private var spoken: String")
    assert "queueReason" in card_spoken, "the Mac's queue sentence, verbatim"
    assert "manualCheckDue" in card_spoken


# --- the decoration goes quiet --------------------------------------------------


def test_decorative_art_is_hidden():
    found = 0
    for path in SOURCES:
        lines = _read(path).splitlines()
        for i, line in enumerate(lines):
            if "Canvas {" not in line:
                continue
            found += 1
            window = "\n".join(lines[i:i + 13])
            assert ".accessibilityHidden(true)" in window, (
                f"{path.name}:{i + 1} draws a canvas a screen reader would read")
    assert found >= 2, f"only {found} canvases found; the sweep found none"
    theme = _read(PHONE / "Theme.swift")
    for struct in ("PixelMark", "BrandMark"):
        assert ".accessibilityHidden(true)" in _struct(theme, struct), struct


def test_group_headings_carry_the_header_trait():
    """A heading is a landmark: the rotor's heading jump is how a screen
    reader skips a group rather than swiping through every row in it."""
    header = _struct(_read(PHONE / "ProcessTable.swift"), "PhoneSectionHeader")
    assert ".accessibilityAddTraits(.isHeader)" in header
    board = _struct(_read(PHONE / "BoardView.swift"), "BoardProjectHeader")
    assert ".accessibilityAddTraits(.isHeader)" in board


def test_the_column_headings_are_absent_and_silent_at_accessibility_sizes():
    header = _struct(_read(PHONE / "ProcessTable.swift"), "PhoneProcessHeader")
    assert "!dynamicTypeSize.isAccessibilitySize" in header, (
        "column headings over stacked rows are noise")
    assert ".accessibilityHidden(true)" in header, (
        "each row speaks its own fields now")


def test_nothing_announces_itself():
    """Announcements were considered and rejected as noisy — nothing should be
    spoken that the person did not swipe to. The rejection lives here."""
    for path in SOURCES:
        text = _read(path)
        assert "UIAccessibility.post(" not in text, path.name
        assert "AccessibilityNotification" not in text, path.name


def test_the_widget_is_out_of_scope():
    """A widget is drawn at fixed system sizes with its own `WidgetTheme`;
    scaling it is a separate decision."""
    sources = sorted(WIDGET.glob("*.swift"))
    assert sources, "no widget sources found"
    for path in sources:
        text = _read(path)
        assert "Theme.mono" not in text, path.name
        assert "dynamicTypeSize" not in text, path.name
        assert "UIFontMetrics" not in text, path.name


def test_area_picker_preserves_the_button_and_selected_state():
    text = _read(PHONE / "Areas.swift")
    assert "AreaChoiceButton" in text
    assert ".accessibilityElement(children: .ignore)" not in text
    assert ".accessibilityLabel(Self.label(area))" in text
    assert ".accessibilityAddTraits(selected == area.slug ? .isSelected : [])" in text
