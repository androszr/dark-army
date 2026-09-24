#!/usr/bin/env python3
"""Normalise the cast's photo portraits and mirror them into both clients.

The panel and the phone draw **still photographs** of the cast; the menu-bar
strip keeps its hand-drawn pixel art (`tools/pixelgrid_ingest.py`). This is
the photo tree's one tool.

    python tools/portrait_ingest.py assets/proposals/dark-army-cast-rebrand/ingest assets/portraits
    python tools/portrait_ingest.py --check

**Ingest.** For every slug in the roster (`identity.NAMES` lowercased plus
`identity.ART_ONLY`, read out of `identity.py` itself so this file never
restates the cast) it looks for ``<slug>.png`` / ``.jpg`` / ``.jpeg`` in the
source folder, centre-crops to a square, resizes to ``PORTRAIT_PX`` with a
high-quality filter, and writes an sRGB PNG to ``<out>/<slug>.png`` —
true-colour where that fits the cap, else a dithered 256-colour palette,
which halves a photograph and is not told apart at avatar size. A file
that is still above ``MAX_PORTRAIT_BYTES`` is **refused in words** rather
than shipped — fifteen of them ride inside two app bundles. A slug with no
source file is reported and skipped, never fatal: the tree is allowed to be
incomplete, and both clients draw the character's initial until it fills in.
Then it writes ``<out>/manifest.json`` (schema, present slugs, per-slug
width / height / bytes / sha256 — no ``frame_ms``, no ``holds``, no
``states``; nothing at runtime reads it) and mirrors the whole tree into
``panel/Sources/BobPanel/Resources/portraits/`` and
``ios/BobPhone/Resources/portraits/``.

**Check.** ``--check`` writes nothing. It verifies that the three copies hold
the same files byte for byte, that every roster slug is present in all three
or in none, that nothing outside the roster is in any of them, and that each
manifest describes exactly the files beside it. It is the seam
``host/tests/test_portrait_tree.py`` runs, and it needs nothing but the
standard library — Pillow is imported only for an ingest.

Nothing here downloads anything. Choosing the pictures is the person's step.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import io
import json
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
IDENTITY = REPO / "host" / "dark_army_daemon" / "identity.py"
SOURCE_TREE = REPO / "assets" / "portraits"
CLIENT_TREES = (
    REPO / "panel" / "Sources" / "BobPanel" / "Resources" / "portraits",
    REPO / "ios" / "BobPhone" / "Resources" / "portraits",
)
MANIFEST = "manifest.json"
SCHEMA = 1

PORTRAIT_PX = 512
#: Hard cap per portrait. Raised from 400 KB on 6 Sep 2026 so a true-colour
#: photograph at 512px (~450-480 KB) ships without the palette rung; fifteen
#: at this cap is ≤ ~9 MB worst case (~6.3 MB as shipped), paid twice — the
#: .app through the panel's SwiftPM bundle and the iOS app.
MAX_PORTRAIT_BYTES = 600_000
SOURCE_SUFFIXES = (".png", ".jpg", ".jpeg")


# ── the roster ───────────────────────────────────────────────────────────────

def roster() -> list[str]:
    """Every slug that may have a portrait: `NAMES` lowercased, then `ART_ONLY`.

    Parsed out of `identity.py` rather than imported, so a tool never pulls
    the daemon package (and its state directory) into a build step.
    """
    names: list[str] = []
    art_only: list[str] = []
    for node in ast.parse(IDENTITY.read_text(encoding="utf-8")).body:
        targets = []
        if isinstance(node, ast.Assign):
            targets = [getattr(t, "id", None) for t in node.targets]
        elif isinstance(node, ast.AnnAssign):
            targets = [getattr(node.target, "id", None)]
        if "NAMES" in targets:
            names = [str(n).lower() for n in ast.literal_eval(node.value)]
        elif "ART_ONLY" in targets:
            art_only = [str(n).lower() for n in ast.literal_eval(node.value)]
    if not names:
        sys.exit(f"error: could not read NAMES out of {IDENTITY}")
    return names + art_only


# ── one tree ─────────────────────────────────────────────────────────────────

def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _rel(path: Path) -> str:
    """A tree named relative to the repo where it is inside it, whole otherwise
    (`check()` is also run over temporary trees by the tests)."""
    try:
        return str(path.relative_to(REPO))
    except ValueError:
        return str(path)


def _png_size(data: bytes) -> tuple[int, int]:
    """Width and height out of a PNG's IHDR — stdlib only, for `--check`."""
    if data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        return (0, 0)
    return (int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big"))


def portraits_in(tree: Path) -> dict[str, Path]:
    """``slug -> file`` for every ``<slug>.png`` in a tree; empty if absent."""
    if not tree.is_dir():
        return {}
    return {p.stem: p for p in sorted(tree.glob("*.png"))}


def build_manifest(tree: Path, order: list[str]) -> dict:
    present = portraits_in(tree)
    cast = [s for s in order if s in present] + sorted(s for s in present if s not in order)
    entries = {}
    for slug in cast:
        data = present[slug].read_bytes()
        w, h = _png_size(data)
        entries[slug] = {"width": w, "height": h, "bytes": len(data),
                         "sha256": hashlib.sha256(data).hexdigest()}
    return {"schema": SCHEMA, "cast": cast, "portraits": entries}


def write_manifest(tree: Path, order: list[str]) -> dict:
    manifest = build_manifest(tree, order)
    (tree / MANIFEST).write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n",
                                 encoding="utf-8")
    return manifest


