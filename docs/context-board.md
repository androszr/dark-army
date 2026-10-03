# Board context — cards, dispatch, the queue and the store

Relocated verbatim from `CLAUDE.md` on 20 Sep 2026 (`docs/ship-efficiency.md`
holds the paragraph map). This is the subject document for **the Kanban
board end to end**: the two write paths, the parallel limit and the queue,
what a claim is, how a card reaches Done, the store's writer rings
(`board.py`), Dark Army as launcher (`dispatch.py`) and which project a session
belongs to (`workspace.py`). Loaded when the work touches
`host/dark_army_daemon/board*.py`, `daemon_board.py`, `dispatch.py`,
`card_*.py`, `workspace.py`, the panel's `Board*` / `Card*` files or the
phone's board — and always on the conservative fallback
(`docs/agent-context.json`). The panel's board *drawing* is in
`docs/context-panel.md`; the daemon's session model in `docs/context-host.md`.

## The board's data flow

**The board's two write paths**, both from a person:

- **Refine** (a Prep card) → `refine_card` → `dispatch.spawn_local` or
  `dispatch.spawn`, on the `board_own_terminal` preference alone (no per-press
  HERE on this verb) (the card's named assistant, `/ship <idea>`). The card *stays in Prep* under `refine_state` /
  `refine_session_id`, never `session_id` / `link_state`, until that session's
  `dark_army_attach_plan` → `attach_plan_by_session` → `BoardStore.attach_plan`: one
  WHERE-guarded UPDATE that writes `plan_path`, moves the card to Backlog and
  clears `refine_state`. **Exception:** when the notes' first line is
  `Plan: <path>.md` (`dispatch.named_plan`) and that file passes
  `_plan_path_refusal` for the card's root, Refine — after every guard —
  attaches it (`_attach_named_plan`) and launches nothing: a finished plan
  named only in the notes is attached, not re-planned (the fit-app stall of
  23 Sep 2026). An agent files such a card with `dark_army_add_card`'s `plan`.
- **Start** (armed, then confirmed) or a card **dropped into In progress** →
  `dispatch_card` → `dispatch.spawn` → `vscode_reveal.spawn_agent` → a new
  terminal in that project's window; an *unplanned* card is refused by
  `_plan_gate_refusal` unless the payload carries `skip_plan_gate`.
  `_reconcile_board(snapshot)` binds the session that appears and then tracks
  it, live → ended. `board_own_terminal` or a press's `own_terminal` selects
  `dispatch.spawn_local` and widens known roots to enrolled folders.
  `own_terminal_spawn_supported` draws START HERE absent on an older Mac
  (phone only).

**Scout cards.** A card's `kind` column is `''` (a build card; the word
`ship` is accepted on write and stored as `''`) or `scout`. A scout is an
investigation ending in a report, never code. Its Prep primary is START
because it has no plan to refine; `_plan_gate_refusal` returns `""` on a
scout as its first rung, so Start from Prep is not asked "this card has no
plan yet". `dispatch.start_prompt` returns `scout_prompt` (`/scout <idea>`,
the scout skill; `/ship scout` is its alias) before the `plan_path` branch. The running scout attaches its
report through `dark_army_attach_report` → `attach_report_by_session` →
`BoardStore.attach_report` (WHERE-guarded, the card stays In progress) and
closes with `dark_army_close_card`. `scout_prompt` leads with the summary, or
the title (minus `Scout:`) when the summary is under
`SCOUT_BRIEF_MIN_WORDS` words; the other rides once on a `Title:` /
`Brief:` line. **Promote** (`board_promote`, on every phone tuple since
21 Sep 2026, `promote_supported`) creates a Prep build card whose `prompt`
leads with `From report: <path>` and dispatches nothing; refused in words
without a report, outside Done, or twice. Both clients draw the report as
a `REPORT` section, the phone off the sealed read's `report`. **The report
has one home and one shape** (`.claude/skills/scout/references/scout.md`):
`scout/<YYYY-MM-DD>-<slug>/report.md` in the git-ignored `scout/` folder,
an answer block of `- **Key:** value` lines, then five headings, parsed and
checked by `scout_report` (`host/dark_army_daemon/scout_report.py`, byte
copies `.claude/skills/scout/scout_check.py` here and in the pack); the
attach refuses a report under `scout/` that fails the check
(`REPORT_MALFORMED_REFUSAL`) and stores the absolute realpath, and Promote
reads the block on the executor — title from the first follow-up, summary
the verdict — composing exactly as before when there is none
(`docs/channel-tools.md`). The attach also records the answer block's
`verdict` and `recommendation` on the card — `report_verdict` /
`report_recommendation` (v28, `attach_report`'s ring), read once at the
attach on the executor, replaced by a re-attach, empty for a report with no
block — drawn through `ScoutVerdictLine` on the tile, the card window and the
phone's card screen; the Reports list still reads the file. Every report also lists in the Mac's Reports tab
and the phone's Scouting tile on the Menu; the index and the body read are
`docs/transport-contract.md`'s.

**A card may hold a standing instruction to take the second path by itself.**
`start_when_planned` (v17, ring 1, `''` off / `'1'` on, normalised at both
`create` and `_update_locked`) is a tick beside Refine on the card face, the
card editor and both composers. The trigger is `attach_plan_by_session`'s own
success — the refinement's **one** completion seam — which calls
`_schedule_auto_start`; that schedules and never awaits, because the attach's
reply travels a `channel_server` socket with a five-second call timeout.
`_auto_start_after_refine` **spends the tick first** (`update(..., bump=False)`)
and calls **`dispatch_card` and nothing else** — no `allow_unplanned`, no
`skip_plan_gate`, no `queued_replay`. One press' worth of capability: a full
project **queues** in `queue_reason`'s words (classified by re-reading
`queue_state`), and a hard refusal is written as `dispatch_error` **at this
call site** — never by widening `_queue_hard_refusal` — leaving the card in
Backlog where `attach_plan` put it. No new event-log kind: `card_dispatched`
and `card_dispatch_failed` exist. The cost: a refinement claims no parallel
slot, so at a limit of 1 the implementation terminal opens while the
refinement's own is still closing. `start_when_planned_supported` on
`_pipeline_writable()` is the version marker (an older Mac draws the tick
**absent** rather than 400ing inside `_board_named_fields`). Not spelled
`auto_start`: `board_autostart` is the queue drain's own switch.

**Several Prep cards may be refined by one press** (25 Sep 2026):
`board_refine_batch` (on both phone tuples behind `refine_batch_supported`;
the phone's Prep select mode is `docs/phone-contract.md`'s) → `refine_cards` runs every Refine
guard per card, then one spawn with a `/ship batch:` prompt. Each card is
marked `dispatching` with a shared `batch_id` / `batch_rank` (v27, ring 2,
the pair the batch-implement sibling reuses beside `link_state`), which
`_launch_inflight` counts as one launch; all bind to the one session. Each
attaches by its plan's `- **Card:**` header, and a member left planless
ends with `BATCH_UNPLANNED_NOTE`. The session's spend is recorded on every
card of the batch, and its origin stamp names the first card. Long form:
`docs/channel-tools.md`.

