# Codex contract

This is the full statement of what Dark Army may observe, decorate and control about a Codex session, and the proof each capability needs. `CLAUDE.md` restates the invariants and points here; this document is the text they were lifted from. Every rule below is stated once, in this file, and `codex_rollouts.py` is where it lives.

## What Dark Army may do

**Codex is a first-class row with deliberately narrower powers.** Dark Army may
*watch* a Codex session, may *decorate* its tab, and may control it only
where the process is provable.

## Its board MCP is board-only

**Its board MCP is board-only.** Session linking installs the copied
`channel_server.py` under a second Codex name, `dark-army-board`
(`--host=codex --name=dark-army`), by
asking `codex mcp` to inspect/add/remove it — Dark Army never edits Codex's
settings file, never replaces a same-named entry whose exact command and
arguments it does not own, and never lets an optional Codex failure undo
the Claude registration. The pre-rename name `bob-companion-board` is
removed only when it is exactly the entry the previous build wrote, and a
foreign one is left with a warning (`docs/channel-tools.md`, *Two names, one
script*). The server's immutable `--host=codex` mode advertises four verbs,
`dark_army_add_card` + `dark_army_close_card` + `dark_army_attach_plan` +
`dark_army_attach_report`, and `call_tool` enforces the same list; it declares no channel or permission
capabilities. `dark_army_close_card` was added on 2026-09-09, on the argument
`CLAUDE.md` already makes for the Close-terminal door: **it writes a board row
and types nothing**. Codex's narrowness is about *control* — typing, replying,
disposing a tab, answering a permission prompt — and none of that is in this
verb. Leaving it out cost what the board is for: a Codex session could open a
card and attach a plan but never say it had finished one, so every card a
Codex session worked stayed in *In progress* for ever, whatever its own
`## Work done` report said. Reply, `dark_army_needs_manual_check`, `dark_army_answer_card`
and the knowledge notes remain off the list.

The daemon attributes all four requests to one top-level `_codex_records`
entry by a fresh exact native root-journal holder matching the registered
parent PID and cwd. Add/attach/report/close resolve off the loop within five seconds;
registry and root generations are checked after awaits and before the store
write. Existing exact-PID/unique-cwd fallback remains only where no
contradictory exact navigation evidence exists. Ambiguity and child records
fail closed. This does **not** make Codex a push
channel. A Codex session already running when Dark Army registers the helper
keeps its old tool list and uses `/ship`'s paste fallback.

## Decoration is not control

**Decoration is not control.** `_push_agents_snapshot()` copies each safe
root's immutable session id, thread id, journal path and cwd **before its
first await**; then `codex_rollouts.resolve_title_ttys()` matches that
exact canonical root journal to the one stable native `codex` process
holding it open — path plus device/inode, pid, executable, cwd, creation
time, argv and tty all rechecked, the fd observed twice, child journals
never targets. A changed file or process, a conflicting exact-resume
signal, multiple holders, or two roots reaching one tty **withdraws** the
mapping rather than guessing. The worker returns only a private
session-to-tty map to `TitleWriter`: no record, stub, `/api/state`, SSE,
persistence or panel field carries it, and it never populates `pid` or
`process_identity`, grants a capability, or reaches Jump or Stop. Codex
ignores its public pid for titles, keeps the same lowest-idle owner per
tty, and uses the same unchanged-title cache as every other provider.
Direct Codex ttys never enter the pid cache.

## A Codex row carries no origin line

`BOB_COMPANION_ORIGIN` (`origin.py`) is stamped into the environment of every
terminal Dark Army opens, and it reaches a row by riding back on the hook stream:
`NOTIFY_SCRIPT` puts it on every message, `_update_session_state` stores it,
and `_enrich_agent_stubs` publishes `origin_by` / `origin_card` /
`origin_line` from `_session_states`. **Codex runs no `dark-army-notify`**
— it is a roster-only row, read out of its own journals — so nothing ever
writes `origin` for it, and a Codex session Dark Army started shows the same absent
line as one a person opened by hand.

This is a **stated limit, not an oversight**. Closing it would mean either
inventing the attribution from the board join the stamp exists to pre-empt
(which lands a frame late and is the exact guess this design refuses), or
reading a foreign process's environment — a capability Codex's own contract
withholds everywhere else on this page. The honest default holds: **a missing
stamp draws nothing**, so a Codex row is unlabelled rather than wrong. The
card the session is on still names it through the ordinary board join once
`_reconcile_board` binds it.

## Navigation has its own private proof

