#!/usr/bin/env python3
"""Snap the cast's hand-drawn GIFs back onto their native pixel grid.

This is the **menu-bar strip's** art pipeline. The strip fits each face to a
fixed ~20pt height inside a 300pt budget, so it keeps small hand-drawn
animated figures; the panel and the phone draw photographs instead
(`tools/portrait_ingest.py`). Two trees, one roster: `CAST` below is the
twenty `identity.NAMES` lowercased plus `identity.ART_ONLY`, and neither tree
may add or rename a slug on its own. A slug listed here with no drawing is
lawful — the strip falls back to its aggregate glyph and a count.

The source art is pixel art that was *rendered large* — roughly 900px square —
and it is not on integer boundaries. Measured block periods on the first set
ran 25.75px for most of the cast, with outliers at 22.3 and 10.67, and the
lattice phase drifted a few pixels across a frame. So neither of the obvious
conversions works: a fixed nearest-neighbour stride lands on drifted boundaries
and shears the character, and a box downscale averages across cell edges into
mush.

What works, and what this does:

1. **Fit the lattice per frame, then reconcile per character.** Build an edge
   signal (how much each column differs from the one before it, inside the alpha
   mask) and score a comb of period ``p`` and phase ``φ`` against it. Per frame,
   because one character's alert frames can genuinely be rendered at a
   different scale from their siblings. But a comb scores nearly as well at half
   or double the true period, so the frames are then pulled onto one harmonic —
   without that, a finely drawn character's frames land on both 10.67 and 21.3
   and its shared canvas comes out twice as tall as its art.
2. **One output pixel per cell, by majority.** Take the central 60% of each cell
   and use its most common colour. Mode rather than mean or median: this is flat-
   colour pixel art, so the commonest colour in a cell *is* the cell's colour,
   while an average invents a shade that was never drawn.
3. **Strip the white sticker outline**, then despeckle. Some source art is
   drawn with a die-cut ring and some is not. Two peels from the outside left
   a 2–3 cell ring on a finer lattice and a trapped blob in hair concavities,
   so the strip is a flood from the exterior through near-white — gated on
   the silhouette actually *being* a ring, otherwise an off-white jacket goes
   with it.
4. **Crop to the union of all frames of all three states**, bottom-centre
   aligned, so a character neither shifts on its feet between frames nor changes
   size when it goes from working to asleep.

Everything downstream is ordinary PNG frames. Nothing here knows about RLE,
RGB565 or a transparency key — the window that needed those has been deleted,
and both surviving surfaces (the menu-bar strip and the panel) load PNGs.

    python tools/pixelgrid_ingest.py \\
        assets/proposals/dark-army-cast/anim assets/cast

Source files are ``assets/proposals/dark-army-cast/anim/<slug>-<state>.gif``
(git-ignored, like every proposal; no machine here holds that folder yet),
drawn by hand in the house style of the frames already under
``assets/cast/cipher-*``. A character nobody has drawn yet is simply absent
from the manifest, and the strip draws the aggregate glyph plus a count for
it — so the rest of the cast may land one at a time.

Pillow only, like its siblings in this directory.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, deque
from pathlib import Path

try:
    from PIL import Image, ImageSequence
except ImportError:                                   # pragma: no cover
    sys.exit("error: Pillow is required — pip install Pillow")

# The roster this tree may hold: `identity.NAMES` lowercased, in order, plus
# `identity.ART_ONLY`. Pinned equal by `host/tests/test_identity.py`. A slug
# here needs no GIFs to be legal — `assets/cast/manifest.json` declares only
# what has actually been drawn, and the strip falls back for the rest.
CAST = (
    "cipher", "vex", "ledger", "mira", "hex", "relay", "forge",
    "watch", "audit", "proxy", "quiet", "nyx", "canon", "velvet",
    "androll", "captcha", "sawa", "franio", "zosia", "ptyś",
    "overwatch",
)
STATES = ("work", "sleep", "alert")
# The first set carried allow-listed exceptions (a period of 20.2 here, a
# letterboxed canvas there) under HOUSE_SLUGS. This cast is drawn fresh, so
# every slug is a new slug: fail-under, the magenta check and the canvas-ratio
# floor apply throughout, and the allow-list is empty on purpose.
NEW_SLUGS = frozenset(CAST)
HOUSE_SLUGS: frozenset[str] = frozenset()
MOTE = (74, 190, 240)
FIT_FLOOR = 2.3
FIT_FLOOR_NEW = 2.2
CANVAS_RATIO_MIN = 0.80
# Per-slug lattice cells per output pixel, for art drawn at a finer density
# than the rest of the set. Empty until a drawing needs it; `--cells` per run.
DEFAULT_CELLS: dict[str, int] = {}

# Source files are `<char>-<state>.gif`. Work used to be the bare `<char>.gif` —
# a 3-frame still-cycle standing in for an animation — and is now its own
# 8-frame loop with a head sway and rising motes, which is both better art and a
# far stronger state cue than the pose it replaced.
_SUFFIX = {"work": "-work", "sleep": "-sleep", "alert": "-alert"}

# Where to look for the lattice. Wide enough for a 10.67 and a 25.75 period
# without admitting periods so small that noise wins.
PERIOD_MIN, PERIOD_MAX, PERIOD_STEP = 8.0, 30.0, 0.05
PHASE_STEPS = 24

# Fraction of each cell sampled for its colour. The edges of a cell carry the
# antialiasing between it and its neighbours; the middle is the colour itself.
CELL_CORE = 0.6

# A cell counts as outline if every channel is above this. Kept high so eyes,
# teeth and pale shirts survive once the flood is allowed to run.
WHITE_MIN = 226
# Pure-enough white to recognise a die-cut ring. Off-white clothing (a pale
# jacket sits around 236) must not trip this, or the gate would eat it.
STICKER_WHITE = 248
# Lowest silhouette share of STICKER_WHITE that still means "this frame has a
# ring". House work frames land at 63–100%; the new five land at 0%.
RING_FRACTION = 0.20


# ── lattice fitting ──────────────────────────────────────────────────────────

def _edge_signal(img: Image.Image) -> tuple[list[float], list[float]]:
    """Per-column and per-row "how much changes here" profiles.

    Alpha edges count too, and count double: the silhouette is the strongest
    lattice evidence in the image, because the character's outline was drawn on
    the grid even where its interior is a flat field.
    """
    w, h = img.size
    px = img.load()
    cols = [0.0] * w
    rows = [0.0] * h
    for y in range(h):
        for x in range(1, w):
            r0, g0, b0, a0 = px[x - 1, y]
            r1, g1, b1, a1 = px[x, y]
            if a0 != a1:
                cols[x] += 2.0
            elif a1:
                d = abs(r1 - r0) + abs(g1 - g0) + abs(b1 - b0)
                if d > 24:
                    cols[x] += 1.0
    for x in range(w):
        for y in range(1, h):
            r0, g0, b0, a0 = px[x, y - 1]
            r1, g1, b1, a1 = px[x, y]
            if a0 != a1:
                rows[y] += 2.0
            elif a1:
                d = abs(r1 - r0) + abs(g1 - g0) + abs(b1 - b0)
                if d > 24:
                    rows[y] += 1.0
    return cols, rows


def _fit_comb(signal: list[float]) -> tuple[float, float, float]:
    """Best (period, phase, score) for a 1-D edge profile.

    Score is the share of total edge energy that falls within one pixel of a comb
    tooth, normalised by how much of the axis the comb covers — otherwise a tiny
    period wins by covering everything.
    """
    n = len(signal)
    total = sum(signal) or 1.0
    best = (PERIOD_MIN, 0.0, 0.0)
    period = PERIOD_MIN
    while period <= PERIOD_MAX:
        for step in range(PHASE_STEPS):
            phase = period * step / PHASE_STEPS
            hit = 0.0
            pos = phase
            while pos < n:
                i = int(round(pos))
                for j in (i - 1, i, i + 1):
                    if 0 <= j < n:
                        hit += signal[j]
                pos += period
            # Teeth cover 3px each; normalise so long and short periods compare.
            coverage = (n / period) * 3.0 / n
            score = (hit / total) / max(coverage, 1e-6)
            if score > best[2]:
                best = (period, phase, score)
        period += PERIOD_STEP
    return best


# ── snapping ─────────────────────────────────────────────────────────────────

def _cell_colour(px, x0: int, y0: int, x1: int, y1: int):
    """The commonest opaque colour in a cell, or None if the cell is empty."""
    counts: Counter = Counter()
    opaque = 0
    total = 0
    for y in range(y0, y1):
        for x in range(x0, x1):
            r, g, b, a = px[x, y]
            total += 1
            if a >= 128:
                opaque += 1
                counts[(r, g, b)] += 1
    if not total or opaque * 2 < total:
        return None
    return counts.most_common(1)[0][0]


def _fit_phase(signal: list[float], period: float) -> float:
    """Best phase for a period we already know. Cheap — used when a frame's
    period has been corrected to match its character and only the offset is
    still in question."""
    n = len(signal)
    best, best_hit = 0.0, -1.0
    for step in range(PHASE_STEPS * 2):
        phase = period * step / (PHASE_STEPS * 2)
        hit, pos = 0.0, phase
        while pos < n:
            i = int(round(pos))
            for j in (i - 1, i, i + 1):
                if 0 <= j < n:
                    hit += signal[j]
            pos += period
        if hit > best_hit:
            best, best_hit = phase, hit
    return best


def fit_frame(img: Image.Image) -> dict:
    """Lattice measurement for one frame, kept separate from snapping so a
    character's frames can be reconciled against each other first."""
    cols, rows = _edge_signal(img)
    px_period, px_phase, sx = _fit_comb(cols)
    py_period, py_phase, sy = _fit_comb(rows)
    return {"cols": cols, "rows": rows,
            "px": px_period, "py": py_period,
            "phx": px_phase, "phy": py_phase,
            "score": min(sx, sy)}