**Several Backlog cards may be built by one press** (25 Sep 2026):
`board_start_batch` (on both phone tuples behind `start_batch_supported`; the phone's Backlog select mode is `docs/phone-contract.md`'s) → `start_cards` runs every Start rung
per card, skips and names a failure, refuses a full project, and spawns one
`/ship batch: implement` session bound to the first card; the rest wait in
Backlog with `batch_id` / `batch_rank` and refuse a single Start. The
session binds **one card at a time** — one claim, not re-bought, so a
person's Start may land between two members — and moves on with
`dark_army_next_card`, leaving an unclosed card `ended`. A session that
ends, a bind that expires, or a reset of the current card sends the
unreached members back with `BATCH_LEFT_NOTE`. Only the batch's owning
session (its lowest-ranked bound card, `_batch_owner`) can walk or release
it, and a marked card outside Backlog is refused a single Start until Leave
batch. Cost, trail and fix rounds are session-wide. Long form:
`docs/channel-tools.md`.

**How many agents may work at once in one project is a number you set, per
project.** The machine-wide `board_parallel` (default **1**) is the default;
`board_parallel_by_root` — `{canonical root: int}` — is the override map, set
from the pipeline heading's picker. Both are clamped to 1..4 by
`BobDaemon._clamp_parallel`, called by `set_board_parallel`,
`set_board_parallel_override` (0 / `None` / non-int **removes** the entry) and
the startup bulk feed `set_board_parallel_overrides`.
**`_parallel_limit_for(root)` is the one resolution seam** — override, else the
machine dial, floored at 1 — and nothing outside the setters, `__init__` and
the snapshot's scalar reads `board_parallel_limit`. The overrides dict is
written from the panel reader thread and read on the executor, so it is
**replaced, never mutated**. A press on a card whose project has no free place
queues it: `_slot_refusal` counts `board_queue.claims` against
`_parallel_limit_for` of the card's own **root**, and
`_decide_queue_dispatches` names `board_queue.slot_head` — the oldest queued
card, in `queue_key` order, when a place is free. The count comes from
`claims`/`holds` with `_claiming_session_ids()` (stated under **A claim ends
when the work does**), the predicate the snapshot publishes as
`work_active`, never a second walk over `link_state`; a fresh press joins the
back of the line, and a queued replay is held only by cards queued *before* it.

**One press gets a whole project moving.** `board_start_project` →
`BobDaemon.start_project(root)` takes `_dispatch_lock` once and calls
`_dispatch_card_locked` on that project's Backlog cards — matched by **root**,
in the board's own order, at most `START_PROJECT_MAX_CARDS` (12). It is N
presses and **not a new capability**: every gate, `dispatch.guard` included,
runs per card, there is no `skip_plan_gate`, and `board_dispatch` off refuses
it. It spawns **at most one process** — `PROJECT_BUSY_REFUSAL` is transient, so
the rest queue and the drain starts them. `queued_replay` is **not** passed (it
would enqueue silently, writing no `queue_state`); a card is classified by
re-reading `queue_state`, and the walk stops at `MAX_QUEUED_PER_PROJECT`, asked
as `queued_count`. It writes **no** card itself — no `dispatch_error` on a skip
— and adds **no** event-log kind. The reply is `_start_project_report`,
composed once in `daemon.py` beside `_queue_reason` and drawn verbatim by both
clients under the project's Backlog heading (on the Mac, the board's own
Backlog row heading, drawn only while the project picker names **one**
project — `BoardView.projectControl`); `start_project_writable` is the
phone's absent-never-inert marker.

The slot count (`slot_head` over `_parallel_limit_for`) is the only rule,
and its cost stands: **at a limit above 1, two agents may edit one tree, the
same file included.**

**A queued card says why in the daemon's words.** `_queue_reason(count,
autostart)` composes the sentence; it rides the snapshot as `queue_reason`,
per card, composed at decoration time so flipping `board_autostart` rewords
every queued card on the next frame, and it is the string `_enqueue_card`
refuses a press with. The panel shows it verbatim
(`BoardCard.queuedLine(autostart:)`), with two plain fallback lines for an
older daemon. **The limit is published resolved, per card**: `parallel_limit`
— `_parallel_limit_for` of its own root, written at decoration beside
`work_active` / `run_active` — plus the board's scalar `parallel_limit` (the
*clamped* machine default, never `preferences.json`) and `parallel_overrides`,
for the picker's tick-state alone and **never a denominator**. The board's
In progress heading draws a `RUN 2/3` picker (`RunLimitPicker`,
`BoardProjectControls.swift`), floored at 1, only while the project picker
names one project with something running or queued — **absent, not empty**
— and derives nothing: `BoardProjectControls.resolvedLimit` is the first
card whose figure is non-zero, the scalar only where every card reads 0.
Choosing a rung sends `set_board_parallel_root`
`{root, limit}` (0 = back to the default) on the **panel's stdin/stdout
channel** — `preferences.json` is the menu-bar app's to write, so the verb is
not on the loopback table. **The phone reaches the same setter by asking
rather than by writing**: `set_board_parallel_root` / `set_board_autostart`
on `LAN_ACTIONS` **and** `REMOTE_ACTIONS` land in
`BobDaemon.request_preference`, which applies and stores **nothing** — it
checks `PHONE_PREFERENCES` (those two names; `board_dispatch` and the
machine-wide dial stay at the desk), refuses in
`PREFERENCE_UNREACHABLE_REFUSAL`'s words when nobody implements
`on_preference_request`, and otherwise hands the request over. The menu-bar
app's `on_preference_request` is `on_alerts`' hop: `callAfter` onto the main
thread, through `PREFERENCE_REQUESTS` onto `_set_board_autostart` /
`_set_board_parallel_root`, then `_push_panel_context(respawn=False)` —
**`respawn=False` is load-bearing**, a phone press must never open a window
on the Mac. The 200 means *accepted*, not applied: one writer, which also
re-feeds both dials at startup, so a phone's change survives a restart; the
phone holds the pressed value (`settlingPreferences`) until a board reports
it. `board_unqueue` and `board_queue_move` are on both phone tuples and in
`_LAN_BOARD` too, both clear-never-set.

**A claim ends when the work does, not when the terminal closes.** It is a
card of the same project whose `link_state` is `dispatching` or `live`, minus
three releases: **`ended` holds nothing**; **a card in `done` holds nothing**;
**a `live` card whose session Dark Army can no longer hear holds nothing** — a
dispatched session that finishes and goes quiet is evicted from the hook
stream after the staleness window and comes *back* as a roster stub in
`running` while its terminal stays open, so `link_state` never reached
`ended` (`CLAIMING_LINK_STATES`' own argument one rung on).
`_claiming_session_ids()` is **pipeline main agents only** — the hook stream
minus any stray Grok child a parent already claims, plus the Grok roster
whole and Codex roots only; background agents are *out entirely*, and
`_bindable_candidate` keeps one from ever being paired with a card. The panel
re-derives none of this — what a queued card waits on arrives as
`queue_reason`, composed per frame and never a column.

The gate lives inside `_dispatch_card_locked` — one function, both gestures —
after the plan gate and *before* `dispatch.guard`. **A queued card does not
move.** (`blocked_by`: `docs/card-dependencies.md`.) The drain
(`_decide_queue_dispatches` on the executor, `_flush_queue_dispatches` on the
loop) takes one card per project per pass: transient refusals **hold**,
every other refusal **dequeues** with its words in `dispatch_error` — except
the plan gate's two confirmations, which a replay never raises.
`MAX_QUEUED_PER_PROJECT` (8) is refused at the store.

**A queued card is a confirmed card.** The drain's replay
(`queued_replay=True`) runs `_plan_gate_refusal(card, replay=True)`, which
asks neither confirmation — no plan, or a plan changed since approval —
because the card could only have been queued by passing, or being confirmed
past, that gate at the press; `queued` itself is the record of the answer, and
`allow_unplanned` is **not** carried across the enqueue (a remembered flag
would be a second truth that outlives `board_unqueue` / `board_reset`). The
one rung that survives a replay is a `plan_path` naming a file that has gone:
that still dequeues with `PLAN_GATE_REFUSAL`, because `dispatch.start_prompt`
would otherwise build `/ship implement <missing path>`. A person's press,
`start_project` and `_auto_start_after_refine` all still ask. **The board is
the one place a card is drawn on the Mac**: the rail's pipeline band and
Backlog tab are gone;
the phone's `PipelineView` stays. `work_active` is the queue gate's published
per-card answer (`board_queue.holds`, released on `manual_steps`);
`run_active` is the same predicate minus that release and is what
`Board.running(in:)` — the RUN count — reads; both published per card, never
re-derived in Swift. Queue reordering is the phone's alone. Every ⌗ / `card ⌗` / banner reveal goes through
`BoardState.requestReveal` → `BoardView.reveal`, the one place that widens
the project filter, writes the `#card` query **and scrolls the card into
view** (vertical alone; every row draws open while a search is up). **The
scroll is AppKit on the card's own frame**: the revealed card reports its
frame (`RevealFrameKey`) and `scrollRevealed` centres it in the
`NSScrollView` `ScrollViewFinder` found, once per reveal
(`pendingScrollCard`).

