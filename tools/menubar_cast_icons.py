#!/usr/bin/env python3
"""Bake the menu-bar strip's icons from the cast's pixel art.

The strip is the one surface that keeps hand-drawn animated figures — the
panel and the phone draw photographs (`tools/portrait_ingest.py`). Two bakes
on every run:

* ``bake()`` writes the aggregate ``dark-army-{work,idle-*,attn,disconnected*}``
  glyphs from one character (Cipher). That is the fallback when a
  category is drawn as a count, and when ``_strip_faces`` empties a
  category because a nickname has no drawing yet.
* ``bake_all()`` writes ``cast-{name}-{category}-{variant}-{i}.png`` for
  every slug in ``manifest.json``'s ``cast`` list. The strip uses a named
  face only when a live category holds a single agent (``MAX_FACES`` is
  1); a name in ``identity.NAMES`` without these files collapses the
  whole category back to the aggregate glyph.

Cipher is the aggregate glyph. The hood-up silhouette is the most
recognisable shape in the cast and the one that survives 20pt best on both
a light and a dark bar: a dark hood over a pale face keeps its outline where
a suit or a beard becomes a smudge, and its palette is neutral, so it never
competes with the amber and red the usage meter uses for 75–90% and above.
Until Cipher's GIFs are ingested the existing aggregate glyphs stay as they
are — ``bake()`` skips a character the manifest does not declare.

Two things this has to respect, both of which come from `app.py`:

* **The frame counts are fixed by the constants there** (8 working, 4 idle, 4
  attention). Rather than edit them, the cast's 2–3 authored frames are spread
  across those slots *in proportion to their authored hold times*, so a 420ms
  frame occupies twice the slots of a 210ms one. At ICON_TICK = 0.2s that lands
  close to the timing the art was drawn with, and app.py stays untouched.
* **Nothing tints an attachment.** The strip draws frames as NSTextAttachments,
  which AppKit never tints, so anything that must contrast with the bar has to
  be baked per appearance. That is why the offline glyph is written twice.

    python tools/menubar_cast_icons.py             # cipher → icons/
    python tools/menubar_cast_icons.py --character vex --preview /tmp/bar.png

Pillow only.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

try:
    from PIL import Image
except ImportError:                                   # pragma: no cover
    sys.exit("error: Pillow is required — pip install Pillow")

REPO = Path(__file__).resolve().parent.parent
CAST_DIR = REPO / "assets" / "cast"
ICON_DIR = REPO / "host" / "dark_army_menubar" / "icons"

# Slot counts, mirroring ICON_STRIP_FRAMES / ICON_IDLE_FRAMES in app.py. Kept
# here as data rather than imported, because importing app.py drags in rumps and
# AppKit and this is a build tool that should run anywhere.
SLOTS = {"work": 8, "idle": 4, "attn": 4}

# Which cast state feeds which category.
STATE_FOR = {"work": "work", "idle": "sleep", "attn": "alert"}

# Integer upscale. Nearest-neighbour at a whole multiple keeps the pixel grid
# exact, which is what `tools/hud_icons_from_menubar.py` relies on when it
# reverses the scale to recover a 16px sprite.
SCALE = 12


def _load(character: str, state: str) -> tuple[list[Image.Image], list[int]]:
    directory = CAST_DIR / f"{character}-{state}"
    frames = sorted(directory.glob("frame_*.png"))
    if not frames:
        sys.exit(f"error: no frames in {directory}")
    manifest = json.loads((CAST_DIR / "manifest.json").read_text())
    holds = manifest["animations"].get(f"{character}-{state}", {}).get("holds")
    images = [Image.open(f).convert("RGBA") for f in frames]
    if not holds or len(holds) != len(images):
        holds = [300] * len(images)
    return images, holds


def spread(frames: list[Image.Image], holds: list[int], slots: int
           ) -> list[Image.Image]:
    """Fill `slots` ticks from `frames`, weighted by how long each was drawn to
    hold. Every frame gets at least one slot — dropping one entirely would lose a
    pose rather than merely mistime it."""
    total = sum(holds) or len(frames)
    counts = [max(1, round(slots * h / total)) for h in holds]
    # Rounding rarely lands on the target; fix it up on the longest-held frame,
    # which is the one least distorted by a slot either way.
    while sum(counts) > slots:
        counts[counts.index(max(counts))] -= 1
    while sum(counts) < slots:
        counts[holds.index(max(holds))] += 1
    out: list[Image.Image] = []
    for frame, count in zip(frames, counts):
        out.extend([frame] * count)
    return out[:slots]


def silhouette(frame: Image.Image, ink: tuple[int, int, int]) -> Image.Image:
    """Flat single-colour cut-out, for the offline glyph.

    Offline is the one state that is *about* the app rather than about an agent,
    and it has no art in the cast. A silhouette says "this is the same character,
    switched off" without pretending to be a pose that was never drawn.
    """
    out = Image.new("RGBA", frame.size, (0, 0, 0, 0))
    src, dst = frame.load(), out.load()
    for y in range(frame.height):
        for x in range(frame.width):
            if src[x, y][3] >= 128:
                dst[x, y] = (*ink, 255)
    return out


def upscale(img: Image.Image, factor: int) -> Image.Image:
    return img.resize((img.width * factor, img.height * factor), Image.NEAREST)


# The state rule, baked under each per-agent face. Measured on the previous art:
# a character's *identity* is a 2–4x stronger visual signal than its own state at
# 17pt, so a pose alone can never reliably say which state it is in. A rule below
# the figure sits outside the identity signal, is ~28 rendered pixels against the
# beacon's 6, and is free in width — height is pinned at 17pt, so a taller canvas
# makes each face narrower and a row of them cheaper.
#
# Form carries the state and colour only reinforces it — solid for working,
# broken for asleep, solid red for attention — so red stays an exception signal
# rather than the message, and nothing here is colour-alone.
RULE_ROWS = 2            # rule thickness, in native art pixels
RULE_GAP_ROWS = 1        # between the character's feet and the rule
RULE_DASH = (3, 2)       # on, off — the sleeping rule's broken pattern
RULE_INSET = 2           # native pixels clear of each edge

# Resolved by hand per appearance: attachments are never tinted, so a neutral
# rule baked once would be invisible on one of the two menu bars.
RULE_INK = {
    "light": {"work": (0, 0, 0, 205), "idle": (0, 0, 0, 120),
              "attn": (215, 45, 35, 255)},
    "dark": {"work": (255, 255, 255, 215), "idle": (255, 255, 255, 130),
             "attn": (255, 90, 80, 255)},
}


def cycle_box(frames: list[Image.Image]) -> tuple[int, int, int, int]:
    """The union of every frame's ink bounds across one animation cycle.

    **One canvas per cycle, not per frame.** Cropping each frame to its own bbox
    is the same idea one step too far: the strip fits a face to a fixed *height*
    and derives its width from the aspect ratio, so a per-frame crop makes both
    the width and the apparent size of the character change on every tick.
    Measured on the first cast: one working cycle ran 468→516px wide and 360→420
    tall, which at 20pt is a 4.6pt swing in advance width — so each face shoved
    everything to its right several times a second, and the animation read as the
    figures jostling one another rather than each one working in place. Worse,
    the tighter-cropped frames scaled *up*: the character pulsed instead of
    swaying, because the sway was being cropped away and then re-added as scale.

    The union box keeps the motion where it was drawn — inside a box that does
    not move — at the cost of the few pixels of slack the widest pose needs.
    """
    boxes = [f.getbbox() for f in frames]
    boxes = [b for b in boxes if b]
    if not boxes:
        return (0, 0, frames[0].width, frames[0].height)
    return (min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes))


def with_rule(frame: Image.Image, category: str, variant: str,
              box: tuple[int, int, int, int] | None = None) -> Image.Image:
    """The character with its state rule beneath it, cropped tight.

    Tight to this *state's* own ink, not to the character's shared canvas. The
    shared canvas is right for the panel — an agent changing state there must not
    resize — but in the menu bar it is paid for in the only dimension that is
    scarce: the strip fits the icon to a fixed height, so a canvas sized for the
    tallest pose renders every other pose small inside it. Measured before this,
    with one canvas per character: one working pose filled **60%** of its
    box and another 64%, against 97–98% for the tightest-drawn two — so at 17pt
    one was a 10.2pt character standing next to a 16.6pt one. Cropping per
    state costs a small size change when an agent switches state, which is worth
    far less than a permanent 40% size difference between characters.

    `box` is that state's crop, shared by every frame of the cycle — see
    `cycle_box` for why it must not be recomputed per frame. Omitting it crops to
    this frame alone, which is only ever right for a still.
    """
    if box is None:
        box = frame.getbbox() or (0, 0, frame.width, frame.height)
    frame = frame.crop(box)
    height = frame.height + RULE_GAP_ROWS + RULE_ROWS
    out = Image.new("RGBA", (frame.width, height), (0, 0, 0, 0))
    out.paste(frame, (0, 0), frame)
    ink = RULE_INK[variant][category]
    px = out.load()
    y0 = frame.height + RULE_GAP_ROWS
    x0, x1 = RULE_INSET, frame.width - RULE_INSET
    on, off = RULE_DASH
    for x in range(x0, x1):
        if category == "idle" and (x - x0) % (on + off) >= on:
            continue
        for y in range(y0, y0 + RULE_ROWS):
            px[x, y] = ink
    return out


def bake_all(out_dir: Path, scale: int) -> list[Path]:
    """Every character, under `cast-<name>-<category>-<variant>-<i>.png`.

    The strip shows one face per *agent*, so it needs the whole cast on disk.
    These sit beside the `dark-army-*` set, which remains the fallback for a category
    drawn in aggregate and for any nickname that has no portrait.
    """
    manifest = json.loads((CAST_DIR / "manifest.json").read_text())
    written: list[Path] = []
    out_dir.mkdir(parents=True, exist_ok=True)
    for character in manifest.get("cast", []):
        for category, count in SLOTS.items():
            frames, holds = _load(character, STATE_FOR[category])
            spread_frames = spread(frames, holds, count)
            box = cycle_box(frames)
            for variant in ("light", "dark"):
                for i, frame in enumerate(spread_frames):
                    path = out_dir / f"cast-{character}-{category}-{variant}-{i}.png"
                    upscale(with_rule(frame, category, variant, box),
                            scale).save(path)
                    written.append(path)
    return written


def bake(character: str, out_dir: Path, scale: int) -> list[Path]:
    written: list[Path] = []
    out_dir.mkdir(parents=True, exist_ok=True)

    for category, count in SLOTS.items():
        frames, holds = _load(character, STATE_FOR[category])
        spread_frames = spread(frames, holds, count)
        for i, frame in enumerate(spread_frames):
            image = upscale(frame, scale)
            if category == "idle":
                # Two variants because nothing tints an attachment. They are
                # identical today: the sleep art's Zzz is a mid-blue that holds
                # up on both bars, unlike the frog's monochrome one, which had to
                # be inverted. The pair is kept so a future contrast fix has
                # somewhere to go without a code change.
                for variant in ("light", "dark"):
                    path = out_dir / f"dark-army-idle-{variant}-{i}.png"
                    image.save(path)
                    written.append(path)
            else:
                path = out_dir / f"dark-army-{category}-{i}.png"
                image.save(path)
                written.append(path)

    sleep_frames, _ = _load(character, "sleep")
    for variant, ink in (("light", (0, 0, 0)), ("dark", (255, 255, 255))):
        glyph = upscale(silhouette(sleep_frames[0], ink), scale)
        name = "dark-army-disconnected.png" if variant == "light" \
            else "dark-army-disconnected-dark.png"
        glyph.save(out_dir / name)
        written.append(out_dir / name)
    return written


def preview(character: str, path: Path, scale: int = 4) -> None:
    """Contact sheet at the real 17pt, on both bar colours — the only way to
    judge this, since every problem it has is a legibility problem."""
    from PIL import ImageDraw

    pt = 17 * scale
    cell = pt + 14
    states = [("work", "work"), ("sleep", "idle"), ("alert", "attn")]
    sheet = Image.new("RGB", (cell * 3 * 2 + 24, cell), (255, 255, 255))
    draw = ImageDraw.Draw(sheet)
    draw.rectangle([0, 0, cell * 3, cell], fill=(246, 246, 248))
    draw.rectangle([cell * 3 + 24, 0, sheet.width, cell], fill=(28, 28, 30))
    for column, (state, _category) in enumerate(states):
        frames, _ = _load(character, state)
        frame = frames[0]
        height = pt
        width = max(1, round(frame.width * height / frame.height))
        scaled = frame.resize((width, height), Image.LANCZOS)
        for x0 in (0, cell * 3 + 24):
            sheet.paste(scaled, (x0 + column * cell + 7, 7), scaled)
    sheet.save(path)
    print(f"==> Preview: {path}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--character", default="cipher")
    ap.add_argument("--out", type=Path, default=ICON_DIR)
    ap.add_argument("--scale", type=int, default=SCALE)
    ap.add_argument("--preview", type=Path,
                    help="write a light/dark contact sheet and bake nothing")
    args = ap.parse_args()

    if args.preview:
        preview(args.character, args.preview)
        return

    written = bake(args.character, args.out, args.scale)
    print(f"==> {len(written)} aggregate icons from {args.character} → {args.out}")
    everyone = bake_all(args.out, args.scale)
    print(f"==> {len(everyone)} per-agent icons for the whole cast")
    for path in written[:4]:
        with Image.open(path) as im:
            print(f"    {path.name}  {im.width}x{im.height}")
    print("    …")


if __name__ == "__main__":
    main()
