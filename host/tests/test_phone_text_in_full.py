"""The phone app does not clip prose.

`ios/` has no test target that runs — the decision, recorded in
`test_phone_theme_drift.py`'s own docstring, is that this repo pins Swift
source with a **Python lint** under `host/tests/`, because `cd host &&
.venv/bin/pytest` is the suite that actually runs on this machine. Nothing
here builds the phone app, so this file is the whole automatic guard under the
sweep that took the ellipses off.

The contract, stated once:

- **Every surviving `.lineLimit(` is on `ALLOWED`, with its reason.** Four
  kinds earn one: a fixed-width column, a horizontally scrolling strip, a
  reserved-height chrome line, and a `5...` **minimum** (which reserves height
  and caps nothing). Anything else is prose, and prose runs on.
- **`.navigationTitle` is always a literal.** UIKit ellipsises an inline bar
  title and no SwiftUI modifier can make it wrap, so a bar that names a model
  field is the exact regression this sweep removed. The thing itself is drawn
  in the body, which this file also pins.
- **Removing a cap from an `HStack` child is a no-op without
  `.fixedSize(horizontal: false, vertical: true)`.** SwiftUI still offers that
  child less than its ideal width and truncates. So the companion modifier is
  counted, and the wrong axis — `horizontal: true`, which would push the
  fleet row's fixed cells off the screen — is banned on every file except
  `Markdown.swift`. A preformatted CRT is a horizontally scrolling strip:
  without `horizontal: true` the ScrollView proposes the pane and the
  drawing wraps, which is what made the phone screens stop looking like
  screens.
- **`Specialists.swift` is byte-pinned** to the panel's copy by
  `test_phone_theme_drift.py`, so its edit had to land on both sides. That
  test is the comparison; this one asserts the shape on each side.
- **Anti-vacuous.** Every sweep is paired with a positive assertion or a
  doctored-source self-check, so a broken needle cannot pass by matching
  nothing.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PHONE = ROOT / "ios" / "BobPhone"
PANEL_SPECIALISTS = ROOT / "panel" / "Sources" / "BobPanel" / "Specialists.swift"

SOURCES = sorted(PHONE.glob("*.swift"))

#: The modifier, not the word. A comment mentioning line limits is prose.
CAP = re.compile(r"\.lineLimit\([^\n]*\)")

#: Every cap allowed to exist on the phone, with the reason it earns.
#: `(file, exact matched text) -> why`. A cap in a file not listed here, or a
#: new cap in a listed file, fails and names the file.
ALLOWED = {
    ("FleetView.swift", ".lineLimit(dynamicTypeSize.isAccessibilitySize ? nil : 1)"):
        "the project chip strip scrolls sideways, same shape",
    ("ProcessTable.swift", ".lineLimit(1)"):
        "AGE sits in a fixed-width cell and reads `12s` / `5m` / `1h` / `—`, "
        "which can never need two lines",
    ("Theme.swift", ".lineLimit(1)"):
        "one-line top-bar chrome over a shipped vocabulary — every call site "
        "passes a literal — and `.accessibilityLabel(spoken)` carries it whole",
    ("BrandBar.swift", ".lineLimit(2, reservesSpace: true)"):
        "the reserved height is what stops the bar jumping every second as "
        "the reconnect countdown reflows",
    ("ComposerView.swift", ".lineLimit(5...)"):
        "`5...` is a minimum, not a cap: it reserves five lines and grows",
    ("AnswerBox.swift", ".lineLimit(5...)"):
        "the same minimum on the answer field",
    ("CommView.swift", ".lineLimit(5...)"):
        "the Comm composer's minimum — five lines of room that grow, "
        "`ComposerView.swift`'s reason",
    ("AgentDetailView.swift", ".lineLimit(1)"):
        "the cast quote on Details, under the origin lines, is a "
        "one-line caption shrinking to 0.7 before it truncates; it is "
        "flavour, never the agent's own words "
        "(`test_phone_decrypt_motion.py` pins it)",
    ("ComposerView.swift", ".lineLimit(1)"):
        "a TextField's single-line entry policy, not display truncation; iOS "
        "scrolls the field",
}

#: How many caps each file that carries one is allowed to carry. A file absent
#: from this table must carry none.
CEILING = {
    "FleetView.swift": 1,
    "ProcessTable.swift": 1,
    "Theme.swift": 1,
    "BrandBar.swift": 2,
    "ComposerView.swift": 2,
    "AnswerBox.swift": 1,
    "CommView.swift": 1,
    "AgentDetailView.swift": 1,
}

#: The one file allowed its own text size: a real terminal is a grid of
#: cells at the terminal's own size, not prose. It still carries **no**
#: `.lineLimit(` — it is absent from `CEILING`, so a cap there fails like
#: anywhere else — and the exemption it names is `test_phone_accessibility.py`'s
#: point-size rule, pinned by name on both sweeps so neither can widen alone.
TERMINAL_EXCEPTION = "TerminalPane.swift"

#: The files the sweep emptied. Named rather than derived, so deleting a cap's
#: whole call site does not quietly shrink the contract.
SWEPT = (
    "RecentlyView.swift",
    "ProfileView.swift",
    "UsageView.swift",
    "Specialists.swift",
)

#: `at most`, not `exactly`: `plans/2026-09-06-phone-dynamic-type-and-voiceover.md`
#: may later drop one of these at accessibility sizes, and must not fail here
#: when it does.
AT_MOST = ("Theme.swift", "FleetView.swift", "BrandBar.swift")

#: Every `.navigationTitle` argument on the phone, as written. Three of them
#: were a model field until this sweep; a new screen adds its title here.
TITLES = {'"new card"', '"profile"', '"PIPELINE"', '"agents"', '"timing"',
          '"knowledge"', '"access log"', '"usage"', '"comm"',
          '"scouting"', '"manual checks"', '"scout reports"', '"report"',
          '"Design system"', '"plans"', '"plan"', '"history"', '"rebuild"',
          '"review"', '"review run"'}

#: Sheet subjects still appear in full in their content, with no bar title.
SHEET_HEADERS = (
    ("CardDetailView.swift", 'Text(card.title.isEmpty ? "untitled" : card.title)'),
    ("WorkRecordView.swift", "Text(file.path)"),
    ("AgentDetailView.swift", "Text(nickname)"),
    ("CatchUpView.swift", "Text(item.title)"),
)

FIXED = ".fixedSize(horizontal: false, vertical: true)"

#: The floor under each file the sweep touched: what was there before plus
#: what step 4-6 added. A cap removed without its companion is the silent
#: no-op this counts against.
FIXED_SIZE_FLOOR = {
    "ProcessTable.swift": 5,
    "ProfileView.swift": 3,
    "UsageView.swift": 2,
    "ComposerView.swift": 2,
    "RecentlyView.swift": 2,
    "AgentDetailView.swift": 5,
}


def _read(path: Path) -> str:
    assert path.is_file(), f"missing file: {path}"
    return path.read_text()


def _caps(text: str) -> list:
    return CAP.findall(text)


# --- the sources are there ------------------------------------------------------


def test_the_sources_are_there():
    """Anti-vacuous guard for every sweep below: an empty `SOURCES` would make
    each of them pass by reading nothing."""
    assert len(SOURCES) >= 35, f"only {len(SOURCES)} phone sources found"
    names = {p.name for p in SOURCES}
    for expected in set(CEILING) | set(SWEPT) | {p for p, _ in SHEET_HEADERS}:
        assert expected in names, f"{expected} is gone from ios/BobPhone"
    assert PANEL_SPECIALISTS.is_file()


# --- which caps survive ---------------------------------------------------------


def test_every_surviving_cap_is_on_the_list():
    found = 0
    for path in SOURCES:
        for cap in _caps(_read(path)):
            found += 1
            key = (path.name, cap)
            assert key in ALLOWED, (
                f"{path.name} carries {cap} — the phone does not clip prose. "
                "A cap earns its place only as a fixed-width column, a "
                "horizontally scrolling strip, a reserved-height chrome line "
                "or a `5...` minimum; add it to ALLOWED with its reason, or "
                "let the words wrap.")
    assert found >= 8, f"only {found} caps found; the sweep matched nothing"
    for _, why in ALLOWED.items():
        assert len(why) >= 20, "every allowed cap states why it survives"


def test_the_swept_files_have_no_caps_left():
    for name in SWEPT:
        assert not _caps(_read(PHONE / name)), f"{name} clips prose again"
    assert not _caps(_read(PANEL_SPECIALISTS))


def test_the_terminal_is_the_one_exception_and_it_still_does_not_clip():
    """The emulator has its own text size, not its own ellipsis: the
    exception is the point-size rule, never this one."""
    from tests import test_phone_accessibility as a11y
    assert a11y.TERMINAL_EXCEPTION == TERMINAL_EXCEPTION
    text = _read(PHONE / TERMINAL_EXCEPTION)
    assert not _caps(text), "the terminal pane clips prose"
    assert TERMINAL_EXCEPTION not in CEILING
    assert "UIFont.monospacedSystemFont(ofSize:" in text


def test_each_file_is_at_or_under_its_ceiling():
    for path in SOURCES:
        count = len(_caps(_read(path)))
        allowed = CEILING.get(path.name, 0)
        assert count <= allowed, (
            f"{path.name} carries {count} caps, at most {allowed} allowed")


def test_the_allow_list_is_a_minimum_not_a_ceiling():
    """`plans/2026-09-06-phone-dynamic-type-and-voiceover.md` may later fork
    `Theme.swift`'s chrome line or `FleetView.swift`'s chip on
    `dynamicTypeSize.isAccessibilitySize`, which removes a cap rather than
    adding one. That must not fail here, so these three are asserted `at
    most`, never `exactly`."""
    for name in AT_MOST:
        assert len(_caps(_read(PHONE / name))) <= CEILING[name], name


def test_the_sweep_would_notice_a_new_cap():
    """Doctored-source self-check: without it a broken regex passes by
    matching nothing at all."""
    doctored = _read(PHONE / "BoardView.swift").replace(
        "                .foregroundStyle(Theme.phosphorBright)\n",
        "                .foregroundStyle(Theme.phosphorBright)\n"
        "                .lineLimit(3)\n", 1)
    caps = _caps(doctored)
    assert ".lineLimit(3)" in caps
    assert ("BoardView.swift", ".lineLimit(3)") not in ALLOWED


# --- no navigation bar names a variable -----------------------------------------


def test_no_navigation_title_names_a_variable():
    """An inline bar title ellipsises and cannot be made to wrap, which is the
    exact regression in the screenshot that started this. So every title is a
    literal, and the long thing is drawn in the body instead."""
    found = set()
    pattern = re.compile(r"\.navigationTitle\((.+)\)\n")
    for path in SOURCES:
        for arg in pattern.findall(_read(path)):
            found.add(arg.strip())
    assert found == TITLES, f"unexpected navigation titles: {found ^ TITLES}"
    for arg in found:
        for field in ("card.title", "file.path", "nickname", "agent.",
                      "entry.", "\\("):
            assert field not in arg, f"a bar title reads {arg}, which clips"


@pytest.mark.parametrize("name,body", SHEET_HEADERS)
def test_the_shortened_bars_still_draw_the_thing_in_the_body(name, body):
    """Nothing was lost, only relocated: the bar stopped naming it and the
    body was already drawing it whole."""
    text = _read(PHONE / name)
    assert ".navigationTitle(" not in text, name
    assert body in text, f"{name} no longer draws {body} in its body"


# --- the companion modifier -----------------------------------------------------


@pytest.mark.parametrize("name,floor", sorted(FIXED_SIZE_FLOOR.items()))
def test_an_uncapped_hstack_line_is_fixed_size(name, floor):
    """Removing `.lineLimit(1)` from an `HStack` child does not make it wrap —
    SwiftUI still offers it less than its ideal width. Without this count the
    whole change would be a silent no-op that nothing in this repo builds."""
    text = _read(PHONE / name)
    count = text.count(FIXED)
    assert count >= floor, (
        f"{name} carries {count} of {FIXED}, expected at least {floor}; a cap "
        "removed without its companion still ends in dots")


def test_the_wrong_axis_is_never_used():
    """`horizontal: true` — or a bare `.fixedSize()` — refuses to compress
    horizontally and pushes the fleet row's fixed AGE/STATE/CTX cells off the
    right of the screen.

    Markdown.swift is the one exception: a preformatted CRT/code block is a
    horizontally scrolling strip, and without `horizontal: true` the
    ScrollView proposes the pane and the drawing wraps. That file is not a
    fleet row. The exact modifier is pinned so this exemption cannot widen.
    """
    markdown = PHONE / "Markdown.swift"
    assert "fixedSize(horizontal: true, vertical: true)" in _read(markdown)
    for path in SOURCES + [PANEL_SPECIALISTS]:
        if path == markdown:
            continue
        text = _read(path)
        assert "fixedSize(horizontal: true" not in text, path.name
        assert ".fixedSize()" not in text, path.name


def test_the_variable_height_row_keeps_its_touch_target():
    """An uncapped command line makes the fleet row's height vary. The 44pt
    floor is what stops a one-word row dropping under the touch target."""
    assert "minHeight: 44" in _read(PHONE / "ProcessTable.swift")


# --- the byte-pinned tile -------------------------------------------------------


def test_the_specialist_tile_lost_its_cap_on_both_sides():
    """`test_phone_theme_drift.py` is the byte comparison; this asserts the
    shape on each side, so a one-sided edit fails in two places rather than
    tempting somebody to re-baseline.

    Every current crew name, counter, stage label and explanation must wrap.
    Area selection replaced the old composer specialist grid; inspect each
    actual Text's modifiers rather than counting the retired grid's lines.
    """
    for path in (PHONE / "Specialists.swift", PANEL_SPECIALISTS):
        text = _read(path)
        assert not _caps(text), f"{path} caps a specialist tile again"
        texts = re.findall(r'^\s*Text\([^\n]*\)(.*?)(?=^\s*(?:Text\(|[}\]]))',
                           text, re.MULTILINE | re.DOTALL)
        for label in ('Text(Self.display(stage.character))', 'Text(counter)',
                      'Text(stage.name)', 'Text(Self.line(stage))',
                      'Text(Specialists.short(name))'):
            assert label in text, f"{path} lost its crew label: {label}"
        assert texts
        assert all(FIXED in modifiers for modifiers in texts), (
            f"{path} lost the modifier that makes a crew label wrap")


def test_the_minimums_were_not_touched():
    """`5...` reserves height and caps nothing. `test_phone_field_wells.py`
    counts them; this states why they are on ALLOWED."""
    assert _read(PHONE / "ComposerView.swift").count("lineLimit(5...)") == 1
    assert _read(PHONE / "AnswerBox.swift").count("lineLimit(5...)") == 1
