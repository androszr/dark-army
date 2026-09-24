"""The front page's pictures, drawn by Dark Army's own screens from one
made-up working day, and never from anybody's real work.

    host/.venv/bin/python tools/demo_shots.py all       # everything, then compose
    host/.venv/bin/python tools/demo_shots.py check     # the privacy guard alone

Subcommands, each usable alone: `check` (the demo day passes the privacy and
capability guard), `strip` (the menu-bar strip, drawn by the real ladder walk
in this short-lived process), `panel` (the Mac window, the waiting agent, the
card window and the checklist, drawn by `swift test` in the panel's own test
harness), `compose` (frames, shadows, metadata stripped, `shots.json`
written). `all` runs them in that order and
proves the running app was left alone: every Dark Army process that was up
before is up after, same pid.

What this never does: start, signal or stop a Dark Army process; open the
installed app or the panel binary (a second panel evicts the person's own);
read the person's state folder (the strip runs under a fresh `HOME`); talk to
the daemon (the panel harness answers every request from the fixture and
logs it). The contract and the manual review are `docs/images/SHOTS.md`.
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import math
import os
import re
import socket
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HOST = REPO / "host"
FIXTURE = REPO / "panel" / "Tests" / "Fixtures" / "demo-shots.json"
PUBLIC_EXPORT = REPO / "tools" / "public-export.json"
DEFAULT_OUT = REPO / "docs" / "images"

# The one home folder a demo path may name. Built rather than written out so
# this file carries no machine path at all.
HOME_ROOT = "/" + "Users" + "/"
# The shortest machine string the denylist keeps (account, host, git name).
MIN_DENY_CHARS = 4
DEMO_USER = "you"

# The pictures, their descriptions and what each shows. The descriptions are
# the README's alt text verbatim (`shots.json` carries them).
SHOTS = {
    "menubar-strip.png": (
        "The menu-bar strip: three agents working, one waiting on you in red, "
        "five cards to do, and three usage meters.",
        "The strip alone: working glyph 3 +2, Cipher's face with a red 1, "
        "to-do 5, Claude / Grok / Codex meters."),
    "agent-question.png": (
        "An agent waiting on a question, with its three suggested answers as "
        "buttons.",
        "The Agents tab with Cipher selected: his question and three answers."),
    "board.png": (
        "The board with a card open, showing its plan, and running cards "
        "showing what each has cost so far.",
        "The board pane with the widget card's window open over it, plan "
        "rendered; running cards show cost, time and context."),
    "first-run.png": (
        "The three-step checklist a new user sees first.",
        "The rail's first-run checklist with step one ticked."),
}

# Everything below is in points; pixels are points x SCALE.
SCALE = 2
MAX_BYTES = 800 * 1024
DISPLAY_CAP = 880
WINDOW_PT = (1180, 748)       # the Agents layer
BOARD_WINDOW_PT = (1640, 1000)
RAIL_PT = 520
KEEP_CHUNKS = {b"IHDR", b"PLTE", b"tRNS", b"IDAT", b"IEND", b"sRGB"}

# The long-lived processes the pid comparison watches: the app bundle, the
# panel's bundle, the pty broker and the host packages run as modules.
# Deliberately not the per-session helpers (the hook handler, each session's
# channel server, the editor bridge): those come and go with Claude sessions
# and would fail the check with nothing wrong.
COMPONENTS = ("Dark Army.app", "BobPanel.app", "pty_broker",
              "dark_army_daemon", "dark_army_menubar")
# A process younger than this is somebody's passing command (a grep, a test
# worker), not the running app; it may come and go without meaning anything.
SETTLED_SECONDS = 120


class ShotError(RuntimeError):
    """A refusal in words; `main` prints it and exits 1."""


# --------------------------------------------------------------------------
# The host packages, under whichever name this checkout carries.
# --------------------------------------------------------------------------

def _host_package(kind: str) -> str:
    """`dark_army_<kind>`, the host package this tool draws with."""
    name = f"dark_army_{kind}"
    if (HOST / name / "__init__.py").is_file():
        return name
    raise ShotError(f"no host package {name!r} under host/")


def roster_names() -> tuple[str, ...]:
    """`identity.NAMES`, read from source so the guard needs no import."""
    source = (HOST / _host_package("daemon") / "identity.py").read_text()
    block = re.search(r"^NAMES[^=]*=\s*\((.*?)\)", source, re.S | re.M)
    if not block:
        raise ShotError("could not read identity.NAMES")
    return tuple(re.findall(r'"([^"]+)"', block.group(1)))


# --------------------------------------------------------------------------
# check — the privacy and capability guard
# --------------------------------------------------------------------------

def _run(argv: list[str]) -> str:
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


def run_time_denylist() -> list[str]:
    """This machine's own identifying strings, derived now and never stored.

    The account, the home folder's name, the host and computer names, the
    git author's name (whole and each word) and email (whole and the part
    before the @), plus the release scanner's own list where that file
    exists — every entry four characters or more, so a short name cannot
    match inside an ordinary word. A CI runner derives its own."""
    words: set[str] = set()

    def add(value: str, tokens: bool = False) -> None:
        value = (value or "").strip()
        if len(value) >= MIN_DENY_CHARS:
            words.add(value.lower())
        if tokens:
            for token in re.split(r"[\s._@-]+", value):
                if len(token) >= MIN_DENY_CHARS:
                    words.add(token.lower())

    try:
        add(getpass.getuser())
    except (KeyError, OSError):   # no account name is simply no entry
        pass
    add(Path.home().name)
    add(socket.gethostname().split(".")[0])
    add(_run(["scutil", "--get", "ComputerName"]))
    add(_run(["git", "-C", str(REPO), "config", "user.name"]), tokens=True)
    email = _run(["git", "-C", str(REPO), "config", "user.email"])
    add(email)
    add(email.split("@")[0])
    if PUBLIC_EXPORT.is_file():
        try:
            extra = json.loads(PUBLIC_EXPORT.read_text()).get("denylist") or []
        except (OSError, ValueError, AttributeError) as exc:
            raise ShotError(f"could not read {PUBLIC_EXPORT.name}: {exc}") from exc
        for value in extra:
            if isinstance(value, str):
                add(value)
    # Words the demo day is built from can never be a finding on their own.
    words -= {DEMO_USER, "code", "users", "localhost", "main"}
    return sorted(words)


def _strings(value, path="$", keys: bool = True):
    if isinstance(value, dict):
        for key, inner in value.items():
            if keys:
                yield from _strings(key, f"{path}.<key>")
            yield from _strings(inner, f"{path}.{key}", keys)
    elif isinstance(value, list):
        for index, inner in enumerate(value):
            yield from _strings(inner, f"{path}[{index}]", keys)
    elif isinstance(value, str):
        yield path, value


def _rows(frame: dict):
    agents = frame.get("agents") or {}
    for bucket in ("running", "waiting", "sleeping", "finished", "abandoned"):
        for row in agents.get(bucket) or []:
            yield bucket, row


_ALLOWED_TRUE_CAPS = {("waiting", "can_type")}
_FORBIDDEN_FLAGS = ("own_terminal", "hosted", "terminal_stream_supported", "channel")


def fixture_findings(data: dict, denylist: list[str],
                     names: tuple[str, ...] | None = None) -> list[str]:
    """Everything wrong with a demo day, in words; empty means it may be drawn."""
    findings: list[str] = []
    meta = data.get("metadata") or {}
    if meta.get("synthetic") is not True:
        findings.append("metadata.synthetic is not true")
    projects = set(meta.get("projects") or [])
    if not projects:
        findings.append("metadata.projects is empty")
    names = names if names is not None else roster_names()
    # Whole words only, in values only: "tom" must not flag "tomorrow", and a
    # JSON key is the fixture's shape, never somebody's name.
    patterns = [re.compile(r"(?<![A-Za-z0-9])" + re.escape(word) + r"(?![A-Za-z0-9])",
                           re.IGNORECASE)
                for word in denylist if word]

    for path, text in _strings(data):
        start = 0
        while True:
            at = text.find(HOME_ROOT, start)
            if at < 0:
                break
            user = text[at + len(HOME_ROOT):].split("/")[0]
            if user != DEMO_USER:
                findings.append(f"{path}: a home folder other than {DEMO_USER!r}")
            start = at + 1

    for path, text in _strings(data, keys=False):
        if any(pattern.search(text) for pattern in patterns):
            findings.append(f"{path}: contains a private string of this machine")

    frames = data.get("frames") or {}
    for frame_name, frame in frames.items():
        where = f"frames.{frame_name}"
        if "mission" in frame:
            findings.append(f"{where}: carries a mission section")
        for bucket, row in _rows(frame):
            label = f"{where}.{bucket}.{row.get('session_id', '?')}"
            if row.get("nickname") not in names:
                findings.append(f"{label}: nickname {row.get('nickname')!r} is not in identity.NAMES")
            if row.get("project") not in projects:
                findings.append(f"{label}: project {row.get('project')!r} is not a demo project")
            cwd = row.get("cwd") or ""
            if cwd and Path(cwd).name not in projects:
                findings.append(f"{label}: cwd outside the demo projects")
            if row.get("pid") is not None:
                findings.append(f"{label}: carries a pid")
            if row.get("tty") or row.get("address"):
                findings.append(f"{label}: carries a tty or an address")
            for flag in _FORBIDDEN_FLAGS:
                if row.get(flag):
                    findings.append(f"{label}: {flag} is true")
            for key, value in row.items():
                if key.startswith("can_") and value is True \
                        and (bucket, key) not in _ALLOWED_TRUE_CAPS:
                    findings.append(f"{label}: {key} is true")
        board = frame.get("board") or {}
        for card in board.get("cards") or []:
            label = f"{where}.board.{card.get('id', '?')}"
            if card.get("project") not in projects:
                findings.append(f"{label}: project {card.get('project')!r} is not a demo project")
            if Path(card.get("root") or "").name not in projects:
                findings.append(f"{label}: root outside the demo projects")
        for project in board.get("projects") or []:
            if Path(project.get("root") or "").name not in projects:
                findings.append(f"{where}.board.projects: root outside the demo projects")
        enrollment = frame.get("enrollment") or {}
        for entry in (enrollment.get("enrolled") or []) + (enrollment.get("pending") or []):
            if Path(entry.get("root") or "").name not in projects:
                findings.append(f"{where}.enrollment: root outside the demo projects")

    typing = [row.get("nickname") for _, row in _rows(frames.get("main") or {})
              if row.get("can_type")]
    if len(typing) > 1:
        findings.append(f"frames.main: more than one row can be typed into ({typing})")
    findings.extend(strip_findings(data))
    return findings


def strip_findings(data: dict) -> list[str]:
    """The strip's counts must be the frame's own buckets, as the live strip's
    are `_activity_counts()` — the two pictures cannot disagree."""
    strip = data.get("strip") or {}
    main = (data.get("frames") or {}).get("main") or {}
    agents = main.get("agents") or {}
    cards = ((main.get("board") or {}).get("cards")) or []
    expect = {
        "working": len(agents.get("running") or []),
        "attention": len(agents.get("waiting") or []),
        "idle": len(agents.get("sleeping") or []),
        "subagents": sum(int(row.get("subagents") or 0)
                         for row in agents.get("running") or []),
        "todo": sum(1 for card in cards
                    if card.get("column_name") in ("prep", "backlog")),
    }
    return [f"strip.{key} is {strip.get(key)!r}, the frame says {value}"
            for key, value in expect.items() if strip.get(key) != value]


def load_fixture() -> dict:
    return json.loads(FIXTURE.read_text())


def fixture_digest() -> str:
    return hashlib.sha256(FIXTURE.read_bytes()).hexdigest()


def cmd_check(args) -> None:
    findings = fixture_findings(load_fixture(), run_time_denylist())
    manifest = Path(args.out) / "shots.json"
    if manifest.is_file():
        shots = json.loads(manifest.read_text())
        rung = (shots.get("menubar-strip.png") or {}).get("rung")
        if rung != 0:
            findings.append(f"shots.json: the strip settled at rung {rung!r}, not 0")
    for line in findings:
        print(f"finding: {line}")
    if findings:
        raise ShotError(f"{len(findings)} finding(s); the demo day may not be drawn")
    print("check: the demo day is synthetic and clean")


# --------------------------------------------------------------------------
# strip — the menu-bar strip, in this process alone
# --------------------------------------------------------------------------

def cmd_strip(args) -> None:
    work = Path(args.work)
    home = work / "home"
    home.mkdir(parents=True, exist_ok=True)
    # Before any host import: every path the app derives follows HOME.
    os.environ["HOME"] = str(home)
    sys.path.insert(0, str(HOST))
    import importlib
    paths = importlib.import_module(f"{_host_package('daemon')}.paths")
    state = Path(paths.STATE_DIR).resolve()
    if home.resolve() not in state.parents:
        print(f"strip: the state folder is not inside {home}", file=sys.stderr)
        raise SystemExit(2)

    from AppKit import (NSApplication, NSAppearance, NSAppearanceNameDarkAqua,
                        NSApplicationActivationPolicyProhibited, NSBitmapImageRep,
                        NSButton, NSDeviceRGBColorSpace, NSGraphicsContext,
                        NSMakeRect, NSPNGFileType)

    app_module = importlib.import_module(f"{_host_package('menubar')}.app")
    nsapp = NSApplication.sharedApplication()
    nsapp.setActivationPolicy_(NSApplicationActivationPolicyProhibited)
    dark = NSAppearance.appearanceNamed_(NSAppearanceNameDarkAqua)
    button = NSButton.alloc().initWithFrame_(NSMakeRect(0, 0, 400, 22))
    button.setAppearance_(dark)

    day = load_fixture()["strip"]

    class _Alive:
        @staticmethod
        def is_alive() -> bool:
            return True

    app = object.__new__(app_module.BobCompanionApp)
    for attr, value in (("_usage_cache", {}), ("_frame_cache", {}),
                        ("_divider_cache", {}), ("_todo_cache", {}), ("_fonts", None),
                        ("_strip_sig", None), ("_strip_width", None),
                        ("_strip_key", None), ("_strip_level", 0),
                        ("_anim_i", -1), ("_daemon_thread", _Alive())):
        setattr(app, attr, value)
    app._working_count = day["working"]
    app._idle_count = day["idle"]
    app._attention_count = day["attention"]
    app._subagent_count = day["subagents"]
    app._todo_count = day["todo"]
    app._limits = day["limits"]
    app._grok_limits = day["grok_limits"]
    app._codex_limits = day["codex_limits"]
    faces = {key: list(value) for key, value in day["faces"].items()}
    app._status_button = lambda: button
    app._strip_faces = lambda: faces

    # The real ladder walk, once: frame 0 of the working cycle.
    app_module.BobCompanionApp._animate_icon(app, None)
    title = button.attributedTitle()
    width = float(title.size().width)
    height = float(title.size().height)
    rung = int(app._strip_level)
    budget = float(app_module.STRIP_BUDGET_PT)
    if rung != 0 or width > budget:
        raise ShotError(f"the strip settled at rung {rung}, {width:.1f} pt of "
                        f"{budget:.0f}: a picture of a strip nobody sees")

    bar = 22.0
    pixels_w, pixels_h = int(math.ceil(width * SCALE)), int(bar * SCALE)
    rep = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
        None, pixels_w, pixels_h, 8, 4, True, False, NSDeviceRGBColorSpace, 0, 32)
    rep.setSize_((width, bar))
    context = NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep)
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.setCurrentContext_(context)
    try:
        NSAppearance.setCurrentAppearance_(dark)
        title.drawAtPoint_((0.0, (bar - height) / 2.0))
    finally:
        NSGraphicsContext.restoreGraphicsState()
    layers = work / "layers"
    layers.mkdir(parents=True, exist_ok=True)
    data = rep.representationUsingType_properties_(NSPNGFileType, None)
    data.writeToFile_atomically_(str(layers / "strip.png"), True)
    (layers / "strip.json").write_text(json.dumps(
        {"rung": rung, "width_pt": round(width, 2), "budget_pt": budget}))
    print(f"strip: rung {rung}, {width:.1f} pt of {budget:.0f}")


# --------------------------------------------------------------------------
# panel — the Mac layers, drawn by `swift test`
# --------------------------------------------------------------------------

PANEL_LAYERS = ("window-agents-cipher.png", "window-board.png", "card-window.png",
                "rail-first-run.png")


def cmd_panel(args) -> None:
    layers = Path(args.work) / "layers"
    layers.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, BOB_DEMO_SHOTS_OUT=str(layers))
    argv = ["swift", "test", "--package-path", str(REPO / "panel"),
            "--filter", "DemoShotsTests"]
    print("panel: " + " ".join(argv[:2] + argv[4:]))
    done = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=1800)
    if done.returncode != 0:
        tail = "\n".join((done.stdout + done.stderr).splitlines()[-30:])
        raise ShotError(f"the panel render failed:\n{tail}")
    missing = [name for name in PANEL_LAYERS if not (layers / name).is_file()]
    if missing:
        raise ShotError(f"the panel render wrote no {', '.join(missing)} "
                        "(was it skipped?)")
    log = (layers / "requests.txt").read_text().splitlines()
    if any("/api/events" in line or not line.startswith("GET ") for line in log if line):
        raise ShotError("the panel render asked for the event stream or wrote")
    print(f"panel: {len(PANEL_LAYERS)} layers, {len([l for l in log if l])} "
          "fixture requests, none live")


# --------------------------------------------------------------------------
# compose — frames, metadata, sizes, the manifest
# --------------------------------------------------------------------------

def _pt(value: float) -> int:
    return int(round(value * SCALE))


def _pil():
    try:
        from PIL import Image, ImageCms, ImageDraw, ImageFilter
    except ImportError as exc:  # the venv carries Pillow (requirements-dev.txt)
        raise ShotError("Pillow is missing: run this with host/.venv/bin/python") from exc
    return Image, ImageCms, ImageDraw, ImageFilter


def _to_srgb(img):
    Image, ImageCms, _, _ = _pil()
    profile = img.info.get("icc_profile")
    img = img.convert("RGBA")
    if profile:
        import io
        try:
            source = ImageCms.ImageCmsProfile(io.BytesIO(profile))
            target = ImageCms.createProfile("sRGB")
            img = ImageCms.profileToProfile(img, source, target, outputMode="RGBA")
        except (ImageCms.PyCMSError, OSError, ValueError):
            pass
    img.info.pop("icc_profile", None)
    return img


def _open(path: Path):
    Image, _, _, _ = _pil()
    with Image.open(path) as img:
        img.load()
        return _to_srgb(img)


def _background(width: int, height: int):
    Image, _, _, _ = _pil()
    top, bottom = (0x15, 0x17, 0x1A), (0x0D, 0x0E, 0x10)
    column = Image.new("RGBA", (1, height))
    for y in range(height):
        t = y / max(1, height - 1)
        column.putpixel((0, y), tuple(int(a + (b - a) * t) for a, b in zip(top, bottom)) + (255,))
    return column.resize((width, height))


def _shadowed(canvas, layer, x: int, y: int) -> None:
    Image, _, _, ImageFilter = _pil()
    alpha = layer.split()[-1]
    shadow = Image.new("RGBA", layer.size, (0, 0, 0, 0))
    shadow.putalpha(alpha.point(lambda a: int(a * 0.55)))
    pad = _pt(24) * 2
    padded = Image.new("RGBA", (layer.width + 2 * pad, layer.height + 2 * pad), (0, 0, 0, 0))
    padded.alpha_composite(shadow, (pad, pad))
    padded = padded.filter(ImageFilter.GaussianBlur(_pt(24) / 2))
    canvas.alpha_composite(padded, (x - pad, y - pad + _pt(10)))
    canvas.alpha_composite(layer, (x, y))


def _rounded(layer, radius_pt: float):
    Image, _, ImageDraw, _ = _pil()
    mask = Image.new("L", layer.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, layer.width - 1, layer.height - 1),
                                           radius=_pt(radius_pt), fill=255)
    out = layer.copy()
    out.putalpha(Image.composite(layer.split()[-1], mask, mask))
    return out


def _framed(layer):
    margin = _pt(32)
    canvas = _background(layer.width + 2 * margin, layer.height + 2 * margin)
    _shadowed(canvas, layer, margin, margin)
    return canvas


def _even(img):
    """Even pixel dimensions, so the point size is a whole number."""
    width, height = img.width - img.width % 2, img.height - img.height % 2
    return img.crop((0, 0, width, height)) if (width, height) != img.size else img


def _strip_chunks(path: Path) -> None:
    """Rewrite a PNG keeping only the chunks a picture needs — no text, no
    time, no profile, no EXIF, no physical size."""
    data = path.read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ShotError(f"{path.name} is not a PNG")
    out = [data[:8]]
    at = 8
    while at < len(data):
        length, kind = struct.unpack(">I4s", data[at:at + 8])
        chunk = data[at:at + 12 + length]
        if kind in KEEP_CHUNKS:
            out.append(chunk)
        at += 12 + length
    path.write_bytes(b"".join(out))


def png_chunks(path: Path) -> list[bytes]:
    data = path.read_bytes()
    kinds, at = [], 8
    while at < len(data):
        length, kind = struct.unpack(">I4s", data[at:at + 8])
        kinds.append(kind)
        at += 12 + length
    return kinds


def _save(img, path: Path) -> None:
    Image, _, _, _ = _pil()
    img = _even(img.convert("RGBA"))
    # An opaque picture needs no alpha channel; dropping it is free bytes.
    if img.split()[-1].getextrema() == (255, 255):
        img = img.convert("RGB")
    img.save(path, format="PNG", optimize=True)
    _strip_chunks(path)
    if path.stat().st_size > MAX_BYTES:
        quantised = img.convert("RGB").quantize(256, method=Image.Quantize.MEDIANCUT,
                                                dither=Image.Dither.NONE)
        quantised.save(path, format="PNG", optimize=True)
        _strip_chunks(path)
    if path.stat().st_size > MAX_BYTES:
        raise ShotError(f"{path.name} is {path.stat().st_size // 1024} KiB even at 256 "
                        "colours; shrink its window in points, never the scale")


def compose_images(layers: Path, out: Path) -> dict:
    """Write every picture; return {file: extra manifest fields}."""
    Image, _, ImageDraw, _ = _pil()
    out.mkdir(parents=True, exist_ok=True)
    strip = _open(layers / "strip.png")
    agents = _open(layers / "window-agents-cipher.png")
    board_window = _open(layers / "window-board.png")
    card = _open(layers / "card-window.png")
    rail = _open(layers / "rail-first-run.png")
    extra: dict[str, dict] = {}

    band_h = _pt(24)

    # menubar-strip: the strip on a rounded piece of menu bar.
    pad = _pt(12)
    piece = Image.new("RGBA", (strip.width + 2 * pad, max(band_h, strip.height) + 2 * _pt(4)),
                      (0, 0, 0, 0))
    ImageDraw.Draw(piece).rounded_rectangle((0, 0, piece.width - 1, piece.height - 1),
                                            radius=_pt(6), fill=(0x1C, 0x1C, 0x1E, 235))
    piece.alpha_composite(strip, (pad, (piece.height - strip.height) // 2))
    small = _pt(16)
    canvas = _background(piece.width + 2 * small, piece.height + 2 * small)
    canvas.alpha_composite(piece, (small, small))
    _save(canvas, out / "menubar-strip.png")

    # agent-question: the Agents tab with Cipher open.
    _save(_framed(agents), out / "agent-question.png")
    extra["agent-question.png"] = {"window_pt": list(WINDOW_PT)}

    # board: the wide board pane, the card window over its lower right.
    pane_w = board_window.width - _pt(RAIL_PT)
    pane = _rounded(board_window.crop((0, 0, pane_w, board_window.height)), 10)
    margin = _pt(32)
    width = pane.width + 2 * margin
    height = pane.height + 2 * margin
    board = _background(width, height)
    _shadowed(board, pane, margin, margin)
    card_x = width - margin - card.width
    card_y = height - margin - card.height
    _shadowed(board, card, card_x, card_y)
    _save(board, out / "board.png")
    extra["board.png"] = {"window_pt": list(BOARD_WINDOW_PT)}

    # first-run: the rail from the fleet counts down to the end of the
    # checklist — the rail's top is the brand bar's empty right end.
    top, bottom = _pt(95), _pt(392)
    piece = _rounded(rail.crop((0, top, rail.width, bottom)), 10)
    _save(_framed(piece), out / "first-run.png")

    return extra


def cmd_compose(args) -> None:
    layers = Path(args.work) / "layers"
    out = Path(args.out)
    manifest_path = out / "shots.json"
    extra = compose_images(layers, out)
    strip = json.loads((layers / "strip.json").read_text())
    digest = fixture_digest()
    manifest: dict[str, dict] = {}
    Image, _, _, _ = _pil()
    for name, (alt, shows) in SHOTS.items():
        path = out / name
        with Image.open(path) as img:
            w, h = img.size
        entry = {"alt": alt, "shows": shows, "width_px": w, "height_px": h,
                 "scale": SCALE, "display_width": min(DISPLAY_CAP, w // SCALE),
                 "bytes": path.stat().st_size, "fixture_sha256": digest}
        if name == "menubar-strip.png":
            entry["rung"] = strip["rung"]
        entry.update(extra.get(name) or {})
        manifest[name] = entry
    manifest_path.write_text(json.dumps(manifest, indent=1, ensure_ascii=False) + "\n")
    print(f"compose: {len(manifest)} pictures in {out}")


# --------------------------------------------------------------------------
# all — everything, with the running app watched throughout
# --------------------------------------------------------------------------

def _etime_seconds(text: str) -> int:
    days, _, rest = text.strip().rpartition("-")
    parts = [int(p) for p in rest.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    return int(days or 0) * 86400 + parts[0] * 3600 + parts[1] * 60 + parts[2]


def live_components() -> dict[int, str]:
    """Dark Army's settled processes by pid — a read-only `ps`, nothing else."""
    done = subprocess.run(["/bin/ps", "-axo", "pid=,etime=,command="],
                          capture_output=True, text=True, timeout=10, check=True)
    found = {}
    for line in done.stdout.splitlines():
        fields = line.strip().split(None, 2)
        if len(fields) < 3:
            continue
        pid, etime, command = fields
        if str(os.getpid()) == pid:
            continue
        if any(name in command for name in COMPONENTS) \
                and _etime_seconds(etime) >= SETTLED_SECONDS:
            found[int(pid)] = command[:120]
    return found


