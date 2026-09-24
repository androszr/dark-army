"""The app icon is one master, shown on every surface that draws a face.

iPhone home / Settings / Spotlight, the Mac Finder / Dock / Cmd-Tab tile,
NSAlert, and the README all derive from `assets/app-icon-1024.png`. A
surface that kept its own copy is how the stop-session alert once showed
the old frog.
"""

import base64
from pathlib import Path
import plistlib
import re
import struct
import sys

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
import app_icon_bake  # noqa: E402
MASTER = ROOT / "assets" / "app-icon-1024.png"
IOS = ROOT / "ios" / "BobPhone" / "Assets.xcassets" / "AppIcon.appiconset" / "icon-1024.png"
ICNS_ASSETS = ROOT / "assets" / "AppIcon.icns"
ICNS_HOST = ROOT / "host" / "AppIcon.icns"
PANEL_PLIST = ROOT / "panel" / "Sources" / "BobPanel" / "Info.plist"
BUILD_SH = ROOT / "host" / "build.sh"
SETUP = ROOT / "host" / "setup.py"
README = ROOT / "README.md"
SVG = ROOT / "assets" / "app-icon.svg"
ICONSET = ROOT / "assets" / "AppIcon.iconset"
FSOCIETY_SOURCE = ROOT / "assets" / "proposals" / "fsociety-brand-mark" / "fsociety-mark-source.png"
FSOCIETY_64 = ROOT / "assets" / "proposals" / "fsociety-brand-mark" / "fsociety-mark-64.png"
TOP_BAR_MARK = ROOT / "assets" / "brand" / "fsociety-mark.png"
HOODED = ROOT / "assets" / "proposals" / "app-icon-hooded-mask" / "app-icon-1024.png"

# `assets/proposals/` is git-ignored — design sources stay on the machine that
# drew them — so a fresh clone (CI) has none. Tests reading them skip there.
PROPOSALS = ROOT / "assets" / "proposals"
needs_proposals = pytest.mark.skipif(
    not PROPOSALS.is_dir(),
    reason="assets/proposals/ is not tracked; no design source on this checkout",
)


def _png_ihdr(path: Path) -> tuple[int, int, int]:
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", f"{path} is not a PNG"
    assert data[12:16] == b"IHDR"
    width, height, _bit, color = struct.unpack(">IIBB", data[16:26])
    return width, height, color


def test_master_and_ios_are_opaque_1024():
    """App Store Connect rejects a 1024 with an alpha channel."""
    for path in (MASTER, IOS):
        width, height, color = _png_ihdr(path)
        assert (width, height) == (1024, 1024), path
        assert color == 2, f"{path} must be RGB (no alpha); got color type {color}"


def test_ios_icon_is_the_master():
    assert IOS.read_bytes() == MASTER.read_bytes()


def test_mac_icns_is_copied_to_the_bundle_source():
    """py2app reads host/AppIcon.icns; assets/ is the bake output."""
    assert ICNS_ASSETS.read_bytes()[:4] == b"icns"
    assert ICNS_HOST.read_bytes() == ICNS_ASSETS.read_bytes()


def test_setup_py_names_the_icns():
    assert '"iconfile": "AppIcon.icns"' in SETUP.read_text()


def test_panel_dock_tile_uses_the_same_icns():
    """The menu bar is LSUIElement. The panel is what appears in the Dock."""
    info = plistlib.loads(PANEL_PLIST.read_bytes())
    assert info["CFBundleIconFile"] == "AppIcon"
    script = BUILD_SH.read_text()
    assert "AppIcon.icns" in script
    assert "BobPanel.app" in script
    assert 'PANEL_APP/Contents/Resources/AppIcon.icns' in script or \
        '"$PANEL_APP/Contents/Resources/AppIcon.icns"' in script


def test_readme_points_at_the_svg():
    assert "assets/app-icon.svg" in README.read_text()
    svg = (ROOT / "assets" / "app-icon.svg").read_text()
    assert "data:image/png;base64," in svg


