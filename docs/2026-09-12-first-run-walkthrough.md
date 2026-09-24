# First-run walkthrough — evidence record

**Plan:** *first run checklist* (finding [50], 5 Sep 2026
usability audit).
**Purpose:** the record a person fills in when they walk a genuinely clean
macOS account through installing and launching Dark Army, before and after
the first-run checklist change. It separates what the source *says* will
happen from what was *seen*, and it says plainly which halves have not been
performed.

> **Status at the time of writing (13 Sep 2026, implementation run):** the
> source-derived expectations below are filled in from the code as it is
> after the change. **Neither native walkthrough has been performed.** The
> two sections marked *not yet performed* are empty on purpose; nothing in
> them is inferred, and a source build on the developer's own account is not
> evidence for either. The plan's two MANUAL acceptance criteria stay
> unverified until somebody fills them in.
>
> **Updated 23 Sep 2026** (the *fresh build onboarding fixes* plan):
> §1's paths follow the 22 Sep 2026 rename; §7 records a fresh-build walk
> replayed headless, and the checklist's progression is now checked by
> `tools/first_run_walkthrough.py` in the suite (§6). The native halves
> (§2, §3) are still not performed.

---

## 1. Source-derived expectations (what the code says will happen)

Read from `host/dark_army_menubar/app.py:main()`, `first_run.py`,
`hooks.py`, `statusline.py`, `vscode_extension.py`, `channel_install.py`,
`notifier.py`, and the panel's `PanelView.swift` / `FirstRunChecklist.swift`.
This is **not observed screen order**; the native record below is.

### 1.1 Before the status item exists (AppKit main thread, synchronous)

| Order | What runs | Expected side effect | Reported on the launch line? |
|---|---|---|---|
| 1 | `ensure_state_dir()` | `~/.dark-army` created 0700; private files 0600 | no |
| 2 | `enrollment.enroll_self()` | **Development checkout only**: enrols Dark Army's own repo. A public bundle has no `repo-root` stamp and enrols nothing. | no |
| 3 | `first_run.apply_first_run()` | Fresh account (no `preferences.json`): writes `{"first_run_completed": true}` and **nothing else** (`FIRST_RUN_PREFERENCES` is `{}`), then `launchd.enable()` — writes `~/Library/LaunchAgents/com.bob-companion.menubar.plist`, `launchctl bootout` (expected to fail on a fresh account; fine), `launchctl bootstrap`. | **yes — login item**: `changed` only when bootstrap exited 0; `failed` when the plist was written but bootstrap refused; `unchanged` when the plist already existed; a prior run reports the plist as it stands and touches nothing |
| 4 | `hooks.install_notify_script()` | `~/.dark-army/dark-army-notify` written when absent or different (content compare) | **yes — hooks** (part of the before/after read) |
| 5 | `hooks.install_close_out_script()` | `~/.dark-army/dark-army-close-out` from the bundle resource | evidence only (not on the line) |
| 6 | `statusline.install_statusline_script()`; `install_statusline()` if not installed | `~/.dark-army/dark-army-statusline`; `statusLine` entry in `~/.claude/settings.json` | evidence only |
| 7 | `hooks.install_hooks()` **if** `are_hooks_installed()` is false | Claude hook groups merged into `~/.claude/settings.json`; `~/.grok/hooks/dark-army.json`; Grok rules file | **yes — hooks**: `unchanged` when script and both configs were current before and after; `changed` when they became current; `failed` when an install ran and the after-read is still not current |
| 8 | `threading.Thread(vscode_extension.ensure_installed, report=…)` | Background: `code --list-extensions`; `code --install-extension … --force` only when the installed version is missing or older than the bundled `.vsix`. A window reload may be needed afterwards. | **yes — editor extension**: `unchanged` (current or newer), `changed` (installed; detail says a reload may be needed), `skipped` (no bundled `.vsix`), `failed` (no CLI / timeout / non-zero; CLI stderr is summarised, never shown), `unknown` (the check raised) |
| 9 | Activation policy → accessory | No Dock icon | no |

### 1.2 App construction (`BobCompanionApp.__init__`)