def reconcile_harmonics(fits: list[dict], tol: float = 0.35) -> None:
    """Pull each frame's period onto the same harmonic as its character's.

    A comb scores almost as well at half or double the true period, so frames of
    one character can fit different multiples of the same lattice — a finely
    drawn one landed on 10.67 and 21.3 in the same animation, which made its
    shared canvas twice as tall as its art. Doubling or halving until every
    frame sits in one band fixes that **without** flattening a genuine scale
    change: an alert cycle really rendered at 20.2 against its siblings' 22.3
    is 10% off, nowhere near a harmonic, so it survives untouched.
    """
    for axis in ("px", "py"):
        values = sorted(f[axis] for f in fits)
        median = values[len(values) // 2]
        for f in fits:
            p = f[axis]
            while p < median * (1.0 - tol):
                p *= 2.0
            while p > median * (1.0 + tol) * 1.5:
                p /= 2.0
            if abs(p - f[axis]) > 1e-9:
                f[axis] = p
                # The phase belonged to the old period; it has to be refound.
                f["phx" if axis == "px" else "phy"] = _fit_phase(
                    f["cols" if axis == "px" else "rows"], p)


def snap_frame(img: Image.Image, fit: dict, cells_per_px: int = 1):
    """One output pixel per lattice cell (or per `cells_per_px` cells)."""
    px_period = fit["px"] * cells_per_px
    py_period = fit["py"] * cells_per_px
    px_phase, py_phase = fit["phx"], fit["phy"]

    w, h = img.size
    px = img.load()
    # Start at the first whole cell at or before the phase, so the lattice covers
    # the image rather than starting inside it.
    x_start = px_phase - px_period * int(px_phase / px_period + 1)
    y_start = py_phase - py_period * int(py_phase / py_period + 1)
    nx = int((w - x_start) / px_period) + 1
    ny = int((h - y_start) / py_period) + 1

    out = Image.new("RGBA", (nx, ny), (0, 0, 0, 0))
    dst = out.load()
    inset_x = px_period * (1.0 - CELL_CORE) / 2.0
    inset_y = py_period * (1.0 - CELL_CORE) / 2.0
    for j in range(ny):
        cy = y_start + j * py_period
        y0 = max(0, int(round(cy + inset_y)))
        y1 = min(h, max(y0 + 1, int(round(cy + py_period - inset_y))))
        if y0 >= h:
            continue
        for i in range(nx):
            cx = x_start + i * px_period
            x0 = max(0, int(round(cx + inset_x)))
            x1 = min(w, max(x0 + 1, int(round(cx + px_period - inset_x))))
            if x0 >= w:
                continue
            colour = _cell_colour(px, x0, y0, x1, y1)
            if colour is not None:
                dst[i, j] = (*colour, 255)
    return out, (px_period, py_period, fit["score"])


def _silhouette_white_fraction(img: Image.Image, floor: int) -> float:
    """Share of the 4-connected silhouette whose min channel is >= `floor`."""
    w, h = img.size
    px = img.load()
    sil = white = 0
    for y in range(h):
        for x in range(w):
            r, g, b, a = px[x, y]
            if a < 128:
                continue
            edge = False
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nx_, ny_ = x + dx, y + dy
                if not (0 <= nx_ < w and 0 <= ny_ < h) or px[nx_, ny_][3] < 128:
                    edge = True
                    break
            if not edge:
                continue
            sil += 1
            if min(r, g, b) >= floor:
                white += 1
    return (white / sil) if sil else 0.0


def strip_outline(img: Image.Image) -> Image.Image:
    """Remove a white sticker ring when the frame has one.

    Two peels from the outside left a 2–3 cell ring on a finer lattice (a 16px
    source stroke spans more output cells there) and a trapped blob in hair
    concavities. Flood from the exterior through near-white instead. Interior
    whites — eyes, teeth, a pale shirt — never touch the outside, so they stay.

    The flood is gated: a real ring is most of the silhouette in near-pure
    white. A character drawn without one has none of that, and an off-white
    jacket (min channel ~236) would be eaten if we always flooded.
    """
    if _silhouette_white_fraction(img, STICKER_WHITE) < RING_FRACTION:
        return img
    w, h = img.size
    px = img.load()
    seen = bytearray(w * h)
    q: deque[tuple[int, int]] = deque()
    for x in range(w):
        q.append((x, 0))
        q.append((x, h - 1))
    for y in range(h):
        q.append((0, y))
        q.append((w - 1, y))
    while q:
        x, y = q.popleft()
        if not (0 <= x < w and 0 <= y < h):
            continue
        i = y * w + x
        if seen[i]:
            continue
        r, g, b, a = px[x, y]
        white = a > 0 and min(r, g, b) >= WHITE_MIN
        if a >= 128 and not white:
            continue
        seen[i] = 1
        if white:
            px[x, y] = (0, 0, 0, 0)
        q.append((x - 1, y))
        q.append((x + 1, y))
        q.append((x, y - 1))
        q.append((x, y + 1))
    return img


def despeckle(img: Image.Image) -> Image.Image:
    """Drop pixels with almost nothing attached to them.

    Stripping the sticker outline leaves crumbs: where the white border met the
    character it is eaten from the outside in, and single cells survive floating
    beside the head. They read as dirt on the screen at any size. A pixel with at
    most one opaque neighbour is not part of a drawing — the art has no detail
    that fine, since every mark is at least one lattice cell wide.
    """
    w, h = img.size
    px = img.load()
    doomed = []
    for y in range(h):
        for x in range(w):
            if not px[x, y][3]:
                continue
            neighbours = 0
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nx_, ny_ = x + dx, y + dy
                if 0 <= nx_ < w and 0 <= ny_ < h and px[nx_, ny_][3]:
                    neighbours += 1
            if neighbours <= 1:
                doomed.append((x, y))
    for x, y in doomed:
        px[x, y] = (0, 0, 0, 0)
    return img


def _union_box(frames: list[Image.Image]) -> tuple[int, int, int, int]:
    boxes = [f.getbbox() for f in frames]
    boxes = [b for b in boxes if b]
    if not boxes:
        return (0, 0, 1, 1)
    return (min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes))


