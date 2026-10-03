# Phone contract

This is how the phone app (`ios/BobPhone`) behaves on its own screen and on its own clock: what it draws, how it wraps, how often it checks in and which way it goes. `CLAUDE.md` restates the invariants and points here; this document is the text they were lifted from. The wire itself — the sealed doors, the reads and the relay — is `docs/transport-contract.md`.

## The phone does not clip prose

**The phone does not clip prose.** Every surviving `.lineLimit(` there is
a fixed-width column, a horizontally scrolling strip, a reserved-height
chrome line or a `5...` **minimum**; everything else wraps and the screen
scrolls, and a cap removed from an `HStack` child carries
`.fixedSize(horizontal: false, vertical: true)` or it is a no-op. A
preformatted terminal or code block is the scrolling-strip case: it takes
`.fixedSize(horizontal: true, vertical: true)` inside its own horizontal
scroll so the drawing does not wrap to the pane — `Markdown.swift` is the
one file that may say `horizontal: true`. A `.navigationTitle` is **always
a literal** — UIKit ellipsises an inline bar
title and no modifier makes it wrap — so the long thing is drawn in the
body instead. Pinned by `host/tests/test_phone_text_in_full.py`.

**And it keeps its own link timing.** Every completed request on either
route is one `LinkTimingSample` (`ios/BobPhone/LinkTiming.swift`): when,
`home` or `away`, the kind, the Mac's inner status, the post leg, the
whole trip and the Mac's own legs off the reply envelope's `timing` (0
where an older Mac sent none). `LinkTimingStore` keeps the newest 500
under `Application Support/link-timing.json` with `HeldPictureStore`'s
discipline (writes floored at 15 s and serialised, cancellation checked on
both sides of the file; `adopt` drops another Mac's ring), survives
`stop()` and is dropped by `forgetPairing`; the file holds route words,
kind words, statuses and seconds, **stamped with the pairing-generation
token as the held picture and the card cache are, and carrying no key,
address or credential; it builds no URL**. Profile → access log draws it above
the refusals: **LINK TIMING** (`LinkTimingSummary.lines`, one per route ×
kind — `away · state · 24 trips · 2.9 s typical · 5.1 s slow` — then the
newest `LinkTimingView.shownSamples` (20) trips one per row, the back leg
always `(est.)`) and **MAC'S OWN FIGURES** (the fetched `timing` entries'
sentences verbatim, only where the Mac sent any); the refusal list is
filtered to `kind != "timing"`, every line wraps, the title stays the
literal `"access log"`, and the widget's client records nothing. The
Mac's half is `docs/transport-contract.md`, *Every phone request is
timed*. Pinned by `test_phone_remote.py` and `LinkTimingTests.swift`.

## The phone opens a card, not a screen

Agent details, board cards, Catch up, saved decisions, changed files and
notifications share one sheet presenter on `ContentView`. `PhoneSheetRouter`
keeps at most three subjects: following a link appends, the same subject
refreshes, and a fourth replaces the top. Back removes one subject; Close or
a swipe clears the trail. A notification tap resets it to that receipt.
The presenter's identity stays stable while the entry's own UUID replaces
its content, so a covered list never tries to present a second sheet.
The wrapping header names the subject in body text and offers Back and Close.
Each trail entry has its own stable UUID, even when two entries name the same
subject; Back restores the earlier entry's view identity. It retains its unsaved
editor and reply text, including the
card's dirty/conflict and revision context, while its view is away. Returning
does not reseed those drafts. Armed controls, dictation and terminal watches
still end with the departing view. These objects are memory only, bounded by
the trail and discarded on pop, replacement or Close, except that a rung
restored from the saved place starts with the draft text and agent page it
was saved with (*The phone comes back where you left it*). A resolved notification
page is retained only after the original receipt checks succeed; Back may read
it after consumption, but never after a lock, pairing change or new receipt.
A card screen draws its WAITS ON section and the Add picker under EDIT CARD
only where the board says `dependencies_supported` (`docs/card-dependencies.md`).

| Subject | Ordinary text | Accessibility text |
|---|---|---|
| Agent | Medium, draggable to large | Large only |
| Card in In progress | Medium, draggable to large | Large only |
| Card in Prep, Backlog or Done, Catch up, decision, changed file, notification | Large only | Large only |

`PhoneSheet.swift` holds the five Foundation-only rules on `PhoneSheetKind`;
`answers(_:column:)` names the two answering cases, the agent and a card in
In progress, and the card's column is the one subject fact the table reads.
`PhoneSheet` exposes them without re-deriving them. A keyboard opening grows
the sheet to large; the terminal cover's keyboard leaves the sheet height
alone, and hiding a keyboard never shrinks the sheet automatically. Changing
subject or Dynamic Type re-applies the initial size. **The cover claims the
keyboard only after presentation has settled, and Done (or a swipe) resigns
it before the cover comes down.** The sheet's detents are stored and not
restated while the cover is up: restating a detent sheet under a
first-responder full-screen cover is the crash. `terminalPresented` is a
flag the keyboard observer reads, not a published property that would
rebuild the sheet. Every text box has a hide-keyboard button while
focused, and while typing the sheet folds its lead and verbs so SEND
stays visible (`test_phone_keyboard_hide.py`). The frame enables finite
entry decoration and draws the same Signal background as the tabs, without a
reading overlay.
An unresolved or unavailable notification refuses a swipe; explicit Close
still consumes its receipt. Resolution retains the pairing and sequence guards.

Agent details lead with identity, the card, the question and the answer
controls. The origin lines, the cast quote, the facts, command and message
remain below in the same scrolling column.

**The agent sheet's half height is a glance.** Under the name sits one status
line — `AgentSheetLead.statusLine` over `PhoneAgentFacts.head`, the fleet
row's age (`FleetAge.text` at the snapshot's own stamp) and its context
figure: "Working — running Bash · 1h · ctx 20%↑" — wrapping, never capped.
A waiting agent's question (`AgentSheetLead.questionText`, the words Details
draws too) sits right above its answer controls on the Conversation screen,
where every open lands, handed in as `ConversationScreen`'s `ask:`, inside
the answer's bounded share. The conversation draws no tool-result rows — a
result stays one tap away behind its call — and two or more consecutive tool
calls fold into one dim line, `⚙ 5 tool calls · Bash ×3, Read, Grep`, a tap
opening the calls (`ConversationFold`); every scroll anchor reads the rows,
never the last turn. The frame puts its detent in the content's environment
(`phoneSheetDetent`, a `SheetDetent?`) and the lead's still is 40pt at half
height and 96pt dragged up or outside a sheet (`AgentSheetLead.stillSize`).
Permission asks, notification cards and the low-priority offer keep their
full shape; Close, Hide, Stop and Delete share one row of small buttons, each
armed before it fires, Close's sentence drawn only while it is armed or in
play. The rules are Foundation-only in `AgentSheetLead.swift`, run under
`swiftc` by `host/tests/test_phone_sheet_lead.py` and on the phone job by
`AgentSheetLeadTests.swift`. **Terminal opens a full-screen cover** from the agent sheet;
Done selects Details and returns to the sheet. Its stream and width release
still follow the existing terminal watch and pane disappearance. Composer,
profile, Pipeline and the two Usage report screens retain their existing
(push) navigation. The reports are pushes inside Usage, absent when the
Mac does not publish the flag, fetched on appear not on tab visit.

**The agent sheet swipes to the next agent that needs you.** A horizontal
swipe on the sheet's header — leftward next, rightward previous — replaces the
top rung with the neighbouring agent in the Needs you order as drawn at that
moment (`PhoneInbox.groups` over `decisionItems`, session targets
`uniqueAgent` resolves, card entries skipped), through
`PhoneSheetRouter.replaceTop`: a fresh entry state, so the content remounts,
the arrival caption plays again and the detent re-aims; the trail does not
grow and Back returns to the list; the displaced rung's typed reply is parked
in the place store's orphan drafts and returns on the next open of that agent,
memory only; the ends stop with no action; the gesture is
`.simultaneousGesture` on the header row only, never the body, and the
whole row takes it (`.contentShape(Rectangle())`, so the blank space right of
a short name counts); VoiceOver has
"Next waiting agent" / "Previous waiting agent" on the title where a neighbour
exists. The rules are Foundation-only in `AgentSheetSwipe.swift`, run by
`host/tests/test_phone_sheet_swipe.py` and `AgentSheetSwipeTests.swift`.

**The sheet's header says where you are in Needs you.** While the swipe is
available — the sheet's agent is in the flattened Needs you order and that
order holds two or more — the title carries a small dim `2 of 5` as a trailing
run of the same text (`PhoneSheetFrame.titleText`, `AgentSheetSwipe.position` /
`mark`), read from the same `waitingOrder` at the moment of the draw so it
moves as agents are answered; hidden with one waiter, with a subject not in the
list, and on every other sheet kind. It is a run, not a slot: Back, the
caption and Close keep their room and the name wraps, never clips. At
accessibility sizes (`AgentSheetSwipe.showsMark`, the environment's
`isAccessibilitySize`, read not measured) the run is dropped and the name
yields nothing; VoiceOver reads the title as `Elliot, 2 of 5 waiting` at every
size (`spokenMark`), beside Next / Previous. Pinned by
`host/tests/test_phone_sheet_position.py` and the position block of
`AgentSheetSwipeTests.swift`.

**A finished agent's report reads as on the Mac**: the sheet draws
`work_report` through `WorkReport` (byte-pinned, `test_work_report_parse.py`)
as labelled sections, Unchecked numbered; the status line and Needs you lead
with its headline, parsing nothing. A row never does: it names what tapping
it opens — the sheet's card line first (`AgentDetailView.cardLine`), the
Mac's `ProcessRow.baseTitle` rule. An older Mac sends none
and the raw report is drawn; no push (S3).

**A card opens with five things and one MORE row, in `CardSections`' order,
and the phone re-derives none of it.** On a card in Prep, Backlog or Done
the card screen leads with the title, the summary, the assistant row (`PhoneProviderSwitch` where `canRetool`,
else the inert mark and name of what ran — `assistantRecord` — so a bound
or finished card still says which assistant has it), one line saying where
the card stands (`CardSections.stateWords(for:linked:)` — "Planned — ready
to start", "An assistant is working on it", or "In progress — nobody
working on it yet" for an In progress card with no link at all; the check
line `CardSections.statusLine` on a card waiting on one) and the **one next
action**: `CardSections.nextAction(stage:reach:)` over a `CardSections.Reach`
the screen fills from the gates it already computes (`canRefine`, `canStart`,
`canMessageSession`, `PhoneCardAck.showsManualClear` / `showsReview`) — a
Prep card leads with Refine, a planned card with Start, a card being worked
with the message box, a card waiting on a check with Mark checked under the
steps (Passed / Failed instead where the card names a check file), an ended run with Done, a finished card with Mark reviewed under the
close note. Then the sections through `CardSections.order(for:)` — the rule
the Mac's card window reads, copied byte-equal into `CardDetailView.swift`
and pinned by `host/tests/test_card_sections.py`: everything else sits
behind one `MORE · n` row (`CardSections.more`, `CardSections.shown`, the
count from `Facts.hidden`), each hidden section a `PhoneCardFoldRow` of its
own once MORE is open. **Three exceptions and only three** open by
themselves (`CardSections.leads`): the check's steps on the manual-check
stage, because the steps are the next action; the close note on a Done
card, because Reviewed is a judgment about that note; and a scout's
`REPORT` on the ended and Done stages, because the report is what the card
was for (on a build card the section has no content and draws nothing).
The stage is `CardSections.stage`
over `column`, `link_state`, the daemon's `manual_check_due` and its
`run_active` — a bound card the daemon says is not running is `ended`, the
pipeline band's own ENDED — never the tool. Every fold row is at touch
measure — a header to VoiceOver, read as
`shown` / `hidden`, its label `CardSections.rowText`'s (`WHAT CHANGED · 4
files`) — and the open set, MORE's own state included, is `@State` on the
view, **not** on `PhoneCardDraftState`, so closing the screen or the card
changing column starts back at the rule. The other verbs — START HERE, the
start-when-planned tick, Start where Refine leads, the armed Done the column
mover raises — are the `OTHER ACTIONS` row under MORE; Collaboration is the
`COLLABORATION` row there, no longer a second fold outside the rule; Delete is
its own section, last in every order. A verb the band draws is not drawn
again in its own section (`nextAction != .x` guards), so none appears twice
and none is lost. An arm raised where its button is not on screen — the
column mover under EDIT CARD arms Done or Start, a plan-gate refusal after
a move arms Start — is confirmed in the band (`armedElsewhere`) while OTHER
ACTIONS is closed, or `Arm.timeoutSeconds` would disarm it unseen. The
MESSAGES row is absent while the band draws the box. **Mark checked** and
**Mark reviewed** are each drawn only
where the Mac states its own marker (`manual_clear_writable` /
`review_writable`, absent decodes false) and the card is due
(`PhoneCardAck`); armed then confirmed, echoing the steps or the close
identity that were on screen from the live snapshot card — never the cache —
so the Mac's store WHERE refuses a stale picture in its own words.

**An In progress card is answered from half height.** It opens medium,
draggable to large, and large under the accessibility text sizes, like the
agent sheet. Its portrait, title, summary, meta line and assistant row
(`identityLead`) move under the pinned sections — status, the band, QUEUED
and WAITS ON — and above MORE, so at half height the status line and the
message box are the first screen. The lead order follows the column
(`statusFirst`, the table's own `answers`), never the detent, so nothing
reshuffles on a drag. The seed's column at open decides; a card changing
column while its sheet is up does not move the sheet, and the next open
reads the new column. The keyboard grows it one way, as every sheet. Where
the box is not offered (`canMessageSession` false) the band draws nothing
and the lead is the status alone. Pinned by
`test_phone_sheets.py::test_an_in_progress_card_leads_with_the_band`.

Pinned by `test_phone_sheets.py`, `SheetPresentationTests.swift`, and the phone
text, accessibility, decrypt and terminal-tab contract suites.

## The Board tab is searched from a line at its top

A `>` line (`BoardView.searchField`) sits above the four rows and below
`pipelineLink`, pinned, never `.searchable` — the tab root hides the
navigation bar, so a searchable field would draw into nothing. The rule is
`BoardSearch.matches`: title, summary and project, `localizedStandardContains`,
trimmed, an empty field matches everything; `#card` is out of scope. Every
row draws open while a search is up through `BoardRowFold.drawnFolded(searching:)`
and the stored fold habit is never written by a search. The heading chip
counts the drawn list while searching. A column with no match says
`no cards match`; when nothing on the board matches, one line under the field
reads `“…” is not in a title, a summary or a project.` The filter runs on
the snapshot the phone already holds and sends no request. Pinned by
`host/tests/test_phone_board_search.py` and `ios/BobPhoneTests/BoardSearchTests.swift`.

