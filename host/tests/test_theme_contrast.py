"""Signal's semantic text and action pairs remain legible on their surfaces."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
COLORS = json.loads((ROOT / "design-system/tokens.json").read_text())["colors"]
PAIRS = (
    ("text", "canvas"), ("text", "surface"), ("text", "raised"),
    ("muted", "canvas"), ("muted", "surface"), ("muted", "raised"),
    ("accent", "canvas"), ("accent", "raised"),
    ("attention", "surface"), ("attention", "raised"),
    ("danger", "surface"), ("danger", "raised"),
    ("accentInk", "accent"),
)


def luminance(hex_color: str) -> float:
    channels = [int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
              for c in channels]
    return sum(weight * channel for weight, channel in
               zip((0.2126, 0.7152, 0.0722), linear))


def contrast(foreground: str, background: str) -> float:
    a, b = sorted((luminance(foreground), luminance(background)), reverse=True)
    return (a + 0.05) / (b + 0.05)


@pytest.mark.parametrize("foreground,background", PAIRS)
def test_semantic_text_pair_clears_wcag_normal_text(foreground: str, background: str):
    ratio = contrast(COLORS[foreground], COLORS[background])
    assert ratio >= 4.5, f"{foreground} on {background} is {ratio:.2f}:1"


def test_contrast_check_detects_a_bad_edit():
    assert contrast("#6B8573", COLORS["surface"]) < 4.5


def test_native_aliases_keep_danger_and_attention_distinct():
    for side in ("panel/Sources/BobPanel", "ios/BobPhone"):
        theme = (ROOT / side / "Theme.swift").read_text()
        assert "static let alarm = danger" in theme
        assert "static let amber = attention" in theme
        assert "static let phosphorBright = text" in theme


@pytest.mark.parametrize("background", ("canvas", "surface"))
def test_a_divider_reads_as_a_line(background: str):
    """A rule, and an unfilled chip's only edge, holds 2:1 on the grounds it
    is drawn on; the generator refuses a fainter `line` (review, 25 Sep
    2026)."""
    ratio = contrast(COLORS["line"], COLORS[background])
    assert ratio >= 2.0, f"line on {background} is {ratio:.2f}:1"


def _over(fg: str, bg: str, alpha: float) -> str:
    f = [int(fg[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(bg[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join("%02X" % round(alpha * x + (1 - alpha) * y)
                         for x, y in zip(f, b))


@pytest.mark.parametrize("side", ("panel/Sources/BobPanel", "ios/BobPhone"))
def test_a_held_button_fades_but_keeps_readable_words(side: str):
    """A disabled `AlarmOutline` must look held — its words at `heldInk`, a
    filled one's fill at `heldEdge` — and still be read (3:1) on every
    ground (review, 25 Sep 2026)."""
    theme = (ROOT / side / "Theme.swift").read_text()
    style = theme.split("struct AlarmOutline: ButtonStyle {")[1]
    assert "static let heldInk: Double = 0.65" in style
    assert ".foregroundStyle(isEnabled ? ink : ink.opacity(AlarmOutline.heldInk))" in style
    assert "? (isEnabled ? color : color.opacity(AlarmOutline.heldEdge))" in style
    for word in ("accent", "danger"):
        for ground in ("canvas", "surface", "raised"):
            held = _over(COLORS[word], COLORS[ground], 0.65)
            ratio = contrast(held, COLORS[ground])
            assert ratio >= 3.0, f"held {word} on {ground} is {ratio:.2f}:1"


def test_the_macs_irreversible_buttons_are_red():
    """`AlarmOutline`'s default is the accent, so a button that deletes
    for good names `Theme.danger` itself (review, 25 Sep 2026)."""
    panel = ROOT / "panel/Sources/BobPanel"
    board = (panel / "BoardView.swift").read_text()
    for words in ('Text("Clear all done items")', 'Button("Confirm clear all")',
                  'Button("Clear all \\(scope.count) now")'):
        after = board.split(words)[1]
        style = after[after.index(".buttonStyle("):].split("\n")[0]
        assert "AlarmOutline(color: Theme.danger)" in style, words
    drafts = (panel / "DraftsSheet.swift").read_text()
    for words in ('"Delete \\(deletableCount) drafts?"', '"Sure?" : "DELETE"'):
        after = drafts.split(words)[1]
        style = after[after.index(".buttonStyle("):].split("\n")[0]
        assert "AlarmOutline(color: Theme.danger)" in style, words


@pytest.mark.parametrize("side", ("panel/Sources/BobPanel", "ios/BobPhone"))
def test_no_theme_colour_is_typed_by_hand(side: str):
    """Every Theme colour comes from the generated tokens, the AppKit/UIKit
    bridges included, so a changed `tokens.json` cannot leave the window
    edge or the scanner ground behind (review, 25 Sep 2026)."""
    import re
    theme = (ROOT / side / "Theme.swift").read_text()
    typed = re.findall(r"(?:NS|UI)?Color\((?:srgbRed|red):", theme)
    assert typed == [], typed
