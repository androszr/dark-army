"""Dark Army's mark is the glitch face, one PNG, on both top bars and the widgets.

The mark is identity, not a cast face: it is still, it is full colour (never
a tinted template), and it fills its square rather than taking `PixelMark`'s
old 0.82 inset. The fleet, the cards and the pipeline keep their character
faces. The picture is continuous tone, so the bars scale it smoothly.

Three copies of the picture must stay byte-identical — master, panel bundle,
phone bundle — and the phone copy must be listed in `project.pbxproj`: a PNG
on disk with no blue-folder entry is invisible at runtime, and a test that
only read the source tree would miss it. The widgets load that same phone copy.

The app icon is this same picture, centred by `tools/app_icon_bake.py` and
pinned by `test_app_icon.py`. The filename `fsociety-mark.png` is the slot
the bars already load.
"""

from pathlib import Path
import re
import struct

import pytest

ROOT = Path(__file__).resolve().parents[2]

RETIRED = ROOT / "assets" / "proposals" / "fsociety-brand-mark" / "fsociety-mark-64.png"

# `assets/proposals/` is git-ignored — design sources stay on the machine that
# drew them — so a fresh clone (CI) has none. Tests reading them skip there.
PROPOSALS = ROOT / "assets" / "proposals"
needs_proposals = pytest.mark.skipif(
    not PROPOSALS.is_dir(),
    reason="assets/proposals/ is not tracked; no design source on this checkout",
)
MASTER = ROOT / "assets" / "brand" / "fsociety-mark.png"
PANEL_COPY = ROOT / "panel" / "Sources" / "BobPanel" / "Resources" / "brand" / "fsociety-mark.png"
PHONE_COPY = ROOT / "ios" / "BobPhone" / "Resources" / "brand" / "fsociety-mark.png"

PANEL_THEME = ROOT / "panel" / "Sources" / "BobPanel" / "Theme.swift"
PHONE_THEME = ROOT / "ios" / "BobPhone" / "Theme.swift"
PANEL_BAR = ROOT / "panel" / "Sources" / "BobPanel" / "SettingsSection.swift"
PHONE_BAR = ROOT / "ios" / "BobPhone" / "BrandBar.swift"
PHONE_APP = ROOT / "ios" / "BobPhone" / "BobPhoneApp.swift"
WIDGET = ROOT / "ios" / "BobPhoneWidget" / "WidgetViews.swift"
PBXPROJ = ROOT / "ios" / "BobPhone.xcodeproj" / "project.pbxproj"
PACKAGE = ROOT / "panel" / "Package.swift"
SETUP = ROOT / "host" / "setup.py"


def _png_ihdr(path: Path) -> tuple[int, int, int]:
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", f"{path} is not a PNG"
    assert data[12:16] == b"IHDR"
    width, height, _bit, color = struct.unpack(">IIBB", data[16:26])
    return width, height, color


def _region(path: Path, name: str) -> str:
    """The source of one Swift struct, up to the next top-level declaration."""
    text = path.read_text()
    match = re.search(
        rf"struct {name}\b.*?(?=\n(?:/// |@|struct |enum |extension |private ))",
        text,
        re.S,
    )
    if match is None:
        match = re.search(rf"struct {name}\b.*", text, re.S)
    assert match, f"no {name} struct in {path.name}"
    return match.group(0)


def test_master_is_a_square_clear_mark():
    width, height, color = _png_ihdr(MASTER)
    assert width == height
    assert width >= 256, f"the mark is a continuous-tone square, not the old 64px tile; got {width}"
    assert color == 6, f"the bar mark is RGBA so its ground can be clear; got colour type {color}"


@needs_proposals
def test_the_retired_64px_crop_is_kept_and_not_the_master():
    width, height, color = _png_ihdr(RETIRED)
    assert (width, height) == (64, 64)
    assert color == 2
    assert MASTER.read_bytes() != RETIRED.read_bytes()


def test_both_bundle_copies_are_the_master():
    master = MASTER.read_bytes()
    assert PANEL_COPY.read_bytes() == master, "panel copy drifted from the master"
    assert PHONE_COPY.read_bytes() == master, "phone copy drifted from the master"


