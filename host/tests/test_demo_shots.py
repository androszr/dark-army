"""The front page's pictures: made-up work only, no hidden metadata, one
command to retake them, and nothing that can reach the running app.

`tools/demo_shots.py` draws them from `panel/Tests/Fixtures/demo-shots.json`;
the contract is `docs/images/SHOTS.md`. These are the guards that need no
screen: the demo day's privacy and capability check (shared with the tool's
own `check`), the picture files, the manifest, the tool's hygiene and one
real strip render under a throwaway `HOME`.
"""
from __future__ import annotations

import argparse
import copy
import getpass
import importlib.util
import json
import os
import re
import shutil
import struct
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
TOOL = REPO / "tools" / "demo_shots.py"
FIXTURE = REPO / "panel" / "Tests" / "Fixtures" / "demo-shots.json"
IMAGES = REPO / "docs" / "images"
PBXPROJ = REPO / "ios" / "BobPhone.xcodeproj" / "project.pbxproj"
OCR = REPO / "tools" / "ocr_text.swift"

EXPECTED_SHOTS = {"menubar-strip.png", "agent-question.png", "board.png", "first-run.png"}
ALLOWED_CHUNKS = {b"IHDR", b"PLTE", b"tRNS", b"IDAT", b"IEND", b"sRGB"}