**A card reaches Done only because somebody said so** — a human drag, the
bound session's own `dark_army_close_card`, or **a person's press on Close terminal**
(all three through `declare_done`, recorded as `closed_by` + `close_note`, its
own UPDATE rather than through `update()`). The third door is `close_session_terminal(..., by_person=True)` →
`_finish_card_for_closed_session` → `close_card_by_session` → `review_card`
(**the press is the person's review**: `reviewed_at` is stamped at once, so
their own close never wears FINISHED · REVIEW; a flagged manual check stays);
its guard is `by_person`, stated under **And a way to finish reading** below.
**A card with an open manual check goes to Done** (25 Sep 2026): the check
is a file under `manual-check/`, the flag names it (`manual_check_path`)
and the close follows; `docs/channel-tools.md` has the order.
It inherits `close_card_by_session`'s three exclusions: a refining session is
invisible to `by_session`; the Done-arrival hook is unreachable, so this
cannot chase `_wrap_up_for_done`; a card already in Done is a no-op. Codex is **not** excluded here — this writes a
board row and types nothing. The cost: `update` does not clear `session_id`
when a card leaves In progress, so closing a terminal can finish a card
somebody had dragged back to Backlog. The human drag arrives by **two** verbs,
`update_card` and `reorder_card`; both hold `_board_write_lock` across their
read-then-write and then call `_after_board_write(before, after)`. The Done
leg is close-only: the card moves, the session keeps its context and its
`## Work done` report on screen. The card always moves; a refused close is
one log line and no orange note.

## A complete Prep card is the handoff

Refine and Start each open a fresh session, and nothing of the conversation
that filed or prepared the card goes with it: the card is the durable,
reviewable substitute for a forked supervisor context.
For Refine the card is the whole brief; for Start the card and its plan are
the whole brief. A session reads what the card carries and fills nothing in,
never from the prior conversation.

| Field | Complete when | Written by | Reaches Refine | Reaches Start |
|---|---|---|---|---|
| `title` | names the change in words, not a stub | a person, Prepare's `TITLE:`, `dark_army_add_card` | yes (`Title:` line, or the `/ship` line with no summary) | scout only (`Title:` line) |
| `summary` | plain words, not the title repeated | a person, Prepare's `SUMMARY:`, `dark_army_add_card` | yes (the `/ship` line) | scout only (the `/scout` line or `Brief:`) |
| `prompt` | the instructions; on an unplanned build card they stand alone, because its title and summary never reach Start | a person, Prepare's `INSTRUCTIONS:`, `dark_army_add_card` (`notes`) | yes (`Instructions:`) | the whole prompt when unplanned; `Instructions:` on a scout; the plan line instead when planned |
| `tool` | one of `claude`, `codex`, `grok` | the composer's picker, a person, `dark_army_add_card` | chooses the assistant | chooses the assistant |
| `project` | the label of the project `root` names | the composer's picker, Prepare's `FOLDER:`; the daemon from the calling session for an agent-filed card | no | no |
| `root` | a known project root (`dispatch.py`'s fourth property) | the composer's picker, Prepare's `FOLDER:` (`card_prepare.parse_folder`); the daemon for an agent-filed card | chooses the folder | chooses the folder |
| `workflow` | at least one stage the project declares (the helpers expected) | Prepare's `SPECIALISTS:`, a person, `dark_army_add_card` (`stages`) | no | no |
| `priority` | a whole number 0..100 as text; `''` is unscored, and the scorer fills it | `card_priority.py` or a person | no | no |
| `area` | required when known; `''` is allowed | Prepare's `AREA:` (`card_prepare.parse_area`), a plan header, a person | no | yes (`dispatch.area_block`) |
| `beneficiary` | required, not a stub; `dark_army_add_card` cannot write it | Prepare's `BENEFICIARY:`, a person, a plan header | yes (`dispatch.objective_block`) | yes (`dispatch.objective_block`) |
| `intended_benefit` | required, not a stub; `dark_army_add_card` cannot write it | Prepare's `BENEFIT:`, a person, a plan header | yes (`dispatch.objective_block`) | yes (`dispatch.objective_block`) |
| `success_criterion` | required, not a stub; `dark_army_add_card` cannot write it | Prepare's `CRITERION:`, a person, a plan header | yes (`dispatch.objective_block`) | yes (`dispatch.objective_block`) |
| `create_token` | create-only: required of a client's create call, not a completeness test of a stored card (`dark_army_add_card` and every card from before the token never carry one) | the composers alone (`BoardClient.swift`, the phone's `ComposerView.swift`), at `create` | no | no |

**Stub text is one definition.** A field whose words are empty after
trimming, one of `board_workflow._OBJECTIVE_NONE`'s words (`none`, `n/a`,
`-`, `—`, matched case-insensitively) or a `<...>` placeholder is empty.
These are the same words that stop a plan header seeding the objective
(`parse_header_objective`), so a card and a plan disagree about nothing.

