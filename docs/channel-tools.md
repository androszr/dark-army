# The channel's inbound verbs

The ten tools `channel_server.tools_for_host` advertises, in full. Lifted out
of `CLAUDE.md` for the reason the other contract documents were (6 Sep 2026):
that file is the current contract and is held under 30,000 UTF-8 bytes
(`host/tests/test_claude_md_size.py` pins the ceiling), and this is the
per-verb argument behind three lines of it. Nothing here is new; every
paragraph was `CLAUDE.md`'s and is reproduced unchanged.

`tools_for_host` is the single capability boundary and `call_tool` re-checks
it, because omitting a tool from `tools/list` is not a guard. Claude gets all
ten; `HOST_CODEX` gets `dark_army_add_card` + `dark_army_close_card` +
`dark_army_attach_plan` + `dark_army_attach_report` + `dark_army_needs_manual_check`
+ `dark_army_next_card` — the six that write a board row and type nothing, all
attributed by `_board_request_session_fresh`; `channel_install`
registers Grok for none of them, so a Grok session reaches none.

Each verb is scoped so that winning the attach race gains an attacker nothing
they did not have:

- `dark_army_add_card` — writes a card attributed to the calling session and can
  start nothing. The schema has **no column field at all**: every
  agent-written card lands in `prep`. Its `root` comes from
  `_session_place()`, which reads the **agents snapshot** (`_session_states`
  has no `cwd` key); a request that does not resolve to a session is
  refused. **Never repeat the tool's own words about where the card went** —
  an already-running session holds the `initialize_result` its channel copy
  was written with; the column is `prep`, always — **unless the call
  carries `plan`**: that path is validated exactly as `dark_army_attach_plan`'s
  is, before anything is written (a bad path files no card), and then
  attached to the card this call created, by id, so the card lands in
  Backlog. No ladder, so a session filing several plans — or a note first —
  never meets the ambiguity refusal. Nothing more than add + attach already
  allowed; a fresh card has no `start_when_planned`, so nothing starts. A
  plan named only in `notes` leaves a Prep card whose Start is a planning
  run nothing can finish (the fit-app stall of 23 Sep 2026). `depends_on`:
  `docs/card-dependencies.md`.
- `dark_army_close_card` — moves a card to Done, bought back with **scope**: **no
  `card_id` property**, `_handle_board_close_request` resolving the card
  solely from `_channel_session(port)`, refusing on no attribution, no card,
  more than one open card, or a card already in Done. Reachable from an
  ordinary dispatched session: `_channel_session` does not consult
  `is_channel` — the gate `_channel_for_session` applies to *pushes*.
  **In a batch the rule is positional** (25 Sep 2026): a batch-implement
  session is bound to every card it has worked, so the batch's current card
  is the one bound `live`; a member the session moved past wears `ended`
  and is not counted (`_narrow_batch_open`); the next member is the lowest
  `batch_rank` still waiting. The schema is still `{note}`, and a flag after
  the close but before the next call picks the newest Done card of the batch.
- `dark_army_attach_plan` — a refinement session attaching the plan it wrote,
  moving its card Prep → Backlog. Same no-`card_id` shape;
  `attach_plan_by_session` resolves in a two-rung ladder (the Prep card
  whose `refine_session_id` is the caller; failing that, exactly one Prep
  card the caller authored with no plan yet **within
  `ATTACH_AUTHOR_WINDOW_SECONDS`**), ambiguity and absence failing closed.
  The path is validated on the executor: realpath-contained inside the
  card's own `root`, `.md`, a real file, ≤ `board_workflow.MAX_PLAN_BYTES`.
  **The batch rung** (25 Sep 2026): when rung 1 finds several cards sharing
  one non-empty `batch_id` — one `refine_cards` press bound them all to this
  session — the plan file's own `- **Card:** <id>` header
  (`board_workflow.read_plan_card`, read inside the shared root after the
  path check) picks the card through `_pick_batch_member`, an exact id
  match within that set; no header, or one naming any other card, fails
  closed listing the members (`BATCH_ATTACH_AMBIGUOUS_REFUSAL`). One
  candidate never reads the header. **A session bound to several cards
  names the member through a file, never the tool call** — the schema is
  still `{path}` — and the batch-implement sibling names no card either: it
  binds one card at a time, a positional rule (the close bullet below).
