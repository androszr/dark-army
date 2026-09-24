# The first-run checklist, the launch line and denied notifications

The contract behind `panel/Sources/BobPanel/FirstRunChecklist.swift`,
`daemon.checklist_facts`, `host/dark_army_menubar/launch_report.py` and
`Notifier.refresh_status`. `CLAUDE.md` carries the two-sentence pointer; this
page is what must hold. Evidence from a real clean-account walk goes in
`docs/2026-09-12-first-run-walkthrough.md`, not here.

## What a newcomer sees

Three steps, in the rail **above** every tab's content — Inbox (the default),
Agents, Backlog and History alike — so the first opening explains how to
begin without a tab change:

1. **Enrol a folder**
2. **Open it in VS Code**
3. **Start a session**

Exactly one step is labelled *Current step* until all three are *Done*; the
rest are *Next*. The words are the accessibility labels
(`Step.accessibleLabel`, "Done: Enrol a folder"); colour is never the only
signal. Under the current step is one instruction: step 1 explains that
enrolling writes a private key file into the folder and offers **Enrol a
folder…** (the settings window's own `NSOpenPanel`, through
`SettingsActions.enrolChosenFolder`; a cancel is inert, a refusal is drawn
in the daemon's words, a success ticks nothing itself — the next snapshot
listing the folder is the tick); step 2 names the folder and says *Waiting
for VS Code to connect*, asks the reader to answer VS Code's question about
trusting the folder's authors first (*Workspace Trust* below), and points at
installing or reloading the extension from Settings; step 3 says to start a
Claude Code, Codex or Grok session in a terminal inside that folder and that
no prompt has to be sent. Step 3 names the terminal route only, with or
without *Dark Army's own terminal*: the board files a card only under a
folder open in a VS Code window (`board.projects`, which `board_create`
checks against `_known_project_roots()`), so "write a card and press START"
would lead a newcomer with no window to a composer that cannot file under
their folder.

With *Dark Army's own terminal* on in Settings (`board_own_terminal`, read
from the panel's settings context as `boardOwnTerminal`), an enrolled
folder's step 2 reads *Not needed* until an editor is actually observed,
with one line under it: sessions started from the board run in Dark Army's
own window. When all three steps are settled the heading reads *Setup
complete*, the line under it names the next move — write your first card,
refine it in Prep, press START in Backlog — and one button, **Write your
first card**, opens the card composer.
Claude Code is required; Codex and Grok are optional. See
[assistant permissions](cli-permission-modes.md).

## The evidence, per root (daemon)

`daemon.checklist_facts(roots, windows, rows, root_enrolled)` is pure and
`BobDaemon._observe_checklist(snapshot)` feeds it on the **snapshot
executor** inside `_push_agents_snapshot`, in its own `try`, storing one
immutable tuple in `_checklist_facts`. `enrollment_snapshot()` **reads** it
and starts no walk — the API path and `detailed_snapshot` cost nothing new.
It is published inside the existing `enrollment` section:

```json
"checklist": {"available": true,
              "roots": [{"root": "/p/a", "editor_observed": true,
                         "session_observed": false, "own_checkout": false}]}
```

- Roots come from the ledger (`enrollment.enrolled_roots()`), sorted; a root
  that left the ledger since the last cycle is filtered at publication.
- **`editor_observed`** is *that exact folder* being a workspace folder of a
  window whose extension host is still alive. `workspace.Window` now carries
  the lock's `pid` (`extHostPid` / `pid`, `0` where an older extension wrote
  none, read as alive); `workspace.pid_alive` refuses a dead lock. A
  containing parent, a same-name sibling and a nested enrolment inside the
  folder all read false. Project naming never reads the pid.
- **`session_observed`** is an admitted **main** session — a row
  `_bindable_candidate` admits from the three live buckets (`running`,
  `sleeping`, `waiting`); never `abandoned` or `finished`, never a background
  agent or a stray child a parent claims — whose `cwd` resolves through
  `enrollment.root_enrolled` to this exact root. Registered, idle, parked and
  unprompted all count. An empty or unresolvable cwd counts for nobody.
- **`own_checkout`** is that root being exactly Dark Army's own checkout —
  the folder `enroll_self()` enrols on a source build, found by
  `enrollment.self_root()` (`dev_build.find_repo_root()`, normalised,
  memoised once per process so no snapshot cycle walks or reads the stamp;
  `""` with no source tree, which marks nothing). The checklist never
  follows that root and the folder picker never offers it: a newcomer who
  built from source has not enrolled *their* project yet. An older daemon
  omits the key; the panel reads it as false.
