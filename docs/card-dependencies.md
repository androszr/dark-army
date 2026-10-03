# Card dependencies — a card that waits on other cards

The long-form contract for `blocked_by` since 24 Sep 2026
(`plans/2026-09-24-card-dependencies.md`). No role loads this document;
`docs/context-board.md` points here from the queue gate's paragraph and
`CLAUDE.md` lists it beside the board's other long-form contracts.

A card may name the cards in its own project that must finish before it.
Pressing Start on a card whose dependencies are not finished **queues** it
with a sentence naming them, and Dark Army starts it by itself once each of
them is done or finished and waiting only on a person's manual check. Nothing
is ever started that nobody pressed Start on.

## The column and who writes it

- **Storage is the old column, revived.** `cards.blocked_by` (v5): card ids
  joined by newlines, read by `board.parse_ids` (de-duped, capped at
  `MAX_BLOCKERS` = 8) and written by `board.join_ids`. It is in `_WRITABLE`,
  in `REVISED_COLUMNS` (so a link edit steps the card's `revision`) and, again,
  in `ApiServer._BOARD_FIELDS`: a thing a person states about their own card.
  `create` and `_update_locked` both judge it through `_blocked_by_refusal`.
- **Four refusals, at the store, for every writer** (`_update_locked`'s
  `blocked_by` branch): an id longer than `MAX_CARD_ID_CHARS` (64); a card
  cannot wait on itself; a list that closes a
  loop is refused ("those cards already wait on each other", `_creates_cycle`);
  and a card can only wait on a card in its own project — both roots through
  `dispatch.normalise_root`. An id that names no card is **not** refused: it can
  hold nothing, and a restore should still mean something.
- **Writers.** A person on the Mac card window and the phone card screen
  (`board_update` with `expected_revision`, the stale-copy guard); a plan's
  `Depends on:` header at attach; `dark_army_add_card`'s `depends_on` for a card
  filed without a plan; the linker below. There is no new action on either
  phone tuple: `board_update` already rides `LAN_ACTIONS` and `REMOTE_ACTIONS`.
- **The v29 wipe.** Values left by a build from before 20 Sep 2026, when the
  column gated nothing, are emptied once on the upgrade to schema 29
  (`BoardStore._clear_retired_blocked_by`, `_retire_initiatives`' shape, run
  before the version marker is written so a failed wipe is retried): no
  column moves, a v28 build opening the file after a downgrade reads and
  ignores the column exactly as before, and the forward-only marker means a
  later open never touches a list a person wrote after the upgrade.

## What "met" means — one resolver

`board_queue.dependency_met(dep, working)`: a dependency is met when it names
**no card** (deleted, or cleared from Done — met and not drawn), when it is in
**Done** (whatever its review state), or when **the run that flagged a manual
check is still its run and has stopped**: `manual_steps` set, the card still In
progress, not `dispatching`, bound to the session that flagged it
(`manual_session_id`, v29, written by `flag_manual` alone and emptied by
`clear_manual`; `''` on an older flag keeps the other tests alone), and
`daemon_board._card_session_working` false — the MANUAL CHECK badge's own
predicate. The narrowings exist because `manual_steps` outlives its run: a card
reset to Backlog after a failed check, or re-started for rework on a new
session, is not finished. Before the first agents snapshot after a restart
(`_dependency_running_ids` answers None) a bound session counts as working, so
a press in that window waits.

`daemon_board._dependency_entries` is the one resolver, called by the gate, the
drain and the snapshot decoration with the same `running_ids` / `active` pair;
`_unmet_dependencies` filters it. An id outside the snapshot's frame (a card
finished a week ago is not in the Done preview) is looked up with **one**
`BoardStore.cards_by_id` read per frame, never a read per card.

## The gate queues; it never refuses