# ── ingest ───────────────────────────────────────────────────────────────────

def _source_for(src: Path, slug: str) -> Path | None:
    for suffix in SOURCE_SUFFIXES:
        cand = src / f"{slug}{suffix}"
        if cand.is_file():
            return cand
    return None


def normalise(source: Path) -> bytes:
    """Centre-crop to a square, resize to `PORTRAIT_PX`, encode as sRGB PNG."""
    from PIL import Image, ImageOps

    with Image.open(source) as im:
        im = ImageOps.exif_transpose(im)
        if im.mode in ("RGBA", "LA", "P"):
            im = im.convert("RGBA")
            flat = Image.new("RGBA", im.size, (0, 0, 0, 255))
            flat.alpha_composite(im)
            im = flat
        im = im.convert("RGB")
        side = min(im.size)
        left = (im.width - side) // 2
        top = (im.height - side) // 2
        im = im.crop((left, top, left + side, top + side))
        im = im.resize((PORTRAIT_PX, PORTRAIT_PX), Image.LANCZOS)
        data = _encode(im)
        if len(data) > MAX_PORTRAIT_BYTES:
            # A photograph at 512px is routinely ~450 KB as true-colour PNG.
            # A dithered 256-colour palette halves that and is not told apart
            # from the original at the sizes the panel and the phone draw an
            # avatar (well under 100pt). Still over the cap after this rung
            # is refused in words by the caller.
            data = _encode(im.quantize(colors=256,
                                       method=Image.Quantize.MEDIANCUT,
                                       dither=Image.Dither.FLOYDSTEINBERG))
    return data


def _encode(im) -> bytes:
    buf = io.BytesIO()
    im.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def mirror(src_tree: Path, dest: Path) -> None:
    """Make `dest` a byte-for-byte copy of `src_tree` (portraits + manifest)."""
    dest.mkdir(parents=True, exist_ok=True)
    keep = {p.name for p in src_tree.glob("*.png")} | {MANIFEST}
    for stale in dest.iterdir():
        if stale.name not in keep and (stale.suffix == ".png" or stale.name == MANIFEST):
            stale.unlink()
    for name in sorted(keep):
        if (src_tree / name).is_file():
            shutil.copyfile(src_tree / name, dest / name)