- No key, digest, session id, pid or session content rides here.
- `_handle_message`'s admission gate is untouched; there is no second filter.
- The 10 s `SNAPSHOT_REFRESH_SECONDS` refresh is the cadence; there is no
  second poller. An absent `checklist` key means an older daemon — the panel
  keeps its existing enrolment UI on that, never draws steps.

## The opening (panel)

`FirstRunChecklist.Opening` is per opening and decided **once**, on the
first authoritative snapshot (`generatedAt > 0` and `enrollment.available`)
after the panel appears; `focus.shown` and `client.visible` going false
reset it. The decision (`FirstRunChecklist.decide`):

| State | Decision |
|---|---|
| no `checklist` on the wire / no enrolment section | `unsupported` — ordinary UI, untouched |
| `first_run_checklist_completed` already true | `alreadyDone` — ordinary UI (a completed install with no roots keeps the plain enrolment prompt; completion is never cleared) |
| any root has `session_observed` | `skipAndComplete` — nothing drawn, completion recorded: the install is already monitoring |
| otherwise | `show` |

While shown, every snapshot recomputes the steps for **one root**, chosen
among `FirstRunChecklist.candidates(enrollment:)` — the enrolled folders
minus every root marked `own_checkout`, sorted — by `selectRoot` (the
followed root while it stays a candidate, else the first by canonical sort;
a picker listing the candidates appears with two or more, and a pick
recomputes every tick from that root alone). With Dark Army's checkout the
only enrolled folder there is no candidate, so step 1 stays current. `decide`
is unchanged on purpose: a session already running in the checkout on the
first snapshot still means this install is monitoring and skips the
tutorial — excluding the checkout there would draw a tutorial over a working
fleet on every developer's Mac. No step from root A may combine with root
B. Facts may arrive out of order and each is shown as it is — a session
seen before its window ticks step 3 while step 2 stays current. Before
completion, closing VS Code returns step 2 to waiting on the next cycle.

**`Not needed`** (`StepState.notNeeded`, marker `–`, secondary tint, read as
*Not needed: Open it in VS Code*) is step 2 alone, when
`progress(root:enrollment:ownTerminal:)` is handed `ownTerminal` true, the
root is enrolled and no editor is observed; an observed editor still reads
*Done*. It is neither current nor next, and `Progress.complete` counts it
with *Done* — so enrolled + session + not needed completes with no VS Code
window at all. `Opening.advance(snapshot:completed:ownTerminal:)` and the
view read the same flag from the same settings context, or the ticks and the
*Setup complete* moment would disagree. Switching the setting off before
completion returns step 2 to current on the next snapshot, as closing VS
Code does. With the flag false the table is exactly the one above.

When all three are settled for the followed root the placement becomes
`completed`: the ticks and *Setup complete* for the rest of this opening,
and `PanelPlacement.saveChecklistCompleted(true)` merge-writes
`first_run_checklist_completed: true` into `panel-position.json` (frames,
board keys and unknown keys preserved; `false` removes the key; anything but
a genuine JSON boolean reads as false). The next opening is ordinary. A
session that arrives *after* the checklist appeared never makes it vanish;
only a session present on the opening's first snapshot skips it.

The completion key grants nothing: un-enrolling stops observation the same
instant whatever it says, and the daemon never reads it.

**Write your first card** (`FirstRunChecklist.firstCardAction`) calls
`PanelView.openFirstCard()`: `deselect()`, then `BoardState.openComposer` —
the path **+ NEW CARD** takes. What it is pre-filled with is
`FirstRunChecklist.firstCardFiling(selectedRoot:boardProjects:)`: the root
the checklist followed, under the board's own name for it, **only** when
`board.projects` offers that root; otherwise the composer opens blank and the
person picks from what the board can file under. Never `filingProject`'s
busiest live project, which on a developer's Mac is the checkout. The
composer is the card window, so it opens from any tab; nothing reaches the
daemon until the card is saved.

## Workspace Trust

