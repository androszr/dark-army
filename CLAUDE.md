# CLAUDE.md

Project guidance for coding agents. **This file is read completely by every
agent, on every run, and it stays under 30,000 UTF-8 bytes**
(`host/tests/test_claude_md_size.py`). It states the universal rules, the
architecture map and where each subject's full contract lives; the detail
itself is in the subject documents under `docs/`, which `## What to read`
below selects. Plans hold history; this file states the current contract;
replace obsolete facts when adding, and put detail in the subject document,
never here.

## Project Overview

**The product is called `Dark Army`.** A bare proper noun, no article
("Dark Army is not allowed to start sessions", never "the Dark Army"),
possessive `Dark Army's`, **no short form** — never `DA`, `Army` or
`Dark Army Companion`. It is the name in every word a person or a model
reads — labels, docs, comments, docstrings, briefs, tool descriptions,
hints, log lines — and in every folder and file (`host/dark_army_daemon`,
`host/dark_army_menubar`, `/Applications/Dark Army.app`, `~/.dark-army`,
the log, the LaunchAgent, the `dark-army-*` helpers). `bob` survives only on
a **closed list** of identifiers that cannot move without breaking an
installed machine: the channel's `bob` / `server:bob` / `bob_*` /
`<channel source="bob">`, kept beside `server:dark-army` for the
dual-name window until *End the channel dual-name window*;
`X-Bob-Token` / `-Frame` / `-Channel` and
`x-bob-companion-authorization`; `bob-tldr` / `bob-actions`; the bundle ids
`com.bob-companion.menubar`, `com.bob-companion.panel`,
`com.robertandrosz.bobphone`; `BobPanel`, `BobPhone`, `BobPhoneWidget`,
`BobFleetWidget`, `BobFleetTile`; the legacy key folder `.bob-companion/key`
(read behind `.dark-army/key`); the `BOB_*` variables; `bob-companion-board`
(migration) and the hidden `bobCompanion.showThisSession` alias;
`# managed by Bob Companion`; the `card_messages` author `bob`; the GitHub
repository's name; the `~/.bob-companion` link, permanent on a migrated Mac
and why every key walk-up skips the home folder
(`docs/first-run-checklist.md`, *The `~/.bob-companion` link*); and internal
symbols (`BobDaemon`, `BobCompanionApp`, …), which reach no person.
`host/tests/test_product_name.py` holds the list. The shell-style prompt
line on the panel, the phone and the widget names the host `darkarmy` (one
word: a hostname holds no space), and the surface-string test catches a
bare lowercase `bob` too.

Dark Army monitors Claude Code sessions: a Python daemon reads hooks,
tracks live sessions and drives an animated pixel-art menu-bar strip and a
native panel with still-photo agent portraits.

Two active components: **host** (Python daemon, Claude Code hook handler,
macOS menu bar app) and **panel** (SwiftUI/AppKit, reading the daemon's local
HTTP + SSE API). A paired iPhone app (`ios/BobPhone`) polls the same state
over a second LAN door and may act through a chosen list of writes; pairing
is the write permission.

**No rendering surface:** the platform draws what it can.

The Kanban **board** is ordinary SwiftUI inside the `BobPanel` process
(`panel/Sources/BobPanel/BoardView.swift`) — the window's left pane, keeping
`Lifecycle.swift`'s promise: one panel, one owner, no second pid file,
orphan watchdog or SSE reader. **The panel owns
every auxiliary window and its hide and close paths take them all down**: the
card screen (`CardWindow.swift`), the settings window (`SettingsWindow.swift`),
the pairing QR and the relay address — same process, same `DaemonClient`, no
second SSE reader; `publishSeen()` ORs the first three into the SSE gate.

**macOS only.** No `sys.platform` branches in `host/` without an explicit
decision. Dependencies: `afplay`, `rumps`/PyObjC, `py2app`.

## Build and test

Requires Xcode for the panel and Python **3.11+** for the host (`host/.venv`;
system Python 3.9 cannot install `pyobjc-core`). The reasoning behind the
strict build, the flags, the phone job and the asset pipelines is
`docs/context-development.md`.

```bash
cd panel && swift build -c release                      # the panel
cd host && ./build.sh --allow-untagged --install        # build + bundle + install; releases omit --allow-untagged
cd host && .venv/bin/pytest -q                          # the host suite (venv: python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt)
cd vscode-extension && npm run build                    # the extension
```