def ingest(src: Path, out: Path) -> int:
    order = roster()
    out.mkdir(parents=True, exist_ok=True)
    refused: list[str] = []
    for slug in order:
        source = _source_for(src, slug)
        if source is None:
            state = "kept" if (out / f"{slug}.png").is_file() else "none yet"
            print(f"  skip {slug:10s} no source in {src} ({state})")
            continue
        data = normalise(source)
        if len(data) > MAX_PORTRAIT_BYTES:
            msg = (f"{slug}: {len(data):,} bytes after normalising is over the "
                   f"{MAX_PORTRAIT_BYTES:,}-byte cap — simplify the picture "
                   f"(a flatter background or a tighter crop) and drop it in again")
            print(f"  FAIL {msg}", file=sys.stderr)
            refused.append(msg)
            continue
        (out / f"{slug}.png").write_bytes(data)
        print(f"  ok   {slug:10s} {PORTRAIT_PX}x{PORTRAIT_PX}  {len(data):,} bytes")
    # Anything in the tree that is not a roster slug is a stray, not a portrait.
    for slug, path in portraits_in(out).items():
        if slug not in order:
            print(f"  drop {slug:10s} not in the roster")
            path.unlink()
    manifest = write_manifest(out, order)
    print(f"==> {out}/{MANIFEST}: {len(manifest['cast'])} of {len(order)} portraits")
    for dest in CLIENT_TREES:
        mirror(out, dest)
        print(f"==> mirrored into {_rel(dest)}")
    if refused:
        print("portraits refused:\n  " + "\n  ".join(refused), file=sys.stderr)
        return 1
    return 0


# ── check ────────────────────────────────────────────────────────────────────

def check(trees: tuple[Path, ...] = (SOURCE_TREE, *CLIENT_TREES),
          order: list[str] | None = None) -> list[str]:
    """Every way the three copies can disagree, in words. Empty means fine."""
    order = roster() if order is None else order
    problems: list[str] = []
    files = {tree: portraits_in(tree) for tree in trees}
    names = {tree: set(present) for tree, present in files.items()}
    union = set().union(*names.values())

    for slug in sorted(union):
        holders = [tree for tree in trees if slug in names[tree]]
        if slug not in order:
            problems.append(f"{slug}: not in the roster, present in "
                            + ", ".join(_rel(t) for t in holders))
        if len(holders) != len(trees):
            missing = [t for t in trees if slug not in names[t]]
            problems.append(f"{slug}: present in "
                            + ", ".join(_rel(t) for t in holders)
                            + " but missing from "
                            + ", ".join(_rel(t) for t in missing))
            continue
        digests = {_sha256(files[tree][slug]) for tree in trees}
        if len(digests) != 1:
            problems.append(f"{slug}: the copies are not byte-identical")
        data = files[trees[0]][slug].read_bytes()
        if len(data) > MAX_PORTRAIT_BYTES:
            problems.append(f"{slug}: {len(data):,} bytes is over the cap")
        if _png_size(data) != (PORTRAIT_PX, PORTRAIT_PX):
            problems.append(f"{slug}: not {PORTRAIT_PX}x{PORTRAIT_PX} "
                            f"(got {_png_size(data)})")

    for tree in trees:
        path = tree / MANIFEST
        expected = build_manifest(tree, order)
        if not path.is_file():
            if names[tree]:
                problems.append(f"{_rel(tree)}: portraits but no {MANIFEST}")
            continue
        try:
            actual = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            problems.append(f"{_rel(tree)}/{MANIFEST}: not JSON ({exc})")
            continue
        if actual != expected:
            problems.append(f"{_rel(tree)}/{MANIFEST} does not describe "
                            f"the files beside it — re-run the ingest")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("src", nargs="?", type=Path, help="folder of <slug>.png/.jpg/.jpeg")
    ap.add_argument("out", nargs="?", type=Path, help="the shipped tree (assets/portraits)")
    ap.add_argument("--check", action="store_true",
                    help="verify the three copies agree; writes nothing")
    args = ap.parse_args()
    if args.check:
        if args.src or args.out:
            ap.error("--check takes no folders")
        problems = check()
        for line in problems:
            print(f"  {line}", file=sys.stderr)
        print("==> portraits: " + ("in step" if not problems else f"{len(problems)} problem(s)"))
        return 1 if problems else 0
    if not args.src or not args.out:
        ap.error("an ingest needs both folders: <src> <out>")
    if not args.src.is_dir():
        sys.exit(f"error: {args.src} is not a directory")
    return ingest(args.src, args.out)


if __name__ == "__main__":
    sys.exit(main())