- `dark_army_attach_report` — a scout session attaching the report it wrote.
  Same no-`card_id` shape; `attach_report_by_session` resolves the one
  open card `by_session`. The card stays In progress; closing it is
  `dark_army_close_card` with the report's path in the note. The path is
  validated on the executor the same way as a plan. A report whose
  resolved path is under the card root's `scout/` folder must also pass
  `scout_report.check` (`host/dark_army_daemon/scout_report.py`, the
  answer block and the five headings of
  `.claude/skills/scout/references/scout.md`), or it is refused with
  `board.REPORT_MALFORMED_REFUSAL` followed by what is wrong and the
  checker's command; a report anywhere else in the root (an older prose
  report under `docs/research/`) attaches unchecked. The store keeps the
  resolved absolute path: the folder is git-ignored, so only the main
  checkout holds the file. The attach also reads the answer block once, on
  the executor (`scout_report.read_header`), and stores its verdict and
  recommendation beside the path (`report_verdict` /
  `report_recommendation`); a report with no block stores empty values.
- `dark_army_needs_manual_check` — the same session as `dark_army_close_card`, saying
  the opposite thing about the same card: same no-`card_id` shape, same
  resolution, same four refusals. There is no verb for *clearing* it; the
  only route out is `board_manual_clear` on the API and **Mark checked** in
  the sheet (`manual_steps` is absent from `_BOARD_FIELDS`). The panel
  draws an amber `MANUAL CHECK NEEDED` line **above** the title, which is
  **never rewritten**. **The line waits its turn**: the note is stored at
  once, but the badge is drawn from `manual_check_due`, published per card
  beside `work_active` / `run_active` from `_card_session_working`
  (`dispatching`, or `live` with the session in the agents snapshot's
  `running` bucket **and** in `_claiming_session_ids()`) — a Swift-side join
  would hide the badge on the finished-and-quiet card that most needs it. A
  category flip writes no board row, so `_reconcile_board` ends with a drift
  check over the flagged cards alone (`_manual_due_seen`). The **sheet**
  draws the steps and **Mark checked** unconditionally.

  **The check is a file, and the flag names it** (25 Sep 2026, the *manual
  check folder* plan). The tool takes an optional `path` beside `steps`: the
  check the session wrote at `manual-check/<YYYY-MM-DD>-<slug>/check.md` —
  an answer block (`Card`, `Project`, `Check`, `Created`, `Status`,
  `Outcome`, `Checked at`), then `## Steps` and `## Why not automated` —
  checked by `python3 .claude/skills/ship/manual_check.py`
  (`host/dark_army_daemon/manual_check.py`, byte-copied). The daemon
  resolves the path inside the card's root and requires
  `<enrolled root>/manual-check/<folder>/check.md` — the enrolled project
  the card's root belongs to, exactly three segments, `manual-check` and
  `check.md` in that exact case, `_manual_check_home`, the one rule the
  list and the press share (else `board.MANUAL_CHECK_PLACE_REFUSAL`); a
  component typed in a different case is taken from the directory only when
  `os.lstat` of both spellings names one `(st_dev, st_ino)`
  (`_canonical_path`), and a typed name that does not exist stays as typed
  and is refused by the checks that follow, so a case-sensitive volume never
  has a different folder or a link swapped in — and a
  clean check (else
  `board.MANUAL_CHECK_MALFORMED_REFUSAL` and the problems), and
  `flag_manual` stores the realpath in `manual_check_path` in the same
  UPDATE; no path flags exactly as before and clears a stale link. **The
  order is flag, then close**: a card with an open check goes to Done, and
  the description says so. The flag may also follow the close —
  `flag_manual` accepts a Done card (the WHERE still pins session and
  column), and `flag_manual_by_session` falls back to the session's one
  Done card it closed itself (`closed_by`), refusing two in words. The
  person records **Passed** / **Failed** from either app
  (`board_manual_outcome`, `docs/transport-contract.md`), which writes the
  file's three status lines and clears the steps; a press on a file that
  already carries an outcome is refused in words and still clears the
  cards flagged with it. **Mark checked** stays for a card flagged with
  steps alone, and comes back on a card whose file the daemon will no
  longer serve (moved, edited out of shape), so no badge is stranded.