`--allow-untagged` bypasses the version gate only; panel and extension
freshness stay strict, and `--dev` / `--install` are never a gate. Cast art
and the app icon are **generated** (`tools/pixelgrid_ingest.py`,
`tools/menubar_cast_icons.py`, `tools/portrait_ingest.py`,
`tools/app_icon_bake.py`); never hand-edit a PNG under `assets/`.

`ruff check .` (error-class rules) and both test suites run in CI on every
push.

## Architecture

### Data Flow

```
Claude Code hooks (SessionStart/PreToolUse/PreCompact/Stop/StopFailure/
                   Notification/UserPromptSubmit/SessionEnd/Subagent{Start,Stop})
    → ~/.dark-army/dark-army-notify → loopback TCP → dark_army_daemon

    Sessions:  dict[session_id → state] → _activity_counts() / categorize()
                 → on_activity_change  → the menu-bar strip
                 → on_agents_change    → /api/state + /api/events (SSE) → the panel
               each row's `project` resolved to its VS Code workspace (workspace.py)
               persisted to sessions.json on structural changes

    Cards:     add/dismiss → _active_notifications → on_notification_change
                 → panel rows + UNUserNotificationCenter banners

    Board:     board.db → _board_snapshot() → on_board_change → /api/state["board"]
                 → the board pane + the menu bar's to-do count (Prep + Backlog)
               every card read is ordered by `CARD_ORDER_SQL` — column, then
               CAST(priority AS INTEGER) DESC, then position, then created_at
```

**A card with a run carries `run_health`** (the daemon's quantised reading
of the run, `run_health.py`) **and `run_figures`** (cost, working minutes,
context and attempts from the two retained ledgers, `run_figures.py`); both
clients draw them verbatim and never age them. In full:
`docs/context-board.md`, under `board.py` and *Cost and time on the card*.

**Where each subject's contract lives.** The table is the map; the documents
are the territory, relocated verbatim from this file on 20 Sep 2026 and kept
current there.

| Subject | Document | Long-form contracts it points at |
|---|---|---|
| The board: two write paths, `start_when_planned`, the parallel limit and the queue, claims, Done, `board.py`'s writer rings, `dispatch.py`'s six properties, `workspace.py` | `docs/context-board.md` | `docs/card-crew.md`, `docs/delivery-leads.md`, `docs/lifecycle-timing.md`, `docs/card-dependencies.md`, `docs/card-worktrees.md` |
| The Mac panel: workspace pane and rail, the process table, detail tabs and the hosted terminal, the inbox, the window, the board's drawing, the card window, drafts, markdown, areas, chatter, the API layer's three rules | `docs/context-panel.md` | `docs/panel-window-contract.md`, `docs/phone-contract.md`, `docs/agent-chatter.md` |
| The Python half: Codex observation, the hook handler, the daemon and its modules, the loopback API and the two sealed doors, enrolment, `paths.py`, titles, the menu-bar app, the session state model | `docs/context-host.md` | `docs/session-state-contract.md`, `docs/transport-contract.md`, `docs/codex-contract.md`, `docs/channel-tools.md`, `docs/knowledge-notes.md`, `docs/pty-broker-contract.md`, `docs/menubar-strip-contract.md`, `docs/kill-switch.md`, `docs/agent-pack.md`, `docs/first-run-checklist.md` |
| Building, testing, the asset pipelines, `/review` | `docs/context-development.md` | `docs/agents.md`, `docs/ship-efficiency.md`, `docs/codex-ship.md` |

## Universal invariants

The rules every change must keep, whichever subject it touches. Each is
stated in full, with its reasons, in the subject document named.

- **macOS only.** No `sys.platform` branches in `host/` without an explicit
  decision. No rendering surface: the platform draws what it can.
- **One source of truth for the buckets.** `BobDaemon._activity_counts()` /
  `session_stats.categorize()` sort every session; the strip and the panel
  read it and neither re-derives (`docs/context-host.md`, *Session State
  Model*).
- **AppKit on the AppKit thread.** Daemon-thread code reaching the menu bar
  goes through `rumps.Timer` or `callAfter`; menu-bar code reaching the daemon
  through `run_coroutine_threadsafe`; nothing blocks the AppKit thread on the
  daemon (`docs/context-host.md`, *dark_army_menubar*).
- **`NOTIFY_SCRIPT` stays stdlib-only.** It is a string in
  `dark_army_menubar/hooks.py`, written to `~/.dark-army/`, run under
  any `python3` including the 3.9 system one: no `dark_army_*` import, no
  third-party import, no 3.10+ syntax. Every walk-up for a project key
  **skips `Path.home()`** (`docs/context-host.md`, *bob-companion-notify*,
  *enrollment.py*).
