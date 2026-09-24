# Panel window contract

This is the full statement of how `BobPanel` behaves as a macOS window: who
owns it, how it leaves, where it is placed, how it is sized and scaled, how
settings are built, and what the keyboard and the pointer do. `CLAUDE.md`
restates the invariants and names the tests; the argument and the detail live
here. What the window *draws* — the rail, the board, the inbox, the detail
pane — is `CLAUDE.md`'s own `### Panel` section.

## Exactly one panel, owned by someone

`Lifecycle.swift` enforces **exactly one panel, owned by someone**: it quits
when `getppid() == 1` (an orphan still holds `panel.pid` and an SSE slot), a
`--hidden` instance evicts the pid in `~/.dark-army/panel.pid` (verified
via `proc_pidpath` first), and EOF on stdin quits when stdin is a **pipe or
socket** rather than when `--hidden` was passed.

## It can actually leave

`NSApplication.terminate` is a *request*, and with an **attached sheet** AppKit
bails out above `applicationShouldTerminate`. Two of the four quit paths are
escalated from outside (`PanelProcess.quit()` follows with `.kill()`,
`PanelLock.claim` SIGTERMs then SIGKILLs a stale holder); the **orphan
watchdog has nobody behind it**, so `PanelExit` splits the departure.

`requested` (stdin `quit`, stdin EOF, the app-menu Quit) ends every attached
sheet first with `NSWindow.endSheet(_:)` — **synchronous** where clearing the
SwiftUI `.sheet(item:)` binding is not — then asks, with a one-second
`.common`-mode backstop under `PanelProcess.quit()`'s 2s wait. `now` is the
watchdog's route: a `Trace` line, `PanelLock.release()` by hand because
`applicationWillTerminate` will not run, then `_exit`.

Sheet-detaching is private to `PanelExit` and legal **only** on a path ending
in process death. An open `NSMenu` starves the main thread and is *not*
addressed.

## The panel is an ordinary window

`.regular` activation policy, a titled/closable/miniaturizable/resizable
`NSWindow` at `.normal` level with `.moveToActiveSpace`, in Cmd-Tab and
Mission Control, with a real menu bar and Dock icon. The red close button and
⌘W put the window away exactly as the strip's toggle does (`windowWillClose`
mirrors `hide()`; `isReleasedWhenClosed = false` keeps the object for the next
`show`). App-menu Quit routes through `PanelExit.requested`; a Dock-tile quit
with a sheet up is refused by AppKit, accepted as residual.
`windowDidChangeOcclusionState` writes the platform's visibility into the same
`visible`/`boardOpen` gate the stdin fast path writes; stdin stays primary,
both writers idempotent through `didSet`. The occluded edge closes the gate after
`OcclusionGrace.seconds` (4 s), cancelled by any seen edge, the fire
recomputing the union; hide, close and quit stay immediate. Appearance stays `.darkAqua`.

## A frame per screen

`Placement.swift` holds **a frame per screen** and the board's tick/fold sets:
`frames`, keyed by `CGDirectDisplayID` holding `[x, y, w, h]`, written with
the read-merge-write discipline of `board_projects` / `board_folded_lanes`.
`applyStoredFrame` resolves the screen (anchor hint → the window's own screen
→ main), takes the stored frame or a centred 0.8-of-visible default, and
**clamps on every show**. Saves ride a 0.5s coalescing timer off
`windowDidMove`/`windowDidEndLiveResize`, never `setFrameAutosaveName`. The
anchor the menu bar sends on `show` picks the *screen*, nothing more.

## Settings is a window with a search box, built from one descriptor tree

`SettingsMenuModel.rows` is the only inventory; the ⋯ button, **Settings…**
and ⌘, open `SettingsWindowController` (`SettingsWindow.swift`; `show` never
re-presents it). `SettingsSearch.groups` heads each submenu and files loose
rows under `looseSection`'s headings (**Sessions**, **Board**, **This app** —
headings, never rows); `filter` matches every token against title, tooltip,
block and heading. The toggle overlay clears **only** on a `$context` publish.
A **key is never renamed** — `preferences.json` is written in keys, so a
rename reads as *absent* and silently resets that switch. `session_timeout`
lives in `preferences.DEFAULTS`.

## The keyboard is one reader, and the verbs are one object

