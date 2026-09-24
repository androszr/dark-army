# Launch legacy audit — 5 September 2026

Analysis and candidate fixes only. No implementation plans or board cards were created. Product code, installed applications and live state were not changed.

The clearest launch risk is the build's willingness to package incomplete or stale components. The clearest removable legacy is the retired file-overlap scheduler. Most traces of the old hardware product are comments and small interfaces; this review found no active ESP32/BLE/LVGL/SDL renderer in the runtime packages inspected.

## Evidence and limits

- Reviewed current architecture, TODO status, host runtime/packaging, panel launch and model contracts, phone compatibility, extension packaging and old asset tools. This is a targeted static audit, not an exhaustive proof that every unused symbol has been found.
- Checkout HEAD: `257e8ff`, with substantial concurrent uncommitted changes. Findings describe the working files inspected, not a frozen release candidate.
- GitNexus repository: `bob-companion`. Index timestamp: 2026-09-05 12:31:32 UTC; indexed commit matches HEAD, but CLI status reports stale working-tree content. MCP failed with database storage version 43 versus runtime 42; the repository CLI successfully supplied queries, context and impact results. Those results were supplemented with current source/reference inspection. No reindex was performed over concurrent work.
- Focused verification: `host/.venv/bin/pytest -q host/tests/test_dev_build.py host/tests/test_board.py host/tests/test_paths_migration.py host/tests/test_observer.py` — **200 passed**.
- Temporary fixtures reproduced findings 2 and 3 below. No real user database or application was used. The first database fixture attempt omitted `connect()` and was corrected; the panel path comparison was normalized for macOS temporary-directory symlinks before checking it.
- No full suite, release build, signing assessment or installed-app smoke test was performed. The existing TODO reports full-host failures during concurrent edits; that is prior evidence, not a fresh result from this audit.

## Candidates, in recommended order

### 1. Make a release build reject missing or stale components

**Priority: before launch. Confidence: confirmed source path. Scope: medium.**

Evidence: `host/build.sh:37` treats the panel as optional, catches Swift compilation failures with a warning, then at line 124 bundles any executable already at `.build/release/BobPanel`. A failed compile can therefore ship yesterday's panel. Extension compilation also falls back to a committed VSIX, and line 86 selects the highest filename version rather than proving it matches the current source/package manifest.

Proposed fix: add an explicit release build contract requiring successful panel and extension builds, required resources/helpers, and the expected extension artifact. Keep any developer fallback as a deliberate development mode. Verify the resulting bundle with the source checkout unavailable. A failed component build must produce no publishable release artifact. Test the failure path with a stale binary already present.

This matters more than reducing source size: it can put early-version behavior into a newly built app.

### 2. Preserve newer board schema versions during a downgrade

**Priority: before launch. Confidence: reproduced. Scope: small.**

Evidence: `host/bob_companion_daemon/board.py:721`, `BoardStore._migrate`, logs when `found > SCHEMA_VERSION` but unconditionally replaces the recorded version with this build's version. A temporary database marked `999` reopened as `13`. The existing newer-schema test at `host/tests/test_board.py:481` checks that the database opens, but does not assert that its version survives.

Proposed fix: only advance the marker, following the existing forward-only approach in `history.py:456`. Add a downgrade/re-upgrade regression that preserves both the marker and unknown data. This is proven metadata corruption; actual card loss was not demonstrated. A lowered marker can cause future upgrade steps to run again.

Keep the migration system and the old Ready-to-Backlog conversion: removing those would abandon existing saved cards.

### 3. Use one panel executable resolver for launch and freshness

**Priority: before launch if the build-status UI remains. Confidence: reproduced. Scope: small.**

Evidence: `host/bob_companion_menubar/dev_build.py:127` still checks `Contents/Resources/BobPanel`. The actual launcher, `panel_process.py:29`, prefers `BobPanel.app/Contents/MacOS/BobPanel`. In a temporary nested-app fixture the launcher found the executable and the freshness resolver returned `None`. With a checkout available, the latter can instead measure its development binary.