| What runs | Expected side effect | Reported? |
|---|---|---|
| `hooks.set_title_env()` | `CLAUDE_CODE_DISABLE_TERMINAL_TITLE=1` in `~/.claude/settings.json` `env`; Grok `title.enabled=false`; Codex `terminal_title = []` where a literal `[tui]` table exists | evidence only |
| `_install_channel_quietly` (only if `channel_enabled` is on — **off by default**) | `claude mcp add -s user …` | evidence only |
| `_refresh_vscode_ext_state()` | Background `code --list-extensions` for the Settings row | no |
| Stale-plist repair (only if the plist exists and names another executable) | `launchd.enable()` again | **yes — login item** (replaces the first-run report) |
| `Notifier(...)` constructed | Nothing yet; needs the run loop | — |

### 1.3 First run-loop passes (`main()` after `app.run()` starts)

| Timer | What runs | Expected native effect |
|---|---|---|
| 0.1 s (repeating until it lands) | attach the status-item click handler | the strip appears (offline glyph until the daemon thread is up) |
| 0.2 s one-shot | `Notifier.start()` → `requestAuthorizationWithOptions:` | **the macOS notification permission dialog**, once per bundle id, ever. Its callback is followed by an explicit `getNotificationSettings` read, which is the only thing that may publish `denied`. |
| 3.0 s repeating until answered | restart notice | none on a fresh install |

Timer intervals do **not** prove native prompt ordering; the observed order
goes in §2/§3.

### 1.4 The first panel opening (after the change)

- The panel opens on **Inbox** by default. The **first-run checklist** is
  drawn in the rail **above** the tab's content, on Inbox, Agents, Backlog
  and History alike, so no tab change is needed to see it.
- It is decided on the first authoritative snapshot of the opening:
  - the daemon publishes `enrollment.checklist` → **shown** with three
    steps (`Enrol a folder` / `Open it in VS Code` / `Start a session`),
    exactly one labelled *Current step*, the rest *Next*;
  - a main session is already running somewhere → **skipped**, completion
    remembered, no ticks drawn;
  - completion already remembered → ordinary panel;
  - an older daemon with no `checklist` block → the old enrolment prompt on
    Agents, unchanged.
- Step 1 ticks when the folder is in the enrolment ledger; step 2 when the
  daemon has seen **that exact folder** as a workspace folder of a live
  VS Code window (a parent, a sibling of the same name, a nested enrolment
  or a dead lock does not count); step 3 when an admitted **main** session
  runs inside it, in any state — no prompt needs to be sent.
- Editor-only changes reach the panel on the daemon's ordinary 10 s
  snapshot refresh (`SNAPSHOT_REFRESH_SECONDS`) while the panel is visible.
- All three observed → **Setup complete** with three ticks for the rest of
  this opening; `first_run_checklist_completed: true` is merged into
  `~/.dark-army/panel-position.json`; the next opening is ordinary.
- The launch line (*This launch: hooks …; editor extension …; login item
  ….*) sits under the checklist and, permanently, under **Settings ▸
  Troubleshooting**.
- An explicit **denied** notification status draws *Notifications are off
  for Dark Army* + **Notification Settings…** (opens
  `x-apple.systempreferences:com.apple.Notifications-Settings.extension?id=<bundle id>`).
  The status is re-read on every deliberate open of the panel and whenever
  the panel comes to the front; allowing notifications and returning
  clears the line on the next context push, with no restart and no second
  authorization dialog.

### 1.5 Where the checklist cannot be right by construction

- A VS Code window without Dark Army's extension writes no lock, so step 2
  reads *Waiting for VS Code to connect* even though the folder is open;
  the step's instruction says to install or reload the extension.
- A session started from a hosted terminal (not VS Code) ticks step 3 with
  step 2 still current; that is shown truthfully rather than inferred.

---

## 2. Native pre-change observations — **not yet performed**

To be filled in on an isolated macOS test account with a genuine clean home
and the **unmodified** app (the build before this change).

