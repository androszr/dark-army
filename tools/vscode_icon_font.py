#!/usr/bin/env python3
"""Bake the Dark Army mask into an icon font the VS Code extension can wear.

VS Code's status bar draws text and codicons, never images, so the mark that
sits in the panel's and the phone's top bar (`assets/brand/fsociety-mark.png`)
reaches the IDE as one glyph in a private icon font contributed by the
extension.  The committed mark is a large continuous-tone square. Tracing it
at full size would turn every stipple dot into a contour, so the glyph is a
run-length trace of a 64px reduction: one rectangle per horizontal run of lit
pixels, every contour wound the same way so the nonzero fill unions them.
Same picture, no second drawing to keep in step.

    python tools/vscode_icon_font.py            # write the font
    python tools/vscode_icon_font.py --check    # fail if it is stale

Needs fontTools and Pillow; neither is a runtime dependency, so run it from
any venv that has them.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SOURCE = REPO / "assets" / "brand" / "fsociety-mark.png"
FONT = REPO / "vscode-extension" / "media" / "dark-army-icons.woff"

# The glyph is the one the extension names; keep both in step with
# package.json's `contributes.icons`.
GLYPH = "darkArmyMask"
CODEPOINT = 0xE000
FAMILY = "Dark Army Icons"

UNITS_PER_EM = 1000
GLYPH_HEIGHT = 900  # how much of the em the mask fills
# How far below the baseline the mask hangs.  The status bar's line box is set
# by the surrounding UI font, not by ours, so this is the one lever that moves
# the glyph vertically: 25 units puts the mask's middle on the middle of the
# hover square, level with the codicons either side of it.
BASELINE_DROP = 25
THRESHOLD = 110  # the mark is light on near-black; anything above is ink
# The status-bar glyph. The committed file is far larger than a status item;
# the trace is this reduction, not the full stipple.
TRACE_SIDE = 64


def _rows(source: Path) -> tuple[list[list[tuple[int, int]]], int, int]:
    """Horizontal runs of ink, one list per image row, top row first."""
    from PIL import Image

    image = Image.open(source).convert("RGBA")
    if image.size != (TRACE_SIDE, TRACE_SIDE):
        image = image.resize((TRACE_SIDE, TRACE_SIDE), Image.Resampling.LANCZOS)
    width, height = image.size
    pixels = image.load()
    rows: list[list[tuple[int, int]]] = []
    for y in range(height):
        runs: list[tuple[int, int]] = []
        start = None
        for x in range(width):
            red, green, blue, alpha = pixels[x, y]
            lum = 0.2126 * red + 0.7152 * green + 0.0722 * blue
            # Clear ground is not ink. A file with no alpha still uses luminance.
            lit = alpha > 128 and lum > 40
            if lit and start is None:
                start = x
            elif not lit and start is not None:
                runs.append((start, x))
                start = None
        if start is not None:
            runs.append((start, width))
        rows.append(runs)
    return rows, width, height


def _bbox(rows: list[list[tuple[int, int]]]) -> tuple[int, int, int, int]:
    xs = [x for runs in rows for run in runs for x in run]
    ys = [y for y, runs in enumerate(rows) if runs]
    if not xs or not ys:
        raise SystemExit(f"{SOURCE} has no ink above threshold {THRESHOLD}")
    return min(xs), ys[0], max(xs), ys[-1] + 1


def _draw(pen, rows: list[list[tuple[int, int]]]) -> int:
    """Trace the runs into the pen; return the ink's left edge in font units."""
    left, top, right, bottom = _bbox(rows)
    scale = GLYPH_HEIGHT / (bottom - top)
    ink_width = (right - left) * scale
    offset_x = (UNITS_PER_EM - ink_width) / 2

    def fx(x: int) -> int:
        return round(offset_x + (x - left) * scale)

    def fy(y: int) -> int:
        # image y grows downwards, font y grows upwards
        return round(GLYPH_HEIGHT - BASELINE_DROP - (y - top) * scale)

    for y, runs in enumerate(rows):
        for x0, x1 in runs:
            a, b = fx(x0), fx(x1)
            hi, lo = fy(y), fy(y + 1)
            if a == b or hi == lo:
                continue
            # One winding for every rectangle: nonzero fill unions them.
            pen.moveTo((a, lo))
            pen.lineTo((a, hi))
            pen.lineTo((b, hi))
            pen.lineTo((b, lo))
            pen.closePath()

    return fx(left)


def build() -> bytes:
    from fontTools.fontBuilder import FontBuilder
    from fontTools.pens.ttGlyphPen import TTGlyphPen
    import io

    rows, _, _ = _rows(SOURCE)
    pen = TTGlyphPen(None)
    # The left side bearing must equal the outline's own xMin: a renderer that
    # trusts `hmtx` over the contours (Chromium's does) shifts the glyph by the
    # difference, which is what pushed the mask off the left of its square.
    left_bearing = _draw(pen, rows)

    builder = FontBuilder(UNITS_PER_EM, isTTF=True)
    order = [".notdef", GLYPH]
    builder.setupGlyphOrder(order)
    builder.setupCharacterMap({CODEPOINT: GLYPH})
    builder.setupGlyf({".notdef": TTGlyphPen(None).glyph(), GLYPH: pen.glyph()})
    builder.setupHorizontalMetrics(
        {".notdef": (UNITS_PER_EM, 0), GLYPH: (UNITS_PER_EM, left_bearing)}
    )
    builder.setupHorizontalHeader(
        ascent=GLYPH_HEIGHT - BASELINE_DROP, descent=-BASELINE_DROP
    )
    builder.setupNameTable(
        {
            "familyName": FAMILY,
            "styleName": "Regular",
            "psName": "DarkArmyIcons-Regular",
            "version": "1.0",
        }
    )
    builder.setupOS2(
        sTypoAscender=GLYPH_HEIGHT - BASELINE_DROP,
        sTypoDescender=-BASELINE_DROP,
        usWinAscent=GLYPH_HEIGHT - BASELINE_DROP,
        usWinDescent=BASELINE_DROP,
    )
    builder.setupPost()
    # FontBuilder stamps the build time into `head`; zero it so the same
    # artwork bakes the same bytes and `--check` can compare them.
    builder.font["head"].created = 0
    builder.font["head"].modified = 0
    builder.font.flavor = "woff"
    buffer = io.BytesIO()
    builder.save(buffer)
    return buffer.getvalue()


def main(argv: list[str]) -> int:
    check = "--check" in argv
    data = build()
    if check:
        if not FONT.exists() or FONT.read_bytes() != data:
            print(f"{FONT} is stale — run tools/vscode_icon_font.py")
            return 1
        print(f"{FONT} matches {SOURCE.name}")
        return 0
    FONT.parent.mkdir(parents=True, exist_ok=True)
    FONT.write_bytes(data)
    print(f"wrote {FONT} ({len(data)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
