# Panel context — the Mac window, the rail, the inbox and the API layer

Relocated verbatim from `CLAUDE.md` on 20 Sep 2026 (`docs/ship-efficiency.md`
holds the paragraph map). This is the subject document for **everything the
Mac panel draws and how it reads the daemon**: the workspace pane and the
rail, the process table, the detail tabs and the hosted terminal, the inbox,
the window contract, the board's drawing, the card window and composer,
drafts, markdown, areas, chatter and the three rules of the API layer.
Loaded when the work touches `panel/` or `ios/` — and always on the
conservative fallback (`docs/agent-context.json`). The board's *rules* are in
`docs/context-board.md`.

## Panel (`panel/`)

A SwiftUI/AppKit executable (`BobPanel`). Takes the desk token off the menu
bar's context push (never from disk), GETs `/api/state`, holds `/api/events` (SSE) open, and renders the live buckets
with a per-agent face from `assets/cast`. Launched and driven over stdin by the
menu bar (`panel_process.py`), not respawned per click.

**The workspace is a wide pane beside a rail, in one window** (`PanelView.body`):
a `BrandBar` across the top, then an `HStack` of the wide pane taking every
point that is left, a hairline, and the fleet as a fixed `PanelMetrics.width`
rail on the right. The wide pane is `BoardView` **or the selected agent's
detail** (`AgentDetailPane`), decided by `RailLayout.leftPane(tab:selected:)`
— the detail only on the Agents tab with a row selected; Inbox always shows
the board. Three routes bring the board back, and all three
unselect the row (`deselect()`): a second click on the selected row
(`RailLayout.selectionAfterTap`), Escape (`TriageIntent.deselect`), and the
detail header's `‹ Board` button. `showCard(card:)` closes the detail so the
reveal has a board to land on; a banner tap keeps the selection and opens
the detail. **The reverse jump from the editor** calls `showCard` when that
session has a card, so the board is what you see (Grok and Codex included).
**`showCard(card:)` sets `revealedForCard` after
`deselect()`**: on a hidden panel the reveal runs *before* the open's aim,
and `selectTopAttentionIfNeeded` reads the hold (never consumes it) so the
top waiter's detail is not drawn over the card just revealed. A changed row
list re-aims the selection only where it was *pruned*
(`RailLayout.reaimAfterListChange`), so a closed detail stays closed; a
deliberate open still aims at whoever needs you — on `FocusRouter.shown`,
bumped by the stdin `show`/`toggle` path when the panel was not on screen,
**never** on `client.visible`, which the occlusion callback also writes and
would reopen a closed detail on a window flip. That aim runs against the
snapshot frozen at the last `hide()` (which stops the SSE client), so the
`shown` handler also sets the one-shot `aimPendingSinceShown`, spent by the
first live snapshot through `RailLayout.aimAfterSnapshot(pending:selected:
revealedForCard:)` — aim only where nothing is selected and no card is held;
`deselect()`, a row click, `show(_:)` and a hidden panel cancel both flags.
**`BoardView` is never unmounted**: the `ZStack` keeps it under the detail at
opacity 0, hit-testing off and hidden from VoiceOver — **never `.disabled`**,
which propagates `isEnabled` into the plan-gate dialog and the drafts sheet
the board owns, and would leave one that is up modal with a greyed-out
Cancel — so its `@State` (the applied card-focus mark, the drafts sheet,
the row folds) survives every selection and its `.onChange(of: cardFocus.request)`
lands the reveal; opening the detail drops the board search field's caret
(`board.searchFocused = false`). The board is still not a mode, so its
shutdown is the panel's problem (`Lifecycle.swift`).