An incomplete Prep card is not ready: it stays in Prep, and nobody presses
Refine or Start on it. A person fills the missing boxes in the Mac card
window, or rewrites the card through a composer's Prepare — Prepare is
composer-only, and the phone cannot edit an objective after create. A
Refine session that opens on one asks for the missing fields among its at
most three interview questions; when more is missing than those can
settle, it writes no plan, and the card stays in Prep. This is a rule for
the person and the workflow, not a gate: `dispatch.guard`, the plan gate,
`refine_guard`, `REMOTE_ACTIONS` and Refine's behaviour are unchanged, and
nothing refuses an incomplete card.

**Who fills what.** Prepare's labels map onto the fields — `TITLE`,
`SUMMARY`, `INSTRUCTIONS` and `SPECIALISTS` through
`card_prepare.parse_idea`, `FOLDER` through `parse_folder`, `AREA` through
`parse_area`, and `BENEFICIARY`, `BENEFIT`, `CRITERION` through
`card_prepare.OBJECTIVE_LABELS` — with the phone's twin in
`CardPrepareRules` and `PreparerBrief`. Prepare never writes `tool`,
`priority` or `create_token`. `dark_army_add_card` writes title, summary, prompt
(`notes`), tool, workflow (`stages`) and kind, naming a project or taking
the calling session's; it has no objective, area or priority argument, so
an agent-filed Prep card is incomplete until a person completes it. A card
`/ship` files with a plan attached leaves Prep at once and takes its
objective from the plan headers (`fill_objective_if_empty`).

**One checklist.** An outside client's list — a bot's `CARD_COMPLETE` or
`validate_card`, names that exist in no file in this repository — is a copy
of this table, and where the two differ this table wins.
`host/tests/test_card_handoff_contract.py` holds the table to the store's
real columns and to what the prompt builders send.

## The store, the launcher and the workspace

