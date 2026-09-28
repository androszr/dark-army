# Dark Army reference

The front page ([README.md](../README.md)) is the story and the quick start.
This page holds the rest: what Dark Army needs, what it installs, how the parts
fit together, every surface and every setting. What it opens on your machine
and your network is [SECURITY.md](../SECURITY.md).

## Requirements

| | |
|---|---|
| **macOS 14 or later** | The menu bar is `rumps`/PyObjC, the window is SwiftUI, the chime is `afplay`, packaging is `py2app`. There is no Linux or Windows build. |
| **Apple silicon** | Built and tested on arm64. An Intel Mac should build from source; untested. |
| **[Claude Code](https://claude.com/claude-code)** | Required. Codex and Grok are optional CLIs; Dark Army checks all three before Start. See [assistant permissions](cli-permission-modes.md). |
| **Python 3.11+** | For building only. The system `python3` (3.9) cannot install `pyobjc-core`; the installed app carries its own frozen interpreter. |
| **Xcode** | Builds the window (`panel/`) and, if you want it, the phone app (`ios/`). |
| **VS Code** *(optional)* | Jump to a session's terminal, auto-compact, Close terminal and starting cards in the project's window. Without it, *Dark Army's own terminal* starts cards on a terminal Dark Army hosts. |

## What every launch installs

Launching the app is the whole setup. On every launch it installs or refreshes:

- **The Claude Code hooks**: `~/.dark-army/dark-army-notify` and its entries in
  `~/.claude/settings.json`. This is what feeds Dark Army.
- **The statusline collector**, where cost and context figures come from. A
  `statusLine` command you already had keeps running: Dark Army chains to it
  and prints its output unchanged.
- **The VS Code extension** `dark-army.dark-army-ide`.
- Hook files for Grok (`~/.grok/hooks/dark-army.json`) and Codex
  (`~/.codex/hooks.json`) where those assistants are installed.

A machine with no `~/.dark-army/preferences.json` is a fresh install, and the
one thing switched on is launch at login. An upgrade never re-ticks a box you
cleared. The first-run checklist and the launch line under it are described in
[docs/first-run-checklist.md](first-run-checklist.md).

## Building from source

```bash
cd host && python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
./build.sh --allow-untagged --install     # build, bundle, install to /Applications
```

`build.sh` builds `panel/` with `swift build -c release`, packages the VS Code
extension, runs `py2app`, assembles the bundle and ad-hoc signs it. Its version
comes from the `vX.Y.Z` tag on HEAD. The flags, in any order:

| Flag | Effect |
|---|---|
| `--install` | Replace `/Applications/Dark Army.app` and stamp the checkout's path into the bundle, which is what enables *Rebuild* under **Advanced**. Without it the app lands in `host/dist/`. |
| `--allow-untagged` | Build an untagged or dirty checkout. It bypasses the version check only; the panel and extension freshness checks stay strict. |
| `--check-only` | Run the checks and stop before `py2app`. |
| `--dev` | Warn and carry on without Swift or npm. Never for a release. |

The build is strict: it refuses a panel that did not compile in this run or is
older than its sources, and an extension package whose version does not match
`vscode-extension/package.json`.

Running without installing:

```bash
cd panel && swift build -c release
cd ../host && .venv/bin/python -m dark_army_menubar
```

Tests: `cd host && .venv/bin/pytest -q` (add `-n auto` for the whole suite
across workers) and `cd panel && swift test`.

## How it works

```
Claude Code hooks ─→ ~/.dark-army/dark-army-notify ─→ unix socket ~/.dark-army/hook.sock
                                                              │
                                                              ▼
                                                      dark_army_daemon
                                                     (inside the app)
                                                              │
                                     HTTP + SSE 127.0.0.1:19874 ─┬──────────────┐
                                                                 ▼              ▼
                                                        menu-bar strip       BobPanel
                                                        (Python/AppKit)      (SwiftUI)
                                     sealed phone door 0.0.0.0:19875 (only while Phone access is on)
```

A hook fires on every session start, tool call, prompt and stop. The handler is
a standalone script that runs under any `python3`, and forwards the event over
loopback. The daemon keeps per-session state, reads each transcript for models,
tokens, cost and branch, reconciles that with `claude agents --json`, Grok's
and Codex's own session records, and serves the result on the local API. The
strip, the window and the phone all read that one picture.

Dark Army makes two outbound requests of its own, both for usage figures: Grok's
billing endpoint (only if you use Grok) and Anthropic's `api/oauth/usage` for the
per-model weekly window. [SECURITY.md](../SECURITY.md) says exactly what each
sends.

## Components

| | |
|---|---|
| `host/dark_army_daemon/` | The daemon: hook intake, session state, transcript stats, history, the board store, the local API |
| `host/dark_army_menubar/` | The menu-bar app, the animated strip, hook and extension installation |
| `panel/` | `BobPanel`, the window |
| `vscode-extension/` | `dark-army-ide`: reveal a session's terminal, type into it, close it |
| `ios/` | `BobPhone`, the iPhone app and its widget ([ios/README.md](../ios/README.md)) |
| `relay/` | The sealed mailbox you deploy for the phone away from home ([relay/README.md](../relay/README.md)) |
| `relay-ws/` | The optional live line beside the mailbox ([relay-ws/README.md](../relay-ws/README.md)) |
| `assets/` | The cast art, portraits and brand mark, all generated by `tools/` |
| `tools/` | The asset pipelines and `tools/demo_shots.py`, which draws the front page's pictures |

## The menu-bar strip

Both mouse buttons open the window; there is no dropdown. The strip shows a face
and a count for the agents working (with a small footnote for their helpers),
a red count for the agents waiting on you, the cards still to do (Prep and
Backlog) printed on a small card, then three stacked meters for Claude, Grok
and Codex, amber past 75% and red past 90% — hover the strip to read the
percentages (and every other number) in words. Resting sessions are not
drawn; when nothing is working and nobody needs you, one resting face holds
the place. When the menu bar is crowded the strip measures itself and drops
detail in order: the helper footnote, then the usage meters together, then
the to-do count. The contract is
[docs/menubar-strip-contract.md](menubar-strip-contract.md).

## The window

The window is the wide pane on the left and a rail on the right. The rail opens
with the fleet counts and usage chips, then four tabs:

| Tab | What it shows |
|---|---|
| **Inbox** | Everything waiting on a person, grouped by project: *answer* (a question or a permission prompt), *look* (a card asking for a hand-check, or one whose assistant has gone), *stopped* (an agent waiting on somebody). Pressing an entry opens its agent or its card. **Dismiss** hides an entry until its subject changes. |
| **Agents** | One tab per project, then a process table: face, name, state, age, what it is doing, context. Selecting a row opens its detail in the wide pane. |
| **Comm** | Mission Control, a standing chief-of-staff session, with quick questions and its terminal beside the board. |
| **History** | The ledger of finished sessions and what they cost, for the project the rail is on. |

An agent's detail shows its figures, its last words or its `## Work done`
report, and the bars that act on it:

- **Reply**: a text box, or one button per answer the agent named.
- **Allow / Deny** a permission prompt, relayed to the live session.
- **Answer a question**: its options as buttons.
- **Close terminal**: closes that session's terminal tab and finishes its card.
- **Low priority**: for a Claude session stopped on a usage limit, types
  `/low-priority` so it can carry on.
- **Jump** (↗) to the session's terminal, and ⌗ to reveal its card.
- **Stop** a live session, or **Retire** a stuck background agent.

Stop, Retire and Close terminal are armed by the first press and confirmed by
the second; none has an undo. Escape backs out a level. A session on a terminal
Dark Army hosts gets a second tab, **Terminal**, with the live screen.

The window is an ordinary macOS window: titled, resizable, in Cmd-Tab and
Mission Control. Its size and position are remembered per screen.

## The board

Four stacked rows, each with a caption, their cards laid out as tiles:

| Row | What it holds |
|---|---|
| **Prep** | Written down, not yet planned. Every new card lands here, typed by you or filed by an agent. |
| **Backlog** | Planned, waiting to be started. A card gets here by having a plan attached. |
| **In progress** | Somebody is working on it, usually an agent Dark Army started. |
| **Done** | Finished, by your drag, the agent closing it, or your press on Close terminal. Folded by default. |

**+ NEW CARD** opens the composer; **Prepare** writes the title, summary and
instructions from one sentence, or press *fill in myself*. A half-typed card
is kept under **DRAFTS (n)**. **Refine** on a Prep card opens a planning
session that attaches a plan and moves the card to Backlog; a tick beside it
starts the card by itself once the plan lands. **START** (armed, then
confirmed) or a drag into In progress opens an agent in the project with the
card's plan; a card with no plan asks *Start unplanned?* first. **HERE** starts
it on a terminal Dark Army hosts. **Done** on a running card moves it to Done.

Opening a card shows its summary, its plan drawn as stages and files and then
rendered in full, the session working it, what the run changed and cost, and
Delete behind an arm. Clearing Done checks the exact set it showed you; if a
card arrives or leaves while you decide, nothing is cleared.

## Projects Dark Army may watch

Dark Army sees only projects you have **enrolled**. Enrolling writes a small
key file into the project (`.dark-army/key`, with a `.gitignore` line) and
records its fingerprint; every hook event carries the key, and anything from a
folder Dark Army does not recognise is turned away: no row, no count. An agent
in a repository you were only passing through cannot use the board to reach
your other projects. Enrol and un-enrol under Settings → **Projects**.

## How many agents at once

A project runs one agent at a time by default. Raise it to four for every
project (Settings → **Board** → *Agents per project*) or for one project
(the In progress heading, while the board shows that project alone). Above one,
two agents may edit the same file. A card started with no free place
**queues**, in the order you pressed, says in words what it is waiting for, and
starts by itself when a place frees if *Start queued cards automatically* is on.

## Notifications

A banner fires when a session starts waiting on you, or a usage window goes
critical: at most once per agent per cooldown, and never while that session's
VS Code window is in front. Clicking the banner opens the window on that agent;
**Open in Editor** goes to its terminal. macOS hides banner buttons in the
Banners style; *Open System Settings…* under **Notifications** switches it to
Alerts.

## The channel and the board tools

Replying to a session and relaying its permission prompts need a way into the
running agent: Dark Army's channel, a small MCP server the daemon reaches over a
loopback port. It is off by default and works only for a session *started*
with it:

```bash
claude --dangerously-load-development-channels server:dark-army
```

Switching it on under **Sessions** installs the server and shows that command.
Sessions started without it still appear; they cannot take a typed reply.

The same server gives agents eight tools, each scoped so a forged message gains
nothing a person would not have to approve: file a card (always into Prep),
attach a plan, attach a scout's report, close its own card, flag a hand-check
on its own card, answer a question on its own card, and read and write the
project's knowledge notes. None takes a card id or a project. Claude gets all
eight, Codex gets four (file a card, attach a plan or a report, close its own
card) and Grok none. The full contract is
[docs/channel-tools.md](channel-tools.md).

## Claude, Codex and Grok

Claude Code sessions have every power on this page. **Codex** sessions are
watched with their context and, where the reading is measured, their cost; a
row can be jumped to where Dark Army can prove which terminal it is in,
stopped only when it is an exactly resumed session, closed once a stopped
native session is proven, and replied to (opt-in) only while stopped. A Codex
session cannot be typed into, and its questions are answered in its own
terminal. A trusted Codex permission hook shows command asks under Needs you;
the [permission modes guide](cli-permission-modes.md) says when they can be
answered from Dark Army. It gets five board tools.
**Grok** sessions are watched and named like Claude's and get no board tools.
Any of the three can be the assistant a card starts. Codex's rules in full are
[docs/codex-contract.md](codex-contract.md).

## Settings

Every setting is in the settings window (⌘, or the button at the top of the
window): a sidebar of eight sections, each a page of cards where every row
shows its explanation under its name, and a search box at the top of the
sidebar that looks across every section — each result carries a trail such as
*Board › Pipeline*, and pressing it opens that section on the row. ⌘1 to ⌘8
switch sections.

| Section | What is in it |
|---|---|
| **General** | **Notifications** (Sound, Banners, *Open System Settings…*), **Panel size**, **Dictation** (the shortcut that starts dictation). |
| **Sessions** | *Session channel (new sessions)*, *Compact full sessions*. |
| **Board** | *Dark Army may start sessions* (off makes Start absent everywhere) with the **Pipeline** under it — *Agents per project*, *Start queued cards automatically*, *Dark Army's own terminal*, dimmed while it is off — then *Close the terminal when a card is done*. |
| **Models** | A table of which model each assistant runs on for Start, Refine and the helpers, machine-wide; each project has its own under **Projects**. |
| **Projects** | Enrolled folders as a list, *Enrol a folder…*, and for the chosen one its agent pack, *Knowledge*, *Checks*, its own model table and un-enrol. |
| **Devices** | *Phone access* with *Away access*, *Relay address…* and *Socket link* under it, *Pair a device…*, and per phone its away window, *Answer from the lock screen* and Un-pair, and for the bot its Read and Write access (Off / 1 hour / 6 hours / 24 hours / No timer). |
| **Security** | Refused knocks on the phone doors and the *Access log…*. |
| **Advanced** | *Reinstall hooks*, *Reinstall VS Code extension*, *Open log*, the launch line, the build line, *Rebuild* when the app was installed from a checkout, and the **Design system** workshop. |
| The sidebar's foot | *Restart*, *Quit* and *Kill switch — stop everything*: every Dark Army process on this Mac, at once, with no clean shutdown ([docs/kill-switch.md](kill-switch.md)). On every section. |