def _pixels(path: Path) -> tuple[str, tuple[int, int], bytes]:
    with Image.open(path) as image:
        return image.mode, image.size, image.tobytes()


def test_committed_pngs_match_the_baker():
    """Every committed PNG is what tools/app_icon_bake.py produces today."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        app_icon_bake.bake(out, skip_icns=True)

        names = [app_icon_bake.MASTER, app_icon_bake.IOS] + [
            app_icon_bake.ICONSET / name for name, _ in app_icon_bake.ICONSET_SIZES
        ]
        for name in names:
            committed = ROOT / name
            assert committed.exists(), committed
            # Decoded pixels, not raw bytes: a Pillow encoder bump must not
            # fail the suite while the picture is unchanged.
            assert _pixels(committed) == _pixels(out / name), name


def test_svg_embeds_the_committed_master():
    payload = re.search(r"data:image/png;base64,([A-Za-z0-9+/=]+)", SVG.read_text())
    assert payload, "no base64 PNG payload in the SVG"
    assert base64.b64decode(payload.group(1)) == MASTER.read_bytes()


def test_icns_carries_the_master():
    """The 1024 rung inside the icns is the committed master."""
    import io

    data = ICNS_ASSETS.read_bytes()
    offset = 8
    payload = None
    while offset + 8 <= len(data):
        kind = data[offset:offset + 4]
        (length,) = struct.unpack(">I", data[offset + 4:offset + 8])
        assert length >= 8, "malformed icns chunk"
        if kind == b"ic10":
            payload = data[offset + 8:offset + length]
            break
        offset += length
    assert payload is not None, "no ic10 (1024) rung in the icns"
    assert payload[:8] == b"\x89PNG\r\n\x1a\n"

    with Image.open(io.BytesIO(payload)) as inside, Image.open(MASTER) as master:
        assert inside.size == master.size
        assert inside.convert("RGB").tobytes() == master.convert("RGB").tobytes()


def _centre_crop(master: Image.Image, source: Image.Image) -> bytes:
    width, height = source.size
    left = (master.width - width) // 2
    top = (master.height - height) // 2
    return master.crop((left, top, left + width, top + height)).convert("RGB").tobytes()


def test_the_icon_centres_the_bar_mark():
    """The Dock tile is the bar's mask, duotone on the dark ground, corners painted."""
    with Image.open(MASTER) as master, Image.open(TOP_BAR_MARK) as mark:
        plate = app_icon_bake.icon_plate(mark)
        assert _centre_crop(master, plate) == plate.tobytes()
        assert master.getpixel((0, 0)) == app_icon_bake.CORNER_INK


@needs_proposals
def test_the_icon_is_not_the_retired_mask():
    """The old fsociety crops stay in proposals and are not what the tile shows."""
    with Image.open(MASTER) as master:
        rgb = master.convert("RGB")
        with Image.open(FSOCIETY_SOURCE) as retired:
            assert _centre_crop(rgb, retired) != retired.convert("RGB").tobytes()
        with Image.open(FSOCIETY_64) as old:
            assert _centre_crop(rgb, old) != old.convert("RGB").tobytes()


@needs_proposals
def test_hooded_artwork_preserved():
    """The artwork the mask replaced is kept, not thrown away."""
    width, height, color = _png_ihdr(HOODED)
    assert (width, height) == (1024, 1024)
    assert color == 2


@needs_proposals
def test_fsociety_artwork_preserved():
    """The retired mask artwork stays in proposals.

    `assets/proposals/app-icon-hooded-mask/` is the precedent: retired icon
    artwork stays on the machine that drew it. The bars wear the glitch mark
    now; these two files are the old mask, kept.
    """
    width, height, color = _png_ihdr(FSOCIETY_SOURCE)
    assert (width, height) == (757, 757)
    assert color == 2
    width, height, color = _png_ihdr(FSOCIETY_64)
    assert (width, height) == (64, 64)
    assert color == 2