- `dark_army_next_card` — a batch-implement session saying it has closed (or
  is leaving) the card it is on. **No properties at all**:
  `_handle_board_next_request` resolves the session from the port
  (`_board_request_session_fresh`, open to Codex like the close), marks the
  open card it was on `ended` — finished-but-not-closed work for a person —
  and binds the session to the next waiting member (`bind_session`, In
  progress), re-running each member's own rungs (vanished plan, enrolment,
  `dispatch.guard` without the launch bounds, dependencies); a member that
  fails leaves the batch with its refusal on it. It starts nothing: the
  person chose every member at the press. What a forger who reaches the
  port achieves is this session skipping ahead in its own batch. The reply
  names the next card's title and plan, or says the batch is finished.
  Only the batch's owning session (`_batch_owner`, its lowest-ranked bound
  card) may walk or release it; a marked card outside Backlog is refused a
  single Start until Leave batch. Each member's stage trail and fix rounds
  are read inside its own window — from its `dispatched_at` (the press for
  the head, the advance's bind for a later member) to its `done_at` or the
  advance that left it — off the session's timed spawn log
  (`subagent_spawns`) and the transcript's per-spawn `spawns` list; a
  member the session leaves unclosed is recorded once more at the advance;
  Codex keeps no timed record, so a Codex session on a second card records
  no trail and shows no fix rounds — withheld, never wrong. A spawn between
  a close and the next call falls in no window: a track short by one, never
  a wrong one.
- `dark_army_answer_card` — one message onto the thread of the card Dark Army just
  asked about. Same no-`card_id` shape; changes nothing else.
  **In a batch it answers the card the session is on** (29 Sep 2026): the open
  cards are narrowed by the same positional rule the `dark_army_close_card`
  bullet states, so a member the session moved past is not counted; the consult
  rung before it and the one-Done-card rung after it are unchanged.
- `dark_army_knowledge_read` / `dark_army_knowledge_write` — **the project's own
  question-and-answer notes**, in full in `docs/knowledge-notes.md`; what
  must hold: `knowledge_store.py` is schema 20's one table, keyed on the
  enrolled **root** and not a card, in neither `_WRITABLE` nor
  `_BOARD_FIELDS`, with no all-roots read; **the scoping is an omission** —
  neither schema names a project and `_knowledge_place` resolves the root
  from the caller alone; the write's `key` rides the wire as **`note_key`**,
  because `key` is the enrolment key `_enrolled_root` reads; both types are
  on `CHANNEL_MESSAGE_TYPES`, on `_route_channel` **and** in
  `_handle_message`'s reply-on-refusal tuple; and **nothing rides SSE** —
  the reader is the session that asked. `render_knowledge` prefixes `STALE`
  when `stale=='1'` and `UNCONFIRMED` when `last_confirmed` is missing/0;
  the `[key]` token stays; cap semantics are unchanged (`break` not
  `continue`, omitted keys named, at least one entry). No new tool args.

- `dark_army_request_start` — **Mission Control asks the person to start
  a card, and it starts nothing.** Claude only; the one verb whose schema
  takes a `card_id`, and safe for that reason: `BoardVerbsMixin.ask_start`
  refuses every caller but the Mission Control session
  (`_board_request_session_fresh` against `mission_snapshot()`), refuses
  a card `dispatch.guard` would refuse on column, session or refinement,
  and otherwise records an in-memory ask (at most `MAX_START_ASKS`, one
  hour, dropped on a daemon restart). The card is then published with
  `start_ask_id` / `start_asked_at` / `start_asked_by` while it could
  still take a Start, and both apps list it on Needs you as ANSWER, wire
  kind `start_asked`. The entry opens the card; the person's own press on
  START (`dispatch_card`, every guard) is the start and ends the ask, and
  Dismiss (`inbox_ack`, kind `start_asked`) drops it. A forger who reaches
  the port gains an entry somebody dismisses. Pinned by
  `host/tests/test_start_ask.py` and `test_phone_inbox.py`.

## Two names, one script

The dual-name window (23 Sep 2026). The server was registered as `bob` on
every enrolled machine and baked into every running session's
`initialize_result`, so it cannot simply be renamed: a session born under the
old name holds the old tool list until it exits. So one installed script,
`~/.dark-army/dark-army-channel`, answers to either name, and
`channel_install` registers it twice at user scope — both names removed
before either is added (`bob` removed first), so the previous build's flagless `bob` never sits
beside the new `dark-army` (the cost is a brief window with no board tools;
a remove that fails for any reason other than the name not being registered
stops the install before any add, and because `bob` goes first a failed
remove never leaves only the passive `bob` registration); the adds then put `dark-army` first:

- `dark-army` — `--name=dark-army`, tools `dark_army_*`,
  `<channel source="dark-army">`, launch line
  `claude --dangerously-load-development-channels server:dark-army`.
- `bob` (legacy) — `--name=bob`, tools `bob_*`, `<channel source="bob">`,
  launch line `… server:bob`. The build before the window registered the
  script with no flag, and that registration *is* the `bob` one, so
  `name_from_argv` reads an absent or unknown `--name=` as `bob`.

The same ten verbs, spelled for a session born before the rename:

- legacy `bob_add_card`, `bob_close_card`, `bob_attach_plan`, `bob_attach_report`;
- legacy `bob_needs_manual_check`, `bob_answer_card`, `bob_knowledge_read`, `bob_knowledge_write`;
- legacy `bob_request_start`, `bob_next_card`.

User scope means the harness spawns **both** copies in every session, and
exactly one may speak (`channel_server.is_active`): the copy whose name the
channel flag's own arguments name (`server:<name>`, or a plugin's
`<plugin>:<name>@<market>`), or `dark-army` when the flag names neither of
ours. `launch_names` reads only those arguments — `--flag X [Y …]` while each
token is a whole entry, and `--flag=X`, comma-separated — never the rest of
the command line, so a prompt that says `server:bob` wakes nothing
(`is_channel` follows the same parse). The line comes from `ps`, which has
already joined argv with spaces, so it is split on whitespace only — a quote
in it is prompt text, and an apostrophe must not hide the flag. A **flagless** process is different: it
is the build before's lone `bob` registration, with no `dark-army` sibling —
a session opened before the install whose helper restarts, or one opened
between the script write and the `dark-army` add — so it is active unless the
launch line names `dark-army` (`name_flag_present`); an explicit `--name=bob`
keeps the sibling rule. The other copy is **passive**: its handshake declares tools only (no `experimental`,
no `instructions`), `tools/list` is empty, `tools/call` answers
`tool unavailable (passive copy of <name>)`, and it opens no socket, sends no
heartbeat and relays no permission prompt. Two reasons it stays silent
rather than merely redundant: two tool lists would double every verb in the
model's context, and `_attach_is_displaced` refuses a second port claiming a
session the first already holds — so a second attach would leave one copy's
tools unattributable. A launch line naming **both** names makes both active;
that is the one shape the window does not serve, and the fix is never to let
the daemon accept two ports.

A process offers only its own prefix: a `dark-army` copy answers
the legacy `bob_add_card` as unknown and the other way round. The attach carries the
registered name in its `"channel"` key; the daemon keeps it on the registry
entry (`"name"`, legacy when absent or unknown, on no snapshot) so
`ask_card`'s direct push names the answer tool that session has
(`daemon_board.ask_direct_tail`). A consult helper is a new session and is
told `dark_army_answer_card`. Codex is registered once, as
`dark-army-board` (`--host=codex --name=dark-army`); the old
`bob-companion-board` entry is removed only when it is exactly the one the
previous build wrote, and a foreign one is left with a warning. The
editor's `darkArmy.showThisSession` keeps `bobCompanion.showThisSession` as a
registered, uncontributed alias.

The window ends with the follow-up *End the channel dual-name window*: drop
the `bob` registration, `LEGACY_NAME`, the passive copy and the alias, once
no running session was born under `bob`. Nothing expires on its own.
