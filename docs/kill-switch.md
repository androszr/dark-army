# The kill switch

One press, and nothing of Dark Army's is left running on this Mac.

Quit is the polite verb. It stops the daemon cleanly and *deliberately leaves
the hosted terminals up*, so a relaunch reconnects to them instead of SIGHUPing
them. This is the other verb, for the moment when that is exactly the wrong
answer: the app is not answering, something is wedged, or you simply want it
all off. The daemon is **not** shut down cleanly — a clean stop is what you
press when the app is still listening. The stores are SQLite in WAL mode and
survive the process ending under them.

The module is `host/dark_army_menubar/kill_switch.py`. Its identification
half is pure and table-testable — no psutil, no signals, no clock. `snapshot()`
and `terminate()` are the only two functions that touch the machine. Pinned by
`host/tests/test_kill_switch.py`.

## What it may signal is a proof, never a guess

`owns(argv, ...)` names a process only because that process's **own argv** says
it is running Dark Army's code. Four rules, and every one of them is an exact
element or an exact path — never a substring, because `grep`, an editor and
this project's own tests all mention these names:

| Rule | Matches |
|---|---|
| `app bundle` | `argv[0]` inside the `.app` this code is executing from |
| `module <name>` | `-m` and one of `OWN_PACKAGES` as **two adjacent exact elements** (`ptyhost.stray_broker_pids`' rule) |
| a helper's name | an argv element that is exactly `<state dir>/<one of STATE_HELPERS>` |
| `panel` | `argv[0]` equal to a path the app named outright |

**`dev_build.is_frozen()` gates the bundle root, and that gate is
load-bearing.** Outside a bundle `dev_build.bundle_path()` falls back to
`NSBundle.mainBundle()`, which for a plain interpreter answers the *Python
framework's* own `Python.app` — a real bundle, just not ours. Believed, that
puts every process running under that interpreter on the list, which on a
development machine is most of them. Both functions read the same `__file__`,
so the gate is exact.

The fourth rule exists because a checkout has no bundle and the panel binary
sits outside one; `PanelProcess.executable_path` is the immutable choice the
app made at startup, so it can be named rather than guessed at.

Containment is component-aware: `/Applications/Dark Army.app` does not
admit `/Applications/Dark Armyista.app`. A session whose *prompt* quotes
`~/.dark-army` is saying a word, not running our code, and is never
signalled for it. `STATE_HELPERS` names each helper once, by its
`dark-army-*` name.

## Then the tree below each

`plan()` sweeps the descendants of every named process. That is how an agent
inside one of Dark Army's **own hosted terminals** is reached: those are
started with `start_new_session=True`, so killing the broker alone would orphan
them, and being the broker's child is the same kind of proof. The list is
ordered **children before parents**, by depth computed from the ppid chain
(a target may itself be another target's child — the broker under the app —
and would otherwise sort as a root).

Above `MAX_VICTIMS` (200) the plan **signals nothing at all** and says so. An
over-wide sweep is the one failure this button must never have.

## What it never touches

A `claude`, `codex` or `grok` session running in a **VS Code terminal**. Dark
Army asked the editor to open that tab; the process is the editor's child and
the person's work. It loses Dark Army — its channel server is ours and does go
— and carries on.

## Signalling

`terminate()` sends SIGTERM to every victim, waits `GRACE_SECONDS`, then
SIGKILLs whichever pid is **still the same process**: start time is the
identity, because the machine may recycle a number inside the grace window.
One pid at a time, and **never a process group**, whose membership this module
has not proved.

## The two surfaces, both armed then confirmed

- **The settings window's sidebar footer** — pinned there beside Restart and
  Quit, on every section. `SettingsMenuModel.killRowId`, first in the
  inventory and the only row in it with `danger` set (alarm ink and an alarm
  edge; the title already carries the meaning, so colour is additive).
  `SettingsWindowState`
  gives it its own arming slot for the reason every armed verb has one. The
  confirmed press sends `kill_all` on the panel's stdout channel — the
  menu-bar app owns every process here, this one included. Nothing is sent
  back: the surface a reply would land on is one of the things being stopped.
- **The emergency menu's leading row** — it leads because that menu exists for
  exactly the state this button is for. A menu item has no second surface to
  arm on, so the first click retitles the row and the second one goes;
  `KILL_SWITCH_ARM_SECONDS` later it disarms itself, because an armed row left
  armed for ever is a trap. rumps keys an item by the title it was *built*
  with, so the retitle does not move the key.

## Where it is not

No board verb, no channel tool, no loopback verb, and not on `LAN_ACTIONS` or
`REMOTE_ACTIONS`. **The phone may not press it**, and neither may an agent.