**Inside the rail, projects are tabs and the fleet is an htop table.**
`ProjectTabStrip` is the first grouping (`resolvedTab`, counts and a red dot
per tab from `tabFlags`, `Other` last); under it `ProcessTableHeader` + one
`ProcessRow` per session. Columns: face / NAME / STATE / AGE / CMD / `⌗` `↗` /
CTX. AGE is compact (`12s` / `5m` / `1h`) from `started_at`, never
`duration_seconds`; an undated row shows a dash. The two chips are the
detail header's, reachable from the list: `⌗` reveals the session's card on
the board (absent where the session is bound to no card), `↗` raises its
terminal through `Triage.jump` (absent unless `isJumpable`); both slots are
reserved on every row and in the header (`test_process_row_chips.py`). The message, the reply, the wrap-up and the
permission bar live in the `StdoutPane`, drawn by `AgentDetailPane` **in the
board's place** for the selected row only. The detail leads with `AgentDetailHeader` (`‹ Board`, face, name, `project ·
branch`, state, the `card ⌗` / `jump ↗` chips), which stays put on both
tabs. **A session Dark Army started says so on its own row.** `origin.py` stamps
`BOB_COMPANION_ORIGIN` into the terminal at the spawn, the hook carries it
beside the enrolment key, and the row publishes `origin_by` / `origin_card` /
`origin_line` — composed once by `origin.sentence()`, drawn **verbatim**,
**all three absent** where nobody stamped one. Four kinds (Start, Refine, a
consult, an ad-hoc terminal, naming none); the tombstone carries it too;
**Codex never carries one**, running no hook handler. An **attribution
boundary, not authentication**: a forged stamp grants nothing, a missing one
draws nothing (`docs/session-state-contract.md`, `docs/codex-contract.md`).
**Subagents are not drawn as rows**: both clients fold `_subagent_rows` into
`Agent.subagentSummary` and the helpers line. The Mac Fleet row's CMD column
leads with the newest live helper's role before the parent's task, so the
working stage is visible without opening the detail. A row carries the parent's
origin card as a **second** field (`card_title`, read as
`SubagentRow.qualified`), never folded into `label`, which keys
`liveStageNames`: folding it in stops a card highlighting its stage. **A session on a terminal Dark Army hosts has two tabs under that band and
a row in an editor has none**: `DetailTab` (`DetailTab.swift`, copied
byte-equal into `ios/BobPhone/AgentDetailView.swift`) is four pure rules —
`defaultTab`, `showsTabBar`, `pane`, `terminalAttached` — that both surfaces
read and neither re-derives, tabled in `DetailTabTests.swift`. **Details**
is the plain pane (`StdoutPane`, `messageHidden: false`) and is where
**every** open lands: no persistence,
no per-session memory, and `.onChange(of: agent.sessionId)` re-applies it
when the selection moves between rows, where SwiftUI keeps the view's
identity. **Terminal** is the live screen, `TerminalPane` filling what is
left, with permission, question, reply, wrap-up and low-priority bars kept
above it as a compact strip when they have something to show
(`StdoutPane.messageHidden`) — a permission ask must be answerable without
leaving the screen it is about. **Leaving the Terminal tab is the
disconnect**: not drawing the pane fires its `.onDisappear`, which cancels
the socket and sends `panel_terminal` empty, so `_panel_focused_sessions()`
empties and the pty's width goes to the phone; returning re-attaches and
every connection opens with `Screen.paint()`, so nothing is lost by
leaving. `alerts.py` re-arms this session's banners while Details shows,
because the screen is not on screen. The switch takes **no key binding**.
**The pane is one socket, not
requests** (`terminal_stream.py`, `TerminalStream.swift`): `GET
/api/terminal/stream?session=` with the write token is answered `200` and
held open as a framed stream both ways — `D` bytes / `X` exit / `E` refusal
down, `I` keys / `S` size up, `kind + u32 length + payload`. The opening
paint is `Screen.paint()` — the emulator's own drawing, scrollback and DEC
modes included — **never the raw ring**, which starts mid-sequence — as
**one or more `D` frames** under `PAINT_CHUNK_BYTES` / the codec's
`max_plain`, so a picture past `MAX_FRAME_BYTES` arrives whole; a send
failing for anything but a hang-up logs and sends
`STREAM_SEND_FAILED_REFUSAL`, held by `TerminalNoteHold`. Keys go up in
order with no drain wait and **no permission-prompt refusal** (the dialog
is on this screen; the phone's line route keeps every refusal). SwiftTerm
is fed straight from the socket, never through `@State` (hidden, it holds
them in `HiddenTerminalBuffer` until the visible edge), and is the one owner of the pty size (`sizeChanged`, debounced; a repeated size is not a
line to the broker). `allowMouseReporting` is **off**; the look is VS
Code's Dark Modern (`TerminalLook`). The emulator parses on *read*
(`Screen.defer`, `LAG_SYNC_BYTES`) — the loop's pty reader parses nothing
— and the broker carries bytes as `b64`, so the two counters agree across a
split character. **The phone draws the same emulator** (SwiftTerm in
`ios/BobPhone/TerminalPane.swift`): at home on the same stream, sealed
per frame on the phone door (`POST /api/terminal/stream`, `SealedCodec`,
a kind on `_home_open` and never an action); away fed by the sealed
`terminal` read's `since_bytes` leg — bounded by `TERMINAL_POLL_RAW_BYTES`
with `data_more`, the paint on `-1` or a fallen-off cursor too
(`_terminal_paint_tail` holds the rest), `grid=0` skipping the rows — and
typing raw `bytes` on `terminal_input`
under the desk's rules. **The width has one owner at a time**: the Mac
while the pane is showing the session (`_panel_focused_sessions()`, whose
one input is the pane's own `panel_terminal` heartbeat — it states the
session id only while the panel can be seen, so nothing is anded onto
it), the
phone once it is not (`terminal_phone_resize`, the hand-back
`_panel_pane_size`).
`terminal_stream_supported` on `_pipeline_writable()` is the marker; long
form `docs/transport-contract.md`, `docs/phone-contract.md`. Pinned by
`test_terminal_socket.py`,
`test_terminal_stream.py` and `test_phone_terminal.py`. The table is three
`processBand`s — Active, Recently finished, Abandoned — each a `PanelSection`
under the shared `SectionHeader` with its own count and fold, in a 7.5-row
viewport, absent rather than empty. **The viewport is measured, not
counted**: `processViewportHeight(count:measured:)` takes the band's own
`ListHeightKey` height (the same seam as `tableHeight`), so the row height is
the rows' business, and `processBandFits` switches scrolling off only when
the measured content fits — `PanelMetrics.processRow` (24pt) is the first
frame's estimate against a 26pt drawn row, and sizing from it cut the last
row mid-line. Pinned by `ProcessBandHeightTests.swift`.

**Order is `byStart` and status is not a term in it** (the daemon's
`started_at`; the panel oldest-first, the phone newest-first). A start time is
fixed for the life of a row, so the table only reorders when a session starts
or ends; an undated row sorts last, and the session id breaks ties.

**"Needs you" is a property carried on the row, not a place above it.**
`attentionRows` — the `waiting` bucket plus any row carrying a notification
card or an open permission prompt — is ranked prompts → cards → plain waiting
→ held-by-dwell, and drives the BrandBar count, the red tab dot, the widened
red edge on `ProcessRow` (STATE keeps telling the truth — `run` stays `run`
on a prompt-blocked row), and `selectTopAttentionIfNeeded` on open. It does
not *move* the row: a waiter is listed where it lives, once (`visibleRows`
walks one id per row). `attentionHold` delays *leaving* by `attentionDwell`
(5s), never arriving, because `needsHuman` **flaps** at turn cadence; the
dwell is stamped from each snapshot (`refreshAttentionHold`, driven by
`generatedAt`) as a **`Set` of ids, not a clock** — a `[String: Date]`
compares unequal on every snapshot — with times in `HoldStamps`, a reference
box `@State` cannot invalidate on. Nothing that needs a human is ever held
*back*.

**Everything waiting on a person is one list** (`Inbox.swift`, `InboxView.swift`).
The rail's tabs are **Inbox**, Agents, Comm, **History** and **Reports**,
and Inbox is the first case of `Tab` and the panel's default. The Reports
tab is the scout-report list in the board's place
(`RailLayout.LeftPane.reports`, `ScoutReportsPane`), the rail its search and
project rows (`ScoutReportsRail`), a text search merged in after a pause;
Escape closes an open report, then leaves
the tab (`closeReport`, `leaveReports`); `docs/transport-contract.md` holds
the two reads. History is the desk-only
wide ledger in the board's place (`RailLayout.LeftPane.history`); the rail
is its scope; its headline is `token_cost_usd` (a reported dollar sits
beside it, never added; no price is a dash, never $0.00); fetched on open
and on a range or project change, not on the SSE loop; `GET /api/lifecycle`
stays, undrawn. The phone's History is the week alone, folded by
`LedgerWeek`, byte-pinned (`test_ledger_week.py`). The card window fetches one
session's record through `SessionRecord.swift`. Every rail section — Active, Recently
finished, Abandoned — folds through the one `SectionHeader`
(`RailWidgets.swift`; buckets and sections are `RailSections.swift`). The inbox is grouped under project
headings in `projectNameOrder` (`Other` last), a heading drawn **only where
there is more than one project**, and draws **three kinds, named by what
you do**, which are also their rank: `answer` (a permission ask or a
question), `look` (a card whose assistant has gone, or a hand-check
somebody asked for), `stopped` (an agent waiting on somebody).
`InboxWireKind` (`permission`, `question`, `ended_work`, `manual_check`,
`waiting`) is the name `inbox_ack` is keyed on and ranks inside a kind. **Nothing FYI is listed**: a ready plan
is the Backlog tab's, a closed card awaiting review the Done column's, a
burst of refused knocks the access-log window's. No tag, every edge red,
and the count is the length of the list on every surface.

**Which side decided each.** The daemon decides `endedWork` (`needs_you`) and
`manualCheck` (`manual_check_due`) — both need hook-stream freshness that is
not on the wire — and `Inbox.swift` **consumes them and re-derives neither**
(`test_inbox_surface.py`). The panel decides the rest: `permission` off the
prompt list, `question` off `Agent.questionList`, `waiting` off
`attentionRows` (the daemon's bucket plus the 5s anti-flap dwell). The
rule: **a judgment goes into the daemon exactly when it needs evidence the
panel does not have on the wire.** `needs_you` asks a second thing — that
the last state Dark Army saw *ended a turn* (`_mid_turn_quiet_ids()`,
`docs/session-state-contract.md`): silence inside one long tool call is a
working agent, not a lost session.

**A turn that ends on a work report is waiting on nobody, and the daemon
says so at the Stop** (`docs/session-state-contract.md`):
`session_stats.finished_quietly` reads the closing message — `## Work done`
with no `bob-tldr` / `bob-actions` — and the `add` path, **after** the
parking rungs and not as one of them, raises no card and writes `idle`
(`quiet=True`), so the row sleeps instead of asking. `StopFailure` and a
report with a summary are untouched. Claude only; `test_quiet_finish.py`.

