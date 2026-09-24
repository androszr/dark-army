# Contributing to Dark Army

Thanks for taking a look. This is a small project — issues and pull requests are
both welcome, on GitHub.

## Platform

**macOS only.** The menu bar app uses `rumps`/PyObjC, the panel is SwiftUI, the
daemon shells out to `afplay`, and packaging uses `py2app`.
Windows support was explored and deliberately reverted; there are no
`sys.platform` branches in `host/` and reintroducing them is a decision, not a
patch.

## Development setup

You need Xcode (for the Swift toolchain that builds the panel) and Python 3.11+
— the macOS system `python3` is 3.9 and cannot install `pyobjc-core`.

```bash
xcode-select --install          # if you don't have it already

# 1. The panel
cd panel && swift build -c release && cd ..

# 2. Host venv
cd host && python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt && cd ..
```

> **Python 3.11+.** The system `python3` (3.9) cannot install `pyobjc-core` — its
> bundled pip fails with "Could not build wheels", which looks like an
> incompatibility and isn't. `brew install python` avoids it.

## Running it

```bash
cd host && .venv/bin/python -m dark_army_menubar
```

The daemon runs on a thread inside the menu-bar app, and the app launches the
panel binary itself. To run the daemon alone (no UI):

```bash
cd host && .venv/bin/python -m dark_army_daemon.daemon
```

To exercise the real hook path, install the hooks (`cd host && ./install-hooks.sh`)
and **restart any running Claude Code sessions** — hooks are read at session start.

## Tests

```bash
cd host && .venv/bin/pytest -q      # the host suite
cd panel && swift test              # the panel's own unit tests
ruff check .                        # from the repo root; error-class rules only

# the phone's own tests — both bundles, on a simulator you have
xcodebuild test -project ios/BobPhone.xcodeproj -scheme BobPhone \
  -destination 'platform=iOS Simulator,name=<a device you have>' \
  -skipPackagePluginValidation
```

The suite is expected green. If a test fails and you did not cause it, report
it rather than working around it. Inside a `/ship` run the implementer applies
a stricter rule: a failing test in a file its delta did not touch is checked
once against a baseline worktree and reported as PRE-EXISTING, never fixed in
the same run; a failure in a file it touched is its own.

`.github/workflows/tests.yml` runs the first three on **every push** —
`ruff check .` and `pytest -q -n auto` (the suite across workers) on one macOS job (`Host (pytest + ruff)`),
`swift test` on another (`Panel (swift test)`). A third job,
`Phone (xcodebuild test)`, builds the iPhone app plus its home-screen tile
and runs both test bundles; it is **path-gated** and runs only when the push
touched something under `ios/` or the workflow file itself.

## Rolling back a build

An older build preserves outcome columns it does not understand, but cannot
invalidate acceptance when it reopens or changes accepted work. After using an
older build to edit the board, treat accepted-outcome counts as unverified until
you recheck the affected cards in the current build. Open each previously accepted
card that changed, compare its objective and acceptance evidence with the work,
and use **Request revision** when that evidence no longer establishes success.
Reaccept only after checking the revised work and recording fresh evidence.
If you cannot identify which cards changed during the rollback, recheck all
accepted cards. An In progress column alone does not establish that acceptance
is stale: acceptance deliberately does not move cards between columns.

## Project layout

| Directory | What | Language |
|-----------|------|----------|
| `host/dark_army_daemon/` | Async daemon: session state, transcript stats, history, the local API | Python |
| `host/dark_army_menubar/` | macOS menu bar app and the animated strip | Python |
| `host/dark_army_menubar/hooks.py` | Installs the Claude Code hooks. Also *contains* the hook handler, as the `NOTIFY_SCRIPT` string written to `~/.dark-army/` — **stdlib only**, keep it that way | Python |
| `panel/` | `BobPanel`, the SwiftUI panel the menu bar opens | Swift |
| `ios/` | `BobPhone`, the paired iPhone app | Swift |
| `relay/` | The sealed mailbox that carries envelopes to a phone away from home | JavaScript |
| `vscode-extension/` | `dark-army-ide` — reveal a terminal, type into it, close it | TypeScript |
| `plans/` | One written plan per change — what the board's cards point at; git-ignored, each machine keeps its own | Markdown |
| `assets/cast/` | The menu-bar strip's pixel art, snapped to its native grid | PNG |
| `assets/portraits/` | The panel's and the phone's still photo portraits | PNG |
| `tools/` | Asset pipelines: GIF → pixel-grid frames → menu-bar icons; stills → portraits | Python |

## Conventions

- **Cast art is generated, not hand-edited, and there are two trees.** The
  menu-bar strip draws hand-drawn pixel art: source is
  `assets/proposals/dark-army-cast/anim/<slug>-<state>.gif`; run
  `python tools/pixelgrid_ingest.py assets/proposals/dark-army-cast/anim assets/cast`
  to snap it to its native pixel grid, then `python tools/menubar_cast_icons.py`
  to bake the strip's icons. The panel and the phone draw still photographs:
  drop `<slug>.png/.jpg` into `assets/proposals/dark-army-cast-rebrand/ingest/` and run
  `python tools/portrait_ingest.py assets/proposals/dark-army-cast-rebrand/ingest assets/portraits`,
  which normalises them to 512×512 and mirrors the tree into both clients
  (`--check` verifies the three copies agree). Both trees are keyed by the
  one roster in `identity.NAMES` + `ART_ONLY`; either may be incomplete — a
  character with no drawing falls back to the strip's aggregate glyph, one
  with no photograph shows its initial. The source art is *not* on an integer
  grid — see `pixelgrid_ingest.py`'s docstring before changing the conversion.
- **`dark-army-notify` must stay stdlib-only.** It runs under whatever `python3`
  the user's Claude Code session has, which may be the 3.9 system one.
- **Kerning after an NSTextAttachment does nothing** — gaps that follow an icon in
  the menu-bar strip must be a real spacer character. See `_render_strip`.

## Commits

Conventional-commit style, matching the existing history:

```
feat(menubar): expandable per-agent menu rows with model + tool breakdown
fix(daemon): evict PID-less ghost sessions after 90s grace
docs: align README with the machine-only build
```

## Releases

Tagging and packaging are described in `.claude/skills/releasing/SKILL.md`. There is
no CI runner for macOS, so release artifacts are built locally with
`cd host && ./build.sh`. That build is **strict by default**: it refuses to
produce an app from a panel that did not compile in this run or is older than
its sources, or from an extension package that does not match
`vscode-extension/package.json`. `--dev` brings back the old warn-and-continue
behaviour for a machine without Swift or npm, and is never used for a release;
`--check-only` runs just the gates.
