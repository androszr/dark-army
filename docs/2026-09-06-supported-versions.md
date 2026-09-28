# Supported versions

- **Date:** 2026-09-06
- **Status:** the contract; pinned by `host/tests/test_supported_versions.py`
- **Asked for by:** candidate 8 of `docs/2026-09-05-launch-legacy-audit.md`

This page answers one question: *does Dark Army support this combination of
pieces?* It names the four components, says which of them are always the same
age, states the window the other two may lag behind by, and lists every
compatibility accommodation in the tree that the window makes safe to delete
and every one it deliberately keeps. Nothing here changes Dark Army's
behaviour. The deletions happen later, one board card at a time, each pointing
back here as its reason.

## The three assistants

| Assistant | Needed | Tested version | Detection and permission source |
|---|---|---|---|
| Claude Code | Required | 2.1.280; broker floor 2.1.239 | `host/dark_army_daemon/dispatch.py` `installed_tools`; `docs/2026-09-07-permission-hold-verification.md` |
| Codex | Optional | 0.155.1; watching from 0.147.0 | `host/dark_army_menubar/hooks.py` `CODEX_HOOKS_PATH`; `docs/cli-permission-modes.md` |
| Grok | Optional | 1.0.41 | `host/dark_army_daemon/dispatch.py` `installed_tools`; `docs/cli-permission-modes.md` |

Dark Army publishes whether each CLI is installed; an absent optional
assistant stays visible in the picker with a reason.

## The four components

| Component | Where it lives | How it updates |
|---|---|---|
| Mac app | `host/` — the Python daemon and the menu-bar app, frozen by py2app into `/Applications/Bob Companion.app` | one installer |
| Panel | `panel/Sources/BobPanel`, shipped **inside** the Mac app at `Contents/Resources/BobPanel.app` | the same installer, the same run of `host/build.sh` |
| Phone | `ios/BobPhone` | independently, through TestFlight and the App Store |
| VS Code extension | `vscode-extension/` | independently: the app carries a `.vsix` and installs it on every launch, but a window keeps the extension it started with until it is reloaded |