Proposed fix: share resolution of the actual launched executable and test nested bundles, restart, development mode and missing executables. Remove the simulator-era explanation in `dev_build.py` while doing so.

GitNexus classified this resolver LOW risk with two direct callers: `_compute_panel_probes` and `_refresh_build_status`; affected flows reach app initialization and background probe work. Re-run against a fresh index before implementation.

### 4. Remove the retired file-overlap scheduler and its inert settings

**Priority: worthwhile pre-launch cleanup. Confidence: high. Scope: medium.**

Evidence: `host/bob_companion_daemon/board_queue.py:33` explicitly retains the old gate despite no runtime use. Candidates include `FileClaim`, `TOTAL`, normalization/overlap helpers, `blocked_by`, `eligible_head`, `queue_status` and `PlanSetCache`. Current production references use the active slot-counting policy instead. The related `board_workflow.declared_files*` parser chain has test callers but no production caller found outside this retired design.

The baggage also crosses layers: `preferences.py:130`, `app.py:1940`, its `set_board_queue` action, `daemon.py:1310`, the board snapshot's `queue_enabled`, and `panel/Settings.swift:52`. The setting is carried and published but does not govern scheduling. The panel retains old queue-holder sentence composition in `Models.swift:633`.

Proposed fix: remove the retired algorithm and tests dedicated solely to it; remove the inert setting/action/state after handling the supported client boundary. Preserve `claims`, `holds`, `run_active`, `needs_you`, `slot_head` and `queue_key`, plus the plan resolver and readers still used for real plans. Keep queue order, parallel limits, dispatch gates and their behavior tests.

GitNexus classified `PlanSetCache` LOW, with module-import dependants in board/daemon/daemon_board and no affected execution process. Those imports are not proof of actual construction. Source searches confirm the distinction between importing the live module and calling its retired half. Git history already preserves the alternative implementation.

### 5. Delete small simulator-era runtime interfaces

**Priority: worthwhile cleanup. Confidence: high from source; graph incomplete. Scope: small.**

Evidence: `daemon.py:1949` retains `_dismiss_from_device` solely for `_wrap_sim_inbound`, which no longer exists. `DaemonObserver.on_connection_change` (`daemon.py:804`) and implementations in `app.py:742` / `api_server.py:375` remain even though no production invocation was found. `StripRung.idle` (`app.py:320`) is explicitly retained to preserve positional tests, and adds a ladder step that changes no rendered feature.

Proposed fix: remove those interfaces and the redundant rung, update observer fixtures and assert actual strip behavior instead of preserving an obsolete tuple position. Preserve the live notification dismissal method and daemon liveness checks. The `headless` option still controls lock takeover and must not be swept up as obsolete.

The disambiguated API observer impact result is UNKNOWN, not a safety clearance. Current whole-host searches, including the string-dispatch observer mechanism, found only definitions/test fixtures. Refresh graph analysis before edits.

### 6. Remove abandoned SVG animation tools

**Priority: low-risk cleanup. Confidence: high. Scope: small.**

Evidence: `tools/gemini_animate.py:20` requires files under `assets/svg-animations`, absent from the tracked tree. `tools/svg2frames.py:7` describes feeding `png2rgb565.py`, which is gone. References found outside historical records are the tools referencing themselves/each other. These are development tools, not current runtime dependencies.

Proposed fix: delete both tools unless there is a concrete intended reuse. Preserve `pixelgrid_ingest.py`, `menubar_cast_icons.py`, `app_icon_bake.py`, the phone sound generator and the source cast artwork. This reduces misleading maintenance paths, not measured app startup time.

### 7. Separate release identity from development-build metadata

**Priority: before public distribution. Confidence: confirmed source. Scope: medium.**

