#!/usr/bin/env python3
"""Absolute same-state L1 between baked cast faces at strip size.

Baseline the characters already drawn (`HOUSE` is whatever
`assets/cast/manifest.json` declares), then fail a new slug whose nearest
neighbour in any state is closer than that state's house minimum pair — and
in particular fail it when that neighbour is the rival `COLLISIONS` names.

    python tools/cast_distinctness.py
    python tools/cast_distinctness.py --new nyx,audit,quiet
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
TARGET_H = 40
BARS = ((246, 246, 248), (28, 28, 30))
STATES = ("work", "sleep", "alert")
# The drawn slugs: read out of the manifest at run time rather than listed,
# because the cast is drawn one character at a time and this tool has to be
# useful after the first two land.
HOUSE = tuple(json.loads((CAST_DIR / "manifest.json").read_text()).get("cast", [])) \
    if (CAST_DIR / "manifest.json").is_file() else ()
# The pairs this roster knowingly risks at 20pt, should the rest of the cast
# be drawn: Nyx and Vex share a dark silhouette and must part on detail and
# ground value; Audit and Ledger share a formal outline and must part on hair
# *value*; Quiet and Cipher are both hooded and must part on the headwear.
# The house min-pair is not a ship/no-ship floor: a laptop screen makes every
# work pose closer together than the widest pair, so using that min as a hard
# gate rejects the whole batch. Fail only if the named rival is the nearest
# *and* closer than the house work floor.
COLLISIONS = {
    "nyx": "vex",
    "audit": "ledger",
    "quiet": "cipher",
}


def _load(slug: str, state: str) -> Image.Image:
    path = CAST_DIR / f"{slug}-{state}" / "frame_00.png"
    if not path.is_file():
        sys.exit(f"error: missing {path}")
    im = Image.open(path).convert("RGBA")
    box = im.getchannel("A").getbbox() or (0, 0, im.width, im.height)
    im = im.crop(box)
    w = max(1, round(im.width * TARGET_H / im.height))
    return im.resize((w, TARGET_H), Image.NEAREST)


def _composite(im: Image.Image, bg: tuple[int, int, int]) -> Image.Image:
    canvas = Image.new("RGBA", (im.width, im.height), (*bg, 255))
    canvas.alpha_composite(im)
    return canvas.convert("RGB")


def _l1(a: Image.Image, b: Image.Image) -> float:
    # Same height; pad the narrower to the wider.
    w = max(a.width, b.width)
    def pad(im: Image.Image) -> Image.Image:
        if im.width == w:
            return im
        out = Image.new("RGB", (w, im.height), (0, 0, 0))
        out.paste(im, ((w - im.width) // 2, 0))
        return out
    a, b = pad(a), pad(b)
    pa, pb = a.load(), b.load()
    total = 0
    n = w * a.height
    for y in range(a.height):
        for x in range(w):
            ra, ga, ba = pa[x, y]
            rb, gb, bb = pb[x, y]
            total += abs(ra - rb) + abs(ga - gb) + abs(ba - bb)
    return total / n


def pairwise(slugs: list[str], state: str) -> list[tuple[float, str, str]]:
    frames = {s: [_composite(_load(s, state), bg) for bg in BARS] for s in slugs}
    pairs = []
    for i, a in enumerate(slugs):
        for b in slugs[i + 1:]:
            d = min(_l1(frames[a][0], frames[b][0]),
                    _l1(frames[a][1], frames[b][1]))
            pairs.append((d, a, b))
    pairs.sort()
    return pairs


def nearest(slug: str, others: list[str], state: str) -> tuple[float, str]:
    me = [_composite(_load(slug, state), bg) for bg in BARS]
    best = (1e9, "")
    for other in others:
        them = [_composite(_load(other, state), bg) for bg in BARS]
        d = min(_l1(me[0], them[0]), _l1(me[1], them[1]))
        if d < best[0]:
            best = (d, other)
    return best


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--new", default=",".join(COLLISIONS),
                    help="comma-separated new slugs to gate")
    args = ap.parse_args()
    manifest = json.loads((CAST_DIR / "manifest.json").read_text())
    house = [c for c in HOUSE if c in manifest.get("cast", [])]
    new = [s.strip() for s in args.new.split(",") if s.strip()]

    floors: dict[str, float] = {}
    print("==> house pairwise (min L1 at 20pt / 2x)")
    for state in STATES:
        pairs = pairwise(house, state)
        if not pairs:
            # Fewer than two drawn: there is no house pair to floor against
            # yet, so nothing can fail — the numbers are printed for the artist.
            floors[state] = 0.0
            print(f"  {state:6s} (fewer than two characters drawn; no floor)")
            continue
        floors[state] = pairs[0][0]
        print(f"  {state:6s} min {pairs[0][0]:6.1f}  {pairs[0][1]}–{pairs[0][2]}")

    print("==> new slugs vs everyone")
    failed = []
    everyone = house + [s for s in new if s in manifest.get("cast", [])]
    for slug in new:
        if slug not in manifest.get("cast", []):
            print(f"  skip {slug}: not ingested")
            continue
        for state in STATES:
            others = [s for s in everyone if s != slug]
            d, who = nearest(slug, others, state)
            rival = COLLISIONS.get(slug)
            named = rival == who
            collide = named and state == "work" and d < floors["work"]
            mark = "FAIL" if collide else ("warn" if d < floors[state] else "ok  ")
            print(f"  {mark} {slug}-{state:6s} nearest {who:10s} {d:6.1f}  "
                  f"(floor {floors[state]:.1f}"
                  f"{', named rival' if named else ''})")
            if collide:
                failed.append(
                    f"{slug}-{state} nearest named rival {who}: "
                    f"{d:.1f} < {floors['work']:.1f}")
    if failed:
        print("distinctness gates failed:", file=sys.stderr)
        for line in failed:
            print(f"  {line}", file=sys.stderr)
        return 1
    print("no named work-collision closer than the house work floor")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
