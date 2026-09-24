#!/usr/bin/env python3
"""Bake every app-icon file from one master: the glitch mark.

The bars wear `assets/brand/fsociety-mark.png` with a clear ground, so only
the mask shows. The Dock, the app switcher, Finder, the phone's home screen
and `assets/favicon.ico` show that same mask painted in Dark Army green on
the bar's own ground (`FIELD`, the panel's background). This script does not
rewrite the mark. It builds an opaque plate from it, centres that plate on
the squircle and writes every committed icon file out:

    assets/app-icon-1024.png                  the 1024 master (opaque RGB)
    assets/AppIcon.iconset/icon_*.png         ten decimations of it
    assets/AppIcon.icns                       iconutil's bake of the iconset
    host/AppIcon.icns                         a byte copy of that
    ios/.../AppIcon.appiconset/icon-1024.png  a byte copy of the master
    assets/app-icon.svg                       the master, base64 in a wrapper
    assets/favicon.ico                        16, 32 and 48 of the same plate

Everything stays mode RGB end to end: App Store Connect rejects a 1024 with
an alpha channel, and py2app, iconutil and Xcode each handle alpha their own
way. The rounded corners are therefore *painted* — flat CORNER_INK outside a
rounded rect filled with the source's own ground colour — not cut out.

Decimation is LANCZOS because the filter follows the source's category. A
grid-aligned decimation is right for authored pixel art, where blending two
pixels invents a colour nobody chose; it is wrong for a continuous-tone
picture, where every pixel is a sample of something smooth and throwing
three of every four away with no low-pass filter is undersampling. This
master is a stippled illustration and carries fine glitch striation — exactly
the content that aliases into speckle. The pixel-art trees keep their own filter
(`tools/pixelgrid_ingest.py`, `tools/menubar_cast_icons.py`).

SOURCE is the committed mark, not the retired SVG raster. Pillow only.
Rerun after replacing the mark:

    python tools/app_icon_bake.py
    python tools/app_icon_bake.py --out /tmp/bake --skip-icns
"""

from __future__ import annotations

import argparse
import base64
import shutil
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]

SOURCE = Path("assets/brand/fsociety-mark.png")
MASTER = Path("assets/app-icon-1024.png")
ICONSET = Path("assets/AppIcon.iconset")
ICNS_ASSETS = Path("assets/AppIcon.icns")
ICNS_HOST = Path("host/AppIcon.icns")
IOS = Path("ios/BobPhone/Assets.xcassets/AppIcon.appiconset/icon-1024.png")
SVG = Path("assets/app-icon.svg")

CANVAS = 1024
# Kept from the icon family this replaces: the flat ink painted outside the
# rounded-rect boundary.
CORNER_INK = (5, 8, 5)
# 0.2237 x 1024, the macOS squircle approximation, consistent with the
# measured corner boundary of the previous icon.
CORNER_RADIUS = 229
# The panel's background. Clear pixels of the mask are painted back in this,
# which is the same ink as the corners, so the tile is one ground.
FIELD = CORNER_INK
HAIR = (31, 90, 31)
FAVICON = Path("assets/favicon.ico")
# The largest centred square whose own corners stay inside the squircle:
# c = (CANVAS - S) / 2 and sqrt(2) * (CORNER_RADIUS - c) <= CORNER_RADIUS.
MAX_SOURCE = 888

# The ten entries `iconutil` expects, name -> pixel size.
ICONSET_SIZES = (
    ("icon_16x16.png", 16),
    ("icon_16x16@2x.png", 32),
    ("icon_32x32.png", 32),
    ("icon_32x32@2x.png", 64),
    ("icon_128x128.png", 128),
    ("icon_128x128@2x.png", 256),
    ("icon_256x256.png", 256),
    ("icon_256x256@2x.png", 512),
    ("icon_512x512.png", 512),
    ("icon_512x512@2x.png", 1024),
)

SVG_TEMPLATE = (
    '<svg xmlns="http://www.w3.org/2000/svg" '
    'xmlns:xlink="http://www.w3.org/1999/xlink" '
    'viewBox="0 0 1024 1024" width="1024" height="1024">\n'
    "  <title>Dark Army App Icon</title>\n"
    '  <image width="1024" height="1024" '
    'xlink:href="data:image/png;base64,{payload}"/>\n'
    "</svg>\n"
)


class SourceRefused(Exception):
    """SOURCE is missing, or is not a picture this baker can centre."""


