# Development context — building, testing, the asset pipelines and reviewing

Relocated verbatim from `CLAUDE.md` on 20 Sep 2026 (`docs/ship-efficiency.md`
holds the paragraph map). This is the subject document for **how the
product is built and checked**: the strict build, the test suites, the cast
and icon pipelines, and the review skill. `CLAUDE.md` keeps the commands and
the universal rules; the reasoning and the detail live here. Loaded when the
work touches `tools/`, `host/build.sh`, `host/setup.py`, the workflows, the
asset trees, the review skill or documentation alone — and always on the
conservative fallback (`docs/agent-context.json`).

## Build Commands

### Panel

Requires Xcode. The menu bar launches the binary over stdin.

```bash
cd panel && swift build -c release
```

### Menu Bar App (.app bundle)

```bash
# Build panel + py2app + bundle and install.
# Releases omit --allow-untagged and require a clean vX.Y.Z tag.
cd host && ./build.sh --allow-untagged --install

```

**Strict build:** `build_check.py` (build-only subprocess, never imported by
runtime) requires this run's panel binary/resources, newer than panel sources,
and this run's extension package matching `vscode-extension/package.json`.
Before checkout writes it requires a Git checkout, exact `vX.Y.Z` HEAD and
clean tree. Flags work in any order:

| Flag | Effect |
|---|---|
| `--install` | Replace `/Applications/Dark Army.app` (same bundle identifier as the old install; it retires nothing). |
| `--dev` | Warn and continue without Swift/npm; untagged version `0.0.0`. Never release with it. |
| `--check-only` | Check version/panel/extension, exit before py2app; dirty/untagged still refused. |
| `--allow-untagged` | Bypass version gate only; panel/extension stay strict. Used by `dev_build.rebuild()`. |

Extension dependencies use `npm ci` and committed `package-lock.json`, not
`package.json` carets; packaging uses local `node_modules/.bin/vsce`, never
fresh `npx`. `build_check.lock_match` refuses mismatches before install with
Dark Army's explanation. Repair via `cd vscode-extension && npm install`, then
commit the lockfile.

Every app carries `Contents/Resources/build-manifest.json` (schema, build
time/mode, component and toolchain versions, extension digests), written
beside `repo-root` before signing. Non-fatal; no runtime reader.

Bytecode is recompiled `unchecked-hash` before signing and never written at
run time (`launcher.py`); `test_frozen_bundle.py` pins both.

### Tests

Host dependencies (`rumps`, `psutil`, etc.) live in `host/.venv`, Python
**3.11+**; system Python 3.9 cannot install `pyobjc-core`.

```bash
# Python tests (host daemon)
cd host && .venv/bin/pytest -v
# The full suite across workers: about three minutes, against nine to ten
# single-process under fleet load. A single file runs plain, one process.
cd host && .venv/bin/pytest -q -n auto -p no:cacheprovider

# Venv: python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
```

**SwiftPM builds only the panel.** `tests.yml`'s phone job runs `BobPhone`
`xcodebuild test` on a simulator: `BobPhoneTests` and `BobPhoneWidgetTests`,
`macos-26` for `testflight.yml` SDK parity. It runs only for `ios/**` or workflow
changes. The unconditional syntax floor is
`test_phone_offline_and_receipts.test_every_phone_source_parses`:
`xcrun swiftc -parse` over all `ios/` Swift sources (~4s).

Phone `xcodebuild test` needs `-skipPackagePluginValidation` and a
`-derivedDataPath` under the run's scratch.

**The phone does not clip prose** — no `.lineLimit(` on a line that wraps, and
a `.navigationTitle` is always a literal. Pinned by
`host/tests/test_phone_text_in_full.py`; the rule in full is in
`docs/phone-contract.md`.

### Cast Pipeline

**Panel art resolves off the installed app**: `PanelResources` tries
`Bundle.main.resourceURL`, then `bundleURL`, then SwiftPM's `Bundle.module`
(a hard-coded `.build` path); `test_panel_resources.py` forbids it elsewhere.

**Two art trees, one roster.** **Twenty** nickname characters in
`identity.NAMES` — fourteen Dark Army callsigns (swapped in place on 22 Sep
2026, so every index kept its position), then `Androll`, `Captcha`,
`Sawa`, `Franio`, `Zosia`, `Ptys` **appended** so the first fourteen keep
their hash indices — mirrored in order by `Cast.names`
in `panel/Sources/BobPanel/Cast.swift` and `ios/BobPhone/Cast.swift`, plus
`identity.ART_ONLY` / `Cast.artOnly` (`overwatch`, the planner's face, which no
session is ever assigned): **twenty-one portrait slugs**. Every slug has one
quote, drawn only under a large portrait (`identity.QUOTES`, `CastQuotes`,
`test_cast_quotes.py`). All twenty are assignable; `terminal_title.short` gives each a unique three-letter tab badge. The **menu-bar strip** draws hand-drawn animated
pixel art from `assets/cast` (a face there is ~20pt, where a photograph is
a smudge). The **panel and phone** draw still photo
portraits from `assets/portraits`, one per slug, the same still in every
state, with a 2pt `StateRule` beneath (solid work / dashed sleep / red alert —
form before colour). Either tree may be incomplete: a character with no
drawing empties its strip category to the aggregate glyph plus count
(`_strip_faces`), and one with no portrait draws its **initial** on a plain
tile — never a hashed stranger. `assets/cast/manifest.json` may declare only
roster slugs (`test_identity.py`); `tools/portrait_ingest.py --check` pins the
three portrait copies byte-identical (`test_portrait_tree.py`). Nothing at
runtime reads either manifest except the menu-bar bake.