def cmd_all(args) -> None:
    before = live_components()
    work = Path(args.work)
    tool = [sys.executable, str(Path(__file__).resolve())]
    common = ["--work", str(work), "--out", str(args.out)]
    cmd_check(args)
    # The strip changes HOME and loads AppKit: its own process, always.
    done = subprocess.run(tool + ["strip"] + common)
    if done.returncode != 0:
        raise ShotError("the strip render failed")
    cmd_panel(args)
    cmd_compose(args)
    cmd_check(args)
    after = live_components()
    gone = sorted(set(before) - set(after))
    if gone:
        raise ShotError("a Dark Army process that was running before is gone: "
                        + ", ".join(f"{pid} {before[pid]}" for pid in gone))
    print(f"live app untouched: {len(before)} processes, same pids")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("command", choices=("check", "strip", "panel", "compose", "all"))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--work", default=None)
    args = parser.parse_args(argv)
    if args.work is None:
        args.work = tempfile.mkdtemp(prefix="dark-army-shots-")
    commands = {"check": cmd_check, "strip": cmd_strip, "panel": cmd_panel,
                "compose": cmd_compose, "all": cmd_all}
    started = time.monotonic()
    try:
        commands[args.command](args)
    except ShotError as exc:
        print(f"demo_shots: {exc}", file=sys.stderr)
        return 1
    if args.command == "all":
        print(f"done in {time.monotonic() - started:.0f}s (work files in {args.work})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