- **Panel writes send `X-Bob-Token`**, never `Authorization: Bearer` — the
  desk token off the context push, never a file; the file is the session
  token. Reads are ungated, so the wrong header looks like it works. Every panel model
  decodes through tolerant helpers: one absent key must never blank the panel
  (`docs/context-panel.md`, *the API layer*).
- **A request has to be addressed to loopback**: `_loopback_host` above the
  routing table, `_authorised` with `hmac.compare_digest`, `_home_open` the
  one verifier on the sealed doors, `REMOTE_ACTIONS` never exceeding
  `LAN_ACTIONS`, and no key, digest, claim or channel id on any snapshot
  (`docs/context-host.md`, *api_server.py*; `docs/transport-contract.md`).
- **The phone's Lock Screen card is the top of Needs you, and its token is
  a secret like the push token's**: `register_activity_token` sits on both
  `LAN_ACTIONS` and `REMOTE_ACTIONS` beside `register_push_token`, the
  token rides no snapshot (`devices_snapshot` says `live_activity` as a
  bool) and the wire is a closed set of short fields, never the ask's words
  (`docs/phone-contract.md`, *The waiting agent is a Live Activity*;
  `docs/transport-contract.md`, *The buzz has a live-card leg*).
- **Destructive verbs re-check at the moment they fire**: `stop_session` by
  identity (the recorded pid is still the harness), `delete_abandoned_agent`
  by category; a verb that trusts a snapshot aims wrong
  (`docs/context-host.md`, *dark_army_daemon*).
- **Only a deliberate human gesture puts work in front of the launcher**, and
  every start verb re-runs `dispatch.guard` under `_dispatch_lock`; a card is
  never moved to Done by the daemon (`docs/context-board.md`, *dispatch.py*).
- **Who may write what is the design of `board.py`**: three rings, the
  single-writer verbs guarded in their own `WHERE`, `revision` stepping on a
  person's change only, forward-compatible reads and writes
  (`docs/context-board.md`, *board.py*).
- **Persisted shapes are forward-compatible.** `sessions.json`,
  `preferences.json`, `board.db`, `~/.claude/jobs/` are read by an older build
  after a downgrade: a new key gets a default on read, a removed key is
  tolerated, a preference key is never renamed, private files are in
  `paths._PRIVATE_FILES` (`docs/context-host.md`, *paths.py*).
- **The menu-bar strip measures itself** against `STRIP_BUDGET_PT` (310pt);
  kerning after an attachment is discarded by AppKit, so a gap after an icon
  is a real spacer character; only `work` animates
  (`docs/menubar-strip-contract.md`).
- **Cast art is generated and the roster is one list**: twenty names in
  `identity.NAMES`, mirrored in order by `Cast.names` on the Mac and the
  phone, plus `overwatch` art-only; `cast.character_for` is the panel's
  `Cast.character(for:)` rung for rung (`docs/context-development.md`, *Cast
  Pipeline*).
- **Shared rules are byte-pinned across clients, and both copies move
  together**: `BoardRowFold` (folds persisted as `board_row_flips`),
  `DetailTab` / `TerminalWhereabouts`, `CardSections`, `CardTimeline`,
  `ProviderChoice`, `Markdown`, `Areas`, `Specialists`, `AgentChatter`,
  `LedgerWeek`, `CastQuotes`, `WorkReport`, `Inbox.oneEntryPerSubject`; each
  pair's pinning test is named beside it in `docs/context-panel.md`.
- **A card outlines one verb, chosen by its column** (`CardActionWeight`,
  the board tile's rule; the phone's card screen does not read it yet):
  Refine in Prep, START in Backlog, Done in In progress, none in Done; every
  other verb is dim words, HERE included, and an absent primary promotes
  nothing — with one kind-aware exception: a scout in Prep outlines START,
  because it has no plan to refine (`docs/context-panel.md`,
  `test_card_action_weight.py`).
- **The daemon acts on a session in exactly three places** — `autocompact`,
  `alerts`, the question burst — each decided on the executor and performed on
  the loop, and every eviction routes through `_forget_session`
  (`docs/session-state-contract.md`).