- **`board.py`** — the Kanban store, and nothing else: it knows nothing
  about sessions and can spawn nothing. The `cards` and `card_messages`
  tables plus a retained outcome ledger in
  `~/.dark-army/board.db`, shaped on `HistoryStore` —
  `check_same_thread=False` behind a `threading.Lock`, WAL capped at
  `WAL_SIZE_LIMIT_BYTES` (8 MiB, `journal_size_limit`, so the coalesce
  `VACUUM` leaves no 177 MB `-wal` behind), `busy_timeout=5000`, an idempotent `_SCHEMA`, `_ADDED_COLUMNS` and a
  forward-only `SCHEMA_VERSION` (30; the retired `initiatives` columns
  are emptied, never dropped — **there is no folder concept on the board
  now**). Four columns:
  `prep` / `backlog` / `in_progress` / `done` (the SQL column is
  `column_name`). **Forward compatibility is the standing rule**: reads are
  `SELECT *` into a dict that ignores unknown keys, writes never drop a
  column they did not write, every added column is `TEXT NOT NULL DEFAULT
  ''`, and a migration names the one thing it changes.

  **Who may write what is the design of this file.** Three rings:

  | Field | Written by | Why not wider |
  |---|---|---|
  | `title`, `summary`, `prompt`, `workflow`, `tool`, `root`, `column_name`, `priority`, `area`, `kind` | any surface (`_WRITABLE` ∩ `ApiServer._BOARD_FIELDS`) | things a person states |
  | `session_id`, `link_state`, `refine_session_id`, `refine_state` | the daemon, through `update` (`_WRITABLE` only; the refine pair is `REFINE_STATES`-validated) | Dark Army's own bookkeeping; a surface may clear a dead link via `board_reset`, never set one |
  | `plan_path` | `attach_plan` alone | a surface that could set it could stamp a card "planned" and walk it past the plan gate with no plan behind it |
  | `report_path` | `attach_report` alone | a surface that could set it could show a scout as reported against a file nobody wrote |
  | `plan_approved`, `plan_approved_at` | `approve_plan` alone | a surface that could set them could show a card as approved against a plan nobody read — the digest is the *version* somebody said yes to |
  | `agent_trail` | `BobDaemon._record_card_stages` alone | a surface that could write it could claim a stage happened that never did |
  | `closed_by`, `close_note` | `declare_done` alone | it could otherwise paint a hand-dragged card with a verifier's signature |
  | `manual_steps` | `flag_manual` alone (set) / `clear_manual` alone (empty) | a surface that could set it could hang a chore on a card nobody flagged |
  | `manual_check_path` | `flag_manual` alone (v26) | a surface that could set it could point a card's check at a file nobody wrote |
  | `project` (following a folder rename) | `relabel_root` alone | a rename is Dark Army observing, not a person's edit: one statement over every card of the root, never an `update()` per card |

  The single-writer verbs share `declare_done`'s shape — the guard riding
  in the UPDATE's own WHERE clause, `rowcount == 0` meaning "that card
  moved". `attach_plan` writes the path, moves the card to Backlog and
  clears `refine_state` in one statement; `declare_done` refuses a card
  that is not the declaring session's, one already in Done, and a note
  empty after stripping (clamped at `MAX_CLOSE_NOTE_CHARS` = 400, not
  refused); `flag_manual` is
  `declare_done`'s opposite statement — same scope, clamp-don't-refuse
  (`MAX_MANUAL_STEPS_CHARS` = 1000), refusing only empty steps, moving
  nothing. `update()` **clears** `closed_by` / `close_note` where a card
  leaves Done. `by_session()` and `by_refine_session()` return `[]` for an
  empty id — both fields default to `''`.

  **Two splits are deliberate and both say declared-vs-observed.**
  `workflow` is what a card *expects* and only it may draw a hollow "still
  to come" marker; `agent_trail` is what Dark Army *saw*, written off the
  append-only `subagents_seen` that `subagent_start` stamps and **not**
  off the live `subagents` set. Codex supplies `observed_roles` through the
  same writer, separately from live helpers. `_stage_name_ok` keeps opaque ids out of
  it — the floor is `OPAQUE_ID_MIN_CHARS` (12), above any real agent type
  and below the shortest id seen (17 hex). Trails are append-only
  (`record_agents` never removes).

  **A card's timeline rides the on-open card read only** — `_card_collect`
  puts `timeline` beside `plan` on the two readers, never on
  `_cards_sync_page` or SSE; marker `card_timeline_supported`,
  `CardTimeline` the third byte-pinned card rule (`test_card_timeline.py`).

  **Bounds are refused at the store**: `MAX_CARDS` (500),
  `MAX_PROMPT_CHARS` (8000), `MAX_SUMMARY_CHARS` (400).
  `_trim_card_for_snapshot` clamps the prompt to
  `BOARD_SNAPSHOT_PROMPT_CHARS` (400) and flags it `prompt_truncated`, the
  full text fetched from `GET /api/board?card=<id>`; the card editor
  disables the prompt field and holds **Save** until it arrives.

  **How important a card is, is the third helper of the same family**
  (`card_priority.py`): one `claude -p --model haiku` per card, **once ever**
  (only hits persist, to `card-priority.json`), cwd `STATE_DIR`, the
  instruction in the prompt and never `--system-prompt`. Decided on the
  executor (`_consider_priority` inside `_reconcile_board` — skips a scored
  card, a `done` card and a card with no words), spawned on the loop
  (`_flush_card_priorities` / `_score_card`); nothing on that path writes
  `dispatch_error`. The answer is a whole number 0..100, **rejected rather
  than clamped** otherwise, written through the ordinary `update` so it
  moves the card's `revision`. **`''` means unscored and `'0'` means scored
  lowest** — distinguishable on the card face, indistinguishable in the
  sort (`CAST('' AS INTEGER)` is 0). Scores are **absolute**: the helper
  sees one card, never the board. The number is the board's first ordering
  term inside every column, so a drag only rearranges cards that share
  one; the queue, the plan gate, `dispatch.guard` and `start_project` are
  untouched. `dark_army_add_card` gains **no** priority argument.
  `priority_supported` on `_pipeline_writable()` is the version marker, so
  an older Mac draws the number box **absent** rather than 400ing.

  **Outcomes are explicit human decisions, separate from Done and Reviewed.**
  `board_outcome_store.OutcomeStoreMixin` shares BoardStore's connection,
  lock and transactions; `board_outcomes` is pure measurement arithmetic.
  Mac card details edit `beneficiary` (200), `intended_benefit` (1000),
  `success_criterion` (1000) and optional ISO `outcome_check_on`. Every
  objective Save needs `expected_outcome_revision` and advances the
  server-owned revision atomically; editing an accepted objective needs
  `confirm_outcome_scope_change`, which removes acceptance as a scope
  change, preserving historical criterion/evidence, without creating
  rework. `board_accept_outcome` and `board_request_revision` are
  loopback-only, behind the token/Host/Origin gates; both need the
  displayed revision, a bounded idempotency key and nonempty
  evidence/reason (4000), and a replay returns its original result even
  after the card changes. Acceptance needs a benefit and criterion, no
  active implementation/refinement run, no open question/permission and no
  outstanding manual check. Decisions hold the dispatch and board-write
  locks; they move no column and start or close no terminal. **Prepare
  may draft the objective text and the Refine/Start prompts carry it** —
  `dispatch.objective_block(card)`, an `Objective:` block appended after
  the attachment block in `_dispatch_card_locked` / `_refine_card_locked`,
  empty when the card has none so an unobjectived argv is byte-identical;
  the sealed door exempts `board_create` alone from its objective 403 —
  but no channel tool names the fields (the plan's headers are the one agent-written seed, above)
  (`test_outcome_api.test_agent_and_generic_field_ownership`). No agent tool
  or mobile action can write a decision; sealed generic objective writes
  are refused too. Evidence is plain text the person supplied, never
  verified or fetched.

  Every observed Done arrival after a bound implementation, and every
  explicit acceptance, records a submission. Reopening Done or resuming an
  accepted card removes acceptance and opens one rework episode. First
  acceptance timestamp and canonical project root never move.
  `GET /api/outcomes?card=…` or `?root=…` and sealed read kind `outcomes`
  serve reports on demand (page size 1..100, offset; JSON capped at 300 KB
  before sealed-envelope escaping; optional finite `[from,to)` UTC period
  up to 366 days, default last 30 days). SSE carries only per-card outcome
  status/revision and `outcomes_supported`; objective text, evidence and
  metrics stay in reports, which both clients fetch on structural changes
  or at most every 30s. Old servers hide the controls; unknown measurements
  remain unavailable. Lifecycle timing (schema 21): queue/execution/review/rework
  as card-time; sealed `lifecycle` read; collector failure must not stop reconcile. See `docs/lifecycle-timing.md`.

  The ledger survives delete, Clear Done and archival, as their Mac
  confirmations state. It keeps minimal objective/decision snapshots,
  unique provider/session pairs, latest cumulative cost, UTC-day
  wait totals — no transcripts or attachments; late links are partial. Questions,
  permissions, due manual checks and submitted-result review are time
  awaiting a person; adjacent monotonic observations count the union once,
  causes are separate subtotals; missing samples, >30s gaps, clock jumps
  and restart mark partial and fill no gap. Only moved samples write, one commit a ledger; waits batch up to `WAIT_FLUSH_SECONDS` (crash loss); failures read unavailable.

  Accepted outcomes count distinct **currently** accepted cards whose first
  acceptance falls in the report period, retained or deleted. Rework rate
  counts distinct submitted cards rejected/reopened in-period against a
  submission from that same period; older submissions' rework is separate
  carry-in. Empty denominators are unavailable. Wait totals are observed
  card-hours, not human-hours. Monetary amounts require explicit
  units/provenance — measured USD history/live readings, no token pricing
  or account allocation — and live/history sources lack a guarantee of
  child inclusion, so they are partial. Genuine zero remains zero;
  cumulative readings replace, decreases or conflicting sources preserve
  the known amount and mark partial. Complete per-outcome cost is computed
  only for fully covered accepted cards in one currency, showing `n of N`
  coverage; partial totals never enter that denominator.

  **What Dark Army observed a run do is a fifth writer ring, and it is a table.**
  `card_runs` (v15, `card_messages`' shape — `CREATE TABLE IF NOT EXISTS`
  inside `connect()`, **no `cards` column**; its own
  `_ADDED_COLUMNS["card_runs"]` entry carries v24's three `shunt_*`
  columns, each with a DEFAULT so a v23 build's `close_run` UPDATE lands)
  holds one row per card keyed on the card's own `dispatched_at`, so no new
  id is minted and the latest replaces the previous by `INSERT OR REPLACE`.
  Written by `open_run` / `close_run` alone, read by `run_for` /
  `run_headlines`, dropped beside `card_messages` at all three deletion
  sites, swept for orphans in `connect()`. It names no member of `_WRITABLE`
  and no key in `ApiServer._BOARD_FIELDS`: **a record a surface could write
  proves nothing**. `close_run`'s guard is `WHERE card_id = ? AND run_at =
  ?`, so a late collector for run *n* cannot stamp run *n+1*'s row —
  `rowcount == 0` means *that card was started again*. `report` / `summary`
  **clamp** (`close_note`'s rule); a `files` payload not in the parsers' own
  shape is **refused**; `run_headlines` selects `length(report)`, never
  `report`. The decision half is `work_record.py` — every bound, both
  parsers, the verdict rule, the sentences both clients draw verbatim, and
  **the git argv, in `work_record.py` and `worktrees.py`**. The daemon reaches the table only
  through the store's verbs: `_schedule_work_baseline` at the end of
  `_dispatch_card_locked` (never awaited), `_consider_work_record` at
  `_reconcile_board`'s one `mark_ended` seam, `_flush_work_records` /
  `_collect_work_record` on the agents-push path beside
  `_flush_card_priorities`. Every git failure still writes the record with
  `files_available` false and a reason in words; refinements get none. The
  snapshot carries eight scalars per card as `work_record`, **absent** where
  there is no record; the report text and file list never ride SSE. **The
  baseline is taken shortly after the terminal opens**, so the record says
  *what changed in this project since work started* — never *what this
  assistant changed* — and the caption says so.

  **Cost and time on the card.** Every card that has had a run carries
  `run_figures` on the snapshot — `cost_usd`, `cost_coverage`
  (`complete` / `partial` / `unknown`), `active_seconds`, `active_ticking`,
  `ctx_percent`, `attempts` — composed at decorate time by
  `run_figures.compose` from `BoardStore.run_figures()`, one locked read
  per frame over the **two retained ledgers and nothing else**: cost is
  the USD sum over `outcome_runs` (both phases, so a refinement's spend is
  on the card; measured USD only, so a Codex card reads "cost unknown" in
  words and never a made-up $0), **graded for the card by that read and
  not by `board_outcomes.lifecycle_cost`** — the report's `complete` is
  reserved for a reading that proves child inclusion, which neither the
  live statusline nor `history.db` does, so every run row's
  `cost_coverage` column reads `partial` and `/api/outcomes` keeps saying
  so; the card's line asks the plainer question and reads `complete` when
  every run row has a USD amount, none was a late bind (`late`) and none
  saw a decrease or a changed source (`cost_conflict`), `partial` exactly
  where the plan reserved the word, `unknown` with no amount at all;
  working time is `lifecycle_spans` `category='execution'`
  `coverage='complete'` (implementation alone — refinement time is not
  execution, and nobody "fixes" that mismatch by adding refinement spans;
  `lifecycle_prune` trims spans past 366 days, so a card that old loses
  seconds it once had, the cost ledger being unpruned), and
  `active_seconds` is **`null` where the card has no execution span at
  all** — a refined-but-never-started card carries its planner's spend and
  no time part, which is not the same fact as under a minute of work;
  `active_ticking` is the `execution`
  membership on the card's active `lifecycle_checkpoints` row; the live
  `ctx_percent` is `metrics.ctx_used_pct` off the agents snapshot's live
  buckets plus the `finished` rows flagged `alive` (`_live_ctx_by_session`,
  `_board_live_ids`' shape — a live session quiet past the idle grace keeps
  its figure), joined only where `link_state` is `live`. `attempts` is `COUNT(DISTINCT session_id)` over the card's
  implementation run rows — never `lifecycle_attempts`, which also counts a
  dispatch that never bound. **Absent where there is no run row and no
  execution span**, `work_record`'s rule. Published at **minute**, cent and
  whole-percent granularity, and the daemon buys the frame:
  `_run_figures_drifted` at the end of `_reconcile_board`, after
  `_observe_board_outcomes`, compares `run_figures.drift_key` per card
  against `_run_figures_seen` — `_manual_due_drifted`'s shape. The key is
  **coarser than the line**: the minute, the **dime**
  (`DRIFT_COST_QUANTUM_USD`), **five points** of context
  (`DRIFT_CTX_QUANTUM_PERCENT`), the coverage and the attempts, and never
  `active_ticking` (nothing draws it) — a working Claude session moves the
  cent and the percent on nearly every ~4 s pass, so a frame per cent
  would be a frame per pass; the cent and the percent still ride whichever
  frame the minute, the dime or the five points buys. No client ages the
  figure and no clock depends on the frame's stamp. `run_figures_supported` on
  `_pipeline_writable()` is the phone's marker; the wording rule is
  `RunFigures.swift`, byte-pinned Mac/phone from `enum RunFigures {` down
  (`test_run_figures.py`), drawn on the card face, in the card window and
  on the phone's card screen — never on the process table or the strip.

  **How the run is going is one line on the card, and the daemon decides
  every word of it** (`run_health.py`). Every card with a bound or past
  implementation session carries `run_health` on the snapshot — `class`
  (`typical` / `large` / `worrying`, or `""` where the run cannot be
  sized), `basis` (`project` / `default`), `turns`, `tokens_k`, `ctx_pct`,
  `asks`, `refusals`, `attempts`, `returns`, `fix_rounds`, `attention`,
  `live` — composed at decorate time by `run_health.compose` off the
  cached agents snapshot row for the card's session (`_snapshot_row_in`;
  the rows the rail draws are the one source of `stats`, `metrics` and the
  counters) plus one `BoardStore.run_health_counts` read and one memoised
  `run_health.Ledger.load()` per frame. Where each figure comes from:
  turns and tokens are `stats.assistant_messages` and
  `total_input_tokens + output_tokens` (never cache reads) off the
  transcript; `ctx_pct` is `metrics.ctx_used_pct` off the statusline;
  `asks` is `tool_counts["AskUserQuestion"]` plus the session's
  `permission_asks`; `refusals` is `permission_denied + stop_failures` —
  three **loop-side** counters on the session state (`docs/context-host.md`,
  *Session State Model*), copied onto the stub and never written from the
  executor; `attempts` is **the same count `run_figures.attempts`
  publishes** — `COUNT(DISTINCT session_id)` over the card's
  `outcome_runs` implementation rows, the sessions that actually bound —
  and never a count of `lifecycle_attempts`, which also holds a row for a
  dispatch whose bind window ran out unbound: one tile draws both lines,
  so the one word means one count (`test_run_health.py` pins them equal on
  a store driven through a timed-out dispatch and a bound retry; an `ended
  → live` resume keeps its session id, so it is still one attempt), and
  `returns` is `outcome_cards.rework_count` — both read, neither written,
  no counter added; `fix_rounds` is implementer spawns after the first off
  `stats.spawn_counts`, **the transcript's one-entry-per-spawn list**,
  because `subagents_seen` is deduped by type at the stamp and
  `record_agents` merges `agent_trail` by name, so neither can count a
  second `bc-implementer` — and Codex and Grok keep no spawn list
  (`NO_SPAWN_LIST_PROVIDERS`), so their cards' `fix_rounds` is **absent**,
  never 0 (the line draws `fixes –`).
  The size class is judged against the project's own **frozen** readings
  once it has `MIN_BASELINE_RUNS` (5) of them (`TYPICAL_RATIO` 1.5 ×,
  `LARGE_RATIO` 3 × the medians of turns and tokens) and against
  `DEFAULT_TYPICAL` / `DEFAULT_LARGE` before that; `attention` is a
  worrying class, a context at `CTX_WORRY_PCT` (85) or `FIX_ROUNDS_WORRY`
  (3) fix rounds, and the word is always drawn first — colour is never the
  only signal. **The figures are quantised before the dict is built**
  (`quantise_turns` exact to 20 then floored to 5, `quantise_tokens_k` two
  significant figures, `quantise_ctx` floored to 5), because a changed
  `board` section rides every `?sections=changed` frame and a per-turn
  figure would make the board news on every assistant message of every
  bound session; `test_run_health.py` pins that two rows one turn apart
  compose equal. **And the daemon buys the frame**: `_run_health_drifted`
  at the end of `_reconcile_board`, after `_run_figures_drifted`, composes
  the same dict per card with a session row off the snapshot the pass was
  handed and compares it with the last pass — `_manual_due_drifted`'s
  shape — so a quantum of turns, tokens or context that moved is drawn,
  and a turn inside one is not; a card whose row left the snapshot keeps
  its last key (the `mark_ended` seam buys that frame itself). **Absent
  where none**, `work_record`'s rule: a card nobody
  has started carries no key. When a run ends, `_freeze_run_health` at
  `_consider_work_record`'s `mark_ended` seam writes the final reading to
  `run-health.json` (`paths.RUN_HEALTH_PATH`, `_PRIVATE_FILES`; one entry
  per card, newest `MAX_LEDGER` = 500 within `LEDGER_DAYS` = 90, corrupt
  reads empty, `atomic_write_json`, idempotent on the card's
  `dispatched_at` **and the reading**: an `ended → live` resume keeps the
  `dispatched_at`, so a later reading with at least the frozen turns and
  tokens replaces the first quiet spell's and the identical reading writes
  nothing), so a finished card keeps its line after the tombstone
  is gone (`live` false, attempts and returns still the store's current
  figures) and the frozen readings are the next run's yardstick — a live
  run never moves its own cut. `run_health_supported` on
  `_pipeline_writable()` is the phone's marker; the panel decodes no
  markers and draws the line whenever the card carries one. The wording
  rule is `RunHealthLine` (`RunHealth.swift`, byte-pinned Mac/phone),
  drawn on the card face, in the card window's status section and on the
  phone's card screen — never on the process table or the strip. **The
  Inbox gets no new kind for a worrying run** (13 Sep 2026): the Inbox is
  everything waiting on a person, and a worrying run waits on nobody —
  there is no verb to press except Stop, `inbox_ack` hides session
  subjects and a card subject would sit for the life of the run, and a new
  `InboxKind` touches both clients' `Inbox.swift`, `NeedsYouView`,
  `test_inbox_surface.py`, `test_phone_inbox.py` and `PhoneInboxTests` for
  an entry `blockingCount` must ignore anyway. The amber line and the
  Inbox-independent `attention` bit give the person the signal where they
  already look; revisit as its own card once the class has been seen on
  real runs, and if added it is FYI (`blocks == false`) and never counted.

  **Every card carries a change number, and it is the store's own.**
  `revision` (v16, `INTEGER NOT NULL DEFAULT 0`) steps up whenever a write
  changes one of `REVISED_COLUMNS` — what a *person* reads on the card,
  never Dark Army's bookkeeping. It is `create_token`'s ring: in neither
  `_WRITABLE` nor `SINGLE_WRITER`. A caller may send `expected_revision` on
  an `update`; a mismatch is refused in `CARD_CHANGED_REFUSAL`'s words with
  **every column untouched**, and **absent means no guard**, which keeps
  the channel verbs, the reconcile and the scorer unchanged. Two mirror
  risks are pinned by `test_card_revision.py`: a revision moving on
  `link_state`, and an `ApiServer._BOARD_FIELDS` member missing from
  `REVISED_COLUMNS` (a silent overwrite).
  `column_name` is counted — a drag is a person's change — so **Dark Army's own
  moves pass `bump=False`**: the dispatch write, `bind_session` and the
  bind-window expiry. Keyword-only and daemon-internal; it suppresses only
  the counter, never a write. The 409 body
  carries `current` — `BoardVerbsMixin.STATED_FIELDS`, never `messages` —
  because it is routinely the first thing a phone hears after losing
  signal.

  `board.db` and its `-wal`/`-shm` siblings are in `paths._PRIVATE_FILES`.
- **`dispatch.py`** — Dark Army as launcher, in one file so the capability is
  auditable in one read. Six properties hold it down:

  1. **Only a deliberate human gesture puts work in front of it** — Start,
     START HERE, the drop into In progress, or an enqueue made by one of
     those. Every caller of `dispatch.spawn` / `dispatch.spawn_local` (card
     start, refine, consult, ad-hoc) re-runs its own guard under
     `_dispatch_lock`; Dark Army never *selects* work. The queue holds only
     membership and order, and the drain re-reads the card out of
     `board.db` and re-runs `guard()` and the plan gate at the instant it
     fires; it adds no timer, it rides the reconcile.
     `board_autostart` (default on) removes the drain alone. Promote
     creates a card and dispatches nothing.
  2. **A preference removes it entirely** — `board_dispatch`, checked at
     the top of every start verb; `dispatch_enabled` rides the board
     snapshot, so the button is *absent* rather than inert.
  3. **State is re-checked at the instant of dispatch** out of `board.db`:
     a startable column (`_STARTABLE_COLUMNS` — `prep`, `backlog`,
     `in_progress`, never `done`), a tool named, no session, not already
     dispatching or refining.
  4. **It refuses a directory that is not a known project** —
     `_known_project_roots()` is the folders of every open VS Code window
     ∪ the cwd of every live session, realpath'd, and the card's root must
     be an exact member and a real directory.
  5. **argv only, and that is not enough.** The executable
     comes from a fixed allowlist, the card contributes exactly one
     element (the prompt, always last), nothing is joined, quoted or
     handed to a shell. `_ARGV` puts codex's and grok's prompt behind `--`
     and `guard()` refuses a prompt starting with `-` or consisting of one
     word naming a subcommand of that tool (`_SUBCOMMANDS`, per-tool).
  6. **Bounded and enforced, not computed**: `MAX_CONCURRENT_DISPATCH` (2)
     machine-wide, at most one per project, `DISPATCH_COOLDOWN` (10s) per
     card. `dispatch_card` holds `_dispatch_lock` across the whole method.

  Above the guard sits the **plan gate** (`daemon._plan_gate_refusal`,
  applied in `update_card`, `reorder_card` and `dispatch_card`, each taking
  `allow_unplanned`): a card with no `plan_path` — or one naming a file
  that is gone — and no session may not *enter* In progress without the
  confirmed press's `skip_plan_gate`. Keyed on the field, not the column.
  The refusal is one constant (`PLAN_GATE_REFUSAL`) whose opening words the
  panel recognises (`ActionResult.isPlanGateRefusal`).

  **The gate has a second rung**: a card whose `plan_approved` digest no
  longer matches the file on disk is refused with `PLAN_CHANGED_REFUSAL` —
  a **second constant**, never a reuse of the first, because both surfaces
  match by prefix and this confirmation reads "start with a changed plan";
  neither may become a prefix of the other. **An empty `plan_approved`
  never gates**: approval is opt-in. Approval is `board_approve_plan` →
  `approve_card_plan`, which re-reads and re-hashes the file on the executor
  and refuses a digest the caller did not just receive. That read is
  `kind: "card"` beside `log` on both sealed doors (a *read*: no lease, no
  action tuple), serving the whole prompt plus the plan document with its
  availability **stated**; `plan_approval_supported` rides
  `_pipeline_writable` so a phone draws Approve absent rather than inert
  against an older Mac. The phone's card screen fetches it on open
  (`fetchCard`, never on the poll or background path) and holds Save in the
  panel's own words until the full text has landed.

  **Start's prompt is `dispatch.start_prompt`.** `attach_plan` writes
  `plan_path` and does not rewrite `prompt`: a card with `plan_path` is
  `Plan: <that path>` followed by `/ship implement`; no plan uses `prompt`.

  **Refinement is `dispatch_card`'s sibling** (`refine_card`,
  `dispatch.refine_guard`, `dispatch.refine_prompt`): same preference,
  lock, cooldown and bounds — one shared in-flight budget. The prompt is
  `/ship <summary-or-title>` then the card's title and stored instructions,
  plus Codex's planning-only skill entry,
  so the planner sees what is already on the card; the assistant is
  `card["tool"]` for all three (grok and codex behind `--`). Attach is
  inbound `dark_army_attach_plan`, which Grok and Codex can call. After
  `refine_guard` has judged that string, `_refine_card_locked` appends the
  same attachment block Start glues on, stripping baked-in path lines now
  that the stored prompt rides along. It never
  touches `session_id`/`link_state` or the column: `_bind_refining_card`
  writes `refine_session_id` + `refine_state="live"`, never
  `bind_session`. Expiry clears `refine_state` with the orange
  `dispatch_error` line; a refinement that ends planless goes `ended`. A
  composer may ask for both in one press: `board_create` with the envelope
  flag `refine` runs `create_card_and_refine`, whose refusal lands on the
  new card's `dispatch_error`. The flag is a `board_refine` in disguise:
  `_sealed_run` passes the payload through `_door_payload`, which drops the
  flag (never refuses) on a door whose `actions` tuple lacks
  `board_refine`; the loopback path honours it unconditionally.

  **`resolve_executable` is not bare `shutil.which`**: the LaunchAgent
  sets no `EnvironmentVariables`. Claude routes through
  `agents_poll.find_claude_binary()`, codex through `CODEX_CANDIDATES`,
  grok through `grok_leader.GROK_CANDIDATES`.

  **Binding is by elimination, narrowed to pipeline mains and proven where
  it can be** — a card in `dispatching` takes the first session new since
  the press with the card's provider and project that `_bindable_candidate`
  admits: an interactive top-level row, never a background agent or a stray
  Grok child. On extension 0.1.10+ the spawn reply carries the terminal's
  `shellPid` (kept in `_spawn_shell_pids`) and only a session whose pid
  descends from that terminal may bind; a row without its pid yet is
  *skipped* within the window, not refused for ever. The card moves to
  `in_progress` when the terminal opens, not when the session binds. If
  none appears — or none can be proven — within `DISPATCH_BIND_WINDOW`
  (120s) the card returns to the column its own state names — `backlog`
  with a `plan_path`, `prep` without — with a `dispatch_error` saying
  which, and the terminal is left alone. Recovery from a dead link is
  `board_reset`: `session_id` and `link_state` are absent from
  `ApiServer._BOARD_FIELDS`.

  **A Codex or Grok card that times out names the likely cause**
  (`dispatch.first_run_hint`): the tool's own *Trust this folder?* screen,
  which Codex 0.156 shows before any session exists in a folder it has not
  seen, whatever the approval mode. Dark Army neither answers nor pre-empts
  it; the person answers it in the terminal and presses Start or Refine again.

  **A started card works in its own worktree** (v30): on a git project with
  isolation on, Start prepares `card/<id8>-<slug>` in
  `<root>/.worktrees/card-<id8>/` (`worktrees.py`, `trust_marks.py`), records
  `worktree_path` / `worktree_branch` (`record_worktree`'s ring) and opens
  the terminal there; the release at Done never uses `--force`. The switch is
  `board_isolation_by_root`. Dead registrations are forgotten once per
  process per enrolled git project, never a present folder
  (`docs/card-worktrees.md`, *Stale registrations*). In full:
  `docs/card-worktrees.md`.

  Deliberately **not** passed: `--no-session-persistence`,
  `--setting-sources ""`, `--strict-mcp-config`, and
  `--dangerously-load-development-channels`. A dispatched session is an
  ordinary session; this does **not** sandbox the agent.

  **A card may name the model** its assistant runs on, chosen from
  `dispatch.MODELS` — a curated tuple per tool, shipped in code, no runtime
  discovery and no free text — and `argv_for` puts `--model <name>` on the
  argv **before** codex's and grok's `--` separator; Default names nothing.
  `guard()` refuses an off-list model in words, `board.py` refuses one at
  the store and clears the field when a card is retooled, and
  `dark_army_add_card` gains no model argument. Dark Army's own helpers keep their own
  settings: the card's model applies to Start alone.

  **A Default card runs on the model chosen for its assistant**: two
  preference keys, `agent_models` (`{provider: {slot: model}}`) and
  `agent_models_by_root` (the per-project override), both written by
  `_set_agent_model`. **`_agent_model_for(root, provider, slot)` is
  the one seam** — override → machine-wide → `agent_models.SHIPPED`,
  re-validated on the way out so a hand-edited name is `""` — at four
  sites: `_dispatch_card_locked` (the card's own `model` first),
  `_refine_card_locked`, the consult and `_prepare_card_text_locked`
  (`card-preparer`). The pack render writes the role's value as `model:`
  into each brief and its two shims. The allowlist is `dispatch.MODELS`
  plus `card_prepare.HELPER_MODELS` for the preparer slot alone; the
  settings window draws only slots the pack can write
  (`shipped_roles()`).

  **A card is never moved to Done by the daemon.** The bound session may
  declare it through `dark_army_close_card`, recorded with its author.

  **The return leg** — a card *arriving* in Done — is close-only, through
  the doors stated under **A card reaches Done only because somebody said
  so** (`_after_board_write` off both drag verbs; `_board_write_lock`
  released before the close). `board_close_terminal` is **on** by default
  and only the toggle writes the key; the gate is `link_state == "live"`,
  **not** `can_type`. **Codex is a silent rung**: it holds no pid anywhere `_roster_pid`
  reads — checked in `_wrap_up_for_done`, which has **no wrap-up
  fallback**: a refused close is one log line, never a `/clear`. The card's
  **Done button arms** (`state.doneArmed`) wherever the press *could* reach
  a live session, `dispatching` included; a Codex card never arms, and the
  **drag** into Done still does not.

- **`workspace.py`** — which project a session belongs to; the unit a
  person means by "project" is the **VS Code workspace**. Six rungs: **the
  enrolled project folder containing the cwd** (`enrollment.enrolled_label`,
  longest root wins); **a scratchpad path mangling an enrolled root**
  (`_by_enrolled_scratchpad`); a window whose folder **contains** the cwd
  (longest wins); a **scratchpad** path, matched by re-mangling each known
  folder `/`→`-` (because `-`→`/` is ambiguous); Claude Code's own
  `workspace.project_dir` / `git_worktree` from the statusline payload; and
  the basename. Applied in `_enrich_agent_stubs`, with a 5s cache on the
  lock read. Window names come from `workspaceFsPath` (basename minus
  `.code-workspace`), then `workspaceName`, then its single folder.

  **The enrolment rungs are one rule with no exceptions**: a project tab
  is named after the project's *own folder on disk*. A named
  `.code-workspace` does **not** keep its chosen name, the ledger's stored
  `label` field is ignored, and nested enrolled roots resolve to the inner
  one. Sessions outside every enrolled root walk the old four rungs
  unchanged. **Naming must never ride `root_enrolled`**, which
  `tests/conftest.py`'s autouse door fixture patches to identity suite-wide
  — `enrolled_label` and `_by_enrolled_scratchpad` are implemented over
  `enrolled_roots()` directly, and a grep criterion pins that
  `workspace.py` never mentions the admission name.

  **Cards follow the rename, and they must**: the board joins card to
  session by *label* — `_bind_dispatched_card` refuses a candidate whose
  row `project` differs before it looks at the cwd. `_reconcile_board`
  relabels once per distinct card root through `BoardStore.relabel_root`,
  a verb of its own rather than an `update()` per card (a rename is Dark Army
  observing, not a person's edit, and `update()` steps every revision):
  one statement in one transaction. `history.db` keeps its stored labels.