A VS Code window on a folder nobody has trusted yet runs in Restricted Mode,
where an extension that declares nothing is never activated: no lock, no
window for the daemon, step 2 waiting with nothing on screen saying why. The
editor extension (0.1.21 and later) declares
`capabilities.untrustedWorkspaces` as `limited`, so in such a window it still
activates, binds `127.0.0.1:0` with a random token and writes its 0600 lock —
the window is observed exactly as a trusted one is — and it can still find,
show and close that window's terminals. It refuses the three verbs that
would act on the workspace until the folder is trusted: `spawn_agent`
(`spawned: false`), `send_text` and `reply_native_terminal` (`matched:
false, sent: false`), each with the reply `error` *this folder is not
trusted in VS Code — choose Trust in the Workspace Trust banner, then try
again*. A reply, never a throw: `dispatch.spawn` puts a reply's `error` on
the card verbatim, where a thrown error would read as "no VS Code window
could start it". `send_text` fans out to every window, so it refuses only
after finding the terminal it was asked about — a bystander window stays
silent — and `vscode_reveal.send_text` hands that owning window's words up
(an unmatched reply with an explicit `sent: false` and an `error`), which
the typed reply, wrap-up, Low priority, a question's first keystroke and a
card message all say in place of the generic no-window refusal
(`daemon._typed_nothing_refusal`). A refused Codex native reply typed
nothing, so it releases the turn's one-reply claim
(`_codex_reply_attempts`) and says the words; a silent miss keeps the claim.
The bridge runs `/bin/ps` by absolute path. `vscode.workspace.isTrusted` is read at the moment of each
request (true wherever trust is switched off machine-wide) and one line is
logged when trust is granted. Step 2's hint names the trust question for
windows running an older extension.

## The replay

`tools/first_run_walkthrough.py` replays a newcomer's first launch in a
throwaway home with no real VS Code: it installs the login item (launchctl
stubbed), the hook script and the hook groups; starts a headless daemon on
spare ports in its own process; enrols a `git init`ed project through the
loopback API; writes the lock the editor extension would; and fires a
`SessionStart` through the installed hook script under `/usr/bin/python3`.
It checks the facts go false/false → true/false → true/true, that the
project is not marked `own_checkout`, and that enrolment wrote `.dark-army/`
(key 0600, its own `.gitignore` of `*`, one line in the project's
`.gitignore`) and no legacy key folder. It prints one JSON evidence line and
`VERDICT: PASS` or `FAIL`.

It sets `HOME` and the port variables before importing any Dark Army module
and then refuses to run unless every home-derived path lies inside the
throwaway home — a process that imported them first would write the real
`~/.claude/settings.json`. The CLI refuses the real home, anything above it,
a non-empty folder and ports 19873–19876 before importing anything, and
`walk()` repeats the home check for a programmatic caller; `$HOME` counts as
a real home too where it differs from the account's. A `--hook-socket` that
already exists, or lies under a real home, or outside the throwaway home and
`/tmp`, is refused — the daemon clears a stale socket at that path before
binding. It stubs, by name, launchctl, the VS Code CLI, the pty broker, the background
agents poller, terminal titles, `/compact` and the session-title namer. The
home must be short (`/tmp/da-…`) for the hook socket's path to bind, or
`--hook-socket` names one. `host/tests/test_first_run_walkthrough.py` runs it
as a subprocess — never in-process, for the same reason — at a one-second
cadence. What it cannot see is the native half: a real VS Code deciding
trust, the panel drawing the ticks, the status item.

## The launch line

`launch_report.LaunchReport` (`app.LAUNCH_REPORT`, one per process) holds a
status and a bounded safe detail (`MAX_DETAIL_CHARS` 160, never stderr, never
a path) for three fields. Statuses: `pending`, `changed`, `unchanged`,
`skipped`, `failed`, `unknown`; anything else is written as `unknown`.

| Field | Read from | Rules |
|---|---|---|
| `hooks` | `_hooks_current()` before and after the ordinary conditional install in `main()` — the handler script byte-current **and** both harnesses' configuration current | `unchanged` when current before and after; `changed` when it became current; `failed` when an install ran and the after-read is still not current, or the install raised. Never re-run to measure. |
| `extension` | `vscode_extension.ensure_installed(report=…)` → `EnsureResult` on the `vscode-ext-install` worker | `unchanged` for current or newer (no downgrade); `changed` after an install, detail says a window reload may be needed; `skipped` with no bundled `.vsix`; `failed` for no CLI, a timeout or a non-zero exit (CLI output summarised as "the install command failed"); `unknown` when the check raised |
| `login_item` | `first_run.apply_first_run()` → `FirstRunReport`, then the stale-plist repair in `__init__` if it runs | `changed` only when `launchctl bootstrap` exited 0 (`launchd.EnableResult.bootstrapped`), except the one mid-launch write that never bootstraps — the stale-plist repair (`launchd.repair_stale_login_item`, run when `is_stale()` finds another interpreter **or another module**, as after the 22 Sep 2026 package rename: it boots the old job out unless it is this process and writes the plist, because a `RunAtLoad` job loaded mid-launch would start a second copy) — which is `changed` with a detail naming the next login, drawn verbatim (*login item updated, effective at the next login*); a repair that raises is `failed`, logged, and the app keeps starting (`app._repair_login_item_if_stale`); a plist written but refused is `failed`; a plist already there is `unchanged`; a prior run reports the plist as it stands and touches nothing; a stub returning `None` is `unknown`; a raise is `failed`. `FIRST_RUN_PREFERENCES` stays `{}` and the marker semantics are unchanged. |