**The Mac app and the panel are matched by construction.** They are two halves
of one artefact: `host/build.sh` compiles the panel, freezes the app around it,
and refuses to produce a bundle whose panel binary is stale
(`host/dark_army_menubar/build_check.py`'s `panel_freshness`). There is no
install path that updates one and not the other, so there is no window between
them and no compatibility case to write down.

**The phone and the extension update on their own clocks.** A person may
update the Mac app and leave the phone where it is, or the other way round;
and `install_extension()` in `host/dark_army_menubar/vscode_extension.py`
says in its own docstring that the extension "only becomes live in windows
opened or reloaded afterwards", so an editor window that was open across an
upgrade is still running the previous extension.

**Every component's number is collected in one place**:
`build_check.release_versions()` (`host/dark_army_menubar/build_check.py`)
returns `version`, `app`, `extension`, `panel_embedded` and `phone` from one
call. A reader who wants to know what a tree would ship asks that function,
not four files.

## The window

For the two independently-updating components, Dark Army supports the current
release **R** and its immediate predecessor **R−1**. "Immediate predecessor"
is the last version *published* before R, not R minus one patch as a matter of
arithmetic; where releases have been consecutive patches, the two coincide.
Below R−1 is out of support.

For the Mac app and the panel there is no window: they are the same artefact
and disagreement between them is a build defect, not a compatibility case.
`build_check.release_gate` already refuses a tree that cannot produce one
consistent number.

**The rule is stated as arithmetic so that it never needs editing.** The
numbers in the next section are today's reading of the rule against the tree,
and the test re-derives them; the rule itself does not move when a release
ships.

**Pre-release reading.** There is no release tag in the repository yet, so the
Mac app, the panel and the phone have no published
predecessor yet: R−1 does not exist for them and their window today is R
alone. N−1 begins to exist at release 2. The extension is the exception — its
predecessors are already installed in real editor windows, which is why it has
a real floor today.

## Floors today

Each row names its **source of truth** as a symbol rather than a literal, so
the row is re-derivable rather than transcribed.

| Component | Floor today | Source of truth |
|---|---|---|
| Mac app | the release being built | `build_check.release_versions()["app"]` |
| Panel | same as the Mac app | `build_check.release_versions()["panel_embedded"]` |
| Phone | `1.0` — this is R; the phone has no published predecessor yet, so its floor equals its current version until release 2 | `build_check.ios_marketing_versions()`, which reads every `MARKETING_VERSION` site in `ios/BobPhone.xcodeproj` (four sites, one distinct value) |
| VS Code extension | `0.1.21` — one release below the current 0.1.22; the highest baseline gate today is REFINEMENT_CLOSE_MIN_VERSION at 0.1.12, below the floor | `build_check.package_version(vscode-extension/package.json)` minus one patch; the gates are `vscode_reveal.SEND_TEXT_MIN_VERSION`, `SPAWN_MIN_VERSION`, `CLOSE_TERMINAL_MIN_VERSION`, `REFINEMENT_CLOSE_MIN_VERSION` in `host/dark_army_daemon/vscode_reveal.py` |

The extension floor is `0.1.21` *because* `0.1.22` is current
(`vscode-extension/package.json`, the only package in that folder is
`dark-army-ide-0.1.22.vsix` — the release whose `spawnAgent` accepts a cwd
inside a workspace folder, so a started card's terminal opens in the card's
own worktree, `docs/card-worktrees.md`) and `0.1.21`, the release that
declares limited support for untrusted workspaces and refuses to start an
agent or type into a terminal until the folder is trusted, was the release
published before it. The floor moves when `package.json` moves, and
`test_the_extension_floor_is_one_release_below_the_current_package` fails the
day it moves without this table being edited. The "minus one patch" form is a
convenience that holds while releases are consecutive patches; if the last
published extension was not the one arithmetic names, edit this document first
and the test's expectation second.

**Baseline controls stay available across the supported window.** The four
existing gates sit at or below `0.1.19`. The only optional capability gate is
`NATIVE_REPLY_MIN_VERSION` at `(0, 1, 22)`, pinned to the current extension by
`host/tests/test_supported_versions.py`: a supported older window retains its
existing controls but must reload the extension before native reply is available.
One more capability gate may sit above the floor: `SUBFOLDER_SPAWN_MIN_VERSION`
at `(0, 1, 22)`, the release that can open a terminal in a card's own worktree
(`docs/card-worktrees.md`). A window below it is refused a Start in a git
project with isolation on, in words naming the reload and the per-project
isolation switch — never started in the main checkout instead; switching
isolation off for that project restores the baseline Start. It must never
exceed the current package, and once the floor passes it, it is an ordinary
baseline gate.
This preserves the verified strict-target send required by
the *codex session controls and review* plan; native reply never falls
back to legacy terminal typing. Any other gate above the baseline floor fails
`test_no_extension_gate_sits_above_the_floor`.

The four, as `host/dark_army_daemon/vscode_reveal.py` declares
them today:

- `SEND_TEXT_MIN_VERSION` — `(0, 1, 6)`, typing onto a session's input line
- `SPAWN_MIN_VERSION` — `(0, 1, 8)`, opening a terminal for a dispatched card
- `CLOSE_TERMINAL_MIN_VERSION` — `(0, 1, 9)`, Wrap up's close of the tab
- `REFINEMENT_CLOSE_MIN_VERSION` — `(0, 1, 12)`, the refinement's own close
  receipt; the highest of the four, and still below the floor

## What the policy makes removable

Every row is a **name**, not a deletion. Nothing in this table has been
removed; each row names the card that would remove it, and that card runs its
own impact analysis first.

| Path | What | Why the policy releases it | Follow-on card |
|---|---|---|---|
| `host/dark_army_menubar/panel_process.py:57` | the bare `resources / EXECUTABLE_NAME` arm of `find_executable()`, kept (comment at line 53) "so a menu bar from before the change still finds something to run" | same-bundle: a menu bar and a panel are never separately versioned, so no supported install has a bare panel beside a nested-app menu bar | Drop the bare-executable arm of `panel_process.find_executable()` |
| `panel/Sources/BobPanel/Models.swift:1457` | `counts["ready"]`, the "one-generation alias so an older daemon does not blank the badge" inside `Board.todo` | the daemon in question ships in the same bundle as this panel; there is no older daemon a supported panel can be paired with | Drop the `counts["ready"]` alias from `Board.todo` |
| `panel/Sources/BobPanel/Models.swift:931` | `BoardCard.queuedLine(autostart:)`'s two composed fallback lines for a daemon that predates `queue_reason` | same-bundle; `queue_reason` is published by every daemon this panel can be paired with, so the fallback branch is unreachable | Drop `queuedLine(autostart:)`'s two fallback sentences |
| `host/dark_army_daemon/vscode_reveal.py:727,769,958,1032` | `SEND_TEXT_MIN_VERSION`, `SPAWN_MIN_VERSION`, `CLOSE_TERMINAL_MIN_VERSION` and `REFINEMENT_CLOSE_MIN_VERSION` **collapse into one floor constant** — the *checks* stay, their separate thresholds do not. This is a simplification of the threshold, **never a removal of the gate**: the audit warns against blanket-deleting the extension version gates because "a running editor window keeps its old extension until reload", and `install_extension()` says the extension "only becomes live in windows opened or reloaded afterwards" | every one of the four is at or below the floor, so no supported install distinguishes them; a window running below the floor fails all four alike, and one constant says so once | Collapse the four `*_MIN_VERSION` gates into one extension floor constant |
| `host/dark_army_daemon/devices.py:220` | `devices.resolve()`, the bearer-token resolver | verified unread: no door calls it; the only two other mentions in the tree, `host/dark_army_daemon/relay.py:496` and `host/dark_army_daemon/relay_client.py:14`, are docstrings citing it as a *discipline* ("re-read from the store per frame"), not callers | Delete `devices.resolve()` and reword the two docstrings that cite it |

The plan that asked for this table also listed
`host/dark_army_menubar/dev_build.py`'s bare `Contents/Resources/BobPanel`
probe; that row is gone because the audit's §3 has already landed —
`dev_build.py` no longer probes a bundle layout of its own and reads
`PanelProcess.executable_path` instead, so the one remaining bare arm is
`panel_process.find_executable()`'s, the first row above. The
`panel_process.py` row overlaps audit candidates 3 and 5; it records the
overlap and claims nothing.

## What the policy protects

These look like the same kind of accommodation and are not. Each stays, and
the sentence beside it says why.

| Path | What | Why it stays |
|---|---|---|
| `panel/Sources/BobPanel/Models.swift`, `ios/BobPhone/Models.swift` | the tolerant decode helpers and every `value(...)` default | not compatibility at all: Swift's synthesized `Decodable` throws on a missing key even where the property has a default, so a legitimately ragged payload (`end_reason` exists only on finished rows) needs them regardless of versions |
| `host/dark_army_daemon/vscode_reveal.py:734` | the gate mechanism itself, and `_parse_ext_version`'s rule that "anything unparseable sorts below every real version" | a supported install can still be *running* a pre-floor extension until the window reloads; the gate is the policy's enforcement, not its legacy |
| `host/dark_army_daemon/daemon_board.py:320,324,344,383,451`, `host/dark_army_daemon/daemon.py:9097` | `dispatch_enabled`, `prepare_enabled`, `outcomes_supported`, `queue_writable`, `preferences_writable`, `home_sealed`, `knowledge_supported` | these markers *are* the phone half of the policy: the phone reports no version to the Mac (no `app_version` or `client_version` rides any frame), so a marker the phone decodes false-by-default is the only mechanism the window has. Absent means the control is **absent**, never inert |
| `ios/BobPhone/Client.swift:149-152` | the token stamped on the sealed channel, naming which pairing the replay counters belong to | a pairing-generation guard, not an authentication path; removing it lets a dead pairing's late counters wind a fresh record's forward and turn every new answer into a refused replay |
| `host/dark_army_daemon/api_server.py:69` | the plaintext LAN routes' `HOME_UPDATE_REFUSAL` 426 — "update the Dark Army phone app and pair it again" | this is precisely the *out-of-support peer is told so* behaviour the policy wants more of, not less |

The seven markers, one per line, with where each is written:

- `dispatch_enabled` — `host/dark_army_daemon/daemon_board.py:320`, whether Start may appear
- `prepare_enabled` — `host/dark_army_daemon/daemon_board.py:324`, whether Prepare may appear
- `outcomes_supported` — `host/dark_army_daemon/daemon_board.py:344`, whether the objective controls may appear
- `queue_writable` — `host/dark_army_daemon/daemon_board.py:383`, whether the queue verbs may appear
- `preferences_writable` — `host/dark_army_daemon/daemon_board.py:451`, whether the phone may ask for a preference change
- `home_sealed` — `host/dark_army_daemon/daemon.py:8783`, whether the Mac takes sealed frames at home
- `knowledge_supported` — `host/dark_army_daemon/daemon_board.py`, whether the knowledge reader may appear

**Where the line is.** This document governs **peer versions** — which
build of one component may talk to which build of another. It never governs
**stored state**. The persisted shapes — `sessions.json`, `preferences.json`,
`board.db`'s forward-only `SCHEMA_VERSION` in
`host/dark_army_daemon/board.py`, the `.clawd-tank` migration in
`host/dark_army_daemon/paths.py`, the legacy hook removal in
`host/dark_army_menubar/hooks.py`, the old LaunchAgent cleanup in
`host/dark_army_menubar/launchd.py`, and the `CLAWD_TANK_PORT` variable
the hook still honours — stay under CLAUDE.md's own forward-compatibility rule
and are out of this document's reach entirely. A future card that wants to
retire one of those argues from that rule, not from this page.

## Decision: what a below-floor peer sees

A peer below the floor keeps today's behaviour — the control is **absent**,
never drawn inert — and gains one plain sentence naming which piece is too old
and what to do about it. Nothing in this run writes that sentence into product
code.

Two places a later card will put it:

- **The phone**, where the absent capability marker is the trigger: a screen
  that would have drawn a control and finds its marker missing says, in one
  line, that the Mac app is out of date and needs updating.
- **The panel's editor-dependent controls** (Jump, Reply by typing, Wrap up,
  the refinement close), where the lock file's `extensionVersion` — read by
  `vscode_reveal._parse_ext_version` — is the trigger: the row says the
  window's extension is out of date and the window needs reloading.