↑/↓ select, Enter jumps, Space unfolds the selected agent's last message in
the pane (`toggleFullText`), D dismisses, S stops, R retires, W wraps up, `/`
filters, Escape climbs `RailLayout.escapeRung`: give up a text box (reply,
terminal input or filter), then close an open History run, then a chosen
History day, then leave History, then close the detail
(`KeyRouter.detailOpen`), then pop the drill, then close the panel.

A local `NSEvent` monitor (`installKeyMonitor`, `KeyMonitor.swift` — an
extension of `AppDelegate`, split out of `main.swift` on 20 Sep 2026 beside
`StdinCommands.swift`), not `onKeyPress`; it asks the
view (`KeyRouter.editing`, published up from `@FocusState`), never
`panel.firstResponder`. Arm-then-confirm lives in `RowActions`, not the row,
and moving the selection disarms it. A **confirmed** Stop or Retire enters
`RowActions.stopping`, which fades the row until the *snapshot* stops listing
it as live (never on the HTTP 200). The filter appears only above eight rows
and **overrides a collapsed section**.

### A focused hosted terminal owns every key

The monitor's terminal branch sits below the sheet, card-window,
knowledge-window, pairing-window and settings-window guards and **above every triage rung**,
gated on `keys.terminalFocused` *or* `TerminalFocus.holdsCaret(window)` — the
published flag rides a 0.15s poll and cannot be the only test, so AppKit's own
answer at the instant of the press is the floor underneath it.

**A focused assistant switcher owns its keys too.** The card face's
`ProviderSwitch` is one focusable group (a roving cursor, ← / → to move it,
Space or Return to choose, Escape to let go, Tab to leave) and it handles
those keys itself through `onKeyPress`. The monitor's guard is
`KeyRouter.controlFocused` — folded by `PanelView` from
`BoardState.switcherFocused`, **the id of the card whose switcher holds the
keyboard** (`nil` for none), `searchFocused`'s route with an identity in
place of a bool — and it sits **below** the terminal guard and **above**
Escape and the letter verbs, returning the event untouched. It is
deliberately not `keys.editing`: Escape on `editing` sends `.clearFilter`,
which empties the board search, and a focused switcher must not. Every card
face reports through `noteSwitcherFocus(card:focused:)`: a claim always
lands, a release lands only from the card holding the slot — a snapshot
that rebinds or recycles *another* card's tile fires that tile's
disappearance, and a bool would have been cleared while the focused row
still drew its ring. The slot is released on the switcher's own Escape, on
its disappearance, and on every hide of the panel — where
`AppDelegate.releaseSwitcherFocus` resigns the first responder (guarded on
the slot being held, so the hosted terminal's caret and a dictation-promoted
field are never the responder it resigns) and the row's own `onChange`
clears it; `PanelView`'s by-hand clears are the floor, never the only
release. The card window and the
ad-hoc sheet need no guard: the window and sheet guards above already return
every key. The rule deciding the tiles, the group's spoken value and where the
cursor may move is `ProviderChoice`, byte-equal on the phone.

Inside the branch nothing is decided: `TerminalKeys.verdict(keyCode:flags:
characters:)` is one pure table — no AppKit event in its signature, so every
row is a test case without a window
(`panel/Tests/BobPanelTests/TerminalKeyTests.swift`) — and the monitor only
performs it. Four verdicts: **`.type`** bytes handed to the view's own
delegate (`TerminalFocus.send`, the route every other keystroke takes),
**`.app`** a SwiftTerm method called directly, **`.swiftTerm`** the event
returned to AppKit, **`.nobody`** consumed and dropped.