It rides the context push as `settings["launch"]` and is drawn by
`FirstRunChecklist.launchLine`: *This launch: hooks current; editor
extension installed; login item enabled.* A `changed` login item whose
detail names the next login is drawn in those words instead of *enabled*.
A pending field reads *checking…*;
a failed one leads the sentence with *This launch had a problem:* and
carries its detail; nothing here ever calls an attempt a success. It sits
under the checklist and, permanently, as an info row under **Settings ▸
Troubleshooting** (`SettingsMenuModel.launchRowId`). A result landing from
the worker calls `_push_panel_context(respawn=False)` through `callAfter` —
a background completion may update the line, never open a panel — and a
result landing before the app exists is held in the report until the first
push.

## Denied notifications

`Notifier.refresh_status(on_status)` asks
`getNotificationSettingsWithCompletionHandler:` (block signature declared
by hand like the other two) and maps `authorizationStatus` through
`authorization_status_name`: 0 → `not_determined`, 1 → `denied`, 2/3/4 →
`authorized`, anything else → `error`; a Mac with no bundle identity or
framework answers `unavailable` at once. One query in flight at a time —
duplicate returns coalesce onto it and every waiter hears the answer. A
`denied` read closes `post`'s gate, an `authorized` read opens it, nothing
else touches it; a request-callback error is **not** a denial. It runs after
the startup authorization callback, on every deliberate open of the panel
(`_on_open_panel`, `_open_panel_on`) and on the panel's
`refresh_notification_status` action, which `applicationDidBecomeActive`
sends while the panel is visible. It never raises the authorization dialog.

The answer rides as `settings["notification_status"]`. Only an explicit
`denied` draws *Notifications are off for Dark Army* with **Notification
Settings…**, which sends the existing `open_notification_settings` action
(`x-apple.systempreferences:com.apple.Notifications-Settings.extension?id=<bundle id>`).
`unknown`, `not_determined`, `unavailable` and `error` draw nothing. Allowing
notifications in System Settings and returning to the panel clears the line
on the next context push, with no relaunch.

## The `~/.bob-companion` link

Until 22 Sep 2026 the app was called Bob Companion and kept its state in
`~/.bob-companion`. That folder was renamed in place to `~/.dark-army` on the
first launch of the renamed app, and the code that carried an install across
was retired on 23 Sep 2026.

On a Mac that made the move, `~/.bob-companion` stays behind as a relative
link to `.dark-army`, **for good**. A pty broker that bound its socket through
the old name keeps resolving to the same socket (`docs/pty-broker-contract.md`),
and a downgrade reads and writes the same folder through it. Because a
project's key folder has the same name, every key walk-up — the notify
script, the status-line collector, the channel server and enrolment — skips
`Path.home()`, so a session under the home directory never reads the link as
a key folder and enrols the whole of `$HOME` (`docs/context-host.md`,
*enrollment.py*).

No code path creates, moves or deletes the link; a fresh install never has
one. The bundle identifiers kept their names. The per-project key folder
**does** move: on every launch Dark Army copies each enrolled project's
`<root>/.bob-companion/key` to `<root>/.dark-army/key` (a folder that
ignores itself, so no project's `.gitignore` is edited), and leaves the old
folder where it is for the read window, read behind the new name. The
shunt wrappers, the close-out shim and the editor extension still use
`~/.dark-army` where it exists, else `~/.bob-companion`.

## Tests

`host/tests/test_first_run_checklist.py`,
`host/tests/test_first_run_walkthrough.py` (the replay),
`panel/Tests/BobPanelTests/FirstRunChecklistTests.swift`, the checklist
cases in `SnapshotStreamTests.swift`, `SettingsMenuModelTests.swift` and
`SettingsWindowTests.swift`. What none of them can see — the frozen bundle's
identity, the native permission dialog, the System Settings destination, the
first launch on a clean account — is the walkthrough document's job.