def icon_plate(mark: Image.Image) -> Image.Image:
    """The mask, duotone, on `FIELD`. Clear pixels stay the field.

    Light stays a pale phosphor, the mid tones take the hair green, and the
    dark half of the mask stays a near-black green so the split still reads.
    """
    mark = mark.convert("RGBA")
    plate = Image.new("RGB", mark.size, FIELD)
    src = mark.load()
    dst = plate.load()
    width, height = mark.size
    for y in range(height):
        for x in range(width):
            red, green, blue, alpha = src[x, y]
            if alpha < 8:
                continue
            lum = (0.2126 * red + 0.7152 * green + 0.0722 * blue) / 255.0
            if lum < 0.35:
                col = (8, 18, 8)
            elif lum < 0.55:
                col = HAIR
            else:
                lift = int(180 + 40 * lum)
                col = (min(255, lift), 255, min(255, lift))
            if alpha < 255:
                scale = alpha / 255.0
                col = tuple(int(FIELD[i] + (col[i] - FIELD[i]) * scale) for i in range(3))
            dst[x, y] = col
    return plate


def compose_master(root: Path = ROOT) -> Image.Image:
    """The 1024 icon: the mark's plate at 1x, centred, corners painted."""
    path = root / SOURCE
    if not path.exists():
        raise SourceRefused(
            f"no icon source at {path}: the mark file is missing. "
            "Nothing was baked and the committed icon is untouched."
        )
    try:
        source = icon_plate(Image.open(path))
    except (OSError, ValueError) as exc:
        raise SourceRefused(
            f"{path} will not open as a picture: {exc}"
        ) from exc

    width, height = source.size
    if width != height:
        raise SourceRefused(
            f"{path} is {width}x{height}: the source is pasted centred at 1x "
            "with no scaling, so a non-square one would sit off the squircle's "
            "axis of symmetry"
        )
    if width > MAX_SOURCE:
        raise SourceRefused(
            f"{path} is {width}px, above the {MAX_SOURCE}px bound: a centred "
            f"square of side S has its corner at c = ({CANVAS} - S) / 2, which "
            f"stays inside the {CORNER_RADIUS}px squircle only while "
            f"sqrt(2) * ({CORNER_RADIUS} - c) <= {CORNER_RADIUS}. Above that "
            "the artwork's own corners are painted over by CORNER_INK."
        )
    ground = source.getpixel((0, 0))

    canvas = Image.new("RGB", (CANVAS, CANVAS), CORNER_INK)
    ImageDraw.Draw(canvas).rounded_rectangle(
        (0, 0, CANVAS - 1, CANVAS - 1), radius=CORNER_RADIUS, fill=ground
    )
    canvas.paste(source, ((CANVAS - width) // 2, (CANVAS - height) // 2))
    return canvas


def bake(root: Path = ROOT, skip_icns: bool = False) -> None:
    """Write every icon file under `root`. `root` may be a mirror tree."""
    master = compose_master(ROOT)

    for path in (MASTER, IOS, SVG, ICNS_ASSETS, ICNS_HOST, FAVICON):
        (root / path).parent.mkdir(parents=True, exist_ok=True)
    (root / ICONSET).mkdir(parents=True, exist_ok=True)

    master.save(root / MASTER)
    master_bytes = (root / MASTER).read_bytes()

    for name, size in ICONSET_SIZES:
        rung = master if size == CANVAS else master.resize((size, size), Image.LANCZOS)
        rung.save(root / ICONSET / name)

    (root / IOS).write_bytes(master_bytes)
    payload = base64.b64encode(master_bytes).decode("ascii")
    (root / SVG).write_text(SVG_TEMPLATE.format(payload=payload))
    # The browser favicon is the same plate, not a second drawing.
    plate = icon_plate(Image.open(ROOT / SOURCE))
    plate.save(
        root / FAVICON,
        format="ICO",
        sizes=[(16, 16), (32, 32), (48, 48)],
    )

    if skip_icns:
        return

    subprocess.run(
        ["iconutil", "-c", "icns", str(root / ICONSET), "-o", str(root / ICNS_ASSETS)],
        check=True,
    )
    shutil.copyfile(root / ICNS_ASSETS, root / ICNS_HOST)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--out",
        type=Path,
        default=ROOT,
        help="bake into this tree instead of the repository",
    )
    parser.add_argument(
        "--skip-icns",
        action="store_true",
        help="skip the iconutil bake and the host/ copy",
    )
    args = parser.parse_args()
    try:
        bake(args.out, skip_icns=args.skip_icns)
    except SourceRefused as exc:
        raise SystemExit(f"refused: {exc}") from exc
    print(f"baked the app icon into {args.out}")


if __name__ == "__main__":
    main()
