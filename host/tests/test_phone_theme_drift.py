"""The phone wears the Mac's look, and it cannot quietly stop.

`ios/BobPhone/Theme.swift` and `ios/BobPhone/Cast.swift` are hand copies of
`panel/Sources/BobPanel/Theme.swift` and `Cast.swift`. That was a deliberate
decision — the phone is an Xcode app target, the panel is a SwiftPM module, and
coupling the two builds to share one file was refused. The price of copying is
drift, and this test is what was bought instead: edit a colour, a cast name or a
column caption on one side and the other side fails here.

A **Python** test under `host/tests/`, not an XCTest: `cd host && .venv/bin/pytest`
is the suite that actually runs on this machine and in every verification pass,
`ios/` has no test target, and the TestFlight workflow runs none — a test in a
target nobody runs is worse than no test. `test_cast.py` is the precedent for
pinning Swift source from here.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

PANEL_THEME = ROOT / "panel" / "Sources" / "BobPanel" / "Theme.swift"
PHONE_THEME = ROOT / "ios" / "BobPhone" / "Theme.swift"
PANEL_CAST = ROOT / "panel" / "Sources" / "BobPanel" / "Cast.swift"
PHONE_CAST = ROOT / "ios" / "BobPhone" / "Cast.swift"
PANEL_MARKDOWN = ROOT / "panel" / "Sources" / "BobPanel" / "Markdown.swift"
PHONE_MARKDOWN = ROOT / "ios" / "BobPhone" / "Markdown.swift"
PANEL_SPECIALISTS = ROOT / "panel" / "Sources" / "BobPanel" / "Specialists.swift"
PHONE_SPECIALISTS = ROOT / "ios" / "BobPhone" / "Specialists.swift"
PANEL_CARD_SHEET = ROOT / "panel" / "Sources" / "BobPanel" / "BoardCardSheet.swift"
PHONE_COMPOSER = ROOT / "ios" / "BobPhone" / "ComposerView.swift"
PANEL_BOARD = ROOT / "panel" / "Sources" / "BobPanel" / "BoardLanes.swift"
PHONE_BOARD = ROOT / "ios" / "BobPhone" / "BoardView.swift"
PANEL_DETAIL = ROOT / "panel" / "Sources" / "BobPanel" / "AgentDetailPane.swift"
PHONE_DETAIL = ROOT / "ios" / "BobPhone" / "AgentDetailView.swift"

# Every token both files must carry. A token missing on either side is a
# failure, never a skip: the interesting drift is one side gaining a colour the
# other never hears about.
TOKENS = {
    "bg", "bar", "well", "phosphor", "phosphorBright", "dim", "faint",
    "hair", "rule", "alarm", "amber", "card",
}

_COLOUR = re.compile(
    r"static let (\w+) = Color\("
    r"red: (\d+) / 255, green: (\d+) / 255, blue: (\d+) / 255\)"
    r"(?:\.opacity\(([\d.]+)\))?"
)
_CORNER = re.compile(r"static let corner: CGFloat = (\d+)")
_NAMES = re.compile(r"static let names = \[(.*?)\]", re.S)
_CAPTIONS = re.compile(r'case \.(\w+): return "(.*?)"')


def _read(path: Path) -> str:
    assert path.is_file(), f"missing file: {path}"
    return path.read_text()


def _palette(path: Path) -> dict:
    """token -> (r, g, b, opacity-or-None), plus `corner`."""
    text = _read(path)
    out = {}
    for name, r, g, b, opacity in _COLOUR.findall(text):
        out[name] = (int(r), int(g), int(b), opacity or None)
    corner = _CORNER.search(text)
    assert corner, f"no `corner` constant parsed out of {path}"
    out["corner"] = int(corner.group(1))
    return out


def _names(path: Path) -> list:
    text = _read(path)
    match = _NAMES.search(text)
    assert match, f"no `Cast.names` array parsed out of {path}"
    return re.findall(r'"([^"]+)"', match.group(1))


def test_the_palette_is_the_same_palette():
    panel = _palette(PANEL_THEME)
    phone = _palette(PHONE_THEME)
    # The preflight the whole test rests on: a future reformat that breaks the
    # regex must fail loudly, not pass vacuously on two empty maps.
    assert len(panel) >= len(TOKENS) + 1, f"parsed too little from {PANEL_THEME}"
    assert len(phone) >= len(TOKENS) + 1, f"parsed too little from {PHONE_THEME}"

    expected = TOKENS | {"corner"}
    assert expected <= set(panel), f"panel is missing {expected - set(panel)}"
    assert expected <= set(phone), f"phone is missing {expected - set(phone)}"

    for token in sorted(expected):
        assert panel[token] == phone[token], (
            f"{token} drifted: panel {panel[token]} vs phone {phone[token]}")


def test_the_parser_would_notice_a_changed_channel():
    """A doctored copy must mismatch.

    Without this, a regex broken by a future `swift-format` pass would make
    every comparison above compare nothing to nothing and pass. Never "fix" a
    breakage here by loosening the comparison to whatever happened to parse.
    """
    panel_text = _read(PANEL_THEME)
    assert "green: 255 / 255, blue: 124 / 255" in panel_text
    doctored = panel_text.replace(
        "Color(red: 124 / 255, green: 255 / 255, blue: 124 / 255)",
        "Color(red: 125 / 255, green: 255 / 255, blue: 124 / 255)")
    assert doctored != panel_text

    found = {n: (int(r), int(g), int(b)) for n, r, g, b, _ in _COLOUR.findall(doctored)}
    real = {n: (int(r), int(g), int(b)) for n, r, g, b, _ in _COLOUR.findall(panel_text)}
    assert len(found) >= len(TOKENS)
    assert found != real
    assert found["phosphor"] != real["phosphor"]


def test_the_cast_is_the_same_cast():
    panel = _names(PANEL_CAST)
    phone = _names(PHONE_CAST)
    assert len(panel) == 20, f"expected 20 names in {PANEL_CAST}, got {panel}"
    # Order matters as much as membership: `character(for:)` indexes this list
    # by a hash, so a reordering hands every agent somebody else's face.
    assert panel == phone


def test_the_column_captions_are_the_panel_s_own_words():
    board = _read(PANEL_BOARD)
    caption_block = board.split("var caption: String")[1].split("}")[0]
    captions = [text for _, text in _CAPTIONS.findall(caption_block)]
    assert len(captions) == 4, f"expected 4 captions, parsed {captions}"

    phone = _read(PHONE_BOARD)
    for caption in captions:
        assert caption in phone, f"caption missing from the phone: {caption!r}"


MARKDOWN_MARKER = "enum Markdown {"


def _markdown_code(path: Path) -> str:
    """The renderer's code region: the marker line through end of file.

    Only the header above it may differ between the two copies — the panel's
    names `BoardCardSheet` and calls itself a plain SwiftUI executable, neither
    of which the phone may repeat as its own. Everything below is one parser.
    """
    text = _read(path)
    lines = text.splitlines(keepends=True)
    starts = [i for i, line in enumerate(lines) if line.rstrip("\n") == MARKDOWN_MARKER]
    assert len(starts) == 1, f"expected one {MARKDOWN_MARKER!r} line in {path}, got {len(starts)}"
    return "".join(lines[starts[0]:])


def test_the_markdown_renderer_is_the_same_renderer():
    panel = _markdown_code(PANEL_MARKDOWN)
    phone = _markdown_code(PHONE_MARKDOWN)
    # Anti-vacuous: a split that captured nothing must fail loudly rather than
    # compare two empty strings and pass, per this file's own doctrine.
    for name, region in (("panel", panel), ("phone", phone)):
        assert len(region) >= 8000, f"parsed too little from the {name} renderer"
        assert "struct MarkdownText" in region, f"no MarkdownText in the {name} region"
    assert panel == phone, (
        "the phone's Markdown.swift has drifted from the panel's. Mirror the "
        "edit onto the other side in this same commit — never re-baseline one "
        "side and never loosen this comparison.")


def test_a_doctored_renderer_would_not_compare_equal():
    """A one-literal change on one side must mismatch.

    Without this, a future refactor that moved the marker line or emptied the
    region would make the comparison above compare nothing to nothing.
    """
    panel = _markdown_code(PANEL_MARKDOWN)
    assert "spacing: 7" in panel
    doctored = panel.replace("spacing: 7", "spacing: 8", 1)
    assert doctored != panel
    assert doctored != _markdown_code(PHONE_MARKDOWN)


SPECIALISTS_MARKER = "enum Specialists {"

# The five specialists this repo declares, and the words shipped beside each.
# Both sides carry them because both sides carry one file.
SPECIALIST_ROLES = {
    "bc-card-preparer": "drafts the card from your words",
    "bc-planner": "writes the plan",
    "bc-implementer": "writes the code",
    "bc-verifier": "checks it against the plan",
    "bc-bug-auditor": "hunts bugs afterwards",
    "bc-integration-reviewer": "checks it survives the real install",
    "bc-security-reviewer": "checks the doors and the keys",
}


def _specialists_code(path: Path) -> str:
    """The specialist table and grid: the marker line through end of file.

    Modelled on `_markdown_code`. Only the header above the marker may differ
    — each copy names the other one — and everything below is one file.
    """
    text = _read(path)
    lines = text.splitlines(keepends=True)
    starts = [i for i, line in enumerate(lines) if line.rstrip("\n") == SPECIALISTS_MARKER]
    assert len(starts) == 1, (
        f"expected one {SPECIALISTS_MARKER!r} line in {path}, got {len(starts)}")
    return "".join(lines[starts[0]:])


def test_the_specialists_are_the_same_specialists():
    panel = _specialists_code(PANEL_SPECIALISTS)
    phone = _specialists_code(PHONE_SPECIALISTS)
    # Anti-vacuous, per this file's own doctrine: a split that captured
    # nothing must fail loudly rather than compare two empty strings.
    for name, region in (("panel", panel), ("phone", phone)):
        assert len(region) >= 1500, f"parsed too little from the {name} specialists"
        assert "struct CrewBand" in region, f"no CrewBand in the {name} region"
    assert panel == phone, (
        "the phone's Specialists.swift has drifted from the panel's. Mirror "
        "the edit onto the other side in this same commit — never re-baseline "
        "one side and never loosen this comparison.")


def test_a_doctored_specialists_file_would_not_compare_equal():
    """A one-literal change on one side must mismatch."""
    panel = _specialists_code(PANEL_SPECIALISTS)
    assert 'checks the doors and the keys' in panel
    doctored = panel.replace('checks the doors and the keys', 'changed job description', 1)
    assert doctored != panel
    assert doctored != _specialists_code(PHONE_SPECIALISTS)


def test_the_specialist_table_ships_seven_names_and_seven_jobs():
    panel = _specialists_code(PANEL_SPECIALISTS)
    for stage, role in SPECIALIST_ROLES.items():
        assert f'"{stage}"' in panel, f"specialist missing from the table: {stage}"
        assert f'"{role}"' in panel, f"role line missing from the table: {role!r}"


@pytest.mark.parametrize("path", [PANEL_SPECIALISTS, PHONE_SPECIALISTS])
def test_the_shared_file_stays_cross_platform(path):
    """`.help(_:)` is macOS-only and `AppKit`/`UIKit` are one platform each.

    `BoardCardView` is the obvious file to crib from and uses `.help` freely;
    copying it here breaks the iOS build, which no CI in this repo catches.
    """
    text = _read(path)
    assert ".help(" not in text, f"{path} uses macOS-only .help(_:)"
    assert not re.search(r"^import (AppKit|UIKit)", text, re.M), (
        f"{path} imports a one-platform framework")


# A sample of the pools: which character belongs to which role. The captions
# these once fed are retired — a session is named from the job it is doing now,
# so a line telling you what its *character* usually does was the thing saying
# the wrong one.
POOL_MEMBERS = {
    "sawa": "bc-security-reviewer",
    "captcha": "bc-security-reviewer",
    "franio": "bc-bug-auditor",
    "ptyś": "bc-bug-auditor",
    "relay": "bc-implementer",
    "zosia": "bc-verifier",
}


def test_area_pools_are_delivery_leads():
    from dark_army_daemon import areas
    for path in (ROOT / "panel/Sources/BobPanel/Areas.swift", ROOT / "ios/BobPhone/Areas.swift"):
        text = path.read_text()
        assert "static let all:" in text
        for area in areas.AREAS:
            assert f'slug: "{area.slug}"' in text
            assert all(f'"{slug}"' in text for slug in area.pool)

def test_the_default_role_caption_is_gone_from_both_surfaces():
    """The retired caption, pinned absent on both sides at once.

    It said what a *character* usually does, which stopped being the useful
    sentence the moment a dispatched session started being named from the job
    it is actually doing. Retired rather than corrected — and retired on both
    surfaces in one edit, or the phone becomes the copy that quietly still
    says the wrong thing.
    """
    for path in (PANEL_DETAIL, PHONE_DETAIL, PANEL_SPECIALISTS, PHONE_SPECIALISTS):
        text = _read(path)
        for token in ("Default role", "defaultRole", "DefaultRole"):
            assert token not in text, f"{path} still carries {token}"
    for path in (PANEL_DETAIL, PHONE_DETAIL):
        assert "Specialists.pools[" not in _read(path), path


def test_the_tile_names_who_usually_takes_the_job():
    """`anchorName(for:)` on both copies, and the tile's own habit line.

    The wording is load-bearing: the real allocation happens when the work
    starts and depends on who is free then, so the tile says "usually" and
    never asserts a name. It wraps rather than clipping.
    """
    for path in (ROOT / "panel/Sources/BobPanel/Areas.swift", ROOT / "ios/BobPhone/Areas.swift"):
        text = path.read_text()
        assert "static func anchorName(_ slug: String) -> String" in text, path
        assert 'Text("usually \\(usually)")' in text, path
        region = text.split('Text("usually \\(usually)")')[1][:400]
        assert ".lineLimit(" not in region, path
        assert "Theme.mono(" in region, path


def test_both_composers_draw_the_grid():
    """The call sites, one per surface, in the same place on each."""
    panel = _read(PANEL_CARD_SHEET)
    assert "AreaGrid(selected: $state.draft.area)" in panel
    # Composer only: a face on a real card would read as a stage that ran.
    assert "if isComposer {" in panel
    phone = _read(PHONE_COMPOSER)
    assert "AreaGrid(selected: $area)" in phone


@pytest.mark.parametrize(
    "path",
    [PANEL_THEME, PHONE_THEME, PANEL_CAST, PHONE_CAST, PANEL_BOARD, PHONE_BOARD,
     PANEL_MARKDOWN, PHONE_MARKDOWN, PANEL_SPECIALISTS, PHONE_SPECIALISTS,
     ROOT / "panel/Sources/BobPanel/Areas.swift", ROOT / "ios/BobPhone/Areas.swift"])
def test_both_sides_exist(path):
    """A missing file is a failure, never a skip.

    A file renamed out from under this test is exactly the drift it exists to
    catch, and a skip would report it as green.
    """
    assert path.is_file(), f"missing file: {path}"


def test_the_phone_draws_a_table_as_a_grid_and_a_card_prompt_as_markdown():
    """The screen that prompted this: a `## Work done` table drawn as raw
    pipes with the `---|---` row and the backticks still in it. The renderer
    is byte-pinned above, so this pins the *shape* once and the card screen's
    use of it — Prepare writes a prompt as Markdown."""
    phone = _markdown_code(PHONE_MARKDOWN)
    assert "static func tableRows(_ text: String) -> [[String]]" in phone
    assert "case .table:" in phone and "Grid(alignment: .topLeading" in phone
    assert "case .code, .table:" not in phone
    assert "run.inlinePresentationIntent?.contains(.code) == true" in phone
    card = _read(ROOT / "ios" / "BobPhone" / "CardDetailView.swift")
    # `fullPrompt` is the card's real instructions when the on-demand read has
    # landed and the frame's preview until then — the source moved, the
    # renderer did not.
    assert "MarkdownText(source: fullPrompt, base: 12, mono: true)" in card
    assert "Text(card.prompt)" not in card
    assert "Text(fullPrompt)" not in card
