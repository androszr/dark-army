# GEMINI.md

Guidance for Gemini CLI when working with code and assets in this repository.

## Project overview

Dark Army is a **menu-bar monitor** for Claude Code sessions. A Python daemon
receives Claude Code hooks, tracks every live session, and serves them on a local
HTTP + SSE API. Three surfaces read it: an animated strip in the macOS menu bar,
a SwiftUI panel that opens from it, and a paired iPhone app (`ios/BobPhone`).
The phone's copy of the cast is **partial** — it carries the names array and the
agent hash, but not `character(forNickname:)`.

**There is no rendering engine any more.** Art is ordinary PNG frames loaded by
AppKit and SwiftUI.

**Your primary role:** technical artist for the cast — the characters each agent
wears in the strip and the panel.

## Asset directories

Two art trees, one roster (`host/dark_army_daemon/identity.py`'s `NAMES`
plus `ART_ONLY`; the cast is Dark Army's own callsigns). The **menu-bar strip** keeps
hand-drawn animated pixel art, because a face there is ~20pt tall and a
photograph at that size is a smudge. The **panel and the phone** draw still
photo portraits.

- `assets/proposals/dark-army-cast/anim/` — **strip source art**: one GIF per
  character per state (`<slug>-work.gif`, `-sleep.gif`, `-alert.gif`), drawn by
  hand in the house style of the frames under `assets/cast/cipher-*`.
- `assets/cast/` — the ingested result: PNG frame folders plus `manifest.json`
  (frame counts, canvas sizes, authored hold times). Only the menu-bar bake
  reads it; a character not yet in its `cast` list falls back to the strip's
  aggregate glyph.
- `host/dark_army_menubar/icons/` — the baked menu-bar strip icons.
- `assets/proposals/dark-army-cast-rebrand/ingest/` — **portrait source stills**, one
  `<slug>.png/.jpg/.jpeg` per character, at least 512×512.
- `assets/portraits/` — the normalised 512×512 PNGs plus a manifest, mirrored
  byte for byte into `panel/Sources/BobPanel/Resources/portraits/` and
  `ios/BobPhone/Resources/portraits/`. A character with no portrait yet shows
  its initial.

## The pipeline

```bash
# 1. Source GIFs → PNG frames snapped to the art's native pixel grid
python tools/pixelgrid_ingest.py assets/proposals/dark-army-cast/anim assets/cast

# 2. Cast → menu-bar icons (aggregate set + one per agent, light and dark)
python tools/menubar_cast_icons.py
python tools/menubar_cast_icons.py --character vex --preview /tmp/bar.png

# 3. Stills → 512×512 portraits, mirrored into both clients
python tools/portrait_ingest.py assets/proposals/dark-army-cast-rebrand/ingest assets/portraits
python tools/portrait_ingest.py --check
```

**Read `tools/pixelgrid_ingest.py`'s docstring before changing the conversion.**
The source art is pixel art *rendered large* and is **not on an integer grid** —
block periods measured ~25.75px for most of the first cast, with outliers at
22.3 and 10.67, and the phase drifts across a frame. Neither a fixed nearest-neighbour
stride nor a box downscale works. The tool fits period and phase per frame,
reconciles harmonics per character, and takes the *mode* colour of each cell's
central 60%.

## Design constraints

- **Three states only**: work, sleep, alert. They map to the categories every
  surface speaks — running, idle, waiting on you.
- **The silhouette is what survives.** Measured at the real 17pt: two different
  characters in the same state differ by ~84 (mean visual distance), while one
  character's own work-vs-sleep poses differ by only 19–40. Identity outsignals
  state by 2–4x, so a state cue must be in the *shape*, not in a small detail.
  The current work loop — the character behind a closed laptop, head sway, rising
  motes — works for this reason.
- **Real alpha**, no colour-key transparency. The old `0x18C5` key is gone.
- **Keep the cast in step** with `identity.NAMES` (Python) and `Cast.names`
  (Swift). A name the daemon can assign but the art cannot draw falls back to a
  hashed face — a stranger wearing someone else's likeness.

## Testing

```bash
cd host && .venv/bin/pytest -q
```

There is no separate art test suite. Judge art by rendering it at true size on
both a light and a dark ground — `--preview` does this — never at 4x only. Several
faults in this project's history were invisible at 4x and obvious at 1x.