def _tool():
    spec = importlib.util.spec_from_file_location("demo_shots_under_test", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def tool():
    return _tool()


@pytest.fixture()
def day():
    return json.loads(FIXTURE.read_text())


# --- the demo day --------------------------------------------------------------

def test_the_demo_day_passes_the_guard(tool, day):
    assert tool.fixture_findings(day, tool.run_time_denylist()) == []


def test_a_real_home_folder_is_a_finding(tool, day):
    home = "/" + "Users" + "/somebody-else/Code/pocket-weather"
    day["frames"]["main"]["agents"]["running"][0]["name"] = f"Editing {home}"
    assert any("home folder" in f for f in tool.fixture_findings(day, []))


def test_this_machines_account_name_is_a_finding(tool, day):
    me = getpass.getuser()
    if len(me) < tool.MIN_DENY_CHARS:
        pytest.skip("this account name is too short to be on the denylist")
    day["frames"]["main"]["board"]["cards"][0]["summary"] = f"Ask {me} about it"
    findings = tool.fixture_findings(day, tool.run_time_denylist())
    assert any("private string" in f for f in findings), findings


def test_a_short_name_matches_whole_words_only(tool, day):
    """A private word is a whole word in a value: "tom" flags "ask tom", never
    "tomorrow" (in the demo day's own titles), and no JSON key is judged."""
    assert tool.fixture_findings(day, ["tom"]) == []
    assert any("tomorrow" in t.lower() for _, t in tool._strings(day, keys=False))
    day["frames"]["main"]["board"]["cards"][0]["summary"] = "Ask Tom about it"
    assert any("private string" in f for f in tool.fixture_findings(day, ["tom"]))
    day["frames"]["main"]["board"]["cards"][0]["summary"] = "Show it"
    day["frames"]["main"]["board"]["cards"][0]["tom"] = "x"
    assert not any("private string" in f for f in tool.fixture_findings(day, ["tom"]))


def test_short_machine_strings_never_reach_the_denylist(tool):
    assert all(len(word) >= tool.MIN_DENY_CHARS for word in tool.run_time_denylist())


def test_the_pid_watch_names_only_long_lived_processes(tool):
    """Per-session helpers come and go with Claude sessions; watching them
    would fail `all` with nothing wrong."""
    watched = set(tool.COMPONENTS)
    assert {"Dark Army.app", "BobPanel.app", "pty_broker"} <= watched
    for helper in ("dark-army-notify", "dark-army-channel", "dark-army-ide"):
        assert not any(helper in name or name in helper for name in watched), helper
    channel = "/usr/bin/python3 /Users/x/.dark-army/dark-army-channel"
    assert not any(name in channel for name in watched)


def test_a_project_outside_the_story_is_a_finding(tool, day):
    day["frames"]["main"]["agents"]["running"][0]["project"] = "payroll"
    assert any("not a demo project" in f for f in tool.fixture_findings(day, []))


def test_a_row_that_could_open_a_terminal_is_a_finding(tool, day):
    day["frames"]["main"]["agents"]["sleeping"][0]["own_terminal"] = True
    assert any("own_terminal" in f for f in tool.fixture_findings(day, []))


def test_a_nickname_off_the_roster_is_a_finding(tool, day):
    day["frames"]["main"]["agents"]["running"][0]["nickname"] = "Elliot"
    assert any("identity.NAMES" in f for f in tool.fixture_findings(day, []))


def test_every_nickname_is_on_the_roster(tool, day):
    names = set(tool.roster_names())
    for frame in day["frames"].values():
        for bucket in frame["agents"].values():
            for row in bucket:
                assert row["nickname"] in names


def test_the_strip_counts_are_the_frames_own_buckets(tool, day):
    """The live strip reads `_activity_counts()`; the demo strip reads these,
    so they must be the frame's buckets and its Prep + Backlog count."""
    assert tool.strip_findings(day) == []
    agents = day["frames"]["main"]["agents"]
    cards = day["frames"]["main"]["board"]["cards"]
    strip = day["strip"]
    assert strip["working"] == len(agents["running"])
    assert strip["attention"] == len(agents["waiting"])
    assert strip["idle"] == len(agents["sleeping"])
    assert strip["todo"] == sum(c["column_name"] in ("prep", "backlog") for c in cards)
    bad = copy.deepcopy(day)
    bad["strip"]["todo"] = 4
    assert tool.strip_findings(bad)


# --- the pictures ----------------------------------------------------------------

def _chunks(path: Path) -> list[bytes]:
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", path.name
    kinds, at = [], 8
    while at < len(data):
        length, kind = struct.unpack(">I4s", data[at:at + 8])
        kinds.append(kind)
        at += 12 + length
    return kinds


def _pictures() -> list[Path]:
    pictures = sorted(IMAGES.glob("*.png"))
    if not pictures:
        pytest.skip("docs/images holds no pictures yet (tools/demo_shots.py all)")
    return pictures


def test_every_picture_carries_only_the_chunks_a_picture_needs():
    for path in _pictures():
        extra = set(_chunks(path)) - ALLOWED_CHUNKS
        assert not extra, (path.name, extra)


def test_every_picture_is_small_and_evenly_sized():
    for path in _pictures():
        assert path.stat().st_size <= 800 * 1024, path.name
        width, height = struct.unpack(">II", path.read_bytes()[16:24])
        assert width % 2 == 0 and height % 2 == 0, (path.name, width, height)


def test_the_manifest_names_exactly_the_four_pictures(tool):
    manifest_path = IMAGES / "shots.json"
    if not manifest_path.is_file():
        pytest.skip("docs/images/shots.json not written yet")
    manifest = json.loads(manifest_path.read_text())
    assert set(manifest) == EXPECTED_SHOTS
    assert {p.name for p in IMAGES.glob("*.png")} == EXPECTED_SHOTS
    for name, entry in manifest.items():
        alt = entry["alt"]
        assert 20 <= len(alt) <= 200 and alt.endswith("."), name
        assert entry["scale"] == 2, name
        assert entry["display_width"] <= 880, name
        assert entry["width_px"] % 2 == 0 and entry["height_px"] % 2 == 0, name
    assert manifest["menubar-strip.png"]["rung"] == 0
    assert manifest["agent-question.png"]["window_pt"] == list(tool.WINDOW_PT)
    assert "rung" not in manifest["agent-question.png"]


def test_compose_writes_exactly_the_four_pictures(tool, tmp_path):
    """The assembly step on stand-in layers: four files, no hero, no phone."""
    Image = pytest.importorskip("PIL.Image")
    layers = tmp_path / "layers"
    layers.mkdir()
    for name, size in (("strip.png", (400, 44)), ("window-agents-cipher.png", (300, 200)),
                       ("window-board.png", (1200, 300)), ("card-window.png", (120, 160)),
                       ("rail-first-run.png", (200, 800))):
        Image.new("RGBA", size, (40, 40, 40, 255)).save(layers / name)
    (layers / "strip.json").write_text(json.dumps({"rung": 0, "width_pt": 200, "budget_pt": 300}))
    out = tmp_path / "out"
    tool.cmd_compose(argparse.Namespace(work=str(tmp_path), out=str(out)))
    assert {p.name for p in out.glob("*.png")} == EXPECTED_SHOTS
    manifest = json.loads((out / "shots.json").read_text())
    assert set(manifest) == EXPECTED_SHOTS
    assert manifest["menubar-strip.png"]["rung"] == 0
    assert "rung" not in manifest["agent-question.png"]
    assert manifest["board.png"]["window_pt"] == list(tool.BOARD_WINDOW_PT)


def _reads(recognised: str, phrase: str) -> bool:
    """Whether the recognised text holds `phrase`, however Vision split it.

    `tools/ocr_text.swift` joins its separate strings with " | ", and Vision
    splits a footer as it pleases — the old board picture read as
    "Name | it first". Collapse the separators and the whitespace on both
    sides, and ignore case, before matching."""
    def flat(text: str) -> str:
        return " ".join(text.replace("|", " ").split()).casefold()
    return flat(phrase) in flat(recognised)


def test_the_reader_catches_a_phrase_vision_split():
    """The negative control, with no picture and no Xcode: the shape Vision
    read off the old board picture is caught, a clean footer is not."""
    old = "Widget for tomorrow's forecast | Tests/TomorrowWidgetTests.swift | Name | it first | Close | Save"
    clean = "Widget for tomorrow's forecast | Tests/TomorrowWidgetTests.swift | Close | Save"
    assert _reads(old, "Name it first")
    assert _reads("Name it first | Close", "Name it first")
    assert not _reads(clean, "Name it first")
    assert _reads(clean, "Widget for tomorrow")


def test_the_card_window_picture_holds_nothing_back():
    """The board picture's card window is a saved, titled card: the Mac's own
    text recognition reads its title and never "Name it first" beside Save."""
    if shutil.which("xcrun") is None:
        pytest.skip("no Xcode toolchain on this machine")
    try:
        probe = subprocess.run(["xcrun", "--find", "swift"], capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        pytest.skip("no Xcode toolchain on this machine")
    if probe.returncode != 0:
        pytest.skip("no Xcode toolchain on this machine")
    board = IMAGES / "board.png"
    if not board.is_file():
        pytest.skip("docs/images holds no board picture yet (tools/demo_shots.py all)")

    result = subprocess.run(["xcrun", "swift", str(OCR), str(board)],
                            capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, result.stdout + result.stderr
    lines = [line for line in result.stdout.splitlines() if "\t" in line]
    assert len(lines) == 1, result.stdout
    _, recognised = lines[0].split("\t", 1)
    # The positive control: the card window's own title was read — without it
    # a blind reader would pass on nothing.
    assert _reads(recognised, "Widget for tomorrow"), recognised
    assert not _reads(recognised, "Name it first"), recognised


# --- the tool --------------------------------------------------------------------

def test_the_tool_cannot_reach_the_running_app():
    text = TOOL.read_text()
    forbidden = [
        r"/Users/", r"1987[345]", r"\bkill\b", r"\bpkill\b", r"\bkillall\b",
        r"\blaunchctl\b", r"open -a", r"BobPanel\"", r"\.build/(release|debug)/BobPanel",
    ]
    for pattern in forbidden:
        assert not re.search(pattern, text), pattern


def test_the_phone_decode_test_is_in_the_test_target():
    project = PBXPROJ.read_text()
    ref = re.search(r"(\w+) /\* DemoShotsTests\.swift \*/ = \{isa = PBXFileReference;"
                    r"[^}]*path = BobPhoneTests/DemoShotsTests\.swift;", project)
    assert ref, "no file reference for BobPhoneTests/DemoShotsTests.swift"
    build = re.search(r"(\w+) /\* DemoShotsTests\.swift in Sources \*/ = "
                      r"\{isa = PBXBuildFile; fileRef = " + ref.group(1), project)
    assert build, "no build file for DemoShotsTests.swift"
    sources = re.search(r"isa = PBXSourcesBuildPhase;[^}]*" + build.group(1), project)
    assert sources, "DemoShotsTests.swift is not in a Sources phase"


def test_the_strip_draws_a_real_picture_at_the_top_rung(tmp_path):
    """The real ladder walk, in its own process under a throwaway HOME."""
    pytest.importorskip("AppKit")
    # Without pytest's marker, so the tool's own HOME redirect is what keeps
    # the state folder inside the throwaway home — the branch `all` runs.
    env = {k: v for k, v in os.environ.items() if k != "PYTEST_CURRENT_TEST"}
    done = subprocess.run([sys.executable, str(TOOL), "strip", "--work", str(tmp_path)],
                          capture_output=True, text=True, timeout=120, env=env)
    assert done.returncode == 0, done.stdout + done.stderr
    strip = tmp_path / "layers" / "strip.png"
    assert strip.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    reading = json.loads((tmp_path / "layers" / "strip.json").read_text())
    assert reading["rung"] == 0
    assert reading["width_pt"] <= reading["budget_pt"]
    width, height = struct.unpack(">II", strip.read_bytes()[16:24])
    assert height == 44 and width >= 2 * 100