| Combination | Verdict | Why |
|---|---|---|
| ⌘⌫ / ⌘⌦ / ⌘← / ⌘→ | `.type` `^U` / `^K` / `^A` / `^E` | a ⌘ key becomes an editing selector and `doCommand` drops the ones it has no case for in silence |
| fn-⌫ (unmodified) | `.type` `ESC [ 3 ~` | lands on `deleteForward:`, same silence |
| ⇧⏎ / ⌥⏎ | `.type` `ESC CR` | a new line, not a send — see below |
| ⌥⌫ / ⌥⌦ / ⌥← / ⌥→ | `.type` `ESC DEL` / `ESC d` / `ESC b` / `ESC f` | the four things Option gave as Meta, restored by name |
| ⌘K | `.type` `^L` | the clear a shell performs itself, so the daemon's emulator sees it and it survives a reattach |
| ⌘C / ⌘V / ⌘A | `.app(.copy/.paste/.selectAll)` | SwiftTerm's own. `copy` is called **only where `selection.active`** — the Edit menu's `validateUserInterfaceItem` gates it there and calling the method straight bypasses that, and SwiftTerm's `copy(_:)` is unconditional: with no selection it clears the pasteboard and writes an empty string. With no selection this does nothing, exactly as VS Code |
| ⌘⌥O, ⌘⌥← / ⌘⌥→, ⌘X / ⌘Z / ⌘⇧Z / ⌘↑ / ⌘↓ | `.nobody` | ⌘⌥O is SwiftTerm's hidden `optionAsMetaKey` toggle and must never fire by accident; the rest reach `doCommand`'s unhandled-selector print |
| ⌘W / ⌘M / ⌘H / ⌘Q / ⌘, | `.swiftTerm` | the window's and the app's; closing the panel hides it and the pty belongs to the broker |
| everything else — Escape, Tab, Return, Space, the arrows, every letter, every ⌃ and every ⇧ combination | `.swiftTerm` | SwiftTerm's handling is correct for all of them, and ⇧ is what selects text while an application tracks the mouse (`shiftBypassesMouseReporting`) |

Ordering inside the function is load-bearing: ⇧⏎ / ⌥⏎ first (Shift alone would
otherwise fall into the unmodified rung), then the ⌥ rows **before** the
bare-key fall-through, then the ⌘ rows, which require `.command` present and
`.control` absent — so ⌘⌃← is SwiftTerm's and not ⌘←'s.

**ESC CR rather than the kitty keyboard protocol.** Return is not a kitty
functional key, so `keyDown` falls through to `interpretKeyEvents` →
`insertNewline:` and the modifier set is empty by the time any encoder sees
it. Negotiating the protocol is not available either: `vtgrid._csi` drops
every private-marked CSI whose mark is not `?`, so `CSI = flags u` and
`CSI > flags u` are never recorded, `Screen.paint()` replays DEC private modes
alone, and **nothing on Dark Army's pty answers a device query at all** — `vtgrid`
is pure and performs no I/O — so an assistant's startup `CSI ? u` goes
unanswered and `keyboardEnhancementFlags` is empty on both sides for the life
of the session. ESC CR is the sequence the assistants already accept.

**Option composes; it is not Meta.** `TerminalLook.apply(to:)` sets
`optionAsMetaKey = false` (SwiftTerm defaults it true) and is re-asserted from
`Coordinator.attach(session:)`, matching VS Code's
`terminal.integrated.macOptionIsMeta` default and Terminal.app, so a Polish or
German layout types its own characters. There is deliberately **no preference
and no settings row**: `SettingsMenuModel.rows` is the settings window's only
inventory and a key can never be renamed.

**A click claims the caret.** AppKit does not promote a plain `NSView` on
click and SwiftTerm's `mouseDown` never asks, so once the caret left the pane
nothing brought it back. `PaneTerminalView` overrides `mouseDown` — calling
`makeFirstResponder` **before** `super`, because a click inside an application
tracking the mouse is forwarded as a report and returns early — and
`viewDidMoveToWindow`, plus a `NSWindow.didBecomeKeyNotification` observer in
`Coordinator.startFocusWatch()` torn down in `shutdown()`. All three ask
`TerminalKeys.claimsCaret(paneAttached:windowIsKey:currentIsTerminal:
currentIsEditable:)`, whose last clause is what stops this becoming a theft in
the other direction: a reply field or the board's search box keeps a caret a
person put there. `keyDown` is `public` and not `open`, so it is **not**
overridden; the monitor is the seam.

**And dictation may not take it.** `DictationFocus.promote` refuses
(`mayPromote(currentIsTerminal:currentIsEditable:)`) while a terminal holds
the caret, and its `.flagsChanged` monitor promotes only for the *recorded*
push-to-talk modifier (`isShortcutModifier`), never for every modifier down.
The old behaviour was the whole fault: `firstEditableTextView` skips only
`isHiddenOrHasHiddenAncestor` and the board under the detail sits at **opacity
0**, so pressing Shift to type a capital letter moved the caret into the
board's search field and every letter after it was read as a triage verb.