**Navigation has its own private proof.** `CodexNavigationProof` holds the
immutable root identity, exact canonical path/device/inode, native PID,
executable, creation time, argv and tty. `resolve_navigation_proofs` uses
the double-observed file holder, rejects competing/unreadable native
candidates and shared root/PID/tty ownership, and never consumes the title
resolver's weaker fallback. Fresh psutil objects defeat cached creation
times. The immutable map is replaced on the existing snapshot executor
path with generation checks; refresh/Hide prune it. Only `can_jump` is
published: `pid`, `process_identity`, Stop and typing stay unchanged. Navigation
alone does not grant Close; a human acknowledgement additionally needs the
stopped-turn proof below.
Forward Jump and reverse Dark Army revalidate off-loop under a five-second total
deadline. Reverse matches each Codex candidate separately, checks ancestry
before tty fallback, and accepts one id only: conflicting shell/tty or a
competing Claude/Grok match opens the plain panel. The same proof supports
existing add/attach attribution, never completion or channel authority.

## Planning completion has one separate close permission

**Planning completion has one separate close permission.** Successful
Codex `attach_plan_by_session` mints a private in-memory receipt for one
card, canonical project/plan/journal, root and turn, expiring after ten
minutes. Two distinct attachments make it ambiguous. The authenticated
loopback-only `close_refinement_terminal` action verifies that unchanged
Backlog card and exact native journal holder, twice, off-loop under an
eight-second deadline. It permits the final running tool command while
refusing a new/unknown turn, questions, permissions and live children;
retained completed child journals are not live work. Successful task-path spawn
or follow-up receipts stay busy until a unique matching direct child has an
observed new turn started after the successful request call and a completion
with that same turn ID. A delayed acknowledgement cannot revive a fast finished
new turn; an old turn ending after acknowledgement cannot settle queued work.
Legacy UUID spawns without follow-up keep their existing completion behavior. Missing, ambiguous,
malformed or incomplete journals refuse close; later commentary is not proof
of completion. This check applies to every known descendant and to fresh
journals at final validation. Start and board writes
are serialized through dispatch then board locks during final disposal.
Bridge 0.1.12+ requires unique ancestry **and** matching shell tty with fresh
membership/process checks; there is no legacy fallback. The receipt is
consumed before sending. Only literal matched/closed true tombstones the
root and records the verified native pid/create-time; no signal or Stop
confirmation is scheduled. Unconfirmed transport is never retried. This
private exception changes no generic Close, Stop, typing or navigation flag,
no snapshot field, persistence or mobile/channel/MCP permission. A tiny OS
validation-to-disposal race remains across the two processes. The `/ship`
close-out helper requests it only under `--close`, on the person's request,
or under `--plan` when the board names the session a card's planning run.

## Control is published per row, never inferred

**A person can acknowledge and close a finished native root.** The existing
`close_terminal` action with `by_person=True` may close an ordinary fresh Codex
CLI session without a plan attachment. `can_close` uses the existing cached
exact navigation proof, affirmative matching turn completion, no questions,
permissions or unresolved/active descendants, and one compatible project
editor connection (0.1.12+). Snapshot enrichment does not reparse journals or
scan processes per row. Navigation alone, silence, an unknown turn and a public
PID grant nothing; `can_stop`, typing and channel capabilities remain unchanged.

The press captures record generations, root, turn and descendant identities,
then reuses `refinement_close_observation(require_stopped=True)` twice under
the existing dispatch/board locks and eight-second bound. Complete journals,
finite ordered start/end times and the same terminal turn ID are required for
the root and every known descendant; new input/tool work invalidates prior
completion even before `task_started` arrives. The exact native holder and
journal revisions must survive both scans. Disposal uses the existing strict
addressed bridge operation, never the generic PID/tty fallback. Only literal
matched/closed true retires the row and permits the existing human card-finish
step; refusal or lost reply sends no retry, signal or `/clear`. This changes
neither explicit-resume controls nor private automatic refinement receipts.
Agent close-out, board Done/delete automation and absent `by_person` gain no
new native-close authority. A small cross-process validation/disposal race
remains, as with private refinement close.

