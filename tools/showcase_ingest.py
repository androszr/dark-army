"""The README's four showcase slides, rendered a second time from the
git-ignored slide deck with the personal footer line switched off.

    host/.venv/bin/python tools/showcase_ingest.py           # render, shrink, write
    host/.venv/bin/python tools/showcase_ingest.py --check   # verify the committed four

The deck lives under `user-data/readme-showcase/slides/` (git-ignored): the
Signal redraw of the Reddit deck, every app picture in it a real render of the
demo day (`docs/images/SHOTS.md`, *The showcase slides*). Its
own renders (`render.sh`, 2160 px, footer and all) are never touched: this
tool writes a temporary `readme-<slide>.html` beside each chosen slide with
one `<style>` override injected before `</head>` — the footer's
`.foot .brand::after` line, which carries the author's name and the flag, set
to `content: none` — renders it with headless Chrome using exactly
`render.sh`'s flags, deletes the temporary file, then shrinks the render to
1200 px, quantises it to a palette PNG under 300 KiB and strips every chunk a
picture does not need. The `readme-` prefix keeps the temporary copy outside
`render.sh`'s `slide-*` glob.

`--check` needs no Chrome and no deck: it re-verifies the committed pictures
(the set of four, width, even size, byte cap, chunks, and no flag red in the
footer band) and exits non-zero on any finding. The contract is
`docs/images/SHOTS.md`, *The showcase slides*; the pins are
`host/tests/test_showcase_images.py`.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from demo_shots import KEEP_CHUNKS as ALLOWED_CHUNKS  # noqa: E402  (needs the sys.path line above)
from demo_shots import _even, _strip_chunks, png_chunks  # noqa: E402  (same)

REPO = Path(__file__).resolve().parents[1]

SLIDES = {
    "slide-02-hero": "hero-mission-control.png",
    "slide-03-problem": "five-terminals-one-of-you.png",
    "slide-06-journey": "write-it-once.png",
    "slide-12-iphone": "walk-away-from-the-desk.png",
}
WIDTH = 1200
MAX_BYTES = 300 * 1024
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
README_OVERRIDE = "<style>.foot .brand::after { content: none !important; }</style>"
FOOTER_SELECTOR = ".foot .brand::after"
COLOUR_STEPS = (256, 224, 192, 160, 128)

# The footer band: the bottom 7 % of the picture, left half. The flag was the
# only red there; the slides' legitimate reds sit above it or on the right.
BAND_TOP = 0.93


class ShowcaseError(RuntimeError):
    pass


def flag_pixels(path: Path) -> int:
    """How many flag-red pixels sit in the footer band's left half."""
    from PIL import Image

    with Image.open(path) as opened:
        img = opened.convert("RGB")
    w, h = img.size
    count = 0
    for y in range(int(BAND_TOP * h), h):
        for x in range(0, w // 2):
            if is_flag_red(img.getpixel((x, y))):
                count += 1
    return count


def is_flag_red(rgb: tuple[int, int, int]) -> bool:
    """Red by dominance, not by level: the shrink and the palette pull the
    flag's red down to about (143, 36, 29) on the hero slide, below any fixed
    brightness floor worth trusting."""
    r, g, b = rgb[:3]
    return r >= 110 and r - max(g, b) >= 70


def _colours(path: Path) -> int:
    from PIL import Image

    with Image.open(path) as img:
        colours = img.getcolors(maxcolors=1 << 24)
    return len(colours) if colours else 0


def verify(out: Path) -> tuple[list[str], list[str]]:
    """Return (report lines, findings) for the committed pictures in `out`."""
    from PIL import Image

    lines: list[str] = []
    findings: list[str] = []
    present = {p.name for p in out.glob("*.png")} if out.is_dir() else set()
    expected = set(SLIDES.values())
    if present != expected:
        missing = sorted(expected - present)
        extra = sorted(present - expected)
        findings.append(f"{out}: expected exactly the four pictures "
                        f"(missing {missing}, unexpected {extra})")
    for name in sorted(expected & present):
        path = out / name
        with Image.open(path) as img:
            w, h = img.size
        size = path.stat().st_size
        if w != WIDTH:
            findings.append(f"{name} is {w} px wide, not {WIDTH}")
        if w % 2 or h % 2:
            findings.append(f"{name} is {w}x{h}, not even-sized")
        if size > MAX_BYTES:
            findings.append(f"{name} is {size // 1024} KiB, over {MAX_BYTES // 1024} KiB")
        stray = sorted({k.decode("latin-1") for k in png_chunks(path)} -
                       {k.decode("latin-1") for k in ALLOWED_CHUNKS})
        if stray:
            findings.append(f"{name} carries chunks outside the allowed set: {stray}")
        flags = flag_pixels(path)
        if flags:
            findings.append(f"{name} has {flags} flag-red pixels in its footer band")
        lines.append(f"{name}  {w}x{h}  {size // 1024} KiB  colours {_colours(path)}  "
                     f"flag pixels {flags}")
    return lines, findings


def _render(slides: Path, stem: str, work: Path, chrome: str) -> Path:
    source = slides / f"{stem}.html"
    html = source.read_text(encoding="utf-8")
    if "</head>" not in html:
        raise ShowcaseError(f"{source} has no </head> to put the footer override before")
    temp = slides / f"readme-{stem}.html"
    shot = (work / f"{stem}.png").resolve()
    if shot.exists():
        shot.unlink()
    # Chrome writes a screenshot only to an absolute path; `render.sh` uses
    # `$PWD/$out` for the same reason.
    argv = [chrome, "--headless=new", "--disable-gpu", "--hide-scrollbars",
            "--force-device-scale-factor=2", "--window-size=1080,1080",
            "--virtual-time-budget=5000", f"--screenshot={shot}",
            temp.resolve().as_uri()]
    try:
        temp.write_text(html.replace("</head>", README_OVERRIDE + "</head>", 1),
                        encoding="utf-8")
        subprocess.run(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=120, check=False)
    finally:
        temp.unlink(missing_ok=True)
    if not shot.exists():
        raise ShowcaseError(f"Chrome wrote no screenshot for {stem}: {' '.join(argv)}")
    return shot


def _bake(render: Path, dest: Path) -> int:
    """Shrink, quantise and strip one render; return the colours used."""
    from PIL import Image

    with Image.open(render) as opened:
        img = _even(opened.convert("RGB").resize((WIDTH, WIDTH), Image.LANCZOS))
    for colours in COLOUR_STEPS:
        # Fast octree, not median cut: median cut spends the palette on the
        # large dark areas and greys out the small ones — the window's
        # traffic lights, a red `wait`, a blue link — which is a picture of an
        # app that does not exist.
        quantised = img.quantize(colours, method=Image.Quantize.FASTOCTREE,
                                 dither=Image.Dither.FLOYDSTEINBERG)
        quantised.save(dest, format="PNG", optimize=True)
        _strip_chunks(dest)
        if dest.stat().st_size <= MAX_BYTES:
            return colours
    raise ShowcaseError(f"{dest.name} is {dest.stat().st_size // 1024} KiB even at "
                        f"{COLOUR_STEPS[-1]} colours; raise MAX_BYTES in the tool and "
                        "its test together rather than switching format")


def ingest(slides: Path, out: Path, work: Path, chrome: str) -> None:
    base = slides / "base.css"
    if not base.is_file() or FOOTER_SELECTOR not in base.read_text(encoding="utf-8"):
        raise ShowcaseError(f"{base} does not define {FOOTER_SELECTOR}: the footer "
                            "override would be aimed at nothing; find which stylesheet "
                            "holds the footer line and update README_OVERRIDE")
    if not Path(chrome).exists():
        raise ShowcaseError(f"no Chrome at {chrome} (pass --chrome)")
    out.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)
    for stem, name in SLIDES.items():
        render = _render(slides, stem, work, chrome)
        _bake(render, out / name)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--slides", default="user-data/readme-showcase/slides",
                        help="the slide deck, relative to the repository root")
    parser.add_argument("--out", default="docs/images/showcase",
                        help="where the README copies go, relative to the repository root")
    parser.add_argument("--chrome", default=CHROME)
    parser.add_argument("--work", default=None,
                        help="keep the 2160 px renders in this folder")
    parser.add_argument("--check", action="store_true",
                        help="verify the committed pictures only; needs no Chrome")
    args = parser.parse_args(argv)

    out = (REPO / args.out).resolve()
    try:
        if not args.check:
            slides = (REPO / args.slides).resolve()
            if args.work:
                ingest(slides, out, Path(args.work).resolve(), args.chrome)
            else:
                import tempfile
                with tempfile.TemporaryDirectory(prefix="showcase-") as tmp:
                    ingest(slides, out, Path(tmp), args.chrome)
    except ShowcaseError as exc:
        print(f"showcase_ingest: {exc}", file=sys.stderr)
        return 1
    lines, findings = verify(out)
    for line in lines:
        print(line)
    for finding in findings:
        print(f"FINDING: {finding}", file=sys.stderr)
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
