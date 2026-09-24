"""White-sticker strip: eat a ring, leave clothing and interior whites."""
import sys
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
from pixelgrid_ingest import (  # noqa: E402
    RING_FRACTION, STICKER_WHITE, WHITE_MIN, _silhouette_white_fraction,
    despeckle, strip_outline,
)


def _rgba(w, h, pixels):
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    px = img.load()
    for (x, y), colour in pixels.items():
        px[x, y] = colour
    return img


def _opaque(img):
    px = img.load()
    return {(x, y): px[x, y]
            for y in range(img.height) for x in range(img.width)
            if px[x, y][3]}


def test_strip_eats_a_sticker_ring_and_keeps_the_interior():
    # 7x7: a 1-cell white ring around a brown body, plus a white tooth inside.
    # The ring is the whole silhouette, so the gate opens.
    brown = (80, 50, 30, 255)
    white = (255, 255, 255, 255)
    pixels = {}
    for x in range(1, 6):
        for y in range(1, 6):
            pixels[x, y] = white
    for x in range(2, 5):
        for y in range(2, 5):
            pixels[x, y] = brown
    pixels[3, 3] = white  # tooth
    out = strip_outline(_rgba(7, 7, pixels))
    got = _opaque(out)
    assert (1, 1) not in got
    assert (5, 5) not in got
    assert (3, 2) in got and got[3, 2][:3] == brown[:3]
    assert (3, 3) in got and got[3, 3][:3] == (255, 255, 255)


def test_strip_eats_a_ring_thicker_than_two_cells():
    # The old two-pass peel left Mira's 2–3 cell ring. A 3-cell ring around a
    # 1-cell body must go in one flood.
    brown = (80, 50, 30, 255)
    white = (255, 255, 255, 255)
    pixels = {}
    for x in range(1, 8):
        for y in range(1, 8):
            pixels[x, y] = white
    pixels[4, 4] = brown
    out = strip_outline(_rgba(9, 9, pixels))
    got = _opaque(out)
    assert got == {(4, 4): brown}


def test_strip_leaves_a_white_jacket_when_there_is_no_ring():
    # Proxy: off-white clothing on the silhouette, no die-cut ring.
    jacket = (236, 237, 241, 255)
    skin = (153, 102, 71, 255)
    pixels = {}
    for x in range(1, 6):
        for y in range(1, 6):
            pixels[x, y] = jacket
    for x in range(2, 5):
        for y in range(2, 4):
            pixels[x, y] = skin
    src = _rgba(7, 7, pixels)
    assert _silhouette_white_fraction(src, STICKER_WHITE) < RING_FRACTION
    assert _opaque(strip_outline(src)) == _opaque(src)


def test_despeckle_after_strip_drops_a_floating_crumb():
    brown = (80, 50, 30, 255)
    white = (255, 255, 255, 255)
    pixels = {(x, y): white for x in range(1, 6) for y in range(1, 6)}
    for x in range(2, 5):
        for y in range(2, 5):
            pixels[x, y] = brown
    pixels[0, 0] = white  # crumb, attached to nothing
    out = despeckle(strip_outline(_rgba(7, 7, pixels)))
    got = _opaque(out)
    assert (0, 0) not in got
    assert (2, 2) in got


def test_gate_constants_keep_jacket_below_sticker_white():
    # The gate is the only thing protecting off-white clothing. If these drift
    # together the jacket test still fails, but the numbers themselves are the
    # contract with the measured art.
    assert STICKER_WHITE > WHITE_MIN
    assert STICKER_WHITE > 236
    assert 0.0 < RING_FRACTION < 0.63