Evidence: `host/setup.py:58` hardcodes both bundle versions to `1.0.0` despite independently baking a Git-derived display version. The nested panel plist has its own fixed `1.0`/`1`. `host/build.sh:168` writes the builder's absolute checkout path into every bundle's `repo-root` resource. Runtime lookup only checks that the target contains `host/build.sh`.

Proposed fix: derive release bundle versions from an explicit release input and omit the development checkout stamp/rebuild affordance from release artifacts. Preserve useful development behavior in development builds. Check identity across the outer app, nested panel and displayed version. The stamp is confirmed build-machine provenance in an artifact, not a demonstrated secret disclosure or exploit.

The script currently uses ad-hoc signing and README states that there is no notarized download. Public distribution readiness is a separate release workstream; passing the existing local signature verification is not evidence that this workstream is complete.

### 8. Define a supported compatibility window, then prune individual fallbacks

**Priority: policy decision before broad deletion. Confidence: mixed by path. Scope: medium.**

Evidence: panel models carry older-daemon aliases and alternate queue composition; `panel_process.py` supports a bare-panel bundle layout; `vscode_reveal.py` has several extension-version capability gates. `devices.py:220` retains the old bearer-token resolver, with no production door calling it after sealed home access. Phone tokens still identify pairing generations (`ios/BobPhone/Client.swift:149`), so the token field itself is not dead.

Proposed fix: specify which Mac/phone/extension combinations launch supports. Prune same-bundle obsolete UI/protocol paths once the release guarantees matched components. Separately remove the unused bearer resolver after checking direct references. Keep phone generation guards, sealed-channel validation and the plaintext routes' explicit 426 upgrade refusal.

Do not blanket-delete tolerant decoding or extension version gates: the phone updates independently, and a running editor window can keep an older extension until reload. Do not assume the preparation helper's mode named `legacy` is unused merely from its name; its empty-idea API path needs a separate caller/product decision.

### 9. Rewrite current documentation around the shipping product

**Priority: before launch. Confidence: confirmed contradictions. Scope: small.**

Evidence: `AGENTS.md:12` says no network dependency without describing optional LAN/relay features; README's requirements say Claude is necessary to show anything despite current independent provider readers. Its optional-extension description omits board launching. `app.py` still describes a Simulator submenu; the panel plist's opening comment describes the superseded bare-executable layout. CLAUDE's board section says schema 7 while code is schema 13. Other current architecture prose retains old constraints alongside their replacements.

Proposed fix: align README, agent guidance, architecture comments and release/build instructions with the actual supported launch scope. Move origin stories to historical documents. Preserve CHANGELOG, plans and historical test fixtures where they explain regressions; old words in Git history do not affect the shipping app.

### 10. Stop accumulating old extension packages and unrecorded build inputs

**Priority: secondary; pair with candidate 1. Confidence: confirmed. Scope: small/medium.**

Eight VSIX versions are tracked, totaling only about 64 KB on disk. This is artifact-selection ambiguity, not meaningful size bloat. No dependency lockfile is tracked; the release script runs `npm install`, and Python dependency files use lower bounds.

Proposed fix: keep only the intentionally supported package if committed fallback artifacts remain, select it explicitly, and record reproducible release dependency inputs. Validate archive metadata against the manifest. Old VSIXs remain retrievable from Git/release history.

## Legacy to retain for now

- `paths.py`'s one-time `.clawd-tank` state migration; `hooks.py`'s exact-owned legacy hook removal; `launchd.py`'s old LaunchAgent cleanup. These prevent lost preferences, duplicate events and competing old processes.
- Old board schema transformations and tolerant handling of unknown persisted fields.
- `CLAWD_TANK_PORT` until an explicit end to pre-rename configuration support. It is a small shared alias across clients/server, not a performance bottleneck.
- Provider process identity, enrollment, dispatch, permission and terminal-control guards. Their complexity does not make them legacy.

Recommended first selection: **1, 2, 3, 7 and 9** for launch correctness and presentation; **4, 5 and 6** for actual code removal. Choose **8** before broad compatibility deletion. **10** can accompany release-build work.