**`cast.character_for` is the panel's `Cast.character(for:)` rung for rung** —
whole nickname, then the stem before `-`, then the session-id djb2 — a banner
drawing one face while the row draws another is worse than one with no face.
`test_cast.py`'s golden is produced by *running* the panel's own
function under `swiftc`, never by reading it; a disagreement is fixed on the
Python side, never by copying Python's answer in.

**Cipher is chief of staff; the other nineteen lead eight areas.**
`areas.AREAS` owns the roster, mirrored by both clients' `Areas.swift` and
pinned by `test_crew.py`; `docs/delivery-leads.md` has it in full. Refine
prefers Cipher; Start prefers the area's first free lead, Universal for no
area. `_role_nickname` rejects busy copies and art-only Overwatch, the planner
banner's face and Cipher's alter ego. `_rename_for_role` at binding remains
the one stickiness exception. A stamped card Start carries `area_line`,
composed once by the daemon and drawn verbatim: lead or stand-in.
Stages keep their canonical names (`crew.ROLES`), no character pools.
`CrewBand` draws one lead plus stage markers and keeps old recorded
faces; a slug the 22 Sep 2026 rebrand retired is published as its successor
(`identity.current_crew`), the stored column untouched. `record_agents` remains the only `crew_trail` writer and never
rewrites a recorded stage; `crew` is absent where empty. Areas grant no
permission and change no dispatch, queue or gate rule.

**The app icon and the top-bar mark are one mask.** The bars, widgets and
Lock Screen card wear `assets/brand/fsociety-mark.png` with a clear ground,
so only the mask shows. The Dock, Finder, home screen and `assets/favicon.ico`
are that mask painted in Dark Army green. `tools/app_icon_bake.py` builds
the plate, centres it on the squircle and bakes every icon file with
**LANCZOS**. The bars draw the clear file with high-quality scaling.
`test_fsociety_brand_mark.py` and `test_app_icon.py` pin the copies and the
bake. **The VS Code status bar wears the same mark**, traced from a 64px
reduction of that PNG into one glyph in
`vscode-extension/media/dark-army-icons.woff`, contributed as `dark-army-mask`
and worn as the status item's whole text; `Dark Army` is the tooltip.

**Every proposal goes under `assets/proposals/`, git-ignored**: only the
ingest tools' output is tracked, and a test reading a source skips.

```bash
# Strip: snap the hand-drawn GIFs onto their pixel grid
python tools/pixelgrid_ingest.py assets/proposals/dark-army-cast/anim assets/cast

# Strip: bake the menu-bar icons (aggregate glyphs from one character)
python tools/menubar_cast_icons.py

# Panel + phone: normalise stills to 512x512, mirror into both clients
python tools/portrait_ingest.py assets/proposals/dark-army-cast-rebrand/ingest assets/portraits
python tools/portrait_ingest.py --check

# App icon: bake every icon file from the committed mark
python tools/app_icon_bake.py
python tools/vscode_icon_font.py
```

Front-page pictures are generated too: `tools/demo_shots.py all` (contract in `docs/images/SHOTS.md`); never hand-edit a PNG under `docs/images/`; `docs/images/showcase/` is baked by `tools/showcase_ingest.py` from the git-ignored deck (`docs/images/SHOTS.md`, *The showcase slides*).

## Signal tokens and workshop

`design-system/tokens.json` is the tracked visual source. Run
`python3 tools/design_system_tokens.py`, then `--check`, to generate and verify
both clients' Swift tokens and their bundled offline web workshops. The
source schema, component-edit workflow, import/export behavior and native
rebuild path are in `docs/design-system.md`. The proposal under
`assets/proposals/` is design input, never a runtime resource.

## Delegation

Big reads and boilerplate writes go to a cheap helper through the shunt skill
(`.claude/skills/shunt/SKILL.md`; the pack template is the one text, and
`tools/sync_shunt_skill.py --check` holds Dark Army's `.claude/skills/shunt/`
and `.agents/skills/shunt/` copies in step, pinned by
`host/tests/test_shunt_skill.py`). A guard installed with Dark Army's hooks refuses
a whole-file read over the project's threshold on Claude, Codex and Grok;
reviewers are exempt by role and by the ship workflow's exemption window.
This checkout's dial is its `.claude/settings.json` (1200 lines).
What the helper did is counted, never estimated: `tools/ship_efficiency.py
shunt --ledgers <dir>` reads the per-session ledgers, and `## Delegation` in
`docs/ship-efficiency.md` says what was and was not measured.

The three token defaults Dark Army's loops follow — keep tool results over
about 1,500 tokens out of the conversation, compact at 85%, reuse a
request's fixed front — and where the compact line is decided are
`docs/harness-token-policy.md`.

## Reviewing a change

`/review` reviews a commit (`/review <sha>`), a branch or a pull request
(`/review #42`); a bare `/review` **asks what to review first** — working
tree, one of the last five commits, a branch or a PR — in the assistant's
own question form. It reads the GitNexus graph for blast radius, explains
risk and fix in **plain language**, grades every finding **BLOCK / FIX /
WARN / NOTE**, opens with `VERDICT: SHIP` or `VERDICT: STOP` (any BLOCK
means STOP), and offers what it finds as Backlog cards. `.claude/review.md`
says what counts as risky here. The skill is
`.claude/skills/review/SKILL.md`; `.agents/skills/review/SKILL.md` and
`~/.claude/skills/review/SKILL.md` are byte copies written by
`tools/sync_review_skill.py` (`--check`), pinned by
`host/tests/test_review_skill.py`. Not `/code-review`, the deep sweep
that applies fixes.
