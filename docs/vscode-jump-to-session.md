# Jump to session: from a Dark Army click to the right VS Code terminal tab

Exploration only — nothing here is implemented. Findings are from 2026-07-26 on
macOS Darwin 25.5, VS Code 1.130.0, Anthropic `claude-code` extension 2.1.217 /
2.1.218 / 2.1.220, with six live `claude` sessions across two VS Code windows.

**The question.** Click a notification card in Dark Army's window, or the bell row in
the menu bar, and land in the exact VS Code window *and* the exact integrated
terminal tab where that session is waiting for you.

## Verdict

Focusing the right **window** works today, with no macOS permission of any kind.
Switching to the right **terminal tab** is impossible from outside VS Code and
needs a small extension of our own. That is not an effort problem — there is no
channel that executes a workbench command in a *chosen* window. Every candidate
fails for its own reason, and they are enumerated below so nobody re-walks them.

The consolation: in a raised window the session's terminal is usually already
the last-focused element, so window-level focus is most of the value.

## The identity chain

Every link is deterministic and already available:

```
click (sim card / menu row)  → session_id      already plumbed on both surfaces
session_id                   → claude PID      daemon already stores it
PID  → env CLAUDE_CODE_SSE_PORT                `ps -E -ww -p <pid>`, same-user, no TCC
port → ~/.claude/ide/<port>.lock               exactly one window + authToken
PID  → shell PID (direct parent) + tty         exactly one terminal tab
```

Verified live: six `claude` PIDs resolved to ports `36242` / `46141`, matching
the two lock files and their `workspaceFolders`. `lsof` confirms each port is
held by a *different* `Code Helper (Plugin)` process (1338, 63915) — one
extension host per window — even though all windows share main `Code` pid 1279.

### Two keys that look right and are not

**Process ancestry cannot identify a window.** The chain is
`claude(3572) ← zsh(3505) ← pty-host(1332) ← Code(1279)`, and that pty-host is
*shared by every window*. Ancestry proves "this runs inside VS Code" and nothing
more. It is still the right join *inside* the target window (see below).

**The lock file's `pid` is not a window key.** It is the shared main `Code` pid
(1279), identical in both lock files. Match on the port.

**Folder matching is already ambiguous on this machine.** Two open windows both
contain `shop-front` — one as a plain folder, one inside an untitled multi-root
workspace with another project. Any cwd→window heuristic is therefore wrong *today*,
not hypothetically. The env port is the only exact discriminator.

### Where the env comes from

`ps -E -ww -p <pid>` prints another same-user process's environment on Darwin
25.5 — no entitlement, no prompt. If Apple ever restricts it, the notify hook
runs as a **child** of `claude` and can read `os.environ` directly; it already
forwards a message per event, so adding `CLAUDE_CODE_SSE_PORT` is a small change
to `NOTIFY_SCRIPT` in `host/dark_army_menubar/hooks.py` plus its mirror in
`host/dark_army_daemon/protocol.py`. Keep this as the documented fallback.

`TERM_PROGRAM=vscode` (or the presence of `CLAUDE_CODE_SSE_PORT` at all) is the
reliable "is this session in VS Code" test — a plain Terminal.app/iTerm session
has neither, and its ancestry ends in `Terminal`/`iTerm2` instead of a pty-host.

## Goal A — raise the right window

Ranked, all verified against VS Code source.

**1. Anthropic's WebSocket, `openFile {makeFrontmost: true}`.** Exact routing,
immune to the duplicate-folder ambiguity, no TCC. Discovery is the chain above;
connect to `ws://127.0.0.1:<port>` with header
`x-claude-code-ide-authorization: <authToken>`, do the MCP `initialize`
handshake, then `tools/call`. Handler is `showTextDocument(doc, {preview,
preserveFocus: !makeFrontmost})`, so the raise is a *side effect* of focusing an
editor — the bundle never calls an explicit app-activate.

Costs: the interface is private and undocumented (stable byte-for-byte across
.217/.218/.220, but no contract); the server keeps **one** client, so connecting
disconnects the live `claude` CLI's own IDE link; it needs a file to open, which
leaves a stray editor tab; lock files go stale (one here was three days old).