**`.app` and `.nobody` both need a terminal that is really there.** The
branch is entered on the poll *or* `holdsCaret`, so for up to 150 ms after the
caret leaves the pane the flag is still true; all three arms hand the event
back when `TerminalFocus.terminal(in:)` is nil, so ⌘X / ⌘Z are not taken away
from the field that now has the caret.

**The OSC-52 clipboard *read* is deliberately unanswered.**
`Coordinator.clipboardRead` returns nil. SwiftTerm's `oscClipboard` answers a
bare `ESC ] 52 ; c ; ?` by base64-ing the delegate's answer back **up the pty,
onto the input line**, and the trigger is output the pty printed — a `cat` of
an untrusted file, a diff, a stray `printf` — not a gesture a person made. So
answering would hand the whole system clipboard, passwords and tokens
included, to whatever is running, with no prompt and no log line. Terminal.app
does not implement the read, VS Code does not, and iTerm2 requires opt-in, so
answering is *more* permissive than all three parity targets; the phone's
identical delegate agrees. The **write** half (`clipboardCopy`) stays, and it
is **not** free: it is reachable from pty output by the same route, so an agent
printing `ESC ] 52 ; c ; <base64>` replaces the person's clipboard silently.
It is kept because it is the direction a person still has to act on — the
substituted text does nothing until they paste it — and because it is what
this pane has always done; gating it behind a preference is its own change,
not this one. ⌘V never comes this way at all: `TerminalView.paste(_:)` reads
`NSPasteboard.general` itself, on a keystroke.

**Paste is chunked, not refused.** `Coordinator.send` splits outgoing bytes
with `TerminalKeys.chunks` at `frameLimit` (128 KiB) — under the daemon's
`TERMINAL_MAX_RAW_BYTES` (256 KiB), which refuses an over-long raw write
whole. Order holds because `TerminalStreamConnection` serialises every write
on its own queue; it is still one socket and still no drain wait. Bracketed
paste survives a reattach because `Screen.paint()` re-emits `\x1b[?2004h`.

## The user sizes the window; content scrolls inside it

`PanelMetrics` sizes drawn bands from constants; the old popover's `Layout`
and `bodyHeight` window arithmetic are gone. No `GeometryReader` may size the
window from the window. `PanelView` still measures process-list content through
`ListHeightKey` into its own `listMeasured`; `tableHeight` uses that measurement
for the inner list, not for the outer window. The fleet rail is 520 logical
points, while adaptive board tiles use 420–600 points in the remaining pane.
Very narrow board panes can clip as documented in `PanelMetrics.boardTileMin`;
ordinary-width usability and native accessibility still require observation
(see [R24 protocol](research/r24-desktop-usability-protocol.md)).

## One scale grows the whole window

`PanelScale` / `ScaleHostView` is the single seam: the AppKit host draws
SwiftUI into a canvas `1 / factor` as many points across, so every
`PanelMetrics` constant, every `Theme.mono` size, every literal frame and
every padding grows together. Neither `PanelMetrics` nor `Theme.mono` is
edited; they stay in logical points. The four named steps (100/125/150/175)
are `PanelScale.steps`; the settings window's **Panel size** heading left on
20 Sep 2026 (never changed from 100%), so a hand-edited `panel_scale` in
`preferences.json` is the one way to pick another — the key is never renamed
and `set_panel_scale` still rides the stdin/stdout channel and no daemon
action table.
The pairing window and the settings window are deliberately unscaled.
The knowledge window is scaled like the card window (eighty notes need
room): one reused `NSWindow`, titled `Knowledge — <label>`, closable /
resizable, `.normal`, `.darkAqua` asserted. `hide()` and `windowWillClose`
`forceClose` it with card, settings and pairing. Occlusion ORs into
`publishSeen()`. The key monitor's knowledge-window guard sits beside the
card-window guard (`owns()` narrow, `isKey` total, below the sheet test).

## The pointer is an affordance too

`Cursor.swift`: `.clickable()` is a transparent overlay owning a
`.cursorUpdate` **tracking area** — not `onHover` + `NSCursor.push()/pop()`,
which leaks here. The overlay's `hitTest` returns nil. Only drawn controls get
it.