def _place(frames: list[Image.Image], size: tuple[int, int] | None = None
           ) -> list[Image.Image]:
    """Crop every frame to one canvas, bottom-centre aligned.

    Bottom rather than top because the characters stand on their feet, and a
    frame that grows upward — the alert hop, the beacon above the head — must
    push out of the top rather than lift them off the floor.

    `size` is passed when the canvas has to span more than these frames: all
    three states of a character share one, so switching state never resizes the
    sprite. Without that, an agent going idle would visibly shrink.
    """
    if size is None:
        box = _union_box(frames)
        size = (box[2] - box[0], box[3] - box[1])
    cw, ch = size
    out = []
    for f in frames:
        b = f.getbbox() or (0, 0, 1, 1)
        piece = f.crop(b)
        canvas = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
        canvas.paste(piece, ((cw - piece.width) // 2, ch - piece.height), piece)
        out.append(canvas)
    return out


# ── driver ───────────────────────────────────────────────────────────────────

def _pink_count(img: Image.Image) -> int:
    """Hot magenta fringe, not near-black hair with a purple JPEG cast."""
    import colorsys
    px = img.load()
    w, h = img.size
    n = 0
    for y in range(h):
        for x in range(w):
            r, g, b, a = px[x, y]
            if a < 16 or (r > 240 and g > 240 and b > 240):
                continue
            hv, s, v = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)
            if 275.0 <= hv * 360.0 <= 335.0 and s >= 0.20 and v >= 0.30:
                n += 1
    return n


def _figure_height(img: Image.Image) -> int:
    """Opaque bbox height with cyan motes ignored."""
    px = img.load()
    w, h = img.size
    ys = []
    for y in range(h):
        for x in range(w):
            r, g, b, a = px[x, y]
            if a < 16:
                continue
            if abs(r - MOTE[0]) < 20 and abs(g - MOTE[1]) < 20 and abs(b - MOTE[2]) < 20:
                continue
            ys.append(y)
            break
    if not ys:
        return 0
    return max(ys) - min(ys) + 1


def ingest(src: Path, out: Path, cells: dict[str, int], scale: int,
           keep_outline: bool, only: list[str] | None = None,
           fail_under: float | None = None) -> dict:
    existing = out / "manifest.json"
    if only and existing.is_file():
        manifest = json.loads(existing.read_text())
        manifest.setdefault("cast", [])
        manifest.setdefault("animations", {})
        manifest["states"] = list(STATES)
        manifest["scale"] = scale
    else:
        manifest = {"cast": [], "states": list(STATES), "scale": scale,
                    "animations": {}}
    chars = list(only) if only else list(CAST)
    failed: list[str] = []
    for char in chars:
        if char not in manifest["cast"]:
            # Keep CAST order: rebuild the prefix we know about.
            ordered = [c for c in CAST if c in set(manifest["cast"]) | {char}]
            extras = [c for c in manifest["cast"] if c not in CAST]
            manifest["cast"] = ordered + extras

        # Snap every state first: the canvas has to span all three, so nothing
        # can be cropped until all of them are known.
        by_state: dict[str, dict] = {}
        for state in STATES:
            gif = src / f"{char}{_SUFFIX[state]}.gif"
            if not gif.is_file():
                print(f"  !! missing {gif.name}", file=sys.stderr)
                continue
            im = Image.open(gif)
            raw, holds = [], []
            for frame in ImageSequence.Iterator(im):
                raw.append(frame.convert("RGBA"))
                holds.append(int(frame.info.get("duration") or 200))

            by_state[state] = {"raw": raw, "holds": holds,
                               "fits": [fit_frame(f) for f in raw]}

        if not by_state:
            continue

        # One character, one lattice band — across all three states, since they
        # were rendered in one batch and share a canvas downstream.
        reconcile_harmonics([f for st in by_state.values() for f in st["fits"]])
        for state, data in by_state.items():
            snapped, measured = [], []
            for frame, fit in zip(data["raw"], data["fits"]):
                img, m = snap_frame(frame, fit, cells.get(char, 1))
                if not keep_outline:
                    img = despeckle(strip_outline(img))
                snapped.append(img)
                measured.append(m)
            data["frames"] = snapped
            data["measured"] = measured
        box = _union_box([f for st in by_state.values() for f in st["frames"]])
        canvas = (box[2] - box[0], box[3] - box[1])

        for state, data in by_state.items():
            placed = _place(data["frames"], canvas)
            if scale > 1:
                placed = [f.resize((f.width * scale, f.height * scale),
                                   Image.NEAREST) for f in placed]

            key = f"{char}-{state}"
            dest = out / key
            dest.mkdir(parents=True, exist_ok=True)
            for old in dest.glob("frame_*.png"):
                old.unlink()
            for i, f in enumerate(placed):
                f.save(dest / f"frame_{i:02d}.png")

            fits = data["measured"]
            hold = round(sum(data["holds"]) / len(data["holds"]))
            manifest["animations"][key] = {
                "frames": len(placed),
                "width": placed[0].width, "height": placed[0].height,
                "frame_ms": hold, "holds": data["holds"],
                "period": [round(fits[0][0], 2), round(fits[0][1], 2)],
                "fit_score": round(min(f[2] for f in fits), 2),
            }
            print(f"  {key:18s} {len(placed)} frames  "
                  f"{placed[0].width}x{placed[0].height}  {hold}ms  "
                  f"grid {fits[0][0]:.1f}x{fits[0][1]:.1f}  "
                  f"fit {min(f[2] for f in fits):.2f}")

            pink = _pink_count(placed[0])
            if pink:
                msg = f"{key}: {pink} magenta-hue pixels"
                if char in NEW_SLUGS:
                    print(f"  FAIL {msg}", file=sys.stderr)
                    failed.append(msg)
                else:
                    print(f"  WARN {msg}")

        work0 = by_state.get("work", {}).get("frames", [None])[0]
        if work0 is not None:
            fig_h = _figure_height(work0)
            ratio = fig_h / canvas[1] if canvas[1] else 0.0
            print(f"  {char:18s} canvas-ratio {ratio:.2f} "
                  f"(figure {fig_h} / canvas {canvas[1]})")
            if char in NEW_SLUGS and ratio < CANVAS_RATIO_MIN:
                msg = f"{char}: canvas-ratio {ratio:.2f} < {CANVAS_RATIO_MIN}"
                print(f"  FAIL {msg}", file=sys.stderr)
                failed.append(msg)
            elif char in HOUSE_SLUGS and ratio < CANVAS_RATIO_MIN:
                print(f"  WARN {char}: canvas-ratio {ratio:.2f} (allow-listed)")

        if fail_under is not None:
            for state, data in by_state.items():
                score = min(m[2] for m in data["measured"])
                if score < fail_under:
                    msg = f"{char}-{state}: fit {score:.2f} < {fail_under}"
                    if char in NEW_SLUGS:
                        print(f"  FAIL {msg}", file=sys.stderr)
                        failed.append(msg)
                    else:
                        print(f"  WARN {msg} (allow-listed)")

    if failed:
        raise SystemExit("ingest gates failed:\n  " + "\n  ".join(failed))
    return manifest


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("src", type=Path, help="directory of <char>[-state].gif")
    ap.add_argument("out", type=Path, help="output directory for frame folders")
    ap.add_argument("--cells", action="append", default=[], metavar="CHAR=N",
                    help="lattice cells per output pixel (default 1). "
                         "For art drawn at twice the set's density, "
                         "--cells <slug>=2 matches it to the rest.")
    ap.add_argument("--scale", type=int, default=1,
                    help="integer upscale of the output (default 1 = native)")
    ap.add_argument("--keep-outline", action="store_true",
                    help="keep the white sticker border (default: strip it)")
    ap.add_argument("--only", action="append", default=[],
                    help="ingest only these slugs (surgical; merges into "
                         "an existing manifest). Repeatable.")
    ap.add_argument("--fail-under", type=float, default=None,
                    help="fail new slugs whose fit_score is below this "
                         f"(recommended {FIT_FLOOR})")
    args = ap.parse_args()

    cells: dict[str, int] = dict(DEFAULT_CELLS)
    for item in args.cells:
        name, _, value = item.partition("=")
        cells[name.strip()] = int(value)

    if not args.src.is_dir():
        sys.exit(f"error: {args.src} is not a directory")
    args.out.mkdir(parents=True, exist_ok=True)

    only = args.only or None
    n = len(only) if only else len(CAST)
    print(f"==> Ingesting {n} characters x {len(STATES)} states")
    manifest = ingest(args.src, args.out, cells, args.scale, args.keep_outline,
                      only=only, fail_under=args.fail_under)
    (args.out / "manifest.json").write_text(json.dumps(manifest, indent=1))
    print(f"==> Wrote {args.out}/manifest.json")


if __name__ == "__main__":
    main()