The sentence comes **from the daemon, in the daemon's own words**, wherever a
daemon is available to compose it, following `queue_reason`'s precedent:
composed once in Python, published on the snapshot, drawn verbatim by both
clients. It is never composed in Swift, so the two surfaces cannot drift apart
and an older client with no sentence to draw draws nothing rather than a
guess. The one case with no daemon to ask — a phone that cannot reach the Mac
at all — is already handled by `HOME_UPDATE_REFUSAL`'s 426.

## Follow-on cards

Card *names* only. This document files no cards and writes no second plan;
each card, when filed, points back here.

1. **Drop the bare-executable arm of `panel_process.find_executable()`** —
   remove the `resources / EXECUTABLE_NAME` candidate and its comment; the
   nested `.app` path and the checkout fallbacks stay. Reason: the same-bundle
   rule above.
2. **Drop the `counts["ready"]` alias from `Board.todo`** — `Models.swift`'s
   `todo` reads `backlog` and `prep` only. Reason: same-bundle.
3. **Drop `queuedLine(autostart:)`'s two fallback sentences** — the function
   returns the published `queue_reason` and the tests that pin the preference
   order shrink with it. Reason: same-bundle.
4. **Collapse the four `*_MIN_VERSION` gates into one extension floor
   constant** — one `EXTENSION_MIN_VERSION` derived from this document's floor;
   every call site keeps its check. Reason: every baseline gate is at or below the
   floor, so the distinctions are unobservable on a supported install. Runs a
   fresh impact analysis on all four constants first.
5. **Delete `devices.resolve()` and reword the two docstrings that cite it** —
   `relay.py` and `relay_client.py` describe the per-frame re-read discipline
   in their own words. Reason: verified unread.
6. **Write the below-floor sentence** — the decision above: a daemon-composed
   line on the snapshot, drawn by the phone on an absent marker and by the
   panel on a pre-floor `extensionVersion`. Reason: the policy says an
   out-of-support peer is told so.