## Several Prep cards are refined from the phone at once

The Prep row carries the Mac's batch Refine (25 Sep 2026). Under its
heading, **SELECT** is drawn only where the board says
`refine_batch_supported` (absent decodes false: an older Mac 404s
`board_refine_batch`, so the control is absent, never present and refused)
and at least two of the cards drawn could be refined — the search and the
project filter narrow what may be ticked exactly as they narrow what is
drawn. In select mode every Prep card draws a box, `[x]` ticked, `[ ]`
open, a dim `[-]` barred (the shape differs, never the colour alone), spoken as "ticked" / "not ticked" / "cannot be
ticked", and a press on the tile toggles it instead of opening the card.
**One rule decides the tick and the card screen's Refine**:
`PhoneRowSelection.tickable("prep", …)` (`RowSelection.swift`,
Foundation only, the Mac's `RowSelection` as a typed twin rather than a
byte copy) is `CardDetailView.canRefine`, so a scout, a planned card, a
card being refined or started and every card when Dark Army's launcher is
off cannot be ticked; `admits` adds the same `root` as the first tick, one
planning session running in one folder.

The batch button reads `REFINE n TOGETHER`, is disabled under
`PhoneRowSelection.minimum` (two), and is armed then confirmed ("Really
refine n?") on its own slot, `Arm.Slot.refineBatch`, so a Refine armed on
a card screen never confirms a batch nor the reverse. The arm's id is the
ticked ids **in board order** (`orderedIds`), joined — the same list on
both presses — so a tick changed between them re-arms rather than fires,
and any change to the ticks disarms. CANCEL leaves select mode with
nothing sent; while a press is out it stays pressable and leaves select
mode without touching the queued press (the QUEUE list keeps it with its
mark and RETRY), and SELECT is absent until that press leaves the queue. The press goes through `enqueue` under
`PhoneRowSelection.scope` (`batch:refine`) with `card_ids`
comma-joined: written down at the tap, Face ID once from away, waiting
offline like a single Refine, the button wearing QUEUED, SENDING… and
SENT. It is judged by `.cardsRefining(cardIds:)` — every ticked card's
`.cardRefining` — so the mark stays SENT until the board shows each one
being planned, and select mode ends when the mark leaves with nothing
said. A refusal — the Mac's words through `queueNote`, drawn and
therefore read (`readQueueNote`), or the phone's own duplicate and Face
ID sentences from a refused enqueue — is drawn under the button with the
ticks kept, so one can be fixed and pressed again. Ticks are pruned on
every board against what in the whole row may still be ticked, never while
the press is out. **Ticks outlive looking away** (25 Sep 2026): a search,
a project change, a fold or leaving the Board tab keeps select mode and
its ticks (each disarms a waiting press), the batch button's count names
every ticked card drawn or not, a folded selecting row keeps its batch
control under the heading, and the selecting row draws its ticked cards
first — the Mac's board does the same. A member deleted mid-flight leaves the
press SENT until the effect deadline, as a single Refine on a deleted
card does. The Mac re-checks everything (`docs/transport-contract.md`,
*The LAN door is sealed*); the tick only hides a press that could not
succeed. Pinned by `host/tests/test_phone_batch_refine.py` and
`ios/BobPhoneTests/RowSelectionTests.swift`.

## Several planned cards are started from the phone at once

The Backlog row carries the Mac's batch Start (25 Sep 2026) on the same
machinery. **SELECT** under the Backlog heading is drawn only where the
board says `start_batch_supported` (absent decodes false), no row is
selecting — `selectingRow` is one optional, so the two rows never select
at once — and at least two drawn cards could be started. The tick is
`PhoneRowSelection.tickable("backlog", …)`: the launcher on, a plan, an
assistant, not a scout, never started, not refining, not queued and not
already in a batch; `admits` adds one project. The button reads
`START n TOGETHER`, armed then confirmed ("Really start n?") on
`Arm.Slot.startBatch`, keyed on the joined ids in board order.

**Unlike the Prep row, the press is synchronous**: `post` under
`PhoneRowSelection.startScope` (`batch:start`), never `enqueue` and never
the outbox. Its reply is the Mac's report — what started, what waits, what
was skipped — drawn verbatim whether or not it succeeded: under the
heading on success, under the button with the ticks kept on a refusal. An
unreachable Mac is a refusal in words; nothing fires later on the phone's
clock. The tile and the card screen draw the Mac's `BATCH r/n · working`,
`· waiting` or `· left the line`, and `canStart` is withheld on
`holdsBatchMark`. Pinned by `host/tests/test_phone_batch_start.py` and
`ios/BobPhoneTests/RowSelectionTests.swift`.

## A Prep or Backlog card is swiped on the Board tab

A card in the Prep or Backlog row slides left (3 Oct 2026) to show two
buttons: the card's one next action and a dots button. The board is one
`ScrollView`, not a `List`, so `.swipeActions` is not available: the slide
is `SwipeRevealRow`, a horizontal `DragGesture` attached with
`.simultaneousGesture` beside the tile's own button (never `.gesture`, which
would steal the tap or the vertical pan). `PhoneCardSwipe` (`CardSwipe.swift`,
Foundation only) holds the arithmetic: a drag counts past 20pt and only when
it is mostly sideways, the buttons are 148pt wide, and the row opens or
shuts past 56pt. One card is open at a time (`revealed`); a tap on an open
card shuts it instead of opening it; select mode, a search, a project
change, leaving the Board and a snapshot that no longer lists the card in
Prep or Backlog all shut it. In progress and Done do not swipe.

**The first button is the card screen's own rule.** `PhoneCardSwipe.primary`
is the two arms of `CardSections.nextAction` this view reaches (Prep: Refine
if `PhoneRowSelection.tickable("prep", …)`, else Start if startable;
Backlog: Start if startable), pinned equal to the Mac's
`CardActionWeight.primary`. `PhoneCardSwipe.canStart` is the card screen's
Start gate, **moved there verbatim** so both read one body. The first press
arms ("Really start?", or "Start unplanned?" on a card with no plan and not a
scout), the second sends `board_dispatch` (or `board_refine`) through
`enqueue` under the card's scope, as `pressStart` does; START HERE, the plain
move and the changed-plan confirmation stay on the card screen, and a
plan-gate refusal is drawn under the tile and read like any other (the
card screen's `noteArrived` arms its confirmation off the note whether or
not it was read, so leaving it unread only stranded the receipt). The
button wears QUEUED / SENDING / SENT while the press is on its way, and a
queued Delete says so under the tile. The Mac's refusal is drawn under the
card in orange, ahead of the phone's own, and the phone's own clears when
the press's mark appears. A drag that scroll takes over snaps the tile
back (`@GestureState`).

**The dots open a menu, and Delete card is behind it**: a confirmation
dialog with Delete card in red and Cancel, then "Are you sure?" with Delete
and Cancel; Delete sends `board_delete` through `enqueue` (no `Arm`, the
dialog is the confirmation) and the tile dims through `cardLeaving`. The
menu is built for more quiet verbs; only Delete card ships. For VoiceOver the
card carries the same verbs as actions (the first button, and Delete card,
which goes straight to "Are you sure?"). The three verbs were already on
both phone doors; no daemon code changed. Pinned by
`host/tests/test_phone_card_swipe.py` and
`ios/BobPhoneTests/CardSwipeTests.swift`; the gesture's arbitration inside
the scroll view is the one thing only a real phone confirms.

## One decision list