**Control is published per row, never inferred.** `codex_rollouts.py`
attaches a process only to a root `codex-tui` / CLI / user thread in the
exact cwd. A candidate must be the canonical native executable whose
basename is exactly `codex`, with `.app/Contents`, `app-server`,
`code-mode-host`, child journals and inaccessible process fields all
failing closed. An exact native `codex resume <thread-id>` match is
`explicit_resume` and publishes Jump + Stop; one root plus one native
process in the cwd is only `unique_cwd` and publishes Jump + Hide,
**never** Stop; an exact navigation proof also grants Jump while keeping
the public PID null. A third rung, `nearest_start`, pairs the rollouts and
the native processes a single folder holds by nearest *preceding* start
time, mutually exclusively — the ordinary state of a project dispatched
into more than once inside the live window, which neither rung above can
resolve. It **publishes no control at all**: `matching_process_identity`
admits only the two kinds above, so a `nearest_start` PID is attribution
data — which Codex thread a board verb came from, and whether a row came
out of the terminal Dark Army opened — and never a button. Unproven plain, app and IDE roots are Hide-only;
finished and child rows have no actions. Every row carries explicit
`can_stop` / `can_jump` / `can_hide` / `can_resume`, decoded by the panel
with false defaults — **PID is data, never UI authority**. Stop reopens
the PID and re-checks executable, cwd, creation time and argv before
SIGTERM and again before any SIGKILL. Hide signals nothing and deletes
nothing: `_hidden_codex` suppresses only the current `(path, (mtime_ns,
size))` revision. **No process, no live row.** A `_safe_cli_root` record
whose `process_seen` is False is dropped from `_codex_records` and
tombstoned (`"no process"`). `process_seen` is a weaker observation than
`pid` / `process_identity` — never a capability, never published as one.
Presence requires a stable native process explicitly resumed into this
thread, or holding its exact canonical root journal (path + device/inode)
across two observations. cwd alone, Node launchers and child journals
prove nothing. One process scan serves `PROCESS_SNAPSHOT_TTL_SECONDS`
(fresh for new roots, Jump); a healthy empty scan sets False. Failed
enumeration, unreadable facts, conflicting ownership or changing
process/journal identity leave None: known-live roots stay, retired
ones stay retired. App/IDE roots remain unknown. Journals are found
within 15 minutes.

## A terminal Dark Army closed stays gone

**A terminal Dark Army closed stays gone.** `_closed_ids` (sid → (monotonic,
pid, create_time)) is a short-lived note of every run Dark Army ended by closing
its terminal — Stop does not stamp it. `_on_agent_records` skips a noted
id; `_refresh_codex_records` skips one unless *this thread* has come back
with a process that is not the one just closed (a new pid/create_time, or
an `explicit_resume` of this thread-id that is not the closed identity),
which pops the note. `process_seen` alone does not count — the process
Dark Army closed may still be observed while exiting. `_collect_agent_stubs`
will not emit a live Codex stub for a noted id. That proof also retires
whatever tombstone the sid holds, `"closed"` included: keeping it would
block the retirement branch from writing a fresh one when the resumed run
dies. An *undecidable* `process_seen` (one failed `_process_snapshot()`
tick) never readmits an already-retired root — failing open means never
retiring on no evidence, not un-retiring on none. Grok already has
`_grok_ended` and is not given a second gate. The note expires on
`FINISHED_RETENTION_SECONDS`. Generic typing, Wrap up, permission answers,
a Codex resume-copy action and app/IDE control remain unsupported —
`can_resume` states the signal, not a verb Dark Army offers.

## Replies to verified stopped native sessions

With the existing `typed_reply` preference enabled (default off), a native root
with exact private navigation proof and an affirmatively completed turn may
publish `channel=true`, `reply_via="typed"`. `can_type` remains false: Codex
questions are read-only here; permission asks are shown through Codex's own
`PermissionRequest` hook (`docs/cli-permission-modes.md`). Reply bypasses Claude channels
and generic terminal typing completely. The root, journal, turn, descendants,
provider identity, preference and immutable snapshot generations are checked
again before preparation and immediately before the single addressed POST.
Unresolved or active helpers, ambiguous ownership and questions refuse it.

One compatible project editor bridge (0.1.19+) must repeat unique ancestry,
matching tty and unchanged terminal membership/shell PID twice. It sends one
Ctrl-U + plain text + Enter operation, without focusing or disposing the tab.
The line must contain at most 2000 characters: no multiline/control characters
or leading `/!#` commands. Both ends validate. This replaces any existing input
line. A small cross-process validation/write race with a person typing remains.

An in-memory session/turn attempt is consumed immediately before the
asyncio stream write; a double click, missing response or timeout cannot retry
that turn. Pre-write validation failures consume nothing, and canceled read-only
workers cannot send after timeout. The bridge also checks the originating
request is still connected and its absolute deadline has not passed at the
final synchronous send; a moved or replaced journal never rearms the same turn. `matched=true, sent=true` means the editor
submitted input, **not** that Codex accepted or consumed it. No receipt, board
completion, public PID, Stop permission, mobile verb or MCP capability is added.

## Native review helpers

