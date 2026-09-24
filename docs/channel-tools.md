# The channel's inbound verbs

The eight tools `channel_server.tools_for_host` advertises, in full. Lifted out
of `CLAUDE.md` for the reason the other contract documents were (6 Sep 2026):
that file is the current contract and is held under 30,000 UTF-8 bytes
(`host/tests/test_claude_md_size.py` pins the ceiling), and this is the
per-verb argument behind three lines of it. Nothing here is new; every
paragraph was `CLAUDE.md`'s and is reproduced unchanged.

`tools_for_host` is the single capability boundary and `call_tool` re-checks
it, because omitting a tool from `tools/list` is not a guard. Claude gets all
eight; `HOST_CODEX` gets `dark_army_add_card` + `dark_army_close_card` +
`dark_army_attach_plan` + `dark_army_attach_report` — the four that write a board row and
type nothing, all
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
  run nothing can finish (the fit-app stall of 23 Sep 2026).
- `dark_army_close_card` — moves a card to Done, bought back with **scope**: **no
  `card_id` property**, `_handle_board_close_request` resolving the card
  solely from `_channel_session(port)`, refusing on no attribution, no card,
  more than one open card, or a card already in Done. Reachable from an
  ordinary dispatched session: `_channel_session` does not consult
  `is_channel` — the gate `_channel_for_session` applies to *pushes*.
- `dark_army_attach_plan` — a refinement session attaching the plan it wrote,
  moving its card Prep → Backlog. Same no-`card_id` shape;
  `attach_plan_by_session` resolves in a two-rung ladder (the Prep card
  whose `refine_session_id` is the caller; failing that, exactly one Prep
  card the caller authored with no plan yet **within
  `ATTACH_AUTHOR_WINDOW_SECONDS`**), ambiguity and absence failing closed.
  The path is validated on the executor: realpath-contained inside the
  card's own `root`, `.md`, a real file, ≤ `board_workflow.MAX_PLAN_BYTES`.
- `dark_army_attach_report` — a scout session attaching the report it wrote.
  Same no-`card_id` shape; `attach_report_by_session` resolves the one
  open card `by_session`. The card stays In progress; closing it is
  `dark_army_close_card` with the report's path in the note. The path is
  validated on the executor the same way as a plan.
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
- `dark_army_answer_card` — one message onto the thread of the card Dark Army just
  asked about. Same no-`card_id` shape; changes nothing else.
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

## Two names, one script

The dual-name window (23 Sep 2026). The server was registered as `bob` on
every enrolled machine and baked into every running session's
`initialize_result`, so it cannot simply be renamed: a session born under the
old name holds the old tool list until it exits. So one installed script,
`~/.dark-army/dark-army-channel`, answers to either name, and
`channel_install` registers it twice at user scope — both names removed
before either is added, so the previous build's flagless `bob` never sits
beside the new `dark-army` (the cost is a brief window with no board tools),
then `dark-army` first:

- `dark-army` — `--name=dark-army`, tools `dark_army_*`,
  `<channel source="dark-army">`, launch line
  `claude --dangerously-load-development-channels server:dark-army`.
- `bob` (legacy) — `--name=bob`, tools `bob_*`, `<channel source="bob">`,
  launch line `… server:bob`. The build before the window registered the
  script with no flag, and that registration *is* the `bob` one, so
  `name_from_argv` reads an absent or unknown `--name=` as `bob`.

The same eight verbs, spelled for a session born before the rename:

- legacy `bob_add_card`, `bob_close_card`, `bob_attach_plan`, `bob_attach_report`;
- legacy `bob_needs_manual_check`, `bob_answer_card`, `bob_knowledge_read`, `bob_knowledge_write`.

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
