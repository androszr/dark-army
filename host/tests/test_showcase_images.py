"""The README's four showcase slides: the right four, small, clean of hidden
metadata, embedded with real descriptions, and carrying neither the author's
name nor the flag the slide deck's footer adds.

`tools/showcase_ingest.py` renders them from the git-ignored deck with the
footer line switched off; the contract is `docs/images/SHOTS.md`, *The
showcase slides*. Every check here reads the committed pictures, so none
needs Chrome or the deck. The name check reads the text in each picture with
the Mac's own text recognition (`tools/ocr_text.swift` through `xcrun swift`)
and skips where there is no Xcode toolchain; the flag is not text, so a pixel
check on the footer band catches it instead.
"""
from __future__ import annotations

import importlib.util
import re
import shutil
import struct
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SHOWCASE = REPO / "docs" / "images" / "showcase"
README = REPO / "README.md"
TOOL = REPO / "tools" / "showcase_ingest.py"
OCR = REPO / "tools" / "ocr_text.swift"

EXPECTED = {"hero-mission-control.png", "five-terminals-one-of-you.png",
            "write-it-once.png", "walk-away-from-the-desk.png"}
ALLOWED_CHUNKS = {b"IHDR", b"PLTE", b"tRNS", b"IDAT", b"IEND", b"sRGB"}
WIDTH = 1200
MAX_BYTES = 300 * 1024


def _tool():
    spec = importlib.util.spec_from_file_location("showcase_ingest_under_test", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _chunks(path: Path) -> list[bytes]:
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", path.name
    kinds, at = [], 8
    while at < len(data):
        length, kind = struct.unpack(">I4s", data[at:at + 8])
        kinds.append(kind)
        at += 12 + length
    return kinds


def _size(path: Path) -> tuple[int, int]:
    data = path.read_bytes()
    return struct.unpack(">II", data[16:24])


def _pictures() -> list[Path]:
    return sorted(SHOWCASE / name for name in EXPECTED)


def test_the_folder_holds_exactly_the_four_slides():
    assert {p.name for p in SHOWCASE.glob("*.png")} == EXPECTED


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_every_slide_is_small_even_and_clean(name):
    path = SHOWCASE / name
    width, height = _size(path)
    assert width == WIDTH, f"{name} is {width} px wide"
    assert width % 2 == 0 and height % 2 == 0, f"{name} is {width}x{height}"
    assert path.stat().st_size <= MAX_BYTES, f"{name} is {path.stat().st_size} bytes"
    assert set(_chunks(path)) <= ALLOWED_CHUNKS, name


def test_the_readme_embeds_each_slide_with_a_real_description():
    text = README.read_text(encoding="utf-8")
    embeds = re.findall(
        r'<img src="docs/images/showcase/([^"]+)" alt="([^"]*)" width="(\d+)">', text)
    assert {name for name, _, _ in embeds} == EXPECTED
    assert len(embeds) == len(EXPECTED), "a slide is embedded twice"
    for name, alt, width in embeds:
        assert 20 <= len(alt) <= 200, name
        assert alt.endswith("."), name
        assert int(width) <= 880, name
    assert "user-data" not in text, "the README points at the private slide deck"


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_no_flag_in_the_footer(name):
    assert _tool().flag_pixels(SHOWCASE / name) == 0


def test_the_flag_check_sees_a_shrunk_flag(tmp_path):
    """The known positive: without it a blind check passes on nothing. The
    swatch is the reddest pixel the hero slide's flag keeps after the tool's
    own shrink and palette."""
    from PIL import Image

    img = Image.new("RGB", (WIDTH, WIDTH), (8, 12, 8))
    for x in range(200, 210):
        for y in range(WIDTH - 40, WIDTH - 30):
            img.putpixel((x, y), (143, 36, 29))
    path = tmp_path / "swatch.png"
    img.save(path)
    assert _tool().flag_pixels(path) > 0


def test_the_flag_check_sees_the_flag_on_the_baked_original(tmp_path):
    original = REPO / "user-data" / "reddit-showcase" / "slides" / "png" / "slide-02-hero.png"
    if not original.is_file():
        pytest.skip("the git-ignored slide deck is not on this machine")
    tool = _tool()
    baked = tmp_path / "hero-with-footer.png"
    tool._bake(original, baked)
    assert tool.flag_pixels(baked) > 0


def test_the_tool_switches_the_footer_line_off():
    text = TOOL.read_text(encoding="utf-8")
    assert ".foot .brand::after" in text
    assert "content: none" in text


def test_no_name_in_the_pictures():
    if shutil.which("xcrun") is None:
        pytest.skip("no Xcode toolchain on this machine")
    try:
        probe = subprocess.run(["xcrun", "--find", "swift"], capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        pytest.skip("no Xcode toolchain on this machine")
    if probe.returncode != 0:
        pytest.skip("no Xcode toolchain on this machine")

    paths = [str(p) for p in _pictures()]
    result = subprocess.run(["xcrun", "swift", str(OCR), *paths],
                            capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, result.stdout + result.stderr
    lines = [line for line in result.stdout.splitlines() if "\t" in line]
    assert len(lines) == len(paths), result.stdout
    for line in lines:
        path, recognised = line.split("\t", 1)
        # The positive control: the footer's own word, at the footer's own
        # size, was read — without it a blind reader would pass on nothing.
        assert "ARMY" in recognised, f"{path}: the footer's DARK ARMY was not read"
        for forbidden in ("Androsz", "Rob "):
            assert forbidden not in recognised, f"{path} shows {forbidden!r}"
        # A copyright byline, not a bare `©`: some OCR builds read the usage
        # meter's provider glyph ("© 34%") as one.
        byline = re.search(r"©\s*(?:\d{4}|[A-Za-z])", recognised)
        assert byline is None, f"{path} shows {byline.group(0)!r}"


def test_the_tools_own_check_passes():
    result = subprocess.run([sys.executable, str(TOOL), "--check"],
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    lines = result.stdout.splitlines()
    assert len(lines) == len(EXPECTED)
    assert all(line.endswith("flag pixels 0") for line in lines), result.stdout