In `_dispatch_card_locked`, after `dispatch.guard` and before the slot rule
(`_slot_refusal`): an unmet list sends a **person's press** to `_enqueue_card`
with the unmet cards, which writes `queue_state='queued'` and answers with the
dependency sentence; the drain's **replay** (`queued_replay=True`) returns
`(False, "")` before any store call and never re-stamps `queued_at`. The
human-gesture invariant holds: a card is only `queued` because a person pressed
Start (or dropped it into In progress, or `start_project` /
`_auto_start_after_refine` replayed one press' worth), and the drain re-runs the
plan gate, the enrolment refusal, `dispatch.guard` and the slot rule under
`_dispatch_lock` at the instant it fires. A dependency becoming met changes
nothing about a card nobody pressed.

**A dependency-held card is not a barrier.** `_slot_refusal`'s "queued ahead"
rung and `_decide_queue_dispatches`' head selection both pass the queued cards
through `board_queue.eligible`, dropping the held ones
(`_dependency_held_ids`), so A — waiting on B and queued first — never holds B:
the 23 Aug deadlock argument one rung on. Among eligible cards the order is
`queue_key`, unchanged.

**Limits stand.** `MAX_QUEUED_PER_PROJECT` (8) is refused at the store: the
ninth press answers "`<project>` already has 8 cards queued" and the card keeps
no `queue_state`. `start_project` queues a held card and counts it as queued.

## Three sentences, composed once

Module-level in `host/dark_army_daemon/daemon.py`, drawn verbatim by both
clients:

- `_dependency_reason(titles, autostart)` — `Queued — Dark Army will start it
  once "X" is done` / `… once "X" and "Y" are done`; `Queued — press Start once
  "X" is done` with the drain off. It rides `queue_reason` on a held queued card
  (decoration) and is the press's reply; it shares no prefix with the plan gate
  or the stale-copy refusal and equals none of `_queue_reason`'s strings.
- `_dependency_line(entries)` — `Waits on: "X" (done) · "Y" (check pending) ·
  "Z" (not yet)`.
- `_dependents_line(titles)` — `Unblocks: "A" · "B"`.

The snapshot publishes, per card, `dependencies` (`[{id, title, column_name,
met}]`), `dependents` (`[{id, title}]`, Done cards left out), `dependency_line`
and `dependents_line` — **all four absent where empty**, `work_record`'s rule.
A met flip is a column write or a `manual_check_due` flip, both already news
(`_manual_due_drifted`), so nothing joins `_CLOCK_FIELDS`.
`dependencies_supported` on `_pipeline_writable()` is the phone's marker.

## The two clients

- **Mac.** `BoardCard` decodes `blocked_by`, `dependencies`, `dependents` and
  the two lines tolerantly (`panel/Sources/BobPanel/BoardModels.swift`); the
  tile draws both lines under the queued line (`BoardCardView.swift`); the card
  window's `WAITS ON` section (`CardSections.Section.dependencies`, pinned
  beside QUEUED in every stage's order) lists each dependency with its word and
  a ✕, the Unblocks line and an **Add…** menu of the same project's other cards
  not in Done — writing the whole list through `board_update` at the draft's
  revision (`BoardCardSheet.swift`, `setDependencies`). What Add… offers is
  `BoardCard.dependencyChoices(for:in:)`, and every write sends `linkedIds` —
  the resolved dependencies — so a deleted card's leftover id neither counts
  against the eight nor survives the next edit.
- **Phone.** The same five fields and `Board.dependenciesSupported`
  (`ios/BobPhone/Models.swift`); both lines on the tile and in its spoken
  sentence (`BoardView.swift`); `WAITS ON` rows with ✕ and the Add picker under
  EDIT CARD (`CardDetailView.swift`), drawn only where `dependencies_supported`,
  written through `PhoneActions.boardUpdate` with `expected_revision`.
  `PipelineView.swift` draws `queue_reason` verbatim already.

## Filing: the plan header and the tool argument

- **`- **Depends on:**`** in the plan template (all three copies, byte-equal
  lines), read at attach by `board_workflow.read_plan_depends_on` inside
  `_seed_from_plan` — both attach routes — and resolved against the card's own
  project by `_resolve_dependency_refs`. Separated by ` | ` and nothing else
  (titles hold commas); a placeholder or `none` is no list. Written by
  `BoardStore.fill_dependencies_if_empty` into an empty list only, through
  `_update_locked`, so the three refusals apply and a person's links win. A
  reference that is missing or ambiguous seeds nothing and is logged.
- **`depends_on`** on `dark_army_add_card` (`channel_server.CARD_TOOL`: array,
  `maxItems` 8, items `maxLength` 200) is for a card filed **without** a plan.
  Each entry is a card id or an exact title (trimmed, case-insensitive, unique
  among the same project's cards, Done included). One bad entry refuses the
  whole list; the card is filed regardless and the reply carries
  `dependencies_detail`.

## Prepare suggests dependencies

Idea-mode Prepare may name the cards a new card should wait on; the answer
fills the composer's waits-on box and nothing reaches `board.db` until Create.

- **Candidates** are built per press by `BobDaemon._dependency_candidates`
  off `_board_state["cards"]`: same project (`dispatch.normalise_root`), not
  Done, board order, titles collapsed and clipped to
  `MAX_DEPENDENCY_TITLE_CHARS` (120) before they are offered and matched, at
  most `MAX_DEPENDENCY_CHOICES` (40). A title shared by two cards
  (case-insensitive, after the clip) and a title that reads as a section label
  (`card_prepare.candidate_ok`) are left out. The phone's offline Prepare
  builds the same list with `CardPrepareRules.candidates(from:root:)`.
- **The block** (`_dependencies_block`) rides only in idea mode, only with at
  least one candidate, after the folder block and before `IDEA:`; the brief and
  `_areas_block()` are untouched and `AREA:` stays last. The legacy press and
  an idea press without candidates are byte-identical to before.
- **The reader** (`parse_dependencies(raw, candidates)`) takes the last
  `DEPENDS ON:` label (heading and inline forms, like `AREA`), reads lines to
  the first blank line or label, and matches each cleaned line to an offered
  title (case-insensitive) or an offered id. `NONE`, no section, unknown lines
  give nothing; de-duplicated, at most `MAX_BLOCKERS`. Only ids Dark Army
  supplied come back; never a refusal of the press.
- **The reply key** is `suggested_dependencies`, a list of ids, idea mode
  only, `[]` on a refusal and from an older daemon (both mean no opinion).
- **The apply rule** is `DependencySuggestion.decide(offer:current:listed:)`
  in `Areas.swift` (byte-equal on both clients): nil unless the box is empty,
  the offer filtered to the project's cards still listed and not Done, joined
  by newlines. A project-picker move clears the box. The Mac's box is
  `DependencyEditor`, also the saved card's WAITS ON control; the phone draws
  its box only where `dependencies_supported`.
- **Create carries the link**: `boardCreate(blockedBy:)` and the phone's
  `fields["blocked_by"]` send it when non-empty; `BoardStore.create` runs
  `_blocked_by_refusal`, the reading `_update_locked` shares, and writes the
  column.
- **Parity**: `test_phone_prepare_parity.py` compiles the phone's twin and
  compares the prompt, the reader and the candidate list with the Mac's.

## The linker

`tools/board_link_dependencies.py` links a project's existing cards from a list
of titles (`tools/dependency-maps/ai-viber.txt` ships). Stdlib only, run by a
person from a shell — never by Mission Control, whose grant has no token, and
never by an agent run against the live board. A dry run by default: it resolves
every title on the live board at that moment (plus the Done archive), prints
each link with the card's column and revision, and writes nothing; any missing
or ambiguous title refuses the run before a single write. `--base-url` must be loopback, no proxy is
consulted, and the reads carry no token. `--apply` sends one
`board_update` per card with `X-Bob-Token` and the revision the dry run read,
so a card changed in between is refused by the store. `--state-file` is the
offline seam `host/tests/test_board_link_dependencies.py` drives.