- **Never commit, push, tag, install or release from an agent run** unless
  the person asked for exactly that; releases go through
  `.claude/skills/releasing/SKILL.md`. The one exception: a run inside a
  card worktree commits to its own card branch, and still never pushes,
  merges, tags, installs or releases (`docs/card-worktrees.md`). Never kill
  the pty broker (`docs/pty-broker-contract.md`).

## What to read

**This file and `AGENTS.md` are always read completely.** Everything else is
selected by `docs/agent-context.json`, a versioned map from a role, the plan's
`Surfaces:` header and the changed paths to the subject documents above, and
the rule it encodes is conservative:

- The reference set is the **union** of what the plan's surfaces select, what
  every changed or planned path selects and the cross-cutting additions the
  map names (a change to `api_server.py` reads the panel's decode rules too).
- **Uncertainty never selects less.** A path no pattern maps, a surface the
  map does not know, an empty scope, conflicting instructions or a boundary
  discovered mid-work all select the **fallback**: every subject document.
  A planner starts from the idea and its surfaces and widens on discovery;
  a reviewer derives the union from the plan and the delta by itself, never
  from the implementer's selection. An unavailable graph result does not
  widen a reviewer that already holds the delta paths.
- Selected subject documents are read **whole**; follow their long-form
  links when the task reaches that contract, not every link transitively.
  Complete instructions already in the current context count as read.
- Role briefs (`.claude/agents/*.md`) are read whole by the role. They link to
  the subject documents rather than repeating them.
- Every full-file read is **bounded**: record the path, digest and ranges
  read and reread only what is missing; a truncated tool read is an
  incomplete read, not permission to assume the rest.

`python3 tools/ship_efficiency.py inventory --root . --check` measures what
each role loads and proves every relocated paragraph still has a home;
`docs/ship-efficiency.md` is the measured contract.

## Key Constraints

- **Menu-bar width**: `STRIP_BUDGET_PT` (310pt), and **kerning after an
  attachment is discarded by AppKit** — a gap after an icon must be a real
  spacer character (`_render_strip`). Ladder in
  `docs/menubar-strip-contract.md`.
- **Notification limit**: 8 at once (oldest dropped).
- **Cast art**: two trees, stated under *Cast Pipeline* and
  `docs/menubar-strip-contract.md`; never in `host/setup.py`'s resource list.

## How a request becomes work

**The crew is `docs/agents.md`** — the `.claude/agents/*.md` files are the brief
each helper reads; that page is the one a person reads.

An explicit request to work directly or avoid skills overrides this default.
Spawned specialists perform their assigned role and return to the coordinator;
they do not start another workflow or file a card.

**Any request for a change to this codebase goes through `/ship`, whether or
not the word `/ship` is typed.** "Add X", "fix Y", "can we make Z faster" — all
of it. Ask what you genuinely need to ask, write the plan, file it on Dark Army's
Kanban board as a Backlog card, and leave the terminal for the person. Then stop. Three things
follow:

- **Do not implement in the same breath as planning.** The card is the handoff.
  A human pressing Start dispatches a fresh session into
  `/ship implement <plan path>` with a context holding the plan and nothing
  else.
- **Do not end the turn on "shall I build it?"** A turn that ends in a question puts the session under Dark Army's *Needs you*, and nothing is blocked. Leave no `<!-- bob-tldr -->` and no
  `<!-- bob-actions -->` on that last message; both mean "somebody is waiting
  on you", and nobody is.
- **Close out afterwards** — `bash .claude/skills/ship/close-out.sh` frees
  the run's baseline worktree (pass `SCRATCH`). A plan run ends on `--plan`,
  closing its own tab once the board names it a card's planning run. Other
  runs leave the tab: bare, it prints `close-out: terminal left open; …`,
  filing the row under Idle, not *Needs you*. The person closes it in Dark
  Army (Close also finishes the card) or asks in words — only then
  `--close`: the same identity checks (Grok by `GROK_SESSION_ID`, Codex by
  exact `CODEX_THREAD_ID` / `CODEX_SESSION_ID` and Dark Army's receipt,
  Claude by ancestry), one close request, never `/clear`; repeat a refusal,
  never retry it. The installed helper wins once it carries both markers;
  until the app is rebuilt, the project's copy runs.

**Both ship entries require a callable canonical roster, independent verification
and conditional integration/security reviews.** Review this run's baseline delta,
staged and new content included. Triggers are independent floors; a `BLOCK`
requires repair, affected verification and the blocking review again, outside
the bug-audit allowance. Missing roles leave work incomplete. All seven local
Codex shims reference authoritative briefs. Diagnosis and native discovery:
[docs/codex-ship.md](docs/codex-ship.md).

Three exceptions only. A **preflight BLOCK** means the plan is
not fit to file, so that one does end on a question. An **explicit instruction to
build it now** outranks the default — say in one sentence that the usual route is
the card, then do it. And **questions, explanations, one-line reads and anything
that changes no code** are not requests for a change.

## Reviewing a change

`/review` reviews a commit, a branch or a pull request; a bare `/review` asks
what to review first. Its contract — the grades, the verdict line, the three
byte-pinned copies — is in `docs/context-development.md`. Not `/code-review`,
the deep sweep that applies fixes.

<!-- gitnexus:start -->
# GitNexus — Code Intelligence

This project is indexed by GitNexus as **dark-army** (38309 symbols, 191326 relationships, 584 execution flows).

> Index stale? Run `node .gitnexus/run.cjs analyze --index-only` from the project root — it auto-selects an available runner. No `.gitnexus/run.cjs` yet? Bootstrap with `npx`, `bunx`, or `pnpm dlx` — e.g. `bunx gitnexus@latest analyze` (npm 11 npx crash; #1939).

## Always Do

- **MUST run impact before editing.** Use `impact({target: "symbolName", direction: "upstream"})` or `node .gitnexus/run.cjs impact "symbolName" --direction upstream --repo .`; report callers, processes, and risk. Never substitute grep for graph analysis.
- **MUST analyze graph changes before committing.** Use `detect_changes({scope: "all"})` (MCP) or `node .gitnexus/run.cjs detect-changes --scope all --repo .` (CLI fallback). `partial: true` or `truncated: true` is not a clean check — a zero means unseen, not unaffected; re-run it. For regression review: `detect_changes({scope: "compare", base_ref: "main"})` or `node .gitnexus/run.cjs detect-changes --scope compare --base-ref "main" --repo .`.
- MUST warn on HIGH/CRITICAL `risk` pre-edit; never use `riskSharedAxes` to waive a HIGH/CRITICAL `risk` warning. Compare File/symbol: MCP File omits axes; Graph-RAG expands File.
- **MUST treat `risk: UNKNOWN` as unresolved, not as low.** An empty caller set is not evidence the symbol is unused — it can also mean the callers are not resolvable by the index (plain-object property access, dynamic dispatch, cross-language calls). `impact` pairs `UNKNOWN` with a `riskNote` saying so. Confirm with a text search before treating the symbol as safe to change or delete; do not proceed on the strength of a zero.
- **MUST use `query({search_query: "concept"})` for concepts/flows, `context({name: "symbolName"})` for a named symbol, or `impact` for blast radius, on read-only callers, dependencies, imports, or execution flow.** Graph first; text search only for empty/`UNKNOWN`/literals.
- For security review, `explain({target: "fileOrSymbol"})` lists taint findings (source→sink flows; needs `analyze --pdg`).

## Never Do

- NEVER edit a function, class, or method before MCP/CLI impact analysis.
- NEVER ignore HIGH or CRITICAL risk warnings from impact analysis, and never read `UNKNOWN` as an all-clear — it means the walk could not answer, which is the one verdict that requires confirming by other means.
- NEVER rename symbols with find-and-replace — use `rename` which understands the call graph.
- NEVER commit before MCP/CLI graph change analysis.

## Resources

| Resource | Use for |
| --- | --- |
| `gitnexus://repo/dark-army/context` | Codebase overview, check index freshness |
| `gitnexus://repo/dark-army/clusters` | All functional areas |
| `gitnexus://repo/dark-army/processes` | All execution flows |
| `gitnexus://repo/dark-army/process/{name}` | Step-by-step execution trace |

## CLI

| Task | Read this skill file |
| --- | --- |
| Understand architecture / "How does X work?" | `.claude/skills/gitnexus-exploring/SKILL.md` |
| Blast radius / "What breaks if I change X?" | `.claude/skills/gitnexus-impact-analysis/SKILL.md` |
| Trace bugs / "Why is X failing?" | `.claude/skills/gitnexus-debugging/SKILL.md` |
| Rename / extract / split / refactor | `.claude/skills/gitnexus-refactoring/SKILL.md` |
| Tools, resources, schema reference | `.claude/skills/gitnexus-guide/SKILL.md` |
| Index, status, clean, wiki CLI commands | `.claude/skills/gitnexus-cli/SKILL.md` |

<!-- gitnexus:end -->