**2. `code -g /abs/file:1`.** Routes through `findWindowOnFile` — prefers the
window whose workspace is a parent of the file, deepest match wins — then
explicitly calls `window.focus()` ("this should help ensuring that the right
window gets focus when multiple are opened", `app.ts`). No TCC, public CLI.

Traps: **`-r` / `--reuse-window` is the wrong flag** — documented as "forces
opening in the *last active* window", i.e. it bypasses window matching entirely.
When no window contains the file it falls back to the last-active window
**silently**. With two windows containing the same folder the tie resolves in
window-iteration order — arbitrary from outside. `code <folder>` reuses a window
only on an *exact* folder/workspace URI match (`findWindowOnWorkspaceOrFolder`),
so against an untitled multi-root workspace it opens a **new window**.
`open "vscode://file/..."` guarantees app-level activation but can trigger the
`security.promptForLocalFileProtocolHandling` dialog.

**3. `open -a "Visual Studio Code"`.** App activation only, lands on the
last-active window. A companion move, not a targeting mechanism.

**4. Accessibility `AXRaise` + title match.** Last resort. Needs TCC
Accessibility (currently denied here — `osascript` returns `-1728`), and it
would fail today anyway: the window titles are `✳ Claude Code — Untitled
(Workspace)`, with no folder name in them. CoreGraphics enumeration works with
no permission but returns `kCGWindowName = None` without Screen Recording.
py2app caveat: TCC grants key on bundle identity **and** code signature, so
ad-hoc re-signed rebuilds silently revoke the grant.

## Goal B — focus the terminal tab

Impossible from outside. Each door and why it is shut:

| Mechanism | Why not |
|---|---|
| Anthropic's WebSocket | No terminal verb. Full surface: `openDiff`, `getDiagnostics`, `close_tab`, `closeAllDiffTabs`, `openFile`, `getOpenEditors`, `getWorkspaceFolders`, `getCurrentSelection`, `checkDocumentDirty`, `saveDocument`, `getLatestSelection`, `executeCode`. The extension *has* the internals (`window.terminals` filtering, `activeTerminal.show()`) but only wires them to its own launch flow. |
| `vscode://` URI handler | `vscode.d.ts`: "In case there are multiple windows open, the **topmost** window will handle the uri." Routed via `ActiveWindowManager` to the last-focused window. Not targetable. |
| `workbench.action.terminal.focusAtIndex1..9` | The commands exist (group-indexed, unbound by default). There is no external channel to execute a workbench command in a chosen window. |
| AppleScript / AX | Needs TCC Accessibility; tabs are labelled by terminal *name*, not PID; walking Chromium's AX tree additionally needs Electron's `AXManualAccessibility`. Doubly fragile. |

### The design that does work

A micro-extension mirroring **exactly** the pattern Anthropic's own extension
uses: a localhost server per window plus its own lock file, so the daemon
connects straight to the correct window's extension host.

```
daemon → ws://127.0.0.1:<our-port-for-that-window>   {pid, tty}
       → match Terminal.processId by ancestry OR tty
       → terminal.show()
```

Why the join is exact: `Terminal.processId` is "the process ID of the shell
process", and that shell is `claude`'s direct parent. Implement it as an
ancestor *walk* rather than a direct-parent check, to survive subshells and
wrappers. Keep `tty` (`ps -o tty= -p <pid>`, one per tab — ttys002/005/006/…) as
a second key: it survives re-parenting if `claude` is ever orphaned. Compare
same-boot and live-process only, since ttys are recycled.

`window.terminals` is *all* terminals in the window regardless of creator, so
user-created terminals work, and none of this depends on shell integration —
that only gates the optional `Terminal.shellIntegration` extras. `processId`
resolves `undefined` for extension-pty terminals and resolves asynchronously
just after creation.

No sandbox blocker: the extension host is a full Node process, one per window,
and extensions may listen on localhost — Anthropic's does precisely this.

One limit to plan around: `terminal.show()` reveals the tab but **does not raise
the OS window** — no API lets an extension raise its own window. Window comes
from Goal A, tab from Goal B.

## The click surfaces

**Menu bar — small.** Notification rows already bind `session_id` and fire a
callback (`_rebuild_notifications_menu` stashes `item._session_id`; the handler
marshals to the asyncio thread). Session rows already carry `session_id`,
`project`, `pid`. A "Reveal in VS Code" child row is ~20 lines. Two constraints:
an `NSMenuItem` that owns a submenu does not send its action, so the item must
be a *child* row — which the current structure already forces; and rumps has no
modifier API, so an Option-click alternative on notification rows means dropping
to PyObjC (`NSEvent.modifierFlags()` inside the callback, or true
`setAlternate_` items via `item._menuitem`). The repo already drops to PyObjC in
four places.

**Simulator — medium, with one non-obvious blocker.** The pipeline is largely
built: SDL mouse events are handled, an LVGL pointer indev *is* registered,
`sim_display_window_to_lcd()` maps window points to the 960×516 framebuffer
correctly, and the outbound sim→daemon channel is proven end-to-end by the
existing `{"event":"dismiss","id":...}` path (`forward_pending_dismiss` →
`sim_socket_send_event` → `SimClient._background_reader` →
`BobDaemon._wrap_sim_inbound`). A new `card_clicked` event is ~10 lines in that
hub. The notification id **is** the session UUID daemon-side, and Dark Army slots
carry `display_id` with a daemon-side reverse map, so a click can always name
its session.

What is missing, in order of nastiness:

1. **The drag hit-test swallows clicks.** `hit_test_cb` returns
   `SDL_HITTEST_DRAGGABLE` for the *entire* interior; on macOS Cocoa consumes
   the mouse-down for `performWindowDrag`, so `SDL_MOUSEBUTTONDOWN` **never
   arrives**. This is not click-vs-drag disambiguation — the events do not
   exist. The fix is carving interactive regions out of the hit test: map the
   point through `sim_display_window_to_lcd()` and ask the UI "is this over a
   card?", returning `SDL_HITTEST_NORMAL` there. The callback fires on every
   hover move, so the query must be cheap and must not walk the LVGL tree.
   Cost: large areas stop being draggable while notifications show — keep the
   sky/top-bar as the grab handle.
2. **No LVGL object is clickable anywhere.** Zero `lv_obj_add_event_cb` /
   `LV_OBJ_FLAG_CLICKABLE` in the tree. The `dismiss_cb` slot exists but is only
   ever invoked from the keyboard path, and the scripted `click X Y` event is
   currently inert.
3. **Click-through-transform is unverified** for the scene subtree
   (`scaled_root`, `transform_scale 768`). Notification UI is parented to
   `screen` at native 3× so it hit-tests directly. Fallback for sprites: manual
   hit-test against the geometry `scene_get_state_json()` already serializes.

## Suggested phasing

| Phase | Scope | Effort |
|---|---|---|
| 1 | Menu bar row + window focus | small |
| 2 | Micro-extension, adds the terminal tab | medium |
| 3 | Clickable card in the SDL window | medium |

Phase 1 exercises the whole identity chain live at minimal cost and ships ~80%
of the value on its own. Phase 3 is pure ergonomics and the only one carrying a
real unknown.

## Open decisions

- **Do we ship our own VS Code extension?** Without it we stop at window-level
  focus. This is the one genuinely product-level call.
- **Which window-focus mechanism**: private-but-exact WebSocket, or public-but-
  silently-wrong-on-ambiguity `code -g`. A hybrid (WebSocket, fall back to
  `code -g`) is plausible but doubles the failure surface.
- **What "jump" means for a non-VS-Code session** (plain terminal, or a session
  the reconciler knows but the hooks never saw). Needs a defined no-op or a
  different target.

## Not established

A marketplace survey for an existing "focus terminal by PID" extension did not
complete. No evidence such a thing exists, but that is not the same as having
checked. Unverified leads: `pokey/command-server` (focused-window only),
REST-style "Remote Control" command bridges (also not window-routable),
Hammerspoon title-based raising recipes.