| Field | Value |
|---|---|
| macOS version | _not recorded_ |
| Test account | _not recorded_ |
| App artifact and provenance (path, version, build, tag, signing) | _not recorded_ |
| Download / quarantine / first-launch prompts (order, exact words, timestamps) | _not recorded_ |
| First status-item appearance (time from launch) | _not recorded_ |
| Notification permission dialog (time, decision) | _not recorded_ |
| Login-item notification from macOS, if any | _not recorded_ |
| Panel: default Inbox on first open (screenshot, exact words, controls) | _not recorded_ |
| Panel: Agents view while unenrolled (screenshot, exact words) | _not recorded_ |
| Enrol a disposable folder (route used, result, time) | _not recorded_ |
| Open that folder in VS Code (extension state, reload needed?, time until the row's project resolves) | _not recorded_ |
| Start a Claude session without a prompt (time until the first visible row) | _not recorded_ |
| File changes, without secrets: `~/.dark-army/*`, `~/.claude/settings.json` hooks/env/statusLine keys, `~/.grok/hooks/*`, `~/.grok/config.toml` title key, `~/.codex/config.toml` title key, LaunchAgents plist, VS Code extensions list | _not recorded_ |
| Channel / statusline / title-pen / agent-pack activity (attempted, written, active, unknown) | _not recorded_ |

---

## 3. Native post-change observations — **not yet performed**

To be filled in on a **second** clean test account with the candidate
bundle carrying this change.

| Field | Value |
|---|---|
| macOS version, account, artifact provenance | _not recorded_ |
| First opening: all three steps visible on Inbox without a tab change? | _not recorded_ |
| Step 1 ticks after enrolling the disposable folder (time) | _not recorded_ |
| Step 2 ticks after opening that folder in VS Code (time; ≤ 10 s while visible expected) | _not recorded_ |
| Step 3 ticks when the first row appears, before any prompt (time) | _not recorded_ |
| "Setup complete" shown for the rest of the opening | _not recorded_ |
| Close the session, reopen the panel, restart Dark Army: ordinary quiet panel, no checklist | _not recorded_ |
| Launch line as displayed vs. actual outcomes (hooks / extension / login item; plus statusline, title, channel side effects for the inventory) | _not recorded_ |
| Smallest supported panel size: every label, current step, tick and action reachable | _not recorded_ |
| VoiceOver: each row reads "Done / Current step / Next: <title>" | _not recorded_ |
| **Denied notifications:** Don't Allow on the first dialog → denied line + Notification Settings on the panel? | _not recorded_ |
| Notification Settings link lands on Dark Army's own notification page? (bundle id, any OS deep-link fallback) | _not recorded_ |
| Allow there, return to the already-open panel: line gone within 5 s, no restart, no second dialog? | _not recorded_ |

---

## 4. Artifact provenance

| Item | Pre-change | Post-change |
|---|---|---|
| Bundle path | _not recorded_ | _not recorded_ |
| `CFBundleShortVersionString` / build | _not recorded_ | _not recorded_ |
| `release-manifest.json` / `build-manifest.json` contents (numbers only) | _not recorded_ | _not recorded_ |
| Git tag / commit | _not recorded_ | _not recorded_ |

---

## 5. Installer outcomes (what the launch line said vs. what happened)

| Category | Displayed status | Actual (from file changes / `launchctl print` / `code --list-extensions`) | Agree? |
|---|---|---|---|
| hooks | _not recorded_ | _not recorded_ | _—_ |
| editor extension | _not recorded_ | _not recorded_ | _—_ |
| login item | _not recorded_ | _not recorded_ | _—_ |
| statusline (inventory only) | n/a | _not recorded_ | — |
| title pen (inventory only) | n/a | _not recorded_ | — |
| close-out script (inventory only) | n/a | _not recorded_ | — |
| channel (inventory only; off by default) | n/a | _not recorded_ | — |

---

## 6. Unverified checks

Both of the plan's MANUAL acceptance criteria are **unverified** as of this
document's last edit:

1. The real first-run journey and checklist progression on an isolated
   account (plan steps 1–8 of the first MANUAL criterion). Why not
   automated: quarantine and first launch, native permission prompts,
   editor-host activation, real session-row timing and visual/accessibility
   presentation need a real installed app on a graphical account; the
   classification, source state and result handling are covered by
   `host/tests/test_first_run_checklist.py` and
   `panel/Tests/BobPanelTests/FirstRunChecklistTests.swift`.
2. Denied-notification recovery, including the System Settings deep link's
   destination (plan steps 1–5 of the second MANUAL criterion). Why not
   automated: the OS-owned destination and the live permission UI cannot be
   verified by an in-process mock; the status mapping, the return refresh
   and the callback delivery are covered by the same two test files.

**The checklist-progression half now has an automated stand-in.**
`tools/first_run_walkthrough.py`, run by
`host/tests/test_first_run_walkthrough.py`, replays a first launch in a
throwaway home (§7): installed hook script under the system Python, a real
headless daemon, an enrolled folder, the editor extension's lock, and the
checklist's two facts going false/false → true/false → true/true. It does
not stand in for the native halves of criterion 1 — quarantine, the
permission dialog, a real VS Code activating the extension (trusted or
not), the panel drawing the ticks — which stay unverified.

What the automated suite **cannot** see and this document is for: the
frozen bundle's identity (the notification centre traps without one), a
real editor host deciding Workspace Trust, and AppKit layout at the smallest
panel size. The installed hook path under the system Python is now exercised
by the replay, against a daemon it starts itself.

---

## 7. Headless replay, 23 Sep 2026

**What was walked.** A fresh clone of `d416b94`, `./build.sh
--allow-untagged`, and the first launch replayed from the bundle's own
Python in a throwaway `HOME` with `launchctl` stubbed, a headless daemon on
`2987x` ports, and `/api/state`'s `enrollment.checklist` polled.

| Observation | Recorded |
|---|---|
| Clone | a full clone of the history failed after 10 min; `--depth 1` took 8 s |
| Host venv (Python 3.11+) | 10 s; a system-Python 3.9 venv fails on `pyobjc-core` with an opaque wheel error |
| Build | about 2.5 min warm; about 6 min with a cold panel build; codesign verified |
| Self-enrolment | `enroll_self()` → `(True, 'dark-army is enrolled')`: the checkout ticked step 1 for itself, and step 2 then asked the newcomer to open Dark Army's own folder in VS Code |
| Workspace Trust **on**, folder untrusted | `editor_observed` stayed false for 2 min: the extension declared nothing for untrusted workspaces, so VS Code never activated it and no lock was written |
| Workspace Trust **off** | `editor_observed` ticked about 12 s after the window opened |
| "Dark Army's own terminal" on | step 2 could never tick with no VS Code window, though the session ticked step 3 |
| "Setup complete" | no next action named |
| Project key folder | enrolment wrote `<project>/.dark-army/` (key 0600, its own `.gitignore` of `*`) and one `.dark-army/` line in the project's `.gitignore`; no legacy key folder |

**What changed.** The daemon marks Dark Army's own checkout
(`own_checkout`) and the checklist never follows or offers it; step 2's
hint names the trust question; the editor extension 0.1.21 declares
`untrustedWorkspaces: limited` — it activates and writes its lock in a
Restricted Mode window and refuses `spawn_agent`, `send_text` and
`reply_native_terminal` there in words until the folder is trusted (this is
the declaration shipped pending the security review; were `limited`
refused, the manifest says `{"supported": false}` and this line quotes the
refusal); step 2 reads *Not needed* with Dark Army's own terminal on; and
*Setup complete* offers **Write your first card**
(`docs/first-run-checklist.md`).

**The replay, re-runnable:**

```
cd host && .venv/bin/python ../tools/first_run_walkthrough.py \
    --home /tmp/da-walk-$$ --api-port 29874 --hook-port 29873
```

Its output on 23 Sep 2026 (working tree on `b264719`, default cadence,
about 22 s):

```
{"api_port": 29874, "daemon_stopped": true, "enrol": {"ok": true, "status": 200}, "facts": [[false, false], [true, false], [true, true]], "failures": [], "first_run_applied": true, "hook_port": 29873, "key_dir": ".dark-army", "key_dir_gitignore": "*\n", "key_mode": "0o600", "legacy_key_dir_present": false, "notify_exit": 0, "notify_python": "/usr/bin/python3", "notify_python_version": "Python 3.9.6", "notify_script_present": true, "own_checkout": false, "plist_present": true, "project_entries": [".dark-army", ".gitignore"], "project_gitignore_lines": 1, "verdict": "PASS", ...}
VERDICT: PASS
```

(`home`, `state_dir`, `project`, `hook_socket` and the hook-event list are
elided above; they name the throwaway folder.) The same replay runs in the
suite at a one-second cadence (`host/tests/test_first_run_walkthrough.py`).