A `tab_gone` row says "tab gone" and draws the same `# work done` block
(`WorkReport`, `test_work_report_parse.py`; *A Grok turn outlives its tab*).

**At most one item per row and one per card, and a card bound to a session
is one subject with it** (`Inbox.oneEntryPerSubject`, the phone's twin: after
the dismissals the top-ranked entry naming a session wins), and **no
kind-written sentence**: the word on the row (`InboxKind.word`) and the detail — the
ask, the question, the agent's `last_summary` or the tool it stopped on, a
card's summary or steps — say what this is. `Inbox.sentence(for:refusal:
queueReason:)` is **Dark Army's own words or nothing** (refusal, then queue
reason), drawn only where non-empty.

**The inbox routes; it does not answer, on both surfaces.** The whole row
is one press: a session entry calls `show(_ row:)`, a card entry
`showCard(card:)`; the phone opens the agent or card sheet. Every board
verb, the answer box and the permission verdict live on the destination. **One verb,
one meaning: Dismiss.** A row's Dismiss (a swipe on the phone) and the
header's **Dismiss all** (armed then confirmed, disarmed by a changed list)
are `inbox_ack` — a daemon-shared hide until the subject changes; **a
session subject also drops its card and demotes `confused`/`error` to
`idle`** (`_settle_acknowledged_session`), so the strip, widget and row go
quiet too. `InboxWireKind.dismissable` is every kind but `permission`
(`inbox_ack.ACK_KINDS` / `NEVER_KINDS`, the same partition; a retired kind
is refused as unknown). Acknowledge, Clear all and the phone's
swipe-to-delete are gone; snooze stays absent. **Every entry draws the
agent's own face** (`inboxFace(for:)`; never a hashed stranger) and **how
long it has waited** from an absolute `since` stamped once in
`Inbox.items(now:)`; undated draws no clock. The phone's Needs you tab is
the same list as **compact rows** (`InboxRow`; a card entry is undated),
**Catch up across projects** sits on the Fleet tab
(`docs/phone-contract.md`), and its session entries come from the live
buckets alone (`PhoneInbox.liveBuckets`), the Mac's `Category.live`.

The phone restores the tab, Menu section, sheet trail and drafts after the
lock and a cold launch (`docs/phone-contract.md`,
*The phone comes back where you left it*).

**The rail uses Signal** (`Theme.swift`, `docs/design-system.md`). Generated
semantic tokens from `design-system/tokens.json` supply graphite surfaces,
warm text, green actions, amber attention and red failure. Human prose uses
`Theme.prose`; paths, commands, IDs, measures and terminal output use
`Theme.mono`. The dark appearance is asserted on both native clients.

The following paragraph records the previous appearance for migration audits;
it is superseded by Signal and is not the current visual contract.

**The rail is a CRT** (`Theme.swift`). Phosphor green on near-black, tokens
from `assets/proposals/board-look/03-fsociety.png`, monospace at every size
through `Theme.mono`. `preferredColorScheme(.dark)` and `.darkAqua` are
asserted, not inherited: a green CRT has no light variant.

**The panel is an ordinary macOS window, and it can leave**, stated
in full in `docs/panel-window-contract.md`; what must hold: `Lifecycle.swift`
enforces **exactly one panel, owned by someone** (an orphan with
`getppid() == 1` quits, a `--hidden` instance evicts `~/.dark-army/panel.pid`,
stdin EOF quits on a pipe or socket); `PanelExit` splits the departure because
`NSApplication.terminate` is a *request* an **attached sheet** refuses —
`requested` ends every sheet with `NSWindow.endSheet(_:)` first, `now` is the
watchdog's route and releases `PanelLock` by hand, and sheet-detaching is legal
**only** on a path ending in process death; the window is `.regular`, titled and
`.normal`-level, closing mirrors `hide()` and `windowDidChangeOcclusionState`
writes the same `visible`/`boardOpen` gate stdin does, both idempotent through
`didSet`, the covered edge after a grace; `Placement.swift` keeps **a frame per screen** and clamps on every
show; `SettingsMenuModel.rows` is the settings window's only inventory,
placed into eight sidebar sections by `SettingsSections` (diagnostics under
**Advanced**; Restart, Quit and the kill switch in the sidebar footer) and a
preference **key is never renamed**; the keyboard is one `NSEvent` monitor
(`installKeyMonitor`) asking `KeyRouter.editing`, with Escape climbing
`RailLayout.escapeRung` and arm-then-confirm in `RowActions` — **a focused
hosted terminal owns every key**, guarded above every triage rung by
`keys.terminalFocused` *or* `TerminalFocus.holdsCaret` (the flag rides a
0.15s poll and cannot be the only test), with **one pure table deciding every
combination** and the monitor deriving nothing —
`TerminalKeys.verdict(keyCode:flags:characters:)`: `.type` bytes at the pty
(⌘⌫/⌘⌦/⌘←/⌘→/fn-⌫/⌘K, and ⇧⏎/⌥⏎ as **ESC CR**, since nothing on the pty
answers `CSI ? u` and kitty is never negotiated), `.app` (⌘C/⌘V/⌘A),
`.nobody` (⌘⌥O), `.swiftTerm` for everything else; **Option composes, it is
not Meta** (`optionAsMetaKey = false`, ⌥←/⌥→/⌥⌫/⌥⌦ restored by name, **no
preference key**); **a click claims the caret** (`PaneTerminalView`,
`TerminalKeys.claimsCaret`) and `DictationFocus.promote` refuses
(`mayPromote`) while a terminal holds it. The table and its ordering are
`docs/panel-window-contract.md`'s. `PanelMetrics`
sizes every band from constants and no `GeometryReader` may size the window
from the window; `PanelScale` / `ScaleHostView` is the one scaling seam; and
`.clickable()` (`Cursor.swift`) is a `.cursorUpdate` tracking area, never
`onHover`.

**A button only where the agent named a choice.** `<!-- bob-actions: Accept
| Iterate -->` beside the `bob-tldr` summary; `session_stats._parse_actions`
reads it (≤3 labels, ≤24 chars, deduped, stripped from the prose, cleared by
the next message that offers none); one button per label, sending it back
verbatim. No declaration means a field and a **Send** disabled while empty.

**And a way to finish reading.** `WrapUpBar` closes the terminal tab — its own
strip behind a hairline, **not a reply**, with **different reach** (reply
needs Dark Army's channel; this needs a VS Code window that can dispose the tab).
Armed then confirmed. `close_session_terminal` calls
`vscode_reveal.close_terminal` and **then** drops the card and forgets the
row, never the other way round; it refuses while a permission prompt is up.
**A press here also finishes the session's board card** — the third door
into Done, above. The panel and the phone both send `by_person`, and **that
payload field alone is the guard, never the action name**: `close-out.sh`
posts the identical `close_terminal` action with no flag, and only when run
with `--close` on the person's request or with `--plan` from a card's
planning run, so an agent closing its own tab
finishes nothing; absent is
False, so an older build moves no card. Best-effort and silent — the tab is
already gone, so it never turns a landed close into a refusal. Reach is
published per row as **`can_close`** — distinct from `channel` and from
`can_type`, computed only for stopped rows — and the button is absent where
it is false; `can_type` still gates the `AskUserQuestion` option buttons and
nothing else. `board_close_terminal` is **on**; the `wrap_up` API action —
sent only by stale `close-out.sh` copies — refuses Grok before any close
(`GROK_WRAP_UP_REFUSAL`), and otherwise closes the terminal where
`board_close_terminal` is on and types `/clear` only on a non-prompt,
non-Codex-identity refusal (`wrap_up_or_close_session`); a prompt-blocked
session and a Codex identity mismatch are refused in
`close_session_terminal`'s words (`PROMPT_BLOCKED_REFUSAL`,
`CODEX_IDENTITY_REFUSAL`) and never cleared. Grok wrap-up types no
`/clear`. The Done leg never clears a conversation.

**And a way to carry on past a usage limit.** `low_priority_session` is
`wrap_up_session`'s sibling for one failure: a Claude session whose current
card is a `StopFailure` with `error_kind == "rate_limit"`. It types
`LOW_PRIORITY_COMMAND` (`/low-priority`) onto the input line through
`vscode_reveal.send_text` — a slash command, so never the channel — and only
once that has landed writes `state = "idle"` (re-stamping both event clocks)
and drops the card; `dismiss_notification` alone would leave `error`, which
`categorize` banks under `waiting`, so the row would stay in Needs you. Four
refusals, in order: `PROMPT_BLOCKED_REFUSAL`, `LOW_PRIORITY_NOT_CLAUDE_REFUSAL`
(an unstamped provider refuses too), `LOW_PRIORITY_NOT_LIMITED_REFUSAL`,
`LOW_PRIORITY_ALREADY_REFUSAL`. **`/low-priority` is a toggle**, so a landed
press is remembered in `_low_priority_sent` for
`LOW_PRIORITY_COOLDOWN_SECONDS` (600s, memory only, popped by
`_forget_session`) and both surfaces arm then confirm. Reach rides per row as
**`can_low_priority`** = `can_type` ∧ Claude ∧ not background ∧ no open
prompt ∧ rate-limit `StopFailure` card ∧ not within the cooldown — computed
beside `can_close` in `_enrich_agent_stubs`, `can_type` first so the one
shared `ps` is never asked twice. The panel draws `LowPriorityBar` above
`WrapUpBar` in the `StdoutPane`; the phone draws `lowPriorityBox`; both decode
the flag false by default and the card's `error_kind` as `""`. The action
`low_priority` is on `LAN_ACTIONS` and `REMOTE_ACTIONS` and deliberately not
in the phone's `settlingActions` — the row is not leaving.

**Asking for the board opens the workspace**: `installBoardObserver`'s
`.panelOpenBoard` notification calls `show`.

**A banner can aim the panel** (`FocusRouter`, `PanelView.show(_:)`). A
`show`/`toggle` on stdin may carry a `focus` session id; the view puts that row
in front (`applyFocus`): its project tab selected, the row selected, the
message unfolded. The request is **not consumed on read** — it carries a
sequence number and the view remembers the last one applied, since a tap
routinely beats the snapshot — and `focusedRow` exempts that row from
`selectTopAttentionIfNeeded` until the selection moves on its own.

**The board** (`BoardView.swift`, with `BoardState.swift`, `BoardLanes.swift`,
`BoardDrop.swift`, `BoardCardView.swift` and `BoardCardSheet.swift`) is four
stacked, foldable rows, **Prep → Backlog → In progress → Done**, each with
a one-line caption, whose cards wrap as tiles (`GridItem(.adaptive)`,
`boardTileMin`..`boardColumn`) in **one vertical scroll**, drawn from
`DaemonClient.boardFeed`, not the client; plain reading surfaces with no CRT
overlay. Done starts
folded; flips from that default persist in `panel-position.json` as
`board_row_flips`. The rule is `BoardRowFold`, copied byte-equal into the
phone's `BoardView.swift` (`test_board_rows.py`); a search overrides a
fold, and a folded heading takes a drop. Prep has **Refine** (dispatches a planning session); the plan that session attaches moves the card to Backlog, and every newly written card lands there. **A card outlines one verb, chosen by its column** (`CardActionWeight`): Refine in Prep, START in Backlog, Done in In progress, none in Done; every other verb is dim words, HERE included, and an absent primary promotes nothing — with one kind-aware exception: a scout in Prep outlines START, because it has no plan to refine.

**The phone's Prep row has a select mode too** (25 Sep 2026): SELECT, a
tick on every card whose own screen offers Refine (`PhoneRowSelection`,
one rule for both), one armed-then-confirmed press sending
`board_refine_batch` through the press queue, drawn only where the board
says `refine_batch_supported`. In full in `docs/phone-contract.md`.

**The phone's Backlog row selects too** (25 Sep 2026): the same ticks,
judged by the Start rule, and one armed-then-confirmed press sending
`board_start_batch` synchronously, never queued, behind
`start_batch_supported`. In full in `docs/phone-contract.md`.

**A Prep or Backlog card swipes on the phone too** (3 Oct 2026): START or Refine and a dots menu holding a confirmed Delete, the card screen's own presses behind a sideways swipe; in full in `docs/phone-contract.md`.

**The Mac board's ticks outlive looking away too** (25 Sep 2026): a
search, a project change or a fold keeps select mode and its ticks, the
batch button counts every ticked card drawn or not, and the selecting row
draws its ticked cards first (`BoardVisible.compute`).

**The drop is the dispatch.** `.draggable(card.id)` on the card,
`.dropDestination` on the row, and `BoardView.drop(_:into:)` resolves
against the *current* snapshot: the payload is the **id**, not the card.
Releasing over In progress starts the assistant the card names; the daemon
moves the column, never this view. Three releases are no dispatch: **no
assistant named** refuses in words and does *not* move the card; **Dark Army's
launcher switched off** and **a card that already has a session** move it
plainly. A fourth is a dispatch *pending a word*: an **unplanned** card
dropped into In progress is **held** as `BoardState.pendingUnplanned` —
nothing is sent — and a `.confirmationDialog` names what is being skipped;
confirm replays with `skip_plan_gate`. The Start button's arm *is* that
confirmation on its route: on a gated card the armed label reads **"Start
unplanned?"**.

The detail sheet gains a first-class **Plan** section when `plan_path` is set:
`PlanDiagram` (the plan's `- **Stages:**` header as tiles, its `## Files to
change` table as an iconed list; either half absent is omitted) above the
document rendered by `MarkdownText`, both read through `BoardDocuments`'
containment. A missing plan file is one orange line plus the path.

**A card leads with its summary.** `summary` is its own field end to end
(store column, API allow-list, channel tool argument), because every surface
must show it **without** the instructions; the prompt preview is the fallback.
The assistant is chosen **on the card** by the mark switcher
(`ProviderSwitch`: one focusable group, ← / → / Space / Return / Escape,
one VoiceOver group named Assistant), gone once a session is bound; no
surface draws an assistant menu. `ProviderChoice` is the shared rule,
byte-pinned to the phone (`test_provider_switcher.py`); the key monitor
stands aside on `KeyRouter.controlFocused` (never `keys.editing`).

**Clearing Done is a bulk destruction, so it is confirmed against an exact
set.** `BoardStore.clear_done(expected_count, expected_token)` is one `BEGIN
IMMEDIATE` transaction: it re-reads the store-wide Done scope under the same
lock as the category-scoped `DELETE` and refuses unless **both** the count and
the membership token match. The token is `_done_scope_token` — SHA-256 over
the Done ids, each prefixed with its byte length — and rides the board
snapshot as `done_clear_token` beside `counts["done"]`, from **one** store
read. `api_server` validates the digest's shape (64 lowercase hex) before the
daemon sees it. Panel side: `BulkClearDoneTests.swift`. The phone is a second
surface of the same `board_clear_done` / count+token pair, armed once then
confirmed (not the panel's two confirmations), drawn absent against a Mac that
does not publish `clear_done_writable`.

**Prepare writes a card's fields from plain words** (`card_prepare.py`),
`session_title.py`'s pattern with two departures: its **cwd is the card's
project root**, not `STATE_DIR`, and it has **two modes on one field**. The
composers open in two phases (`ComposerPhase`, banked as `expanded` on the
draft): idea, Prepare, assistant and attachments first; the rest after
Prepare answers or the person presses fill in myself. Without `idea` — the legacy press, byte-identical argv — it writes
`prompt` and `workflow` only and never rewrites the person's title and
description. With `idea` — the composers' one box — it drafts seven answers: `title`,
`summary`, `prompt` and `workflow` (`MODE_HEAD_IDEA`, `parse_idea`,
`title_refusal` / `summary_refusal`, which **reject rather than truncate**)
plus the card's objective — `beneficiary`, `intended_benefit`,
`success_criterion` (`OBJECTIVE_LABELS`, `parse_objective`,
`objective_refusal`: over `board_outcomes.OBJECTIVE_LIMITS` refuses the
press, empty and `NONE` are no suggestion). **A suggestion lands only in an
empty box** on both composers — title and summary are overwritten because
idea mode asked for them; the objective is the person's whenever typed. The
three boxes sit under Instructions on both composers before the first save,
bank with the draft, ride `board_create` only when non-empty, and the phone
draws them only where `objective_on_create_supported` rides
`_pipeline_writable()` (an older Mac 403s the whole sealed create).
Neither mode saves anything; the answer lands in the composer as editable
text. The two heads and two parsers are separate, so a legacy answer echoing
`SUMMARY:` cannot change legacy parsing. Compatibility, on both clients: the
idea rides in `summary` too when nothing was typed there, the daemon prefers
`idea`, and a returned `title`/`summary` is applied **only when non-empty**.
`idea` is composer scratch — no card, no store column, no snapshot; only
`card-drafts.json` and the phone's draft slot. **The helper is the card's own
assistant, on that assistant's cheap model** (`card_prepare.HELPER_MODELS`,
`helper_tool`): `codex exec` on `gpt-6-luna`, `grok -p` on `grok-4.5`,
and claude — or a name Dark Army cannot run headless — `claude -p` on Haiku with
its four flags (no transcript, no hooks, no MCP servers, `--model haiku`).
The brief is the named agent `bc-card-preparer` — a project copy wins, then
a user copy, then the shipped `card_preparer_brief.BRIEF`; a copy that no
longer asks for the labelled answer is set aside for the bundled one. Claude
is started *as* that agent (`--agents` / `--agent`); grok by `--agent` on a
file Dark Army writes under the state directory; Codex, which has no such switch,
gets the brief at the top of the prompt. The data half (which labels to fill,
the roster, the fields) still rides the prompt, never `--system-prompt`. The
roster excludes the preparer, so a card never lists it as a stage. Codex gets
`--ephemeral`, `--ignore-user-config` and a `read-only` sandbox, grok
`--no-subagents`, `--disable-web-search`, `--no-plan`. Neither of those two
can switch Dark Army's hooks off, so `card_prepare.env_for` points
both hook addresses at `QUIET_HOOK_*` and the helper is never a row. The
executable is `dispatch.resolve_executable(helper)`. With attachments, Claude
reads them under a per-file `--allowed-tools` grant and is told **not** to
list their paths; codex and grok cannot reach `~/.dark-army/attachments/`
from the project sandbox, so Prepare refuses those two in words.
`_prepare_card_text_locked` strips link-only lines with
`attachments.strip_path_lines` *before* `card_prepare.refusal` judges the
text; the list is handed to the assistant once, at start —
`_dispatch_card_locked` and `_refine_card_locked` both append the block, and
Start strips first. Prepare is always offered; `prepare_enabled` stays in the
snapshot for the phone.

**Prepare also offers an opinion about *which project* the card belongs
to** (the `FOLDER:` label names a project root). The prompt gains a
`FOLDER:` section before `AREA:`, so their trailing text lands inside
`SPECIALISTS` where `_clean_specialists` already drops it, and neither
`_SECTION` nor `_SECTION_IDEA` is widened. The closed set it may name is the
board snapshot's `projects` **∩** `_known_project_roots()` — a root with a
live session but no open window is unselectable in either picker.
`card_prepare.parse_folder` returns a member of that list or `""` — never the
model's own string — computed *after* `refusal`, so an off-list, invented or
`NONE` answer is **no suggestion**, never a refusal. It rides the reply as
`suggested_root` beside `title` / `summary` (absent means no opinion), and both
composers **apply it on arrival** through the picker's own path
(`selectProject` behind `projectBinding` on the panel, `projectRoot` on the
phone) when it names a listed project that differs from the current one
(`ProjectSuggestion.decide` on the panel; the phone inlines the same rule).
A picker move clears the composer's `workflow`. The root the picker held goes
into `revertRoot`, drawn as `Dark Army picked <X>` plus one button `Use <Y>
instead`, absent when the previous value was empty or unlisted; the button or
a touch on the picker clears both slots. The line is gated on `revertRoot`,
never on `offer`. Both are view state on both surfaces: no card, store
column, snapshot or banked draft. With fewer than two offerable roots the
folder block is empty; the area section remains present.

**`BoardCardSheet` is one surface for reading and editing, and a window, not
a sheet** (`CardWindow.swift`; the old type name stays).
`CardWindowController` owns **one** reused `NSWindow` (titled, closable,
resizable, `.normal`, `.darkAqua` asserted) following the single
`BoardState.editing` slot: another card swaps that window's content, never a
second window or draft. `BoardState` is injected `@ObservedObject`,
constructed exactly once. `hide()` and the panel's `windowWillClose` close it
through `closeEditor()` + `forceClose()`, in that order; `show()` re-presents
whatever `editing` still names; every close route lands in `closeEditor()`,
and `windowShouldClose` is the `composerSaving` refusal. Two costs: the key
monitor has a card-window guard beside the sheet guard (`owns()` narrow,
`isKey` total, **below** the sheet test), and the SSE `visible`/`boardOpen`
gate is the union of the two windows' occlusion. **`Notification` must be
written `System.Notification`** in that delegate — this module has its own
`Notification` model, so a callback declared against it compiles, warns and
is never called. **A saved card's sections follow `CardSections`** (Foundation
only; `stage` over `column`, `link_state`, `manual_check_due`, `run_active`
(bound, not running: `ended`), never the tool): the lead five, then
one `MORE · n` row hiding the rest; the open set is `@State` cleared on card
swap and stage change; the lead's assistant row writes at once, like the
tile's; `closeEditor()` disarms. Phone copy byte-equal
(`test_card_sections.py`). **A scout's report is its own `REPORT` section**
(`reportSection`; `docs/context-board.md`). A card flagged with a check file
draws the file in its manual-check section with **Passed** / **Failed** and
**Open in Checks** (the Checks window, `ManualChecksWindow.swift`, from
Settings → Projects), where Mark checked is hidden.

The composer is the same view with `card == nil`, plus three things a card
could never show. **The documents the card points at**: `BoardDocuments` pulls
path-shaped tokens out of the prompt and resolves each **inside the card's
own project root**, standardized, so neither an absolute path nor `../`
escapes; the panel reads the file itself. **The session doing the card**, with
a message box riding the panel's `reply` route (`kind="user"`), drawn only
where `channel` is true. And **deletion**, armed then confirmed; `BoardState`
keeps `armed` and `deleteArmed` as **separate** slots.

**A half-typed composer survives its own close** (`Drafts.swift`,
`DraftsSheet.swift`). `BoardState.bankComposerDraft()` writes a row to
`card-drafts.json` and releases the staging folder *without* removing it;
autosave every `CardDrafts.autosaveInterval` (10s) covers `PanelExit`'s `now`
route. An empty composer discards (`worthKeeping`: any typed field, or a staged
file). A successful create removes the row **before** `keepStagedAttachments()`
clears `stagingId`. A `DRAFTS (n)` button sits beside + NEW CARD, absent at
zero. **The store is panel-local and the daemon never reads, serves or
snapshots it** — its entire involvement is `card-drafts.json` in
`paths._PRIVATE_FILES`.

**A `.md` file is drawn as a document, not as its bytes** (`Markdown.swift`).
`AttributedString(markdown:)` handles inline text; the local parser handles
blocks line by line. Links become labels; only `.md` is rendered; list-item
hanging indents fold into the item; a single newline is not a line break.
`Markdown.tableRows` drops table alignment rows and renders a `Grid` of inline
cells; code spans are tinted. The phone copy from `enum Markdown {` down is
byte-pinned by `test_phone_theme_drift.py`: edit both copies together.

**A card can name its area** (`Areas.swift`, byte-equal on Mac and phone).
`AreaGrid` offers eight reflowing tiles with the usual lead's portrait and
name, drawn only when `areas_supported` is true. `area` defaults to empty in
every decoder and rides the revision-guarded writes and queued phone creates.
Prepare's final `AREA:` answer is a closed-list suggestion, applied only into
an empty box; the daemon answers `universal` where the helper named none, so
an empty box is never left blank, and idea-mode and area labels tolerate
markdown decoration (`**TITLE:**`, `## AREA`) while legacy `_SECTION` does
not. A plan's
`- **Area:**` header seeds an empty field on attachment and launch through
`fill_area_if_empty`; a later launch can re-seed a person's cleared field.
**The objective takes the same route**: the plan's `Who benefits:` /
`Intended benefit:` / `Success criterion:` headers (`read_plan_objective`)
land via `fill_objective_if_empty` — empty boxes only, clamped, stepping
`outcome_revision` — at attach and the launch backfill; `dark_army_add_card` names
no objective field.
Start appends its area and usual lead after the objective block, asking for
`.claude/leads/<slug>.md` only when that managed brief exists.

`Specialists.swift` keeps seven stage descriptions, byte-pinned below its
marker by `test_phone_theme_drift.py`. A known stage draws a short marker;
an unknown name keeps its djb2 face. The crew band shows one area
lead and keeps recorded legacy faces and the existing step-counter arithmetic.
`workflow` still rides the card end to end. An undeclared helper is refused
on a changed saved-card editor value and dropped on create (`_roster_verdict`),
where the composer has no typed specialists box to repair. The phone's
`Cast.swift` remains a partial copy: the names, art-only face, portrait lookup
from `Bundle.main` and agent hash. `CastQuotes` is byte-pinned from `enum
CastQuotes {` down (`test_cast_quotes.py`). Neither portrait copy animates.

**Dark Army talks while it waits; nothing spins** (`AgentChatter.swift`, the
fourth marker-pinned pair). In full in `docs/agent-chatter.md`; what must
hold: the lines ship in code, the reveal is a pure function of `now - began`
under a `TimelineView`, a caption is never replaced (only the spinner), and
`ProgressView` stays at **zero** across both clients.

**The Comm tab talks to Mission Control** (`PanelView.Tab.comm`,
`CommRail.swift`, `CommRules.swift`): the wide pane is the board **with
Mission Control's terminal column beside it** (`RailLayout.LeftPane
.mission`; `BoardView` keeps its structural place under a trailing
padding of `PanelMetrics.commTerminalWidth` and the column is overlaid
trailing — never an `HStack` move, or the board's `@State` resets), and the
rail draws `CommRail`: the status line, the four quick-question chips, a
`TextEditor` composer with the card composer's `DictateButton` gate, Send
(`client.terminalInput(session:text:)`, the text route), Open where it is
off or ended, and an armed-then-confirmed End (`missionOpen` /
`missionEnd` on `BoardClient`, `X-Bob-Token` through `post`). The column is
a second `TerminalPane` for the mission session drawn **only** on Comm —
`AgentDetailPane` only on Agents — so one pane states `panel_terminal` for
that session at a time and the pty's width keeps one owner. The reply prose
is not drawn on the Mac: the transcript is on screen. Source of truth is
`snapshot.mission` (`MissionSection`, `TerminalModels.swift`; absent carries,
present-but-undecodable throws, `Snapshot.Section.mission` the twelfth case)
and `snapshot.agents.row(session:)`; `Agent.stub(sessionId:)` is what the
pane is handed before the row lands. `CommRules` — the chips, `line(from:)`,
`status(...)`, `canAsk`, `showsEnd`, `showsOpen` — is Foundation-only and
byte-pinned to `ios/BobPhone/CommRules.swift` from `enum CommRules {` down
(`test_comm_rules.py`, which also runs it under `swiftc`). Escape on Comm
is `RailLayout.escapeRung` unchanged; the arrows walk no rows there.

Three rules in the API layer (`Models.swift` — envelope and fleet — with
`BoardModels`, `EnrollmentModels`, `TerminalModels`, `ActionModels`;
`DaemonClient.swift` with its `BoardClient` / `Fetchers` / `OutcomeClient`
extensions): Swift's synthesized `Decodable` **throws on a missing key** even
when the property has a default, so every model decodes through tolerant
helpers — one absent field must never blank the panel. Writes need the
**`X-Bob-Token`** header, not `Authorization: Bearer`; reads are ungated, so
the wrong header looks like it works and every action silently 403s. And
**the panel decodes no version markers**: the fourteen `*_supported` /
`*_writable` flags are for the phone, which can meet an older Mac; the panel
ships with its daemon, so `Board` carries none and every "drawn absent on an
older Mac" sentence below is the phone's.

**The header carries the desk token, and it never comes from disk.** It
arrives on the menu bar's `context` push over stdin (`PanelContext.deskToken`,
empty until the first push, never blanked by a push without one); the file
under `~/.dark-army` is the session token, which only closes terminals and
reads. A 403 sends `context_refresh`, at most once a second, and the app
pushes the context again (`docs/transport-contract.md`, *The loopback door
has two tokens*). Settings → Advanced → **Copy desk key** (armed, then
confirmed, drawn only while a key is held) puts it on the clipboard for the
person's own tools.