Needs you is the live decision list, not a second waiting-only feed and not
Catch up (Catch up remains a historical episode list behind its own link,
which sits on the **Fleet** tab, not above the decisions). Each subject is
one row: `s:<sessionId>` or `c:<cardId>`, stable as the reason changes.
Classification matches the Mac Inbox's **three kinds, named by what you
do**, in this rank: **Answer** (a permission ask or a question), **Look at**
(a card whose assistant has gone, or a hand-check somebody asked for),
**Stopped** (an agent waiting on somebody). The finer wire name
(`PhoneInboxWireKind`: `permission`, `question`, `ended_work`,
`manual_check`, `waiting`) is what `inbox_ack` is keyed on and ranks inside
a kind. **Nothing FYI is listed**: a ready plan is the Board's, a closed card
awaiting review is its Done column's, a burst alert is the Profile screen's
Access log (gated on `board.accessLogSupported`). A session is admitted on
the Mac's own rule (`needsHuman`): it is a **live** row — running, waiting
or sleeping, `PhoneInbox.liveBuckets`, the Mac's `Category.live` — that is
in the published waiting bucket, has a published permission, or has a
notification row; a nonempty question decides the *kind* and admits nothing
by itself, and a finished or abandoned row is never an entry whatever it
still carries — there is no phone-side dwell. A permission whose session
has no agent row is still one read-only item; a later matching row upgrades
that same key. A card is never merged with the session that worked it.
A press on a card entry opens that session's agent sheet while the session
is still in the fleet (`PhoneInboxRoute.sheet`; the Mac's `PanelView.open`
does the same with the agent's row), and the card sheet once it is gone.

The tab badge and the top bar both read `Snapshot.needsYouCount`, which is
the length of that list: every entry blocks somebody. "nobody needs you"
appears only when the list is empty. A refreshed snapshot recomputes both
the rows and the count.

**The rows are short and clip nothing** (`InboxRow`): face, the kind's word,
title and how long it has waited (from the frame's stamp and the row's idle
figure; a card entry is undated, the phone carrying no card clock), then
the detail in full — the ask, the question, the agent's own one-line
summary or the tool it stopped on, a card's summary or steps — and Dark Army's
own line only where it has one (a card's `dispatchError`, then
`queueReason`; the orphan line for a permission with no row). The kind's
word is spelled next to the red accent, never colour alone.

**The tab routes; it does not answer.** The whole row is one press and
opens the agent or card sheet, where the answer box, the permission verdict,
Mark done / Send back and Mark checked / Mark reviewed (`board_manual_clear`
/ `board_review`, with Passed / Failed as `board_manual_outcome`, each chosen on its own line of both phone tuples, each
carrying a current-state echo) live; outcome accept/revise are not phone
verbs. Navigation posts no action. **An agent's row also carries that
agent's own verbs** (`InboxAgentVerb`, 23 Sep 2026): after Dismiss, the
swipe offers **Close** (Acknowledge & close terminal, `close_terminal` with
`by_person`, confirmed in a dialog) and **More** (Hide, Low priority, Stop),
each gated on the same `can_*` flag the agent screen reads and queued under
the session — so a finished agent is acknowledged and closed without
opening it; the verdict and the answer stay on the agent screen. **One verb, one meaning: Dismiss** — the
trailing swipe on any row but a permission ask (`PhoneInboxAck.showsDismiss`
over the reducer's `dismissable`), and **Dismiss all** above the list, armed
against the exact id set and disarmed by a changed list — both `inbox_ack`,
a daemon-shared hide published on snapshot section `inbox` until the
question changes, the kind changes, or the session or card goes; snooze
stays forbidden and there is no phone-side UserDefaults hide list. A
*session* dismissal also drops the row's notification card and demotes a
`confused` / `error` state to `idle`, so the widget, the Fleet row and the
Mac's strip go quiet with the list; a card dismissal is the hide alone. An
older Mac (`inbox.available` false) draws no Dismiss control. A refused
dismiss is the Mac's own words under the row, in refusal colour. There is
no Delete on the list.

Pinned by `host/tests/test_phone_inbox.py`, `PhoneInboxTests.swift` and
`DecodeToleranceTests.swift`.

## The verbs are one word list

Stop, Delete, Hide, Dismiss, Close terminal and Low priority are spelled
the same way on the Mac and the phone (25 Sep 2026,
`plans/2026-09-25-usability-accessibility-pass.md`). `ios/BobPhone/Verbs.swift`
is Foundation only and byte-equal from `enum Verbs {` down with the panel's
copy: each verb's plain label, its armed "really?" label (Hide and Dismiss
fire on the first press and arm to their own word), the spoken form, and the
one no-undo sentence Close terminal carries — the Close dialog's title. The
agent screen's quiet verbs, `InboxAgentVerb.label` and the Needs you swipe
read it; the Mac's `WrapUpBar`, `LowPriorityBar`, `StopBar`, `DeleteBar`, the
row's Hide and the triage legend do too. What a press does is still each
caller's, with the same actions and fields; only the words moved. Pinned by
`host/tests/test_verbs_shared.py`, which runs the table under `swiftc`.

## The terminal is a real emulator, and it is the one exception

A Dark Army-hosted terminal on the agent screen is **the same emulator
the Mac's window uses** — SwiftTerm, in `ios/BobPhone/TerminalPane.swift`
(`PhoneTerminalPane`, `PhoneTerminalHost`), fed the same bytes: at home
from one held-open sealed stream (`SealedTerminalStream`,
`docs/transport-contract.md`'s terminal section), away from the poll
(`PhoneClient.fetchTerminalBytes` → `onTerminalBytes`, a `painted` frame
resetting the emulator first). The bytes go straight into the view,
never through `@State`; the coordinator is the panel's reconnect pattern
(`setup(isReset: true)` on every attach, a generation on every callback,
1 s / 2 s reconnects, the resize debounced) and cancels the socket on
`.background` **alone** — `scenePhase != .background`, never `== .active`:
a Control Centre pull, a banner and the away Face ID sheet all drive the
scene inactive, and tearing the socket down for one of those costs a fresh
`_home_open` (a disk write), a re-armed away lease and a whole repaint.
Coming back out of the background attaches afresh with a fresh paint.
**A stream that cannot open says so**: the reconnect doubles its wait to
`maxReconnectDelay` (30 s), writes a note from the second close, stops
after `maxAttempts` (6) and stops at once on a 409 — the Mac saying it
does not host this session — and the header reads `offline`, not `live`,
whenever there is no feed. Keys are
SwiftTerm's own keyboard and its Esc/Ctrl/arrow accessory bar, sent up
the stream at home and as `terminal_input` `bytes` away (in order, one
receipt behind the last, through `post`), with no phone-side rule about
what may be pressed. A key pressed before the stream's head has landed is
**held** (`pendingInput`, ≤ 4 KiB) and sent the moment it does; a key that
cannot be held is reported (`onInputDropped` → the pane's note), never
swallowed. Away, keys are **batched** (`AwayKeys`, 26 Sep 2026): only a
committing key — Enter, Ctrl-C, Ctrl-D, a lone Escape — sends at once;
letters, Backspace, Tab and arrows wait for a 0.8 s pause. Each batch is one
write against the Mac's **own key bucket**,
`RELAY_MAX_KEY_WRITES_PER_MINUTE` (10 a minute, beside and never from the
writes bucket, so typing cannot spend an Approve's allowance). The phone
mirrors that bucket (`AwayKeys.Budget`) and **holds** a batch the Mac would
refuse; a `too many remote requests — slow down` that comes back anyway
puts the keys back in front, in order, and retries — no key is dropped for
the rate, and the pane removes that refusal's receipt, so no REFUSED line or
agent-screen note stands for keys that land seconds later. Held keys belong
to the session they were typed into (`pendingFor`): a pane re-aimed at
another agent drops them with a note rather than type one agent's line into
another; coming home sends them up the stream only after any batch still on
the relay has answered, so nothing overtakes it; Done sends what is held in
one last write. The unsent text is drawn under the grid as it is typed
(`showPending`), with what it waits for. The pane fills its full-screen cover under one
row holding Done and a **fold** (`TerminalStrip`): the strip of what still
needs a press — the question, the permission buttons, the card verbs — is
one line until a tap opens it, and opens by itself only when an ask
arrives (`TerminalStrip.needsPress`); opened, it is bounded
(`hostedStripMaxHeight`) and sits **outside** the outer `ScrollView`, so
SwiftTerm's own scroll view owns the drag. Folded by default because with
the emulator's keyboard up an always-open strip left the terminal a
sliver. An older Mac
(`terminal_stream_supported` absent) gets one sentence and no emulator.

**A picture an agent names opens in a sheet.** `ImageLinks.paths` finds each picture path in an agent's message (a bare name right after a path takes that folder; web addresses and hidden folders never match; at most twelve) and `ConversationImageChips` draws them as chips under the message, outside its combined accessibility element. A tap opens **that one picture** in its own sheet (`PicturePopup`, `.picturePopup` on `ConversationScreen` and `HelperConversationPane`) presented over the conversation, never a rung on the sheet trail — a rung replaced the agent screen, which came back rebuilt on Main at the top — so Close or a drag down returns to the same tab at the same scroll position; and only a tapped picture is ever asked of the Mac, which serves it only from Dark Army's own checkout or an onboarded project, never Documents, Desktop or elsewhere at home (the sealed `image` read, `docs/transport-contract.md`) — no prefetch, no neighbours, no filmstrip. The sheet draws it with pinch and double-tap zoom (`ZoomablePicture`, a `UIScrollView`, so a GIF still moves), the Mac's facts about the file and what was shrunk, and offers **no way to save it**: no Share, no Photos. The phone's only copy is `ImageMemo` — memory only, never disk, sixteen at most, each dropped a day after it arrived (pruned on every applied state) and all of them with the pairing.

**Main carries the card's journey** (`CardJourney`, `JourneyRail` in
`AgentDetailView.swift`, 26 Sep 2026): under the lead, on a session working
or planning a card, five stops — Idea, Plan, Build, Check, Done. The current
stop comes from the board card alone (column, link, refinement, manual
steps); a passed stop's time comes from the card's timeline, from its first
moment to the earliest later moment of a stop after it, and reads blank
where either end was never witnessed or no later moment follows it (an
agent's own close writes `submitted` and `moved_done` at one instant, both
Done). Under the rail, the Mac's own open line (`CardTimeline.openLine`),
aged on the phone from the report's `generated_at`, drawn only while the
report was read under the card's current `CardJourney.fetchKey` — every
field `open_state` reads — so a caption from before a move or a review is
never shown after it. The timeline is the card screen's own `card` read,
asked only when that key has moved since the held report: never per minute
and never on a mere return to Main; an overtaken read is dropped, and an
older Mac gets the rail untimed. `CardJourneyTests`.

**The tabs lead the agent sheet, and Main is the first.**
Fleet opens `AgentScreen` (Main, Conversation, Details, Terminal when hosted)
with `PhoneAgentScreenBar` at the top of the sheet. Main is the lead (still,
name and status, card, every verb) as one scrolling page; every other tab
keeps only the card's title under the bar (`AgentScreen.titleOnly`,
`titleLine`) and scrolls the rest as one page — the conversation's turns,
ask and answer buttons (`AnswerBox` `part: .choices`) included, opening at
the foot. **The conversation's message composer is pinned under that page**
(`AnswerBox` `part: .composer`, 26 Sep 2026): a stack sibling, never an
overlay, drawn whether the agent is working or stopped, so a person can
always write to it; where `channel` is false one dim line says the message
may be turned down, and the Mac's refusal is the note. The whole box on
Main and Needs you keeps its stopped-and-channel gate. `DetailTab` still
decides the stream. Turns are
oldest-first, newest at the bottom: `>` for the person, `MarkdownText` for
the agent, one dim `⚙` line per tool (tap for brief and a one-line result,
never the bytes). Follows new turns only at the bottom; scrolled up, a
`↓ n new` row above the composer goes to the foot, and never paints over
the page. **The tab is live while it is watched (26 Sep 2026,
`ConversationLive`):** a message sent from it is drawn at once under the
turns as an echo marked `sending`, then `delivered` once the Mac took it,
read from the press's own receipt; the journal's person turn with the same
words, written after the press, replaces it, and an echo still waiting
after three minutes, refused or stuck is dropped (the refusal is the answer
box's note). While the agent is `running` or an echo waits, the page is
followed quickly — at home one small conversation page about every second
(`PhoneClient.followConversationOnce`, one ask at a time, `fetchConversation`
unchanged); away, the Mac's own socket push asks for the page
(`tookPush`), never a faster relay loop — and the check-in cadence below is
untouched. One dim line under the turns says what the agent is doing, the
fleet row's words (`PhoneAgentFacts.head`), only while it is working.
Answers arrive a whole message or tool call at a time: a journal is written
per message, never per word.
`ConversationCacheStore` is pairing-stamped, pruned at five days.
Catch-up 40 hops at home, 3 away; never `backgroundRefresh`. When the
hops left cannot reach the present (`ConversationCatchUp.jump`), the cursor
jumps to the last page and lands it as a reset — the newest turns first,
never minutes of paging from turn 0. Pages persist once per catch-up, in
order. Absent
`conversation_supported`, Conversation draws one sentence.

**The terminal is the second of two tabs, and the stream follows the tab.**
A hosted row draws `PhoneDetailTabBar` under its identity line —
**Details** and **Terminal**, the phone's copy of the Mac's `DetailTab`,
written byte-equal and pinned so (`host/tests/test_detail_tabs.py`). A row
running in an editor draws no tabs and is untouched. **Details is where
every open lands** — no persistence and no per-session memory — and it is
the ladder above: the last message, the facts, the answer buttons and the
card verbs, in `hostedDetails`. The Terminal tab opens the live screen and the
bounded strip in a full-screen cover. The phone asks for the terminal only while that tab is
showing (`syncWatch`, called from `.onAppear`, a changed `ownTerminal` and
a changed tab), so on Details it stops watching, and the Mac may take the
pty's width back. `AnswerBox`'s free-text reply yields to the emulator
(`terminalWins`) only on the Terminal tab; its buttons always stay.

**And it is the one named exception to two rules above.** The terminal
has its own text size — a grid of cells at the terminal's own size, not
prose that reflows — set in points with a `+` / `−` in its header and
remembered (`@AppStorage("terminalFontSize")`), so `TerminalPane.swift` is
`TERMINAL_EXCEPTION` on `test_phone_accessibility.py`'s point-size sweep;
it still carries no `.lineLimit(`. And it speaks as **one element**
reading the emulator's non-blank rows off SwiftTerm's own buffer
(`getLine(row:)`), because a screen reader would otherwise read a TUI as
hundreds of fragments; the size buttons carry names of their own. **The
width has one owner at a time**: while the Mac's panel pane is showing
that terminal the Mac decides and the phone's size frames are ignored;
once it is not, the phone's screen decides (`terminal_phone_resize`,
`_panel_focused_sessions()`), and the pane coming back takes the width
back without restating it. **Away the size rides the check-in**: the
pane keeps `PhoneClient.terminalPhoneSize` current while it polls, the
poll quotes it as `cols` / `rows` on the sealed `terminal` read, the Mac
applies it through the same `terminal_phone_resize` (never the panel's
unconditional resize), and a size that took answers a `painted` frame
whatever cursor was quoted — the ring's earlier bytes were drawn for the
old width. Until 21 Sep 2026 the away pty kept the Mac's width and every
check-in replayed output laid out for a screen three times wider than the
phone's, which the emulator wrapped into an unreadable smear. The cost: after the panel hides, the phone's
next size may be ignored for up to `FRONTMOST_TRUST_SECONDS` (15 s).
Pinned by `host/tests/test_phone_terminal.py` and
`test_phone_terminal_stream_drift.py`.

## The Menu tab gathers Usage, Comm, Scouting and Manual checks

Four tabs: Needs you, Fleet, Board, **Menu** (`MenuView.swift`), a grid four
across (two at accessibility sizes) of `MenuSection` tiles — Usage, History,
Comm, Scouting, Manual checks, Plans — beside the bundled Design system,
each pushed on the Menu's own stack with a back button. History, Manual checks, Scouting and Plans each say "not built yet"
against a Mac that does not serve their read (below). `PhoneTab(stored:)` reads a section name as `.menu`, so an old `comm`
draft and `bobphone://usage` still land. Pinned by
`host/tests/test_phone_menu.py`.

**Rebuild & restart opens from the Menu's Rebuild tile.** `MenuSection.rebuild`
is lit once the Mac's `rebuild` section says `available` and the phone is on the
home door (`client.via != .relay`); the verb `rebuild_app` is on the Mac's home
list only (`docs/transport-contract.md`), so from away the tile is dim and
`RebuildView` says so rather than asking for Face ID to be turned away. The
first press arms (`Arm.Slot.rebuild`, "Really rebuild and restart?"), the
second sends through `client.post` — never `enqueue`, and a lost reply is never
replayed (`ReceiptLedger.neverReplayed`; a person's RETRY sends once), so a rebuild is never banked and fired on
the phone's clock — and the screen reads "Rebuilding…" and
warns the Mac will drop off the link. `RebuildRules.line` owns the words: a
dropped link after a press (or a held picture still saying `rebuilding`) reads
"Restarting — waiting for the Mac", and "Rebuilt at HH:MM" only when
`lastFinishedAt >= pressedAt`, so last week's stamp never reads as this press
landing; a failure is the Mac's own words in `Theme.alarm`. An agent whose row
says `rebuild_offered` (its report's `dark-army-next` marker,
`docs/session-state-contract.md`) draws `rebuildBox` on its screen with the same
arm. Pinned by `RebuildRulesTests.swift` and `test_phone_menu.py`.

**The week's token cost opens from the Menu's History tile.** Against a Mac
that publishes `history_week_supported` the tile is lit
(`MenuSection.lit(…, historyWeek:)`) and pushes `HistoryWeekView`
(`.navigationTitle("history")`); an older Mac leaves it dim and the page
that says so. The screen draws four lines — the seven-day total (the token
cost of Claude, Grok and Codex together, `.privacySensitive()`; "not
priced", never $0.00, where no day had a price), `$X reported — not added
in` beside it only where a provider reported one, `n not priced` where some
work had no price, and the span — then seven columns, one per local day,
each a bar stacked Claude / Grok / Codex (`Theme.phosphor`, `Theme.control`,
`Theme.phosphorBright`; never amber or red) over the day's figure, its
reported mark and its weekday, with a legend naming all three in words; at
an accessibility size the columns become rows with a horizontal strip, and
each day is one element spoken from what it draws. "some cards were left
out" is drawn under the total when the Mac's page says `truncated`. The
arithmetic is `LedgerWeek`, the fold the Mac's History calls, byte-pinned
from `enum LedgerWeek {` down (`test_ledger_week.py`), so the numbers are
the Mac's; the day keys are the phone's own local days, so a phone in
another time zone can place a figure on a neighbouring day while the week's
total is unchanged. The week is the sealed `history_week` read
(`docs/transport-contract.md`), relay first when away, asked **on appear
and on pull only** — never the poll, `backgroundRefresh` or the widget —
and held for the life of the screen, never on disk; while it loads Dark
Army talks (`AgentChatterView`), and a Mac out of reach is one sentence and
a Retry. Read-only. Pinned by `host/tests/test_phone_history_week.py`.

**Manual checks open from the Menu's Manual checks tile.** Against a Mac
that publishes `manual_checks_supported` the tile is lit
(`MenuSection.lit(scoutReports:manualChecks:)`) and pushes
`ManualChecksView` (`.navigationTitle("manual checks")`): a search line, a
status filter (`all` / `open` / `passed` / `failed`) and the list off the
sealed `manual_checks` read, open first then newest first, each row
`ManualCheckRules.rowLine` over the check, prose wrapping in full. A row opens
the file as a document (`MarkdownText`, `base: 12, mono: true`); where the Mac
publishes `manual_outcome_writable` it offers **Passed** / **Failed**, each
armed then confirmed, with a note (`board_manual_outcome`). The words and
the order are `ManualCheckRules`, byte-pinned with the Mac's Checks window
(`test_phone_manual_checks.py`). **On the card screen** a card flagged with a
check file draws the file under its steps off the on-open `card` read's
`manual_check` (`reportSection`'s branches) and the same armed Passed /
Failed (`PhoneCardAck.showsManualOutcome`); Mark checked is hidden there
(`showsManualClear` requires an empty `manualCheckPath`) and stays for a
card flagged with steps alone.

**Scout reports open from the Menu's Scouting tile.** Against a Mac that
publishes `scout_reports_supported` the tile is lit (`MenuSection.lit`) and
pushes `ScoutReportsView` (`.navigationTitle("scout reports")`): a `>`
search line over the list, newest first, each row the title, the verdict
wrapping in full and `project · date`, one element spoken from what it
draws. `ScoutReportSearch.matches` narrows by title, verdict, question,
project, card title and recommendation — the rule is `ScoutReports.swift`,
byte-pinned with the Mac. A tapped row pushes `ScoutReportReaderView`
(`.navigationTitle("report")`), which fetches the text **on open** and never
on the poll or `backgroundRefresh`, and draws the answer block as labelled
lines (`ScoutReportHeader.rows`) above the body through `MarkdownText`, the
card screen's call. The list and the body are the sealed `scout_reports` /
`scout_report` reads (`docs/transport-contract.md`), relay first when away.
After a 300 ms pause on three or more characters, and only against a Mac
publishing `scout_reports_body_search_supported`, the screen asks the
sealed `scout_reports` read again with `q` in the body and merges the hits
through `ScoutReportSearch.merge` (byte-pinned, newest first, each report
once), each hit drawn with its snippet under the verdict and spoken with it.
An older Mac keeps the instant search alone and is asked for nothing more.
An older Mac leaves the tile dim and the page that says so. Pinned by
`host/tests/test_scout_reports_surface.py`.

**Plans open from the Menu's Plans tile.** Against a Mac that publishes
`plans_supported` the tile is lit (`MenuSection.lit(scoutReports:manualChecks:plans:)`)
and pushes `PlansView` (`.navigationTitle("plans")`): a `>` search line over
every enrolled project's plans, newest first by the day in the file name,
each row the title, `status · area` where the plan states them,
`project · day`, and `card: <title> · <column>` where a card holds the plan,
one element spoken from what it draws. `PlanSearch.matches` narrows by
title, project, status, area, the file name's slug and the card title
(`Plans.swift`, phone-only — the Mac lists no plans, so no byte pin). A
tapped row pushes `PlanReaderView` (`.navigationTitle("plan")`), which
fetches the text **on open** and never on the poll or `backgroundRefresh`,
and draws it whole through `MarkdownText` under the title. Where the row's
card is on the board the phone already holds, a **CARD · <title>** button
opens that card's screen through `PhoneSheetRouter` —
`PhoneInbox.sheet(for:snapshot:)`'s rule, resolved at the draw; a card the
board no longer carries draws no button. Read-only: nothing here writes,
starts or deletes a plan. The list and the body are the sealed `plans` /
`plan` reads (`docs/transport-contract.md`), relay first when away. An
older Mac leaves the tile dim and the page that says so. Pinned by
`host/tests/test_phone_plans.py`.

## The Comm section talks to Mission Control

The Menu's Comm section (`MenuSection.comm`, `CommView.swift`) talks to **Mission
Control**, the Mac's standing chief of staff, which answers and acts
(`docs/context-host.md`). One scrolling column: the identity line, the
status word from `CommRules.status` (`older Mac` / `off` / `starting` /
`thinking` / `ready` / `ended`), the **reply** — the row
`snapshot.row(session: mission.sessionId)` drawn as the agent sheet draws
a hosted session's last message (`lastSummary` else `lastText` through
`MarkdownText`, `.fixedSize`, `.textSelection`, no `.lineLimit(`), a
chatter line while it is `running`, "— nothing yet —" with no row — the
four **chips** (`CommRules.chips`, touch measure, spoken as the question;
a chip is Send with that question), the **composer** (`TextField(axis:
.vertical)` at the `5...` minimum, `MicButton(id: "comm.ask")`), **Send**
(`terminal_input`'s **text** route through `CommRules.line`; the Mac's
refusals verbatim), **Terminal** (`PhoneTerminalPane` in a
`.fullScreenCover` under one Done row, watched on appear, released on
disappear; Done resigns first responder first) and **End** (armed then
confirmed on the `Arm` slot `missionEnd`). `mission_open` / `mission_end`
ride `post` like every write and are in no `settlingActions` set.

Open Mission Control leads the column when status is `off` or `ended`,
one press of the same `mission_open`, and a refusal is drawn under that
button rather than under a second Try again.

**Mission Control is not fleet work.** Its session lives in Dark Army's
own checkout, so without a rule the Fleet tab listed it under that
project. `FleetView.allRows` drops every row `CommRules.isMissionRow`
names — the snapshot's `mission.session_id`, or the `mission` origin
stamp — and the needs-you chip reads the same filtered rows; the Comm
tab still draws it by id. `test_comm_rules.py` runs the rule and pins
the fleet's use of it.

**Open-on-appear, once per visit** (a visit: Comm pushed from the Menu). Where
`mission.available && !mission.alive`, the screen posts `mission_open` once per appearance
(`openedThisVisit`), never on a timer; `starting` while in flight, Send
disabled; a refusal is drawn under that button rather than under a second
Try again. The Mac spawns
nothing while one is alive, so a second visit, a force-quit and a Mac
restart re-attach to the same session; nothing on the phone remembers a
session id, so a `/clear` successor follows. An older Mac draws one
sentence. `CommRules` is byte-pinned to the panel's copy
(`test_comm_rules.py`).

**Mission Control's helpers are tabs** (`CommHelperTabs`, `subagent_conversation_supported`): each is read-only, its own turns watched as `<session>#<agent>` (`ConversationSubject`); a finished helper keeps an open tab, marked `done`.

The Mac composes a Bearings digest (`docs/context-host.md`) the phone
may read as sealed `bearings` (`bearings_supported`); **no screen draws
it yet**.

## The phone keeps the last picture, and prepares a card by itself

**The phone keeps the last picture it saw.** `HeldPictureStore`
(`ios/BobPhone/HeldPicture.swift`) holds the bytes of the last full `state`
answer under `Application Support/held-picture.json` — `CardCacheStore`'s
discipline: `[.atomic, .completeFileProtection]`, the encode and the write
off the main actor, loaded once behind the Face ID gate, stamped with the
pairing and dropped by `adopt` on a pairing swap or `forget` on an un-pair.
`applyState` remembers on its applied branch, foreground only, floored at
`minWriteInterval` (15 s) and skipped on an unchanged digest.
A delta answer (`sections_unchanged` non-empty) is never remembered — the
held picture is always a whole answer, at most `fullStateFloor` +
`minWriteInterval` behind the screen.
`restoreHeldPicture` is the **second and last** site that assigns
`snapshot` (`snapshot = held`, `test_phone_unchanged_state.py`): on a launch
that has decoded nothing it hands the bytes to the same `Snapshot` decoder
and sets `pictureAsOf` — and nothing else. No `status`, no `lastHeard`, no
`heldStateDigest` (the first poll asks for the whole picture), no `onLive`,
no receipts, no widget: a held picture is not evidence. The tab roots draw
`HeldPictureBanner` — `// as of 5m ago · the Mac is out of reach` on
`FleetAge`'s clock — whenever the Mac is not answering and something has
been decoded, beside `StaleBanner` and `ReconnectBar`, never in their place.
Pinned by `test_phone_held_picture.py` and `HeldPictureTests.swift`.

**And opens a buzz from it.** A tapped notification is resolved **held
first**: `NotificationDestinationView` is a thin view over
`NotificationDestinationModel` (`NotificationDestinationModel.swift`), whose
synchronous `open()` runs before anything is asked of the Mac — it reads the
buzz's entry out of the notification log by receipt and hands it with
`client.snapshot` (live or restored) to `HeldDestination.resolve(entry:
snapshot:)` (`HeldDestination.swift`), a pure Foundation-only rule: the
card when `cardId` is in `snapshot.board.cards`, else the agent with its
category through `PhoneInbox.uniqueAgent`, else `.words(entry)`; nil with no
entry at all. The held view is drawn under `HeldPictureBanner` — the amber
`// as of … ago` line on `pictureAsOf` (the entry's own `seenAt` where no
picture was ever decoded), its tail `checking with the Mac` while a resolve
is in flight — as the same `PhoneCardDetailView` or `AgentDetailView` the
live page draws, or `HeldNotificationWords` (title, second line, when it
arrived, the kind only where a page once said it; every line wraps). The old
"Offline. This notification will open after Dark Army reconnects." sentence
is drawn **only** when there is no held view. Then `resolve()` — today's
live path verbatim over injected closures: one `refreshNow`, the receipt
page only when `status == .live`, else `offline` — runs as before and keeps
re-running on every changed `generatedAt` and on `status` turning `.live`
while no page has landed, so the live page replaces the held view the moment
the Mac answers, without another tap. **The held view yields to any page,
available or not**: it is drawn only while `page == nil`, so a page the Mac
answered with but marked unavailable (expired, an older Mac, no history)
falls through to its reason — the Mac has spoken, and a held view under
"out of reach" would contradict it. A `resolve()` that ended `offline`
draws Retry under the held view (dim while a resolve is in flight), so a
live Mac that answered nothing this once leaves something to press. **A
held view is never mistaken for a live one**: it consumes no receipt
(`router.consume` and `rememberNotification` stay on the live page's branch;
the swipe stays refused while `page == nil`, Close still consumes), assigns
no `snapshot` (the two permitted sites stay two), quotes no digest, sets no
`status`, `lastHeard` or `onLive`, and posts nothing of its own — every
control on those screens still goes through the queue, the person's press.
`HeldDestinationTests.testATappedReceiptComposesFromTheHeldPictureWithoutAFetch`
is the success criterion in code: `open()` composes the card with the
injected refresh and fetch counters at zero and under two seconds, and
`resolve()` against a not-live stub refreshes once and fetches never.
Pinned by `test_push_subject.py`, `test_phone_sheets.py` and
`HeldDestinationTests.swift`.

**And the phone keeps a log of the buzzes it saw.** `NotificationLogStore`
(`NotificationLog.swift`) holds `NotificationLogEntry` rows — `receiptId`
(`""` for a buzz from a Mac older than receipts; the `id` is then a UUID
minted at bank time, so two such buzzes never merge), `title`, `subtitle`,
`sessionId`, `cardId`, `kind`, `seenAt` (`UNNotification.date`), `tapped` —
under `Application Support/notification-log.json` with `HeldPictureStore`'s
discipline: `[.atomic]` then `.protectionKey: .complete` inside
`Task.detached(priority: .utility)`, writes chained in order, `load()` once
behind the Face ID gate, stamped with the pairing token and dropped by
`adopt` on a pairing swap or `forget` on an un-pair (`forgetPairing` now
drops four stores). `NotificationLogEntry.from` reads exactly the two lines
and the three ids (`receipt_id`, `session_id`, `card_id`) off the
notification and nothing else — no `act`, no `request_id`, no body — and
**never a kind**: the payload carries no kind key (`push.js` turns it into a
sound), so `kind` is `""` until `PhoneClient.fetchCatchUpPage` lands a 200
page for a `receipt_id=` query and `enrich(receiptId:page:)` fills `kind`
and `title` from its first `DecisionItem` where the entry's field is empty
— and `sessionId` / `cardId` only from a page of exactly one item: a
collapsed "N agents need you" receipt spans N decisions and binds no
subject, or the held open would aim at the first agent's card. The file is
decoded tolerantly (`init(from:)` over `KeyedDecodingContainer.value`,
`CachedCard`'s rule; `id` falls back to the receipt, then a fresh UUID), so
a record missing a key never blanks the log. **Four sources**, all through `PushRegistrar.bank`
into a memory-only `banked` list (the delegate and the tray reads run before
the gate, and the log is a protected file read only behind it): the tapped
buzz (`didReceive`, `tapped: true`, banked before the route is taken), the
ones waiting in the tray when the app opens (`clearBadge` reads
`deliveredNotifications()` **before** it clears), the ones the Mac's quiet
word clears (`clearDeliveredIfQuiet`, the same read-then-remove) and the
ones that arrive while the app is open (`willPresent`, still `return []`).
The unlock block drains it — `client.absorbBankedNotifications(token:)`
after `PhoneRouter.pair(token:)` and after `notificationLog.load()`, with
the Keychain record's token (a cold launch has not set `client.record` yet)
— and `absorb` merges by receipt (the earliest `seenAt`, `tapped` ORed, the
first non-empty field kept), prunes past `maxAge` (30 days) and `maxEntries`
(200), stamps the token and writes; an empty token keeps nothing. A bank
made while the gate is already open — a buzz that arrived on screen, one
the Mac's quiet word cleared — drains at once through
`absorbBankedNotificationsIfOpen`, refused until `start(record:)` has run
(the unlock block's `.restart` arm or the pairing `onChange` — both behind
the gate) and `notificationLog.loaded` is set (only the unlock block does),
so the file is still never written before the process's first unlock and
never over the entries it already holds (after a re-lock inside `BackgroundGrace`
the session survives and a bank does write, the footing
`heldPicture.remember` already has). `load()` reads the file **once per
process** (`guard !loaded`): the gate re-runs at every unlock while writes
are chained detached tasks, and a re-read with one pending would take the
older file and lose the entries just drained from the bank; after
`forget()` / `adopt` the file is gone, not replaced, and no other process
writes it. A kill between the tray read and the unlock loses
what was banked, never the receipt, which the router keeps on disk —
stated, not fixed: writing before the gate would put the person's
notification titles on disk outside the discipline every other phone store
keeps. An un-pair drops the banked list with the log
(`PushRegistrar.forgetBanked()` beside `notificationLog.forget()`): left
there, the next unlock would file the old Mac's buzzes under the new
pairing's token — and the unlock gate's `else` (no Keychain record after
`pairing.load()`) drops it again, because `bank` keeps accepting while the
phone is unpaired (an old Mac pushes until its own un-pair; the 403 paths
unregister nothing) and a guard inside `bank` would lose the cold launch's
tray, whose read runs before `pairing.load()`. A pairing becoming current
drops it a third time: `start(record:)` calls `PushRegistrar.forgetBanked()`
when the token differs from `client.record`'s (compared before `stop()`
nils it), because between an un-pair and the next scan no gate re-runs,
the foreground drain refuses and `bank` keeps accepting, so the pairing
`onChange`'s `start` and the first drain under the new record would file
the old Mac's entries under the new token; the cold launch and the gate's
`.restart` lose nothing because the unlock block drains before it calls
`start`, and a same-token restart keeps the bank. **Catch up reads it when the Mac is out of reach**: with
`offline || client.status != .live`, a `NOTIFICATIONS SEEN ON THIS PHONE`
section under `HeldPictureBanner` (its tail the fleet's rule: `checking
with the Mac` until `status == .unreachable`) lists
`notificationLog.entries` newest first (title, second line, `FleetAge` age,
one `spoken` sentence), each row a `DecryptButton` that resolves through
`HeldDestination.resolve` and opens the held card or agent as its own sheet,
or, for `.words`, unfolds the row in place. The widget and
`BackgroundRefresh` never read the log or the resolver. Pinned by
`test_push_subject.py`, `test_phone_held_picture.py`,
`test_phone_offline_and_receipts.py` and `NotificationLogTests.swift`.

**And PREPARE keeps working with the Mac out of reach**, with the person's
own Anthropic key. `AnthropicKeyStore` (`AnthropicPrepare.swift`) keeps it
in the Keychain — `kSecAttrAccessibleWhenUnlockedThisDeviceOnly`, never
synced — under Profile → PREPARE ON THIS PHONE ("key saved · ends …1234",
REPLACE, REMOVE; the key is never shown back). The composer's
`prepareRouteAvailable` is "the Mac, or this phone": `phoneRoute` is
`offline && AnthropicKeyStore.hasKey`, decided per press from the poll
loop's own verdict. `PhonePreparer.prepare` builds **the Mac's prompt**
(`PreparerBrief.text` / `modeHeadIdea`, byte-pinned to
`card_preparer_brief.BRIEF` and `card_prepare.MODE_HEAD_IDEA`, through
`CardPrepareRules.promptForIdea`), sends one `POST` to
`api.anthropic.com/v1/messages` on `claude-haiku-4-5-20251001`, and reads
the answer with **the Mac's readers** — `CardPrepareRules` is a
Foundation-only port of `card_prepare.py`'s parsers and refusals, sliced
and run under `swiftc` against the Python functions by
`test_phone_prepare_parity.py`. The roster is the helpers Dark Army ships
minus the preparer (the Mac's `_roster_verdict` drops an undeclared one at
create, never refuses); the FOLDER menu is the catalogue's roots; photos
refuse the phone route in words. The result is the composer's own
`PhonePrepareResult` with `preparedVia: .phone`, so the apply block is
shared, and the line under the button says which side answered. **The key
leaves the phone to Anthropic and nowhere else**: the URL is a literal that
appears once in `ios/`, the one reader of `AnthropicKeyStore.load()` is the
preparer, the answer comes back as fields and never as a body, and the
parity test greps every transport, store and widget file for the store's
name.

**The new-card form's assistant block also draws each offered assistant's
token usage**, as `ComposerUsageRow`, off `client.usage` through
`ComposerUsageRule` — the same bars the Usage tab already holds. Every
offered assistant shows its seven-day used percentage; Claude also shows
the five-hour window. **One column per assistant, under its own tile**
(the tiles are drawn `fills`, equal widths, so the columns line up) and
each window on its own line — `7d 42%` over `5h 18%` — never one run-on
line. A stale or missing bar is `–`, never a zero. The row is read-only
and starts no poll of its own: it redraws when the existing check-in
assigns `usage`. Pinned by `host/tests/test_phone_composer_usage.py` and
`ComposerUsageRuleTests`.

**The form offers Build / Scout** (`kindControl`, the Mac composer's
chips) only against a Mac whose board says `scout_supported`; an older
Mac drops `kind` on `board_create` and would write a build card, so the
control is absent there rather than present and ignored. `kind` rides the
create, the banked outbox entry and the draft only as `"scout"`; a scout
draft has no plan, so Save & Refine and start-when-planned are absent for
it. `test_scout_cards.py` pins the three files.

**A scout's report is its own section** (`CardSections.Section.report`,
`REPORT`, on every scout): "still out" words before the attach, then the
text off the on-open `card` read (`CardFull.report`, `_card_report`,
`_card_plan`'s twin) through `MarkdownText`, or the path and why not.
Above the text, the card's stored `report_verdict` · recommendation word
through `ScoutVerdictLine` (byte-pinned in `ScoutReports.swift`), drawn in
full and absent on an older Mac.
Under a Done scout's report, **Promote** (`PhoneActions.boardPromote`),
one press, unarmed, drawn only where the board says `promote_supported`.

## A glance is not a reconnect

`.inactive` locks the screen (`lock.lock()`) and **leaves the poller
running**; `.background` pauses it (below). The unlock path keeps a
running poller and restarts a stopped one. A Control Centre pull, a banner
or the Face ID sheet before an away write therefore comes back to the
picture it left, not to attempt zero and a fresh check-in. The app-switcher
snapshot still shows the lock screen, which is what the lock was for.
Pinned by `test_phone_glance.py`.

**And a brief backgrounding is not a reconnect either** (21 Sep 2026).
`.background` stamps `PhoneClient.departedAt` and flushes both counter
pairs to the Keychain (`suspend()` — nothing is torn down: the loop, the
channels, the record and the held digest stay). While departed the loop
polls nothing (no check-in, no attempt noted), and it stops itself past
`BackgroundGrace.window` (300 s — deliberately below the 15-minute
background-refresh floor, so a `BGAppRefreshTask`'s own client never
shares the `to-phone` mailbox with a stale loop; a pop there is a
removal). The unlock gate decides in one place, `BackgroundGrace.verdict`:
back inside the window with the poller alive, it keeps the poller and asks
for exactly one check-in now (`wake()`, which waits out a check-in the
system cut mid-flight rather than joining it, then polls with
`probeHome: false`); back later, or with nothing running, it starts fresh
as a cold launch does. The gate's `load()` re-publishes a record whose
counters moved (`suspend()` landed them, and `PairingRecord`'s synthesized
`==` sees them), and the pairing-record change handler ignores a
counters-only change while the poller runs
(`BackgroundGrace.keepsPoller` over `PairingRecord.identity` — everything
but the four counters; a changed address, list or key still restarts), so
that publish cannot undo `wake()`. `wake()` waits for the `polling` slot to
clear, not merely for the cut task's value, since the loop's own
continuation clears it after the value's waiters resume. The lock at
`.background`, the widget reload and the refresh booking are unchanged;
`departedAt` is memory only, so a process iOS killed starts fresh. A
check-in that lands while departed (in flight at `.background`, finishing
in the seconds before iOS suspends the process) applies the picture, the
digest, the counters and `lastHeard` but fires **no `onLive`** — both
`applyState` sites are guarded on `departedAt == nil` — so the outbox
drain and the `.sent` receipt replay never ask for Face ID behind the
lock (`LAContext` cannot present from a backgrounded app; a queued press
would close `.stuck` with no prompt shown); they wait for `wake()`'s
check-in, made after the person unlocked. What a return inside the window shows: the
picture you left, the AWAY badge still up (`status` stays `.live`),
possibly a brief `StaleBanner` until the check-in lands — never the
offline line or a fresh relay round trip. Pinned by
`test_phone_background_grace.py` and `BackgroundGraceTests.swift`.

## The phone comes back where you left it

Every lock tears the hierarchy down, so the app keeps a small note of where
the person was (26 Sep 2026): the tab, the Menu section, the profile push,
the sheet trail and, per rung, the agent page and the text typed into it —
an agent reply, a card's touched editors with the revision they were typed
against (`draftTouched` stops the reseed and `editRevision` is what Save
quotes, so a card changed since is refused with the Mac's copy, never
overwritten; touched text with no revision is not restored, and an editor
reopens only with its own text), its message
box. The card's status note is the Mac's words, not the person's, and is
not kept; nor is an open conflict, which the next Save raises again.
`PhonePlaceStore`
(`ios/BobPhone/PhonePlace.swift`, Foundation only) holds it under
`Application Support/place.json`, written `[.atomic, .completeFileProtection]`
by `flushNow()` on the line after `outbox.flushDraftNow()` in both
`.inactive` and `.background`, before `lock.lock()` — `ContentView` hands
the store a `compose` closure reading the live hierarchy — and debounced on
every tab, section, trail or profile change. A crash between those loses
only text typed since the last one. The file carries the SHA-256 hex of the
pairing token, never the token; the unlock gate reads it after
`client.notificationLog.load()` (a protected-read failure retries at the
next unlock, `PhoneRouter.restore`'s rule), `adopt`s the pairing right after
`PhoneRouter.shared.pair(token:)` — another Mac's place is deleted, file and
all — and only then `arm`s it; a pairing made while unlocked is adopted in
the record's change handler, and `forgetPairing()` (the deliberate un-pair
and the 403 alike) and an unpaired gate delete it. **Applied once per
unlock, after Face ID**: `ContentView`'s one arrival runs `applyPlace()`,
then the draft's tab, then the router's slot. The tab and section land at
once; the trail waits for a picture (`generatedAt != 0`, the held one or the
first live one) — or is dropped, its text kept, the moment the person
moves off the restored tab or section first — and is cut by
`PhonePlaceRules.cut`: from the bottom, an
agent by session over all five buckets, a card by id, Catch up only whole,
stopping at the first rung the picture no longer lists — a decision, a
changed file, a notification or a Catch up group is never restored, and
nothing is fetched to resolve one. The rungs are rebuilt in one assignment
(`PhoneSheetRouter.restore`), each with its own entry state. **A tap, a
widget link or a banked composer draft wins** (`PhonePlaceRules.wins`): the
place is set aside and only its typed text is kept, memory only, handed to
the next fresh open of that same agent or card (`takeOrphanDraft`, on Main);
a tap arriving after a restore still wins. **The Terminal page comes back
as Details**: the cover is a live socket claiming the pty's width and is
never re-attached by an unlock. A fresh open still lands on Main; the rung
remembers its page for Back and for the place, which is the one exception
to the Mac's "every open lands" rule. Scroll positions are not kept. The
widget and `BackgroundRefresh` never name the store. Pinned by
`test_phone_place.py` and `PhonePlaceTests.swift`.

## Keys typed from away are batched

Away, every terminal send is one relay write against the Mac's key bucket
(`RELAY_MAX_KEY_WRITES_PER_MINUTE`, 10), so `PhoneTerminalHost` holds keys
and sends the batch on a committing key (`AwayKeys.flushesAtOnce`: Enter,
Ctrl-C, Ctrl-D, a lone Escape) or after `AwayKeys.pause` (0.8 s) of quiet,
paced and retried as the pane section above states;
`terminal_input` rides with `refreshAfter: false`, since the 8 s poll brings
the screen back anyway — but **a batch that landed asks for its echo at
once** (`flushAway` → `fetchTerminalBytes()`, a sealed read that spends no
write and checks no lease; 21 Sep 2026): before that the keys were on the
pty from the 200 and the screen learned it only on the next check-in's
terminal leg, which read as a terminal that takes no input. The pane says
so **under** the emulator (`AwayKeys.hint`, in the stack, never a bottom
overlay: with the keyboard up the overlay covered the prompt line) while
nothing else is noted. At home the live stream is untouched.

## Dismiss all, photos away, the lease reminder, the widget's face

**Dismiss all** on Needs you is the Mac's `Inbox.dismissable` rung for
rung (`PhoneInbox.dismissable`: every entry but a permission ask), one
`inbox_ack` per row, armed against the exact id set and disarmed by a
changed list. **A photo picked away stays on the phone** (`local = offline ||
knowsItIsAway`) and a card holding one is banked whatever the route; the
outbox reads `photosNeedHome` as "wait for home", never as a refusal. **The
lease reminder** (`LeaseReminder.swift`) books one local notification
twelve hours before the published `leaseExpiresAt`, re-booked when it
moves and cleared when there is none or on un-pair. **The widget's face
opens its agent**: `FleetSummary.Face` carries `sessionId`, the medium
tile wraps the column in `FleetLinks.agent` (`bobphone://fleet?session=`),
`PhoneRouter.open` holds the id in memory behind the face check and
`applyPendingTab` opens the agent sheet where the fleet still lists it.

**The profile screen switches the bot's access** (26 Sep 2026). Where the
Mac publishes `bot_access` on a devices row (`PhoneDevices.bot`), the
profile screen draws **BOT ACCESS** after AWAY: the bot's name, then per
side a line in `BotAccessRules.words` — "on — no timer", "on until
<time>" (through `AwaySpan.until`) or "off" — a `.menu` `Picker` of the
five positions (Off / 1 hour / 6 hours / 24 hours / No timer) seeded and
re-seeded from the published mode so a desk change wins, and **RESTART
TIMER** while a timer runs. A choice that differs from the published mode
posts `set_bot_access` through `PhoneClient.post` — receipt token, LAN
then relay, Face ID away — judged by `ReceiptEffect.botAccess` against
the Mac's `bot_access`, and **never re-sent**: a record a failed press
left `sent` is dropped by the sweep's `evidenceBeforeSending` before any
resend. SENDING… shows while it is
out; a refusal is drawn inline under that side in the Mac's words and
the menu goes back to the Mac's current position, a move that never
posts (`botReseeding`, `BotAccessRules.shouldPost`). The key's
presence is the version marker: an older Mac publishes none and the
section is absent. The Mac is the authority and re-checks every request
the bot sends (`docs/transport-contract.md`, *The bot's access is two
grants*); `BotAccessTests.swift`.

## Allow, Deny and Acknowledge on the banner

With the desk's per-phone switch on (**Answer from the lock screen**, the
Devices menu → `set_lock_screen_actions`, loopback only), a buzz about one
alert carries an `act` word and identifiers — `permission` with
`request_id` + `session_id`, `acknowledge` with `session_id` — and
`push.js` names the category (`bob.permission` / `bob.acknowledge`) iOS
draws the buttons for. `LockScreenActions` registers three
`.authenticationRequired` actions, so iOS asks for the unlock before the
press runs; the press is **one** write through `PhoneClient
.lockScreenWrite` (a fresh client on the Keychain record, `quietPost`'s
walk — no Face ID sheet, no receipt, no outbox) posting the verbs the app
posts, `permission_verdict` or `dismiss`. The Mac re-checks the pairing,
the lease and the tuple as for any press. A collapsed buzz, a finish, a
machine alert, or a phone whose switch is off carries nothing and the
buttons are not drawn — the push is byte-identical to before. Pinned by
`test_lock_screen_actions.py`.

**The banner's three lines are iOS's own rendering of the `aps` dict the
mailbox built, and the phone reads none of them.** Top to bottom: who and
what kind ("Vex wants to run a tool", `aps.alert.title`), the card or
session it is working (`work`, the subtitle) and what is needed (`need`,
the body — "Approve running Bash", the question's text or the agent's own
one-line summary, clamped to 120 characters on both ends; absent where there
is nothing to say). `Push.swift` reads only `act`, `session_id`,
`request_id`, `receipt_id` and `destination_version` off `userInfo`; the
words are for the person on the lock screen, and the app fetches the real
content over the sealed channel once it is open. The wire is
`docs/transport-contract.md`, *And one line saying what is needed*.

**A buzz about one agent may wear that agent's portrait.** The notification
service extension (`ios/BobPhoneNotification`) wakes only when the payload
carries `mutable-content`. It reads `userInfo["face"]` and loads one
portrait from the appex's own `portraits` folder. When the signed
entitlements include the communication capability it builds an
`INSendMessageIntent` and applies `updating(from:)`, then writes the
original title, subtitle and body back if those three were rewritten, so
the banner does not become a bare slug. TestFlight signs the app and the
appex with the `.communication.entitlements` siblings, and the app declares
`INSendMessageIntent` in `NSUserActivityTypes`, so the portrait takes the
app icon's place (23 Sep 2026); an export the App ID refuses re-signs with
the plain files and ships with a warning. When the call throws — the grant
is not in the signed blob — the same helper attaches the portrait beside
the words and the app icon stays. The sender is keyed on the slug plus a
fingerprint of the portrait's bytes (`NotificationFace.senderIdentifier`),
because iOS keeps the first image it saw for a sender: a recast that kept
a slug must still show the new face. A slug that is not a cast name,
or a missing file, delivers the original content and no portrait. The
helper sees no pairing secret, no App Group and no Keychain, and it does
not donate the intent. Foreground suppression is unchanged: `willPresent`
still returns `[]` while Dark Army is on screen. Until the mailbox sends
`mutable-content`, the extension never wakes and the banner is today's icon.

## The phone goes relay-first once it knows it is away

**The phone goes relay-first for writes once it knows it is away** — its
last successful poll travelled by relay (`PhoneClient.knowsItIsAway`,
`via == .relay`, the same field `BrandBar` draws as AWAY, never a second
derivation): `post`, `quietPost` and `prepareCard` run the sealed leg
*before* the home walk, `upload` refuses in `photosNeedHome`'s words at
once, and the away `poll` asks the relay first and then probes home,
which is what turns AWAY off again by itself. **PREPARE's reply can go
missing and is asked for again** (21 Sep 2026): the press carries a
`command_token` of its own (`PhoneClient.prepareMark`, minted for this
verb and never through `receipts.open` — a draft is not a pending press,
and its whole point is the body `post` throws away), so when the relay leg
ends without an answer — the deadline passed, a poll popped the reply, the
phone slept past the mailbox's expiry — `recoverPrepare` sends the same
body up to `prepareReplayAttempts` (4) more times, `prepareReplayGap` (5 s)
apart, each waiting `prepareReplayTimeout` (30 s), and the Mac's receipt
ledger answers with the draft it already wrote. The Mac answers the
single-flight "already writing" refusal with a **202** the ledger skips
(`daemon_board.PREPARE_BUSY_DETAIL`, `api_server._prepare`), so a replay
that lands mid-write is asked again rather than freezing "give it a
moment" into the ledger; every other refusal stays a 409. Cancellation
stops the loop between hops. **The direct walk's
non-primary candidates share one probe length** (`Client.directProbe`,
4s): only the address on file keeps the action's own patience — 135s for
PREPARE, which is load-bearing at home because `timeoutInterval` is an
inactivity timer — and away that patience used to be spent on silent
addresses before the only leg that could answer was tried. A cold
launch goes the way the last poll went too — `seedRouteFromLastPoll()`
sets `via = .relay` from `lastKnownAwayKey` when the record carries a
relay channel, so `pollOnce` takes its own away branch on the first
poll; it never seeds `.lan`.

**The check-in a person waits on is fast either way (25 Sep 2026).**
`start()` and `wake()` set `wakeCheckIn`, spent by the next `pollOnce`:
when the last poll went home and a relay channel exists, that check-in
tries **only the address on file, for `Client.wakeProbe` (1s)**, not the
walk's eight seconds and the other addresses — at home the Mac answers in
milliseconds, away the relay is tried a second later. A quick probe that
missed is not proof of being away (a slow Mac, a radio waking from power
save), so once the relay has filled the screen the same check-in walks
every home address at the ordinary probe length, and a 200 sets
`via = .lan` before the AWAY badge settles — on a `wake()` too, which polls
with `probeHome: false`, so the walk is gated on the quick check-in alone.
The socket follows the last route: away last time, `start()`, `wake()` and
`prewarm()` (from `.active`, while Face ID runs) want it at once; home last
time, it comes up only when the quick probe misses, just before the relay
rung, whose request waits the moment it takes. A home opening therefore
opens no relay line and spends none of the Mac's `peer:1` budget (review,
25 Sep 2026). Any home answer — a refusal included, which leaves `via`
unassigned — closes it again; nothing is sent on it before the unlock, and
a failed or cancelled unlock closes it (`abandonPrewarm()`). A `RelayChannel.request` made while the
line is still coming up (`RelaySocket.comingUp`: a connect actually in
flight — the reconnect ladder's sleep holds no task and never counts — or
open for under `peerGrace` 0.4s with no `peer:` word read yet) waits up to
`RelaySocket.readyGrace` (1.5s) for it before choosing socket or mailbox —
the socket answers in tens of milliseconds, the mailbox in seconds. The
Mac's side of the cost: `peer:0` ends its wake arm, so a line opened at
home holds the mailbox for seconds (`docs/transport-contract.md`). Pinned
by `test_phone_wake_fast.py`.

**And while it is away and on screen it holds the socket lane open**
(`ios/BobPhone/RelaySocket.swift`, the away door's fast lane —
`docs/relay-socket-contract.md`).
A record whose pair reply carried `relay_ws_url` (`PairingRecord.relayWSURL`,
tolerant decode, part of `identity` so a changed address restarts the
poller as a changed mailbox address does) gets a `RelaySocket` on its
`RelayChannel` from `PhoneClient.start()` alone — `backgroundRefresh` and
`lockScreenWrite` never open a line. The socket is **wanted** while
`via == .relay` and the app is on screen, and on every launch, return and
Face ID prompt until a home answer lands: `via`'s `didSet` opens it on
`.relay` and closes it on `.lan`, `suspend()` closes it at `.background`
(iOS suspends background sockets, so the line is never left to die
invisibly), `start()`, `wake()` and `prewarm()` want it, `stop()` closes it. It builds only
`wss://…/ws?ch=<channel>&side=phone` (`RelaySocket.url`; any other scheme
opens nothing), pings every `pingSeconds` (20) and reconnects on a ladder
of 1, 2, 4, 8, 16 then `maxBackoff` 30 s, reset after 30 s stable. The relay's
plaintext `peer:1` / `peer:0` words are read by the socket into
`RelaySocket.peerPresent` (false on open, reset on close; `peerWord` is the
parse) and never handed to `onText`. **The
request rule**: while the socket is open **and the Mac is on the other side
of it** (`isOpen && peerPresent`) every request — the check-in's
`state`, every press — goes down it first (`RelayChannel.sendViaSocket`,
sealed with the same key and the shared `sendCtr`, timed under
`LinkTimingRoute.socket`); a peerless line goes straight to the mailbox
with no ten-second wait, and a closed socket, a departing peer, a failed
send or no answer
within `RelayChannel.socketDeadline` (10 s) falls the **same** request
through to the mailbox `send` with a fresh counter, so nothing new can fail
in a way the mailbox could not already recover from; the per-mailbox
`inFlight` serialiser is untouched and socket requests queue through it.
Every frame the line carries is opened by `tookSocketFrame` under the shared
`recvCtr` — a `reply` or `err` routed by `re` to its waiter, a `push` to
`onPush` — and a `push` lands through the one `applyState` site
(`tookPush`: `departedAt == nil` and `knowsItIsAway` only, then `takeUsage`,
`publishWidgetSummary()`, `lastHeard`) with one `LinkTimingSample(route:
"ws", kind: "push")` whose one figure is how old the picture was on arrival
(the frame's `ts`); a socket that has just opened makes one check-in at
once (`socketStateChanged` → `poll(record, probeHome: false)`), which arms
it on the Mac and fetches the picture. The 8 s cadence is untouched
(`noteAttempt` still `8_000_000_000 : 4_000_000_000`) and keeps running as
the fallback probe. LINK TIMING lists the `ws` route between `away` and
`home` (`LinkTimingSummary.lines`), a `push` row drawing `age … on arrival`
instead of the legs; the profile's AWAY section gains a `socket` row
(`AwayState.socket`, a word — `connecting` / `open` — never an address or
a health record, absent when no line is wanted). Pinned by
`test_phone_remote.py`, `RelaySocketTests.swift` and `LinkTimingTests.swift`.

## A press is queued, not awaited

**Every press from the phone is written down and released, never awaited.** The receipt (`ReceiptLedger`,
`Receipts.swift`) is the queue entry: `PhoneClient.enqueue` writes it down
in `.queued` at the tap and returns at once, so the control is the person's
again the moment the press exists and Approve then Start on one card is two
quick taps. Nothing on the press path holds `writesInFlight`, awaits the
transport or calls `refreshAfterWrite()`; the snapshot the ordinary poll
delivers is what says a press landed (`receipts.settled(against:)`), so
the cadence rule below is untouched. **One sender**, `flushReceipts`,
kicked by a press and by the same `onLive` the outbox sweep rides (the
retry tick for a held-off press, at poll cadence, no new timer), takes
presses in creation order through `ReceiptLedger.nextSendable`: **a scope
is held only while its own earlier press is unresolved** — `.queued`,
`.sending` or `.sent` (`Receipt.unresolved`); a press the Mac `.accepted`
releases it, which is what lets Approve then Start chain before the
approval shows on the board — the scope being the session, else the card,
else the bare verb, resolved once in `PhoneClient.scopeKey` for `post` and
`enqueue` alike, while two subjects' presses may interleave, the mailbox
serialising them anyway. The sender marks a record `.sending`
(counting the attempt) and hands it to `post` under its original token, so
`command_token` is still minted in one place and the Mac's own ledger
dedupes a replay; `post` closes the record exactly as a replay always has,
so a queued press the Mac refuses is `.stuck` carrying the Mac's words. The
same subject, the same verb **and the same payload** while an earlier copy
is unresolved (`.queued`, `.sending`, `.sent`) is refused at once in the
phone's own words (`PhoneClient.queuedTwiceRefusal`, in
`OutboxStore.transportSentences` for `stillSendingRefusal`'s reason) — a
true double-tap carries identical `fields`; a different press on the same
subject and verb (a reply with other words, a verdict on a second prompt,
the tool then the model, ▲ then ▼, the autostart dial on then off) queues
behind the first, and only the two answer verbs are keyed on `question_id`
alone; a different verb on the same subject queues behind it too. **The app-open unlock is the face (25 Sep 2026):** `LockGate.unlock()` succeeding calls `RemoteAuth.grantForSession()`, so every `RemoteAuth.authorize()` below returns at once until the app locks again (`LockGate.lock()` → `RemoteAuth.reset()`, on every departure) — one Face ID per opening, none per action. An unlock that resolves after a `lock()` (the lock's `epoch` stepped meanwhile) or with the app in the background grants nothing and leaves the app locked. Un-pairing inside an opening resets the grant, so the new pairing's first away write asks once. The Mac's checks are unchanged: device token, away lease and `REMOTE_ACTIONS`, per request. The rest of this paragraph is the fail-closed path for a write made without that unlock. **Face ID is asked once, at the press**, before the record
exists — a declined sheet leaves nothing queued — and remembered on it
(`Receipt.authorised`), which the sender passes to `post(authorised:)` so a
queue draining from away never raises the sheet again; a press queued at
home that falls back to the relay still asks, by design. **The pass covers
that press, for `replayWindow`, and RETRY asks again**: a record the sender
takes with its pass older than the window — held off that long, or read
back after a relaunch — is closed `.stuck` on `faceNeededLine` rather than
sent on a stale check (fails closed; the sender itself never raises the
sheet), and `ReceiptLedger.retry` drops the pass with the token, so a
re-send made from away goes back to the sheet as a first press does — a
pass carried across RETRY would let one Face ID cover a press re-made
hours later. The note a screen draws (`queueNote(for:)`) is bounded the
same way: a refusal older than `refusedShown` is not the note, and a
`.stuck` record wearing a `phoneAuthored` sentence never is — the QUEUE
list is where those live with their RETRY — so a Start refused hours ago
cannot re-arm "Start unplanned?" on a card that has a plan by now, and an
answer that stalled is not the box's note for ever. On the agent screen
the session's one note has two readers, split by verb (`QueueNote.isAnswer`):
the answer box takes an answer's or a reply's refusal, the screen every
other verb's, and only the reader that draws it reads it. The pressed control
wears the queue's mark instead of going grey — `QUEUED`, `SENDING…`, `SENT`
(`.accepted` draws `SENT`: the Mac said yes, the effect has not shown yet)
from `PhoneClient.queueMark(for:)`, spoken as the word drawn
(`Receipt.spoken(mark:)`: "Queued", never "Sending" for a control drawn
QUEUED) — and a refusal appears on the row in the Mac's words from
`queueNote(for:)`, the subject's newest refusal **only while it is the
subject's newest receipt in any state**: a fresh press queued behind it, or
a later press the Mac accepted or that landed, supersedes it for good.
Every screen that draws the note reads it **on appearance as well as on
change** (a refusal that lands while the screen is not up never fires
`.onChange`); the card screen arms Start off that note for the plan gate's
two confirmations, once per receipt, in `.onAppear` too. **Drawn is read**:
every screen that draws a queue note calls `readQueueNote(for:)`, which
moves a `.stuck` record wearing the Mac's words to `.refused` — REFUSED on
the QUEUE list for `refusedShown`, then aged and capped like any finished
business, never a row for ever — and **keeps** it, so it stays the
subject's newest receipt and an older refusal on the same subject can never
become the note under a confirmation still armed; a `.stuck` record wearing
a `phoneAuthored` sentence is left alone (nobody refused it; RETRY is what
it offers). **The answer hold rides the receipt**: an answer
is judged by `.questionGone` (`effect(for:)`), so the 200 leaves the
record `.accepted` and the mark `SENT` until the snapshot shows the
question gone, and `ReceiptLedger.duplicate` counts an `.accepted` copy of
those verbs (`heldWhileLanding`) — a second tap is turned away, never a
second keystroke burst. A **reply** on a waiting row is upgraded by
`enqueue` against the snapshot to `.replyHold`, the same evidence with one
difference: it carries the person's words, so it is never dropped unsent as
goal-achieved when the row leaves `waiting` before the sender takes it, and
a row that keeps waiting on the same question (blocked on a permission, a
channel message not yet consumed) closes it `.done` after
`ReceiptLedger.replyHold` (10 s, about the away poll cadence) rather than
`.stuck` at the effect deadline. **The pre-send drop is only for a goal
already reached**: `evidenceBeforeSending` never drops `.cardRevision`,
`.doneScopeChanged` or `.replyHold` (each carries a payload), and the
judge must be one the offering snapshot cannot already satisfy — Approve
carries the digest it asks for (`.planApproved(cardId:digest:)`, landed
when `plan_approved` **equals** it, so a *changed* plan's stale approval
is not "already landed"), and a verb offered on a row already out of the
live buckets — Delete on an abandoned row (`delete_agent` maps to
`.rowGoneAnywhere`), a Hide or Close on a finished one (upgraded by
`enqueue`) — is judged by the row leaving **every** list, not the live
three. The card screen's one-field toggles and Refine are judged by the card too, never `.none`: the assistant by `.cardTool(cardId:tool:)` (the card names the tool asked for), the model by `.cardModel`, Refine by `.cardRefining` (a `refine_state`, a `refine_session_id`, a `plan_path` or the card out of Prep). Settling on the 200 alone put the switcher back to the old tile and the button back to "Refine" until the next board frame — from away, a minute — and the press read as "nothing happened"; the pressed control now wears the mark (the assistant caption draws it beside its name) until the frame agrees, and **the tapped value is the one drawn for that whole stretch**: `pendingTool` / `pendingModel` (the `pendingStartWhenPlanned` idiom — view state, set on the tap, cleared when the mark leaves, on a refusal's note, on a refused enqueue and when another card comes behind the view) select the tile the person chose and name the model they picked, while the row dims (`PhoneProviderSwitch` at 0.45 opacity — `.disabled` alone draws a plain tile no differently) and the model menu is held. A dimmed row still on the Mac's old tile read as a tap that missed and was tapped again. `settling`, `settlingAnswers`, `settlingCards` and
`settlingPreferences` are **row-leaving visuals only** (a confirmed Stop or
Delete the Mac accepted; the pressed dial value held until a board reports
it, its backstop counting from the moment the press leaves the queue, and
ended at once with no notice of the phone's own when the Mac refuses the
press — `queueNote` outranks `pipelineNotices`), never a lock on a control.
**A Delete pops its screen when the row or card is gone from the snapshot,
or when the mark leaves with nothing said** — never on the queue's own
`ok`, which means written down: a refusal from the Mac's re-check needs a
screen to land on. The sender walks past a subject whose synchronous press
holds `post`'s lock (`nextSendable(holding:)`) rather than spending an
attempt and a backoff on a transmit that cannot start. **A poll that goes
through ends every `.sent` press's backoff** (`releaseHolds()`, before
`settled(against:)` on both live paths): the hold was written by a
transport failure, the poll proves the transport back, and the next sweep
re-takes the press at poll cadence under the same attempt ceiling — a fresh
`.queued` press behind it on the same scope no longer waits up to
`backoffCap` for trouble that is over. A Face ID sheet
declined on a **queued or replayed** press — one queued at home and
delivered by the relay fallback, or a `.sent` press re-taken outside the
grace — closes it `.stuck` on `ReceiptLedger.faceNeededLine` (a
`phoneAuthored` sentence, drawn STUCK) so it stays a row with RETRY; a
first press that declines leaves nothing, as before. The Profile screen's
`QUEUE (n)` section is `ReceiptLedger.queueRows`, **oldest first** so the
order reads, each row led by `Receipt.statusWord` (`QUEUED` / `SENDING…` /
`SENT` / `LANDING` / `DONE` / `REFUSED` / `STUCK` — a `.stuck` record
wearing one of the phone's own `phoneAuthored` sentences), with RETRY (a
no-op on a `.queued` or `.sending` row: a fresh token mid-transmit would
send the press twice) and DISCARD (off while the row is `.sending`) as
before; a refusal the person read stays listed for `refusedShown` (600 s).
**Five presses keep the synchronous `post`**
because the screen reads their reply body: the guarded card Save
(`expected_revision`, whose 409 carries the Mac's copy), card creation
(`OutboxStore`, its own queue), Clear done (count and token off the
reply) and START PROJECT (`board_start_project`, whose 200 `detail` is the
Mac's report of what started and what was left alone, drawn verbatim)
and START n TOGETHER (`board_start_batch`, whose 200 `detail` is the
Mac's report of what started, what waits and what was skipped; a batch
Start banked and fired later would put work in front of the launcher on
the phone's clock, not the person's);
terminal keystrokes stay on `post` as a stream with their own batching. Pinned by `host/tests/test_phone_action_queue.py` (the four pure
functions under `swiftc`, the wiring by grep) and `OfflineCacheTests.swift`.

## The poll cadence is 4s at home and 8s through the mailbox

**The poll cadence is 4s at home and 8s through the mailbox, and the
screen observes it rather than driving it.** `PhoneClient.noteAttempt()`
is the one site that computes that pause: it returns the `UInt64` the
loop sleeps **and** stamps `link` (`LinkAttempt` — consecutive failures,
the instant the sleep ends, its length in seconds, and the route from the
same `via` read), so the countdown drawn and the sleep taken are the same
number by construction. `ReconnectBar` (`BrandBar.swift`, beside
`StaleBanner`) draws that under the offline sentence — which is the Mac's
or `Trouble`'s own words, verbatim, never composed there — as
`RECONNECTING… attempt n · retry in Ns · <route>` with one block cell per
real second. It holds no `@State`, no clock and no `via` of its own; the
AWAY badge stays the view layer's only `via` read. `ConnectingView` swaps
its spinner for the same bar once `link.failures > 0`, so a phone that
never connects does not read as frozen. `stop()` resets the note, and
`BackgroundRefresh` — which never enters the loop — gains nothing.
While a check-in runs, `PhoneClient.phase` says which rung it is on
(`PhoneClient.Phase` strings, stamped by `notePhase` alone, cleared
when `poll` ends; `RelayChannel.onSent` marks the moment the mailbox
took the frame); `ConnectingView` and `ReconnectBar` draw it and
compose nothing, and it replaces the countdown only while an attempt
is in flight. Pinned by `host/tests/test_phone_reconnect_display.py`;
**no backoff, no jitter, no re-timing**.

## The home-screen tile keeps moving with the app closed

**The home-screen tile keeps moving with the app closed, and the widget
still does no network.** The same widget also fills the Lock Screen's
circular slot. `ios/BobPhone/BackgroundRefresh.swift` registers
a `BGAppRefreshTask` (`$(DARK_ARMY_BUNDLE_ID).refresh`, permitted in
`Info.plist` beside `UIBackgroundModes: fetch`) before launch finishes,
books it on every `.background` and again at the top of each run. A run
is a fresh `PhoneClient.backgroundRefresh(record:budget:)` — one bounded
check-in under `budgetSeconds` (24, inside iOS's ~30), cancelled at
expiry, going the way the last poll went (`lastKnownAwayKey`, written
from `via`'s `didSet`): away is relay first then one home probe, home
is two probes then the relay. It wires `onPromote` / counters and
**never `onLive`**, so the outbox — and its Face ID ask — stays shut.

**The tile and Live Activity use Signal's generated palette.** The widget
target compiles `ios/BobPhone/SignalTokens.generated.swift` directly, with
`WidgetTheme` mapping its surface, readable text, muted text, green accent,
amber attention, red danger and divider roles. Home Screen counts and the
Lock Screen card use neutral text for ordinary values, amber for Needs you and
staleness, and red only for a critical usage meter. Agent words and captions
use proportional type; prompts, clocks and measures keep monospace. The
Dynamic Island uses the same roles. The circular accessory keeps its system
background and primary material because vibrant mode can flatten colors; its
tick weight, length and caption still carry meaning by form.

Profile → Knowledge is a read-only list of one enrolled project's notes,
gated on `knowledgeSupported`, with a picker over `enrollment.enrolled`
only. `.navigationTitle("knowledge")` is a literal; answers wrap, with no
`.lineLimit(`. Confirm / Edit / Mark stale are absent. Pinned by
`test_phone_knowledge.py` and `test_phone_text_in_full.py`.

The floor is `BackgroundRefresh.minutes` (15/30/60, Profile → WIDGET),
iOS may stretch it, and the summary carries `dimAfter`
(`max(600, floor + 300)`) so the tile dims to the schedule rather than
at ten; `FleetSummary` decodes it tolerantly. The pairing item moved to
`kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly` — readable with the
screen off — by `SecItemUpdate` in `PairingStore.load()`, never a
delete-and-add. Pinned in `test_phone_background_refresh.py`.

**The tile stays inside WidgetKit's daily reload budget** (21 Sep 2026:
it sat on yesterday's numbers under "1 day" while the app checked in all
day — WidgetKit honours 40–70 reloads a day and *defers* the rest until
the window rolls, and every background run and every departure had been
asking for one unconditionally). The foreground's reloads are exempt and
keep the 60 s / 540 s throttle. A reload with nobody watching — a
`BGAppRefreshTask` run (`backgroundRun`) or a publish or flush after
`.background` (`departedAt != nil`) — is *counted*: it is asked for only
when it would change the tile (`WidgetReloadBudget.worthIt`: the figures
moved since the tile last drew them, or the note the tile holds is past
its own `dimAfter`) and only while the persisted ledger has room
(`WidgetReloadBudget.allows`: three per two hours, 36 a day, stamps in
`UserDefaults` under `stampsKey`). Every reload of any kind records the
note it drew and when (`lastReloadAtKey`, `lastReloadNoteKey`) so a
background run's fresh client reads what the tile holds instead of
reloading blind. The widget's dim entry sits at the note's own
`generatedAt + dimAfter` (never before the next second; a note already
past it draws dim from the first entry), so the departure flush is no
longer what keeps the dim clock honest. Foundation-only, run under
`swiftc` and grep-pinned by `test_phone_widget_reload_budget.py`.

**A state read may carry the usage bars** (21 Sep 2026): the phone sends
`with_usage` on both `state` reads and the Mac folds `_usage_report_for`'s
body in under a sibling `usage` key — on the full picture and the
`unchanged` line alike, after `_state_digest`, so moving bars never change
the digest. `takeUsage(from:)` applies them as the `usage` read does; the
separate leg runs only when nothing rode (an older Mac). **A press closes at
the number the Mac names**: `accept(_:revision:)` lowers a `.cardRevision`
effect to the reply's `revision` when it is below the expected step (a save
with nothing to change), and `refuse` closes `.done` on `inbox_ack.py`'s
two "already gone" sentences (`goalMetRefusals`). **A scout's report is a
document on the card screen**: the on-open `card` read carries `report`
beside `plan` (`_card_report`) and `reportSection` draws it.

## The waiting agent is a Live Activity

**The one agent at the top of Needs you is a Live Activity** — a card on
the Lock Screen and, on phones that have one, in the Dynamic Island: the
portrait, the nickname, the kind word (`permission` / `question` /
`attention`), a clock counting from when the row went quiet and the card or
session it is working (`ios/BobPhoneWidget/NeedsYouActivityViews.swift`,
one more `ActivityConfiguration` in the widget bundle; `ios/Shared/
NeedsYouActivity.swift` is the `ActivityAttributes` compiled into the app,
the appex and the widget tests). **The subject is the head of
`decisionItems` restricted to session entries of the three kinds the card
can draw a face for** (`NeedsYouActivityRule.subject`) — card entries carry
no face and are skipped — with `slug` from `Cast.character(for:)`, `work`
on the Mac's `_compose_push_work` rule (the bound card's title, else the
name, never `New session`, clamped to 80 — the "unnamed" judgment is the
daemon's, published on the row as `unnamed: true` exactly where the name
is the placeholder and absent otherwise, so the phone reads a flag and
never the string, and an older daemon's row decodes `false`) and `since` = the row's own
`quiet_since` — the daemon's one stamp for the moment the row went quiet,
the number its push carries too, so the two ends agree to the second;
`generatedAt - idleSeconds` stands in against an older Mac (`Agent.
quietSince` decodes 0), and it is the inbox's `since` as well.
`plan(current: subject:)` is the whole decision: nothing → something
starts, changed updates, empty ends, equal (every key but a `since` drift
under 2 s) is no step. **`current` is what is standing, not what was
remembered** — `standing(remembered:activityState:listed:)` reads
ActivityKit's own word first, and an activity ended out from under the
controller (the Mac's `end` landing while the app is alive in the
background, a swipe off the Lock Screen, the eight-hour cap) is nothing
up, so the next agent is a `.start`, never an `.update` of a card nobody
can see; the controller settles this before every plan and on adoption,
and watches `activityStateUpdates` between them. **The two ends rank a tie
the same way**: inside a kind the phone's `PhoneInbox.before` orders by
title (`localizedStandardCompare`), then the target key, never by wait,
and `live_activity.subject` sorts by kind, `title_key` (nickname, else
name, else session id), then `session_id` — so the card the phone starts
for the head of its list is the card the Mac's first update names
(`test_the_ranking_agrees_with_the_phones_kind_order`). **Admission is
the phone's rule on both ends**: `waiting`, an open prompt or a published
notification card (`notifyIds`; the Mac hands the composer
`_notification_snapshot()`'s session ids, never `_active_notifications`'
raw keys, so a card the hysteresis window still withholds admits nothing
on either end) admits a row, a question decides only the
kind, and the one-entry rule holds — a `needs_you` or `manual_check_due`
card bound by `session_id` to a row that merely stopped takes that row's
entry on both ends (`live_activity.carded_sessions`), so neither end
starts a card the other would end
(`test_a_notification_card_admits_a_row_the_phone_lists`,
`test_a_card_bound_to_a_stopped_session_takes_its_entry_the_phones_one_entry_rule`).
The one accepted drift is that the phone removes acknowledged items and
the Mac composer does not read the acks (`_inbox_acks`), so a pushed
update may re-show a subject the phone had hidden until the next local
reconcile corrects it — in practice `_settle_acknowledged_session` demotes
an acked session out of the buckets, and the residual is a question ack on
a row still in `waiting`. The buzz gate, unlike the composer, does read
the acks (`live_activity.shown_sessions`; `docs/transport-contract.md`,
*A buzz names only what the phone lists*), so a reminder about a dismissed
row is withheld rather than pushed and swept.

**The app starts, adopts or ends it; the Mac only updates and ends it.**
`LiveActivityController` (`ios/BobPhone/LiveActivity.swift`, main actor) is
reconciled from `applyState` on every applied foreground snapshot, requests
only while the app is `.active` (`Activity.request` fails in the
background), adopts an existing activity on `.active` rather than minting a
second (`adoptExisting`, which adopts nothing ActivityKit reports ended
or dismissed; extras are ended — **two activities never**), and `endAll`s
on forget, before `stop()` tears the record down. An external end
unregisters the dead token (an empty token, `unregisterIfSent`), which is
what lets the Mac forget the activity and start clean on the next
registration. Each activity's
ActivityKit **update** token rides `PhoneClient.registerActivityToken` —
`registerPush`'s twin, `quietPost`, never a Face ID prompt — under the
sealed verb `register_activity_token`, **only where the Mac states
`live_activity_supported`** on `board`; against an older Mac the card still
starts and ends locally and no token is ever posted. An unchanged token is
not re-sent (`activity.sentToken`); an end posts an empty token. No
push-to-start, no buttons (Allow / Deny / Acknowledge stay on the banner),
no preference: the card follows `phone_push` and `remote_access`, the
buzz's own gates. `stale-date` is `NeedsYouAttributes.staleAfter` (30 min):
a Mac gone quiet dims the card and replaces the clock with a dash; iOS ends
it at eight hours. The wire is `docs/transport-contract.md`, *The buzz has
a live-card leg*. Pinned by `LiveActivityRuleTests`,
`NeedsYouActivityStateTests`, `test_phone_widget.py` and
`test_live_activity.py`.

**The card is the fleet, and the waiter sits on top.** The card is up while
`working + attention > 0` (`NeedsYouActivityRule.isUp`, the strip's own
counts, read on the phone as `snapshot.counts` — the home-screen tile's
own numbers, never re-derived) and ends when that sum is 0, so a fleet of
only standing-by agents leaves the Lock Screen clean. The counts, the cost
and the tokens are `fleetState`: `working`, `needsYou` (`counts.attention`)
and `standingBy` (`counts.idle`), plus `snapshot.fleetFigures` read
verbatim (`costUsd`, `tokensK`, and the last hour's `costUsdHour` /
`tokensKHour` — the Mac composed them in `fleet_figures`,
and the phone sums nothing). The top waiter's face and kind word, the same
`subject`, are drawn above that summary while `sessionId` is non-empty and
are simply absent when it is not. `needsYou` can read 0 under a permission
face: the count is the waiting bucket and the face is the inbox's head,
and both clients already draw that pair. The Mac states
`live_activity_fleet` on the board; absent decodes false and `fleetState`
is today's `subject`, so a new phone against an older Mac keeps the
face-only card. The phone posts `shape` 2 with its activity token (the
remembered key is `activity.sentToken.v2`, so an upgrade re-registers
once); an older phone never says, the Mac stores shape 1 and keeps sending
the face-only card, ending it when nobody waits. A mailbox that drops the
five fields decodes them absent, and absent is not zero: a missing count
draws a dash, a missing cost draws "cost unknown". The cost text is
`.privacySensitive()`, so the person's Lock Screen privacy setting can
hide it; the counts are not.

## The phone honours the text size you chose, with no ceiling

**The phone honours the text size you chose, with no ceiling, and every
row speaks as one sentence.** `Theme.mono` is the app's **one font seam**
and the only `.system(size:` in its own views: it scales the point size a
call site asks for through `UIFontMetrics` off the nearest text style, so
every screen tracks the system setting at once. There is **no cap** — no
`maximumPointSize`, and **no view may call the `.dynamicTypeSize(_:)`
modifier**, because clamping a subtree is that ceiling by another name.
Layouts reflow instead: ten files read `@Environment(\.dynamicTypeSize)`
and fork on `isAccessibilitySize` (`Theme`, `ProcessTable`, `UsageView`,
`PipelineView`, `RecentlyView`, `BrandBar`, `FleetView`, `BoardView`,
`AgentDetailView`, `PhoneSheetHost`),
the fleet row trading its five columns for labelled stacked lines.
`AdaptiveStack` is the shared `HStack`-becomes-`VStack`; the threshold is
always **read**, never measured off a `GeometryReader` or `UIScreen`.
VoiceOver side: every icon-only control carries a label or is hidden,
`PipelineView.controlButton` takes a **required** `label:` so ▲/▼/× are
never silent, an arm-then-confirm button says which step it is on, and
`PhoneProcessRow` / `PhoneBoardCard` / `RecentlyRow` / `UsageWindowRow`
are each one element with a composed `spoken` — assembled **only** from
the fields that row already draws, with the Mac's own sentences
(`queue_reason`, `dispatch_error`) verbatim. Decoration is silent: the
pixel faces, the meter and the provider silhouettes are
`.accessibilityHidden(true)`. **Nothing announces itself** —
`UIAccessibility.post` was considered and rejected as noisy. The assistant
switcher (`PhoneProviderSwitch`, on the card screen and in the composer, the
menus it replaced gone) is a contained group named Assistant whose value is
the chosen name or "not chosen" ("Sending" while a pick is on its way); each
tile is a button by name carrying `.isSelected` when chosen, its silhouette
hidden; the rule is `ProviderChoice`, byte-pinned to the panel's by
`host/tests/test_provider_switcher.py`. The whole
contract is `host/tests/test_phone_accessibility.py`.
`ios/BobPhone/Markdown.swift` and `Specialists.swift` are **untouched**:
both are byte-pinned to the panel and every phone call path through them
already routes through `Theme.mono`. `panel/Sources/BobPanel/Theme.swift`
keeps its fixed sizes — the Mac has no Dynamic Type — and
`ios/BobPhoneWidget/` is out of scope of Dynamic Type, drawn at fixed
system sizes with its own `WidgetTheme`. The same widget serves the
home screen and the Lock Screen's circular slot; the Lock Screen half
is drawn in vibrant mode where form carries every distinction, and the
circular view deliberately omits `.privacySensitive()`.

## The Fleet tab names the Mac's battery

`power_source.py` reads `pmset -g batt` on the snapshot executor, memoised
`POWER_SOURCE_INTERVAL` (60s), and publishes the omittable `power` section:
`available`, `present`, a whole `percent`, `source` (`ac` / `battery`) and
`charge` (`charging`, `discharging`, `charged`, `not_charging`,
`finishing`). **No clock rides it** — the time estimate is dropped, so the
section is news only when the battery moved. `MacPower.line`
(`FleetView.swift`) owns the words — charging, on battery, charged, on
power, on power, not charging, finishing charge — drawn as one label
between the refresh notice and **Catch up across projects**, spoken the
same. A desktop (`present` false) or an older Mac draws nothing; the panel
does not decode it. Pinned by `test_power_source.py` and
`test_phone_mac_power.py`.

## Deliberate interactions have finite decrypt decoration

`DecryptMotion` and `DecryptFeedback` play one short public caption, and only
when a screen arrives. OPEN runs on completed visible navigation and on a tab
arrival (1.5s), then the caption goes and the photograph holds still. INPUT on
a button and REFRESH on a pull are gone: an enabled press keeps the native dim
and fires at once, and a pull keeps the system's own spinner. The arrival
caption is drawn only while that play is on. A tab root reserves the strip
above its content, always laid out so nothing jumps when a play starts or
ends; a sheet draws the caption in its header row beside the title, the
header's rule lit while it plays, and reserves nothing extra — its content's
surface publishes itself to the frame's `DecryptCaptionHost` and mounts no
strip. Actions never await decoration. Polling,
reconnects, cache changes, receipts and terminal bytes stay silent. One screen
episode at a time, superseded rather than queued.

The clock uses monotonic time, at most 20 glyph updates per second, at most 24
ASCII glyphs, and stops at its deadline after a 0.2s final-caption hold. Semantic
labels and errors stay intact. The caption is Theme.mono, accessibility-hidden
and cannot intercept hits. Reduce Motion shows the finished caption with no
scramble, then hides it at the deadline. Inactive tabs cannot
claim a surface; completed UIKit appearances distinguish navigation from mounting.
Inactivity, lock, unpair and disappearance cancel decoration without changing
terminal or draft lifetime. Native menus, pickers, camera, biometrics and keyboard
keep platform rendering. Evidence and the remaining physical-device checks are in
`docs/mobile-decrypt-validation.md`.

### Collaboration summary

Saved-card and agent Details embed the same version-1 evidence semantics and
focus rules as the Mac (`Collaboration.swift` is byte-pinned across clients).
Five connections are shown initially, with a local disclosure for the rest.
Parent relationships and observed SendMessage attempts have separate labels;
counts remain session-level, never delivery receipts or card outcome totals.
Live-helper, retained-ended, ambiguous, unresolved and present-without-inbox
states remain distinct. State labels describe the stored picture's observation;
the existing home/away/offline freshness presentation remains authoritative.

Navigation rechecks exact current provider/session and card ID/root, uses the
existing PhoneSheetRouter and preserves its bounded Back trail. Helpers open
their owner. Missing Done cards say unavailable in this view. Unknown schemas
or missing sections show unavailable coverage; malformed rows mark partial
coverage. No map controls post actions, create a new client, or add a timer.
Long addresses are inert text and wrap at accessibility sizes. The synthetic
native screen-check entry points are in `docs/card-crew.md`.