An explicit `source.subagent` (including the native string `"review"`) always
stays a child, even with missing or invalid parent metadata. Top-level and
legacy nested `parent_thread_id` must agree; self-parenting, cycles and project
mismatches cannot attach content or control. The copied `session_id` is never
parent identity. Children do not become independent waiting rows or pipeline
claims, and completed review helpers do not count as live helpers.

Completed explicit review children remain under their root as `review_reports`
within the existing journal retention window: four reports, 16 KiB UTF-8 each,
64 KiB combined. Truncation and omitted report counts are explicit. This does
not overwrite the parent's output or invent specialist history. The desktop
renders complete bounded review objects as separate findings and a conclusion;
partial/malformed JSON remains selectable raw text with newlines. Report paths
are labels, never opened or fetched. Report identity grants no actions. Hide in
detail uses the selected row's existing `can_hide` and revision-scoped route.

Source builds do not update the installed daemon or editor processes. This
change has no live delivery demonstration until a separately authorized install
and a disposable-session trial.

## `codex_rollouts.py`

**`codex_rollouts.py`** — the ownership rules are stated once in the
sections above; this file is where they live. A
standalone `<recommended_plugins>…</recommended_plugins>` injection
counts as synthetic title input **only** when nothing but whitespace
follows the close. A bounded tail read of `~/.codex/session_index.jsonl`
overlays Codex's newest non-empty `thread_name` on the deep-copied
rollout record, after the rollout cache. Missing, malformed and older
unindexed sessions keep the first real user prompt; Dark Army never opens
Codex's internal SQLite state.
Tool payloads accept object/JSON-string `arguments` and legacy `input`.
`turn_aborted` ends the observed turn and clears tools/questions; a newer
turn reopens it, and a named old turn's completion cannot close the new one.
Pending `request_user_input` and `request_user_input_async` publish the shared
question shape, keyed by call id. The latter's measured title/string-option
payload is normalized without inventing a question from prose. Its native
`accepted:true` output is registration, not a human answer: pending survives
acknowledgements and unknown outputs. Matching answers/cancellation, a real
user reply, a new turn or an aborted turn release it; synthetic context does not.
Normal `task_complete` / `task_completed` releases synchronous questions only:
an async question remains queued in the native client after the assistant ends
its turn. It stays on the row with all options and continues to block stopped-turn
reply/close authority. Observed on 23 Sep 2026: the async call and `accepted:true`
at 13:22 preceded `task_complete` at 13:24, while the client still showed one
queued question. `test_async_question_outlives_completed_turn` replays that
sequence with synthetic content through the shared snapshot, including later
answer, cancellation, new-turn and abort cleanup.
Named stale results cannot clear a current question. Alias and cancellation
coverage remains synthetic where native evidence is unavailable (fixture
provenance and the dated validation ledger distinguish these cases).
Questions win over helper activity in the shared category, granting no answer
transport. Assistant summaries/actions reuse `session_stats`' marker rules.
The shared `_work_report` extractor retains `last_report` independently of
latest prose, across tool/chatter and synthetic context, until the next real
user prompt. Finished rows retain it, and the work-record collector prefers
that report to the latest commentary.

Successful spawn receipts join by returned child id or the native task path
matched to child `agent_path`. Canonical spawn `agent_type`/child `agent_role`
identify the role; task names and nicknames do not. `observed_roles` is an
internal bounded ordered history, projected through the existing
`BoardStore.record_agents` writer into durable `agent_trail`/`crew_trail`.
Completed, interrupted and failed children release live helper rows; a newer
explicit follow-up can reactivate the same role. A native relative follow-up
uses its successful result's canonical `task_name`, never a guessed alias.
Active descendants remain visible through completed ancestors; only actual
live helper rows propagate, and observed descendant roles remain in history.
The fold is independent of file discovery order. Completed-only history cannot
make a finished parent look busy. No new public field, timer or progress counter
is added.

Refine's Codex prompt names `.agents/skills/ship/SKILL.md` in the project and
planning-only mode; Start begins with `Plan:` and implements that file without
restarting the interview. Local skills and the rendered pack require a 0–3
question assessment, answers/assumptions handoff, real role names and visible
stage starts/results. Missing question tools use one plain question and the
existing waiting-summary marker in the original session. Attachment is first;
only confirmed no-refinement attribution permits a new card. Completion calls
available `dark_army_close_card` only after every gate with no outstanding manual
work, reporting the actual column/refusal. Without `dark_army_needs_manual_check`,
manual steps are named in `## Work done` and the card stays open; no flag is
claimed. Tool availability lasts for the native session lifetime.

Codex rows publish a bounded `interaction_note` explaining replies/questions
in the original session. Desktop `StdoutPane` and phone `AnswerBox` render it
verbatim, replacing their generic cannot-type caption; capabilities remain
the only authority for controls.