def test_phone_top_bar_wears_the_mask():
    region = _region(PHONE_BAR, "PhoneBrandBar")
    assert "BrandMark(size: 24)" in region
    assert "PixelMark" not in region
    assert "elliot" not in region
    assert "faceState" not in region


def test_connecting_screen_wears_the_mask():
    region = _region(PHONE_BAR, "ConnectingView")
    assert "BrandMark(size: 40)" in region
    assert "PixelMark" not in region
    assert "elliot" not in region


def test_lock_screen_wears_the_mask():
    region = _region(PHONE_APP, "LockedView")
    assert "BrandMark(size: 48)" in region
    assert "PixelMark" not in region
    assert "elliot" not in region


def test_panel_top_bar_wears_the_mask():
    region = _region(PANEL_BAR, "BrandBar")
    assert "BrandMark(size: 20)" in region
    assert "PixelMark" not in region
    assert "elliot" not in region
    assert "faceState" not in region


def test_the_state_the_mark_no_longer_needs_is_gone():
    """Identity does not pose, so nothing computes a pose for it."""
    assert "workingCount" not in PHONE_BAR.read_text()
    assert "workingCount" not in PHONE_APP.read_text()
    assert "faceState" not in PHONE_BAR.read_text()
    assert "faceState" not in PANEL_BAR.read_text()


def test_the_house_of_faces_still_exists():
    for path in (PANEL_THEME, PHONE_THEME):
        text = path.read_text()
        assert "struct BrandMark" in text, path
        assert "struct PixelMark" in text, path


def test_the_mark_is_never_tinted_and_never_inset():
    regions = [
        _region(PANEL_THEME, "BrandMark"),
        _region(PHONE_THEME, "BrandMark"),
        _region(WIDGET, "WidgetBrandMark"),
    ]
    for region in regions:
        assert "interpolation(.high)" in region
        assert "interpolation(.none)" not in region
        assert "template" not in region, "the mark is not a template"
        assert "isTemplate" not in region
        assert "renderingMode" not in region
        assert "size * 0.82" not in region, "the mark fills its square"


def test_the_inset_belongs_to_nobody_now():
    """The 0.82 inset was `PixelMark`'s allowance for the empty canvas around a
    pixel-art frame. `PixelMark` draws a photograph that fills its tile now
    (6 Sep 2026), so the inset is gone from both bodies — and it must not
    reappear on the mask, which fills its square by design."""
    lines = [
        line
        for path in (PANEL_THEME, PHONE_THEME)
        for line in path.read_text().splitlines()
        if "size * 0.82" in line
    ]
    assert lines == [], f"the 0.82 inset belongs to nothing any more; got {lines}"


def test_the_phone_actually_bundles_the_folder():
    text = PBXPROJ.read_text()
    assert "path = Resources/brand" in text, "no blue-folder file reference"
    assert text.count("brand in Resources") >= 2, (
        "need both the build file and its place in the Resources phase"
    )


def test_the_panel_actually_bundles_the_folder():
    assert '.copy("Resources/brand")' in PACKAGE.read_text()


def test_the_host_never_ships_this_png():
    assert "fsociety-mark" not in SETUP.read_text()


WIDGET_COPY = ROOT / "ios" / "BobPhone" / "Resources" / "brand" / "fsociety-mark-widget.png"


def test_the_widgets_load_a_small_copy_of_the_mark():
    """A Live Activity draws a large image as a grey box on the Lock Screen:
    the 768px master showed as a blank square beside the prompt line. The
    widgets load a 96px RGBA copy, shipped in the same blue folder."""
    width, height, color = _png_ihdr(WIDGET_COPY)
    assert (width, height) == (96, 96)
    assert color == 6
    assert WIDGET_COPY.stat().st_size < 40_000
    region = _region(WIDGET, "WidgetBrandMark")
    assert '"fsociety-mark-widget.png"' in region
    assert '"fsociety-mark.png"' not in region
