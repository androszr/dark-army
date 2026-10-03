# Host context — the daemon, the hook handler and the menu-bar app

Relocated verbatim from `CLAUDE.md` on 20 Sep 2026 (`docs/ship-efficiency.md`
holds the paragraph map). This is the subject document for **the Python
half**: Codex observation, the hook handler (`dark-army-notify`), the
daemon and its modules (`api_server.py`, `bearings.py`, `event_log.py`, `enrollment.py`,
`paths.py`, `terminal_title.py`, `session_title.py`), the menu-bar app and
its submodules, and the session state model. Loaded when the work touches
`host/dark_army_daemon/` or `host/dark_army_menubar/` — and always on
the conservative fallback (`docs/agent-context.json`). `board.py`,
`dispatch.py` and `workspace.py` are in `docs/context-board.md`.

## Host (`host/`)

- **Codex observation and control** — `docs/codex-contract.md` is the full
  contract (`codex_rollouts.py`; rollout/spender tests). Async acks preserve
  questions; canonical roles, observed history and retained reports share
  existing surfaces. **PID is data, never authority**: capability fields
  default false. Exact resume grants Jump/Stop; unique-cwd grants Jump/Hide,
  never Stop; navigation proof can grant Jump with null public PID. No process
  means no live row; uncertain inspection changes no admission. Closed threads
  stay gone until a different process returns; tty proof stays private.
  A human may close a proven stopped native root via `can_close`; fresh
  journal/holder/turn checks and bridge 0.1.12+ apply (`docs/codex-contract.md`).
  Codex board MCP exposes add/attach/close only. Private planning-terminal close
  requires a ten-minute receipt from successful session-attributed attachment.
  Opt-in stopped-native reply uses exact ownership and bridge 0.1.21+;
  shared-server TUI replies instead use the local server's explicit direct-input
  capability and addressed turn protocol (`codex_input.py`), including queued
  async questions during a turn; `docs/codex-contract.md` states its guards.
  `can_type` stays false. Generic typing, Wrap up, synchronous question/permission answers
  and app/IDE control stay unsupported. Native review children remain beneath
  their explicit parent as bounded `review_reports`, never independent waiters.

- **dark-army-notify** — Standalone hook handler (installed to
  `~/.dark-army/dark-army-notify`, its name since 22 Sep 2026, by the menu
  bar app). Reads Claude
  Code hook stdin, converts to a daemon message, forwards over the private
  `~/.dark-army/hook.sock` by one address rule
  (`docs/hook-door-contract.md`). Stdlib only, so it runs under any
  `python3` including the 3.9 system one, and carries the **project's
  enrolment key** (`_project_key`, a home-skipping walk up from the hook's
  own `cwd`, Grok resolved through the `~/.grok/sessions` reverse lookup
  first), stamped onto every message in `_with_common`; an unkeyed message is
  turned away (`enrollment.py`). It does **not** start the daemon: if nothing
  is listening it exits 0 and the event is dropped.

  **The pid walk is memoised per session.** `_session_pid_cached` keeps the
  answer under `~/.dark-army/hook-pids/` (0600 in a 0700 dir,
  `HOOK_PID_CACHE_DIR`) and believes it only while `_proc_identity`
  (`proc_pidinfo` over `ctypes`, a capability probe returning `None`, never
  a platform test) reads the same start time and comm **and**
  `_memo_on_our_chain` finds the pid on this hook's own ancestor chain via
  `pbi_ppid` — two harnesses share a session id after `--resume`. **A
  fallback is never written** (`_walk_session_pid`'s `found` flag).
  `subprocess` is a module global, so a test can stand in.

  The handler's standing hints, the shunt read guard, the close-out helper
  install and the `PermissionRequest` broker are `docs/hook-door-contract.md`,
  *The handler*. `NOTIFY_SCRIPT` is a string in `dark_army_menubar/hooks.py`,
  written to `~/.dark-army/` on install; edit the string.

- **dark_army_daemon/** — Async Python daemon (asyncio): session state
  tracking with staleness eviction and subagent lifecycle tracking. The board
  verbs are `daemon_board.BoardVerbsMixin`, a pure move that `BobDaemon`
  inherits; module constants stay in `daemon.py` and the mixin reaches them
  through the module at call time, so a monkeypatch on `daemon` still bites.
  `_handle_message` is the enrolment gate over three routers,
  `_route_channel` / `_route_lifecycle` / `_route_control`.
  `session_store.py` saves state atomically to
  `~/.dark-army/sessions.json` for restart recovery. Two destructive verbs
  with **opposite** safety properties: `stop_session` aims at live sessions
  and is guarded by **identity** (the PID must still be the harness we
  recorded); `delete_agent` / `delete_abandoned_agent` / `jobs_store.py`
  destroy a record and are guarded by **category** — only the `abandoned`
  bucket, re-checked at the moment of deletion — the only way to retire a
  background agent `claude agents --json` lists forever; it deletes
  `~/.claude/jobs/<short-id>/`, never the transcript. `samples.py` keeps ~15
  minutes of `(t, ctx%, cost, output)` per live session, published as
  `trend`. `session_stats.py` streams a transcript once to roll up models,
  tokens, duration, turns, per-tool counts and files touched, memoised by
  mtime in a `StatsCache`. A card surfacing plays an `afplay` chime (0.5s
  debounce), muted by `notification_sound`, read when the timer *fires*
  rather than when it is armed. `power_source.py`: battery, `docs/phone-contract.md`.

  **`ptyhost.py` and `pty_broker.py` keep a hosted terminal across a
  restart**, stated in `docs/pty-broker-contract.md`; what must hold: **a child
  on Dark Army's pty inherits no session identity** (`subprocess_env.clean_env`'s
  `SESSION_ENV_VARS`; `PtyHost.start` puts back the caller's stamp alone);
  `data`
  frames slice at `DATA_FRAME_BYTES` in order from one coroutine and
  `attach()` spawns **only on `_broker_connect`'s `refused`**; the broker
  quits on `IDLE_EXIT_SECONDS` / `ORPHAN_CHECK_SECONDS` with nothing live
  and nobody connected — **or the moment its socket is no longer the one it
  bound** (`Broker._stranded()` on the `(st_dev, st_ino)` recorded in
  `start()`, so a *replaced* door is a deleted one), a rung **outranking**
  the live-terminal one it made dead code, closing those terminals on the
  way out and never firing without a recorded identity or with a daemon
  connected. The backstop is `PtyHost._sweep_strays`: **one shot** at the
  first attach that connects, off the loop, `SIGTERM` at one pid, never a
  group, on `stray_broker_pids`' exact argv match, never the connected or
  pid-file broker, and **nothing at all** when it cannot name the live one.

  **`channel_server.py`** is the one route that reaches *into* a session: an
  MCP server Claude Code spawns over stdio, declaring `claude/channel` (push
  an event the model sees as `<channel source="dark-army">`) and
  `claude/channel/permission` (relay a tool-approval prompt out, take a
  verdict back). Stdlib JSON-RPC, **copied** to
  `~/.dark-army/dark-army-channel` on install and reinstalled on
  every launch — a stale copy still speaks the handshake and drops every
  field added since. It listens on an ephemeral loopback port and
  *announces* itself (`channel_attach`, heartbeated) carrying its own
  `GROK_SESSION_ID` / `CLAUDE_CODE_SESSION_ID` (`SESSION_ID_ENV`, Grok
  first); the daemon keys `_channels` by port and falls back to resolving
  one **by pid at the moment of use**. Registration is `claude mcp add -s
  user` — not `--mcp-config`, not a hand-written `~/.claude.json`. Every
  session spawns the process, but only one started with
  `--dangerously-load-development-channels server:dark-army` registers it as a
  channel, so `channel_server.is_channel()` reads the launch flag off the
  *parent's* command line. Delivery is best-effort — `push_channel_event` returning False. Two guards: **The port is a lock, not a
  hiding place**: `listen()` mints a `secrets.token_urlsafe(32)`,
  `channel_attach` carries it, the daemon replays it on every line down
  (`_send_to_channel`, read from the registry) and `_authentic` checks it
  with `hmac.compare_digest`; **never written to a file**, never published
  (a test asserts it absent from `detailed_snapshot()` and
  `_permission_snapshot()`), and failing **closed** — a server that never
  called `listen()` refuses everything. **`_attach_is_displaced` answers the
  claim itself**: `channel_attach` arrives on the hook socket, which
  authenticates nothing, so its port, pid and session id are merely
  *claimed*; the first channel to claim a session keeps it while it
  heartbeats (`CHANNEL_CLAIM_SECONDS`, 45s — over the channel's 30s
  heartbeat, under the 95s staleness window).

  **Ten tools, the only inbound verbs here**, each scoped so that winning
  the attach race gains an attacker nothing they did not have. In full, and
  the dual-name window, in `docs/channel-tools.md`; what must hold:
  `tools_for_host` is the one capability boundary and `call_tool` re-checks
  it (Claude ten, Codex the six board verbs, Grok unregistered
  and so none; Codex's six all spend `_board_request_session_fresh`;
  `dark_army_next_card` moves a batch session to its next card,
  `docs/channel-tools.md`). **No
  verb but Mission Control's `dark_army_request_start` (which starts
  nothing) takes a `card_id`, and none takes a project**: the seven card verbs
  resolve the card from `_channel_session(port)` alone —
  by pid at the moment of use, refusing what they cannot attribute — and an
  agent-written card lands in `prep`, always, whatever the tool's own reply
  says. `dark_army_knowledge_read` / `dark_army_knowledge_write` are **the project's own
  Q&A notes**, in full in `docs/knowledge-notes.md`; what must hold: schema
  20's one table, keyed on the enrolled **root** and not a card, in neither
  `_WRITABLE` nor `_BOARD_FIELDS`, no all-roots read; `_knowledge_place`
  *reaches* on the session's cwd but *keys* on `root_enrolled(cwd)`, or
  `<proj>/host` becomes its own bucket; the read is capped and says so; the
  write's `key` rides as **`note_key`**, `key` being the enrolment key
  `_enrolled_root` reads; both types are on `CHANNEL_MESSAGE_TYPES`,
  `_route_channel` **and** `_handle_message`'s reply-on-refusal tuple;
  **nothing rides SSE**. A human reader exists: token-gated loopback GET
  and sealed kind `knowledge` (`root` in the body), `knowledge_supported` /
  `knowledge_writable` on `_pipeline_writable()`, loopback person writes
  `knowledge_confirm` / `knowledge_stale` / `knowledge_edit` (not on the
  phone tuples).

  Both card verbs ask for a `summary` beside the notes. A channel process
  already running keeps its old `initialize_result` for the life of that
  session and simply lacks a new verb; `test_channel_install.py` asserts the
  freshly written copy carries every named verb. Two verbs ride the channel outward:
  `answer_permission` and `reply_to_session`, the latter pushing
  `kind="user"`. **The reply has two routes.** With `typed_reply` on
  (default off) `reply_to_session` tries `_reply_by_typing` first — Ctrl-U +
  text + Enter via `session_io.send_text`, stopped rows only, refusing
  multi-line (never collapsed), a `/!#` first character, an open prompt or
  question, a busy `_answering`, Codex and no typing window, every check
  before the first keystroke — then the channel as fallback. The row's
  `channel` means *reachable by some route*; `reply_via`
  (`typed`/`channel`/`""`) says which; both surfaces read, never derive.
  Codex replies instead take a strict, stopped-native branch with two fresh
  journal/holder observations and one attempt per turn; no channel fallback.
  `_session_reachable` is **not** widened; a mid-turn push stays the
  channel's alone.
  `wrap_up_session` and `low_priority_session` do **not** ride
  it: a slash command is expanded by the client on the input line, so both go
  through `vscode_reveal.send_text`. Permission prompts are in `/api/state`
  as `permissions`, answered through `answer_permission` / the
  `permission_verdict` action, never as notification cards. The preference
  keys stay `channel_enabled` and `typed_reply`; every rename in that menu is
  a label only.

  **Collaboration** lists task/agent evidence on Mac and phone.
  `collaboration.py` projects final provider state: explicit parents, observed
  calls (never delivery), distinct present/ended/ambiguous/unknown identities,
  exact card links and partial coverage. Navigation rechecks identities;
  no Send or timer. Published addresses alone survive in existing
  30-minute tombstones; silence stays unknown. The additive section carries its
  own clock; `_build_mesh` is its legacy adapter. Rules: `docs/card-crew.md`.

  - **`api_server.py`** — the loopback HTTP + SSE the panel reads. **A
    request has to be addressed to loopback, not merely arrive there.** Reads
    are ungated; the defence against **DNS rebinding** is the `Host` header,
    not CORS: `_loopback_host` checks it once in `_handle_client`, **above**
    the routing table, allowing an *absent* Host (HTTP/1.0 omits it, a
    browser never does). `_authorised` compares with
    `hmac.compare_digest` and refuses a `""` token outright.
    `load_or_create_token` uses `os.open(..., O_CREAT|O_EXCL, 0o600)`, never
    write-then-chmod.

    **The loopback door has a desk token and a session token.** The desk
    token is memory only and reaches the panel over its stdin pipe; the file
    is the session token, which only closes terminals, pops the panel and
    reads (`_session_authorised`, `SESSION_ACTIONS`, `SESSION_READS`;
    `docs/transport-contract.md`, *The loopback door has two tokens*).

    **The Bearings digest** (`bearings.py`, pure; loopback `GET
    /api/bearings`; sealed `bearings` on both doors) is
    `docs/transport-contract.md`'s, *`bearings` is a sealed read*.

    **A frame that says nothing is not sent.** `_broadcast` is a
    **trailing-edge limiter**: `BROADCAST_MIN_INTERVAL` (200ms) floors any
    two frames; one whose `_news()` (`_CLOCK_FIELDS` stripped) matches the
    last waits `BROADCAST_QUIET_INTERVAL` (1s), then goes to full clients
    only; one moving a `_POLL_ECHO_FIELDS` stamp alone waits
    `BROADCAST_ECHO_INTERVAL` (15s), never stripped. `_flush_due` pulls news
    forward. `AGENTS_PUSH_MIN_INTERVAL_SECONDS` floors pushes; only a full
    one (key moved, `FULL_PUSH_INTERVAL_SECONDS`) reconciles. `_flush`
    builds one `_Frame` (one `state()`, each key serialised once), derives
    the review board and renders variants lazily; the socket push builds
    once per push. Queues are bounded (`SSE_CLIENT_QUEUE_MAX`, 8): overflow
    drops the oldest and puts a **whole** frame.

    **`?sections=changed` is the opt-in that keeps an unchanged *section* off
    a frame — every one of them.** `_flush` omits each of
    `_OMITTABLE_SECTIONS` whose clock-stripped JSON matches the last frame
    sent (`_last_section_news`); the two reconciler scalars ride every sent
    frame. Key **absence** is the marker — `cards: []` is a real state
    inside a *present* section. The attach frame is a full `state()` and
    `/api/state` stays complete.
    **`?cards=delta`**, the third opt-in, sends only the moved cards plus
    `card_order` and `cards_delta: true`, merged by `BoardCardDelta.merge`;
    the first board after an attach or overflow is whole.

    Panel side, `Snapshot.carried` marks the absent sections and
    `DaemonClient.apply` fills each forward from the last frame that carried
    it, with that frame's clock: **absent carries, present-but-undecodable
    throws** and `parseFrame` REJECTS the whole frame — the same way round
    is how a schema disagreement becomes a silently blanked panel.
    So the panel's rule everywhere: **no clock on screen may depend on
    `snapshot.generatedAt` advancing.** A row's relative
    figures (`idleSeconds`, `stats.durationSeconds`) are aged from
    `Snapshot.agentsStamp` through `AgentFacts.aged` under a 1s `TimelineView`
    gated on visibility; `Inbox.items` is handed `agentsStamp`, not the
    frame's clock; `ProjectFactsBar` composes against a live wall clock.
    The board's headings read no clock at all: nothing on them ages out.

    **`?done=review` is the second opt-in, and it keeps the *finished* cards
    off every frame.** A client sending it is registered in
    `_done_review_clients` and its board
    is `board.review_only(state)`: every card except the finished ones stamped
    `done_preview`, which `_build_board_state` writes on a Done card the
    recent-preview leg produced **alone** — the awaiting-review leg never
    stamps it, so a close nobody has acknowledged still rides every frame. The
    rest is fetched once from `GET /api/board?column=done` (or the sealed
    `done` read) in the snapshot's own shape and held. The variant has its own
    news slot (`_last_review_board_news`), never `_last_section_news`: the two
    go stale at different moments. **Two tokens ride the board and are not
    interchangeable** — `done_clear_token` is exact membership and the only
    thing `clear_done` compares; `done_view_token` also moves on a revision
    and on a review, which is what tells a held copy it is stale.
    `BoardStore.done_tokens()` reads both under one lock. A client that never
    sends the query keeps today's whole Done preview, so no older screen can
    lose its finished column.

    **Refused knocks on the phone doors are logged and a burst alerts**
    (`access_log.py`, `docs/transport-contract.md`): `_record_access` never
    changes a verdict, no channel id / code / key is logged, `ctr`/`ts`/`kind`
    weigh zero, section `security`, verb `access_alert_ack`, read `access_log`.

    One raise per `ALERT_FLOOR_SECONDS`; later bursts fold as `burst_more`
    (no banner, diary or wake), merged in `open_alerts`; the snapshot carries
    the newest `MAX_PUBLISHED_ALERTS` plus `hidden`. The same log carries
    the phone's link timing (`link_timing.py`, `timing` lines, `?timing=1`):
    `docs/transport-contract.md`, *Every phone request is timed*.

    **The scout-report list and body** (`scout_index.py`, pure; loopback
    `GET /api/scout-reports`, with a `q=` text search over the bodies, and
    `GET /api/scout-report`, token-gated;
    sealed reads `scout_reports` / `scout_report` on both doors) are
    `docs/transport-contract.md`'s, *`scout_reports` and `scout_report` are
    sealed reads*.

    **The plan list and body** (`plan_index.py`, pure; loopback
    `GET /api/plans` / `GET /api/plan`; sealed `plans` / `plan`) are
    `docs/transport-contract.md`'s, *`plans` and `plan` are sealed reads*.

    **The phone's week** (loopback `GET /api/history-week`, token-gated;
    sealed `history_week` on both doors, a closed projection) is
    `docs/transport-contract.md`'s, *`history_week` is a sealed read*.

    **The per-frame log pair is behind a switch**: `_log_broadcast` is DEBUG,
    the panel's `snapshot` line behind `Trace.verbose`
    (`BOB_COMPANION_LOG_LEVEL=DEBUG`, `BOB_PANEL_TRACE=1` via `launchctl
    setenv`, relaunch). Rare lines stay unconditional.

    **The LAN door and the relay are two sealed doors under one contract**,
    in full in `docs/transport-contract.md`. What must hold:
    `_handle_lan_client` (`0.0.0.0:19875`, preference `lan_access`, default
    off) takes sealed frames under a per-device home key and `_home_open` is
    the **one verifier**; the three plaintext routes answer **426** and run
    nothing. A typed-address pair is SRP-6a over the code (`srp.py`),
    refused (`pair_plain`) unless the Pair window armed it with
    `allow_typed`; a knock landing on a tunnel address is refused unread
    (`tunnel`); over `LAN_MAX_OPEN` / `LAN_MAX_OPEN_PER_PEER` it is 503
    (`busy`, weight 0) — a shared Wi-Fi still sees the door, the seal is the
    boundary. `LAN_ACTIONS` and `REMOTE_ACTIONS` are two named tuples,
    `REMOTE_ACTIONS` never exceeds `LAN_ACTIONS`, and the sealed reads
    (`bearings` included) are on neither and check no lease.
    `relay.note_lan_proof` alone may
    extend an away window; `set_lease_days` may only clamp down. The
    bot is gated by `relay.bot_grant_valid` instead, on every door it
    reaches (`docs/transport-contract.md`, *The bot's access is two
    grants*). `POST
    /api/upload` is LAN-only with its own body rule (`_lan_body_rule`);
    `/api/action`'s cap is pinned. The sealed `state` read's `unchanged:
    true` and `sections_unchanged` are **present keys**,
    not omissions. No key,
    digest, claim or channel id ever rides a snapshot.

    Phone sheets, detents, the terminal cover, the 4s/8s cadence
    (`noteAttempt()`, `ReconnectBar`, **no backoff, no jitter**), relay-first
    writes (`knowsItIsAway`), `BackgroundRefresh` (**never `onLive`**),
    Dynamic Type with **no ceiling** through `Theme.mono` alone, `spoken`
    rows and the one widget are all `docs/phone-contract.md`'s, pinned by
    the phone accessibility, text-in-full, reconnect-display and
    background-refresh tests. Four more rules live there: **the phone keeps
    the last picture** (`HeldPictureStore`; `restoreHeldPicture` is the
    second and last `snapshot =` site and quotes no digest) and **prepares
    a card by itself** with a Keychain Anthropic key (`CardPrepareRules`,
    a `swiftc`-run parity twin of `card_prepare.py`; the key reaches
    Anthropic and nowhere else); **a glance is not a reconnect**
    (`.inactive` locks; `.background` stamps a departure, and the poller
    stops itself past `BackgroundGrace.window` (5 min) and is kept on a
    return inside it); and
    **Allow / Deny / Acknowledge on the banner** behind the desk's per-phone
    `set_lock_screen_actions` switch (`_compose_push_act`, identifiers only).

    **And the top waiter is a Live Activity on the phone's Lock Screen**
    (`live_activity.py`, `_push_live_activity`; `register_activity_token` on
    both phone tuples, the token on no snapshot): in full in
    `docs/phone-contract.md`, *The waiting agent is a Live Activity*, and
    `docs/transport-contract.md`, *The buzz has a live-card leg*.
  - **Mission Control** (`mission.py`; verbs in `daemon_board.py`) — the
    standing chief-of-staff session on Dark Army's own pty in **Dark Army's
    own checkout**, agent `mission-control` with
    `docs/mission-control-brief.md` (`dispatch.mission_argv`; origin
    `mission`). It **acts like any session**: a write
    raises the ordinary `permission` event; only plain replies are
    withheld (`daemon._mission_reply`). **It is always called Mission
    Control**: spawned with `--name "Mission Control"`, and any row
    stamped `mission` wears `mission.NAME`, never a generated title.
    After an eviction and a `/clear`, `_mission_successor` resolves (never
    binds) the stamped session whose pid the terminal owns, so the record
    and `ask_start` follow it. `mission.allowed_tools(root)`
    builds the `--allowed-tools` grant (pre-approved reads) per spawn:
    `Read(//<root>/**)`, `Read` of `mission.STATE_SCRATCH`, bare `Grep` /
    `Glob`, one Bash rule, `mission.CURL_RULE` =
    `Bash(curl -s http://127.0.0.1:19874/*)` — the API door (`API_PORT`,
    never the hook door `hook.sock`), measured on CLI
    2.1.278: `:*` misses a URL; a quoted URL, a `?` and a pipe into
    `python3` are denied, `wc` / `head` is not, so the brief forbids
    pipes. Never bare `Read` or `Bash(curl:*)`. The brief curls
    `/api/state/pretty` (`_route`, loopback only; SSE stays one
    line): `/api/state` is one ~100 KB line `Read` refuses
    whole, and the rule carries no `?`. Loopback `/api/board?card=` has no
    plan; `plan_path` is read in the checkout. **The identity is the
    terminal**: `mission.json` (`paths.MISSION_PATH`, private,
    forward-compatible) records the broker handle; the pty is named
    `mission.PTY_NAME` (44 chars, longer than a card terminal's
    `title[:40]`), and `_is_mission_terminal` — that name plus, for a
    bound session, no **known foreign** origin stamp off state or
    tombstone (an absent stamp is accepted) — is what the adopt rung and
    `end_mission()` re-check. `open_mission()` (`open_adhoc_terminal`'s
    sibling under `_dispatch_lock`, refused while `PtyHost.connected` is
    false) is idempotent while alive **on the current brief**, adopts a
    live terminal passing the test, re-spawns past an exited one, reuses
    `adhoc_guard` / `_adhoc_launches`, holds no claim. **The brief rides
    `--agents` at spawn and nothing re-reads it** — not `/clear`, not a
    restart (the broker keeps the process) — so the record keeps
    `brief_digest` (`mission.brief_digest`, sixteen hex of SHA-256 over
    the brief) and `open_mission()` compares it with the file: a mismatch,
    or a record with no digest (written by a daemon that compared
    nothing), closes the terminal and spawns the brief on disk; an
    unconfirmed close keeps what runs. An adopted terminal is stamped
    current. This is how a Mission Control spawned "read-only" stopped
    saying so once the brief that made it act landed. **So is the
    folder**, on both rungs: a root that is not `_find_own_checkout()`
    (the main checkout, never a card's side folder) is replaced.
    `end_mission()` closes the terminal
    alone: the record is blanked only on a confirmed close
    (`PtyHost.close` False keeps it: `MISSION_CLOSE_FAILED_REFUSAL`),
    keeps `ended` / `ended_at`, so `mission_snapshot()` (omittable,
    **never a handle**) says `exited` once the broker forgot it; the row's
    eviction is the ordinary `terminal closed` rung. `_pty_handle_for`
    answers an evicted Mission Control's last id off the record, resolved
    only. Every reply is quiet by origin (`_mission_reply`;
    `docs/session-state-contract.md`). `mission_open` / `mission_end` are
    on both phone tuples; `terminal_input` reaches every pty Dark Army hosts.
    `test_mission_control.py`.
  - **Review runs** (`review_run.py`, `daemon_review.py`) — a card-less
    chore on Dark Army's own pty, the ticked steps the only authorised ones,
    nothing written under a project: `docs/review-runs.md`.
  - **`event_log.py`** — the Mac's diary, for the phone to read *later*: a
    bounded, append-only JSONL journal (`~/.dark-army/event-log.jsonl`,
    in `paths._PRIVATE_FILES`, ≤ 500 lines, 24 h) of session starts and ends,
    permission asks and outcomes, and the board verbs. **The sentence is
    composed in one place**, `event_log.sentence()`; `append` refuses
    `FORBIDDEN_KEYS` at any depth; the finish rides `_record_finished` through
    `_log_session_end`, exactly once, and a restart is not a start
    (`_seed_logged_starts`). Served as `GET /api/log` on loopback and the
    sealed `log` read on both doors, beside `work_record` and `card_sync` —
    all three in full in `docs/transport-contract.md`. Pinned by
    `test_event_log.py`, `test_event_log_hooks.py` and
    `test_phone_event_log.py`.
  - **`enrollment.py`** — **which projects Dark Army is allowed to watch at all**.
    The hook socket authenticates nothing. Enrolling a project writes
    `<root>/.dark-army/key` (0600, in a 0700 folder whose own `.gitignore`
    is `*`, plus a `.dark-army/` line appended once to that project's
    `.gitignore` where it is a git repository) and records its **SHA-256 digest** — never the
    key — in `~/.dark-army/enrollment.json` (`paths._PRIVATE_FILES`). It is an *enrolment and scoping* boundary, not authentication against a hostile local process: it stops an untrusted repository's agent using Dark Army's board as a lateral channel into other projects, not a local process that reads the key file.
    Readers try `.dark-army/key`, then `.bob-companion/key`; `migrate_key_folders()`
    copies an old-only key on launch, never over a differing key.

    **The key travels on every message, not once per session**, so un-enrolment
    bites on the next thing a project says. The ledger is memoised on
    `(mtime_ns, size)`. **The key identifies the project; the root a message
    claims never does.** `resolve(key)` matches the digest alone and returns
    the enrolled root, failing closed on an empty key and on an empty ledger.
    There is **no "missing key means allowed" grace**;
    `install_notify_script()` rewrites the handler on every launch.

    **The most damaging bug available here is the walk-up.** Dark Army's own state
    folder `~/.dark-army` and its old-name link `~/.bob-companion` carry
    both names a project's key folder has, so every client's walk (`NOTIFY_SCRIPT`, `STATUSLINE_SCRIPT`,
    `channel_server.project_key`) **skips `Path.home()`**, or a session under
    the home directory would read `~/.dark-army/key` and enrol the whole
    of `$HOME`. Containment is component-aware (`/a/proj` must not admit
    `/a/project2`), reusing `workspace._contains`.

    **Two of the three assistants knock; one is filtered.** The gate is
    `BobDaemon._enrolled_root(msg)` at the very top of `_handle_message`. On
    a refusal the five board verbs get a **reply** (`ENROLLMENT_REFUSAL`), the
    statusline gets `{}`, everything else `None`. A session Dark Army holds is
    `_forget_session`'d on the spot, but **the session's own folder decides
    that, never the refused message**: the door asks `_session_place(sid)` —
    the agents snapshot, not `msg["cwd"]` — and forgets only where
    `root_enrolled` no longer admits that folder; an empty cwd is a **keep**.
    The three sources that never knock — Codex journals, Grok's
    `active_sessions.json`, `claude agents --json` — **filter** on
    `root_enrolled(cwd)` instead, and `_collect_agent_stubs` keeps a stub with
    an **empty** `cwd`.

    **Filtering the records is enough, and a second gate is forbidden.** The
    row, the strip count, the tab title, the reply route and `can_type` all
    go quiet from one filter. Dispatch alone re-checks: `_dispatch_card_locked`
    and `_refine_card_locked` refuse an unenrolled root at the instant of the
    press, and `_known_project_roots()` is intersected with the ledger.

    **A quiet machine says why.** `enrollment_snapshot()` feeds both
    `ApiServer.state()["enrollment"]` and the panel; **no key and no digest
    ever appears in it**. `available` is *stated*, never inferred from an
    empty `enrolled` list. `pending` is the quarantine ledger (`_unenrolled`,
    memory only, `MAX_UNENROLLED` = 16): an entry is kept only when the
    claimed `cwd` resolves inside an open VS Code window's folder; everything
    else increments a bare integer. `last_seen` is in
    `api_server._CLOCK_FIELDS`.

    **Dark Army's own checkout enrols itself on upgrade, and nothing else does**
    (`enroll_self()` from `app.main()`, over `dev_build.find_repo_root()`);
    an older build never reads `enrollment.json`, so a downgrade simply
    stops gating. The panel draws the empty state as an
    enrolment prompt on Agents, a one-line banner when something was turned away, and a
    **Projects** group in the settings window (⌘,) (enrolled roots with an armed Un-enrol, plus
    an `NSOpenPanel` **Enrol a folder…**, plus the agent-pack offer or its
    synced-profile row — Dark Army's own checkout is refused in the row,
    never silently missing).

    **A newcomer sees three steps first** — Enrol a folder → Open it in VS
    Code → Start a session — above every tab, decided once per opening from
    `enrollment.checklist` (per-root `editor_observed` / `session_observed`,
    built by `_observe_checklist` on the snapshot executor). In full in
    `docs/first-run-checklist.md`.

  - **`paths.py`** — `ensure_state_dir` is the seam every read and write
    funnels through: `~/.dark-army` to 0700 and `_PRIVATE_FILES` to
    0600 (`buzz-ledger.jsonl` among them), re-checked on a 60s interval
    rather than latched once per process
    (SQLite creates `history.db` on first connect, 0644, long after the first
    call). `_restrict` never raises — a permission that could not be set is a
    log line, not a daemon that will not start. **Under pytest the whole
    directory moves**: `_home()` returns a temp folder when `pytest` is in
    `sys.modules`, so every constant derived from it (`BOARD_PATH`,
    `PREFS_PATH`, …) is isolated by default and no test can reach the running
    fleet's files. The panel carries the same guard as `PanelStateDirectory`
    (`CardDrafts`, `CardAttachments`, `PanelPlacement`, `PanelLock`), so a
    `swift test` run never banks cards into the real Drafts
    sheet.

  - **`terminal_title.py`** — the agent's badge on its own VS Code /
    Terminal tab, an OSC 0 escape written straight at the session's tty.
    **Two halves, and neither works alone.** First, take the pen away:
    `hooks.set_title_env()` puts `CLAUDE_CODE_DISABLE_TERMINAL_TITLE=1` in
    the `env` block of `~/.claude/settings.json`, and conservatively adds
    `terminal_title = [] # managed by Bob Companion` to a literal `[tui]`
    table in `~/.codex/config.toml` (a user-owned value or ambiguous TOML
    wins). Then **the daemon** is the replacement — a pure projection of the
    agents snapshot. Nicknames are stem-aware (`Peter-19a7` reserves `Peter`); overflow suffixes are four hex characters of `sha1(session_id)`, never `session_id[:4]`; three letters, not the whole name; `·` rather than `:`. Every payload leads with a `Claude Code`
    **primer**: VS Code renders `${sequence}` only after classifying the
    terminal as an agent CLI (`terminal.integrated.tabs.allowAgentCliTitle`),
    and the startup `✳ Claude Code` that would trip it is the write the env
    flag suppresses. **A tab Dark Army itself opened is named the same way, and
    only this way**: a VS Code terminal created with a fixed `name` never
    installs the OSC listener, so the extension's `spawnAgent` passes no
    name and `title_for` composes the launched row's title from the card —
    the card's title where the row is still `New session`, prefixed with
    the verb its terminal was opened for (`Gid · refine: Mobile cards`,
    `ask:` for a consult, nothing for Start) — so a planning tab and a
    building tab for one card still read apart. Grok is named the same way (`title.enabled = false`
    under `[ui.notifications.title]` in `~/.grok/config.toml`, plus the
    primer); with `use_leader` the hook walk finds the shared `grok agent
    leader` with no tty, which `looks_like_grok` excludes,
    `_ensure_session_pid` taking the TUI pid from `active_sessions.json`.
    `finished` rows are skipped. **One writer per tty**, most-recently-active
    wins; `_written` memoises what each tab says. The pen is taken on every
    launch and never given back. **The Claude env flag also gates Claude
    Code's AI title generation**, which is why `session_title.py` exists.

  - **`session_title.py`** — the name a row wears when Claude Code writes
    none. `ai_title.py` prefers the transcript's `ai-title`, which the env
    flag also gates. **One `claude -p` per session**, with four flags that
    stop the namer becoming a session on somebody's screen:
    `--no-session-persistence`, `--setting-sources ""` (**no hooks**),
    `--strict-mcp-config`, `--model haiku`; the instruction rides **in the
    prompt**, never `--system-prompt`. Decided on the snapshot thread
    (`_consider_title`), spawned on the loop (`_flush_session_titles`), one
    at a time, detached. **One attempt per session, ever** — a miss is
    remembered like a hit; an answer that is not a title is *rejected*, not
    truncated. Hits persist to `~/.dark-army/titles.json`, misses do
    not.

- **dark_army_menubar/** — macOS status bar app (rumps). **The status item
  is an animated multi-face strip that measures itself**, stated in full in
  `docs/menubar-strip-contract.md`; what must hold: `_compose_strip()` lays out
  one animated face per live category (work / attn) with a count, at most one
  face each (`MAX_FACES` is 1), rendered by `_render_strip()` and ticked by
  `_animate_icon()` on its own `rumps.Timer`, fast while `work` moves and once
  a second otherwise (`docs/menubar-strip-contract.md`, *The clock rests when
  nothing moves*); **only `work` animates** — `idle` and `attn` hold
  one frame through `_held`/`STILL_FRAMES`, aggregate and cast alike (last slot
  for the sleeping Zzz, first for the waiting red), which is what stops the
  repaint on a quiet Mac; counts stay **real text** in `labelColor` and only
  the usage stack (`_usage_stack_image`) and the to-do card are `NSImage`s,
  resolving their own ink;
  the collapse ladder (`STRIP_LADDER`, `STRIP_BUDGET_PT` = 310pt, rungs named
  on `StripRung`) is a **measured** search whose floor is the two live figures
  and their counts, and the usage stack is one rung, given up whole; colour is
  an exception signal alone (`menu_format.USAGE_WARN_PERCENT` 75%,
  `USAGE_CRIT_PERCENT` 90%), and the percentages are spoken on the tooltip and
  accessibility label (contract, *The usage stack ends the strip*);
  `menu_format.py` formats the usage stack and its words and nothing else; **there is no dropdown** — both mouse buttons open the panel
  (`_StatusClickHandler`), `self.menu` is empty, preferences are the app's own
  state (`self._settings`) in `~/.dark-army/preferences.json`, and
  `_install_emergency_menu_if_needed()` restores kill / log / restart / quit
  **when the panel cannot run, or when the self-restart cap is spent** — a dead
  daemon thread (`_daemon_alive` alone) makes `_health_check` take
  `_on_restart`'s route, 3 an hour in `restart-watch.json`, said once through
  the inert `post_notice`; an unstampable ledger counts as spent, and a
  person's press never does. Pinned by `test_menubar.py`, `test_cast.py`,
  `test_limits.py` and `test_self_restart.py`.
    - **The kill switch** (`kill_switch.py`) stops every Dark Army process
      in one press; `docs/kill-switch.md`.
    - **The shared agent pack** (`pack_render.py`, `pack_install.py`,
      `pack_ledger.py`) — Dark Army owns the `/ship` + `/review` + specialist
      briefs and keeps enrolled projects in step after a confirmed press per
      project. In full in `docs/agent-pack.md`; what must hold: `pack_install`
      is the only module under `host/` that writes into a project root,
      `PACK_DESTINATIONS` is the allowlist re-checked at the instant of the
      write, and its three `PANEL_ACTIONS` are on neither loopback nor
      `LAN_ACTIONS`; `SEED_ONCE_KEYS` (the `/knowledge` skill, seeded if
      absent) and the `.gitignore` block (`gitignore_offered`, offered
      once) are exceptions to "the pack rewrites what it owns".
  - **`first_run.py`** — a fresh install writes the marker and enables launch
    at login, exactly once: the preferences file's existence proves a prior
    run, so an upgrade never re-ticks a box the user cleared;
    `FIRST_RUN_PREFERENCES` is **empty** — nothing else is opted in. The
    statusline collector and the title pen are `main()`'s on every launch,
    and a periodic health check shows the disconnected icon on a dead daemon
    thread. `launch_report.py` says truthfully what a launch did to hooks,
    the extension and the login item (`settings["launch"]`);
    `Notifier.refresh_status` reads the notification permission explicitly
    (`settings["notification_status"]`; only `denied` warns). Both in
    `docs/first-run-checklist.md`.
  - **Rate-limit windows** are on both surfaces, from one reading:
    `_refresh_limits` (a 30s timer) computes `limits.snapshot()` **off the
    main thread** over the statusline metrics in the agents snapshot — polled,
    not pushed — and the same reading feeds the strip's usage stack and the
    panel's footer chips (`docs/menubar-strip-contract.md`).
  - **`notifier.py`** — the last mile of the alert path:
    `UNUserNotificationCenter`. **It lives here, not in the panel**:
    `UNUserNotificationCenter.current()` requires a bundle identity, which
    the panel binary (launched from `Contents/Resources/BobPanel.app`) has
    not. `objc.loadBundle` pulls UserNotifications in at runtime; the
    block-taking selectors have signatures declared by hand; no Developer
    ID is needed. Authorization is requested from the *first run-loop tick* (`main()`);
    every failure ends in silence and a log line. `title` is "<nickname>
    needs you", `subtitle` is `project · branch`, `body` is the card's
    question and **never empty**; `threadIdentifier` is the session. Taps
    route through `_on_notification_action` to `reveal` / `dismiss` / `mute`,
    plus `open` — **a click on the banner's body** opens Dark Army's panel on that
    agent (`_open_panel_on` → `PanelProcess.show(..., focus=sid)` →
    `FocusRouter`; `show` rather than `toggle`); the button is **Open in
    Editor**. `Notification Settings…` deep-links to this app's row in
    System Settings.
  - **`build_check.py`** — the build's own decision logic (`panel_freshness`,
    `vsix_match`, `package_version`, `archive_version`), stdlib only, run by
    `build.sh` as `python -m dark_army_menubar.build_check panel|vsix`. It
    lives here so py2app freezes it with no `setup.py` change, and imports
    nothing from the runtime. It also **decides the release version**
    (`release`): the git tag is the source of truth, a strict build **refuses**
    a tree that is not a checkout, is untagged or is dirty (`--dev` allows an
    unnumbered one), the OS's two version fields take the plain `X.Y.Z` — or
    `0.0.0`, never a development string — and it writes
    `release-manifest.json` into the bundle, numbers and no paths. `setup.py`
    imports it; the runtime never does, and `version.py` keeps its own copy
    of the git rule (pinned equal by `test_release_version.py`). The
    `repo-root` stamp is `--install`-only; a strict zip build refuses a
    `$HOME` checkout and scans the bundle (`home`).
  - **`logsetup.py`** — the log holds a day, not a lifetime. A
    `TimedRotatingFileHandler` at midnight whose `namer`/`rotator` **gzip**
    beside the live file, carrying the day's **mtime** onto the archive;
    retention is `RETENTION_DAYS` (7). `roll_stale_log()` at startup answers
    the stdlib's two timing bugs (rollover dated from the file's mtime; a
    file written today can still *start* weeks ago).
    `build_handler()` runs **inside** `basicConfig(handlers=[…])`. Nothing
    here may raise into startup.
  - **`dev_build.py`** — build status and log warnings compare the selected
    panel's on-disk mtime against Swift source (plus Python and icons when
    frozen), both reading `PanelProcess.executable_path`, the immutable choice
    saved at construction. Selection tries nested app, legacy bare
    executable, checkout release/debug and PATH; missing source or artifact
    yields no build label.
    `build.sh` stamps the checkout path into
    `Contents/Resources/repo-root` (before signing) and `find_repo_root()`
    reads it back **verified, not trusted**: no `host/build.sh` there means no
    repo. An installed bundle's rebuild passes **`--install`**. The rebuild's
    other entry points — the phone's `rebuild_app`, an agent's next-step button —
    reach `_on_rebuild` through `BobDaemon.request_rebuild` and the observer's
    `on_rebuild_request` (`docs/transport-contract.md`).
  - Restart spawns a **fresh detached process** rather than `os.execv`: a
    re-exec'd process keeps its PID and macOS will not re-register the
    `NSStatusItem`. The teardown (`_restart_now`) runs on a **worker thread**
    and touches no AppKit; `_on_restart` only marks the app as going down.

### Session State Model

**What the daemon tracks about a session, and the three things it does to one
on its own**, stated in full in `docs/session-state-contract.md`. What must
hold:

- **One source of truth for the buckets.** `BobDaemon._activity_counts()` sorts
  every session into working / idle / attention through
  `session_stats.categorize()`, and both the strip
  (`on_activity_change`) and the panel read it — neither re-derives.
  States run `registered` → `thinking` → `working` → `idle` → `confused` /
  `error`; a `StopFailure`'s `error` is a **machine token** never shown
  raw (`stop_failure_message`, riding on as `error_kind`); `confused`
  counts as attention only beside a card (`docs/session-state-contract.md`).
- **A turn nobody is waiting on is parked**, suppressing **both** card and
  state. Three reasons: live subagents and a session nobody has prompted
  (`_parked_reason`, pure; `prompted` tested `is False`, so a session restored
  from `sessions.json` reads as prompted), plus **an async subagent still
  running** (`_async_subagent_reason` over `subagent_watch`, apart because it
  reads files) — `SubagentStop` fires when the *spawning turn* ends, seconds
  in, so the live set empties while the child works on. The child's
  own transcript answers it: the path is composed from the hook's own
  `transcript_path`, stored **before** the card decision (`.` mangles to `-`
  too), and opened `O_NONBLOCK` on a regular-**fd** test, a hook having chosen
  that name. The reading is whether the file **moved since the last look** —
  its stop hook first, then the mtime that look saw, stored back so a
  finished child releases its parent at the next decision and not in 15
  minutes — never the shape of its last line (many finished children write no
  `end_turn`); both markers survive as an early release only. **A fourth
  rung is the shell's version of the third** (`_background_task_reason`
  over `background_watch`): a `Bash` run in the background returns at once
  and the harness ends the turn, to resume the session itself when the
  command exits — one Stop in five on this machine (399 of 2,038, 20 Sep
  2026). The session's own transcript tail names the launch (a
  `tool_result` *beginning* with the harness's sentence, never a quoted
  one) minus every `<task-id>` notified back; the task's output file at the
  one name the harness writes (`…/tasks/<id>.output`, same `O_NONBLOCK`
  open) says whether it exited (`[exited with code`). Nothing is stored
  between looks, so a finished task releases at the next decision;
  `PARK_MAX_SECONDS` (60 min, over a TestFlight archive) bounds being
  wrong; Grok and Codex end no turn on pending work and never park here. A
  parked parent is exempt from eviction (`async_park_at`, on the same
  window, stamped by both file-reading rungs). Cleared by
  `UserPromptSubmit`, failing **open**. `StopFailure` is never parked.
- **Four evictions, and a tombstone after all of them.** Wall-clock staleness
  (`preferences.DEFAULTS["session_timeout"]`, 300s), a dead PID (90s
  `PIDLESS_GRACE_SECONDS` for one that never had a pid; `waiting` is exempt),
  **a hosted terminal the broker no longer holds** (`hosted` on the state,
  persisted; `_retire_hostless_sessions` after the startup attach and the
  pidless rung of `_check_liveness`, verdict `terminal closed`, judged only
  while `PtyHost.connected`; `docs/pty-broker-contract.md`), and a
  subagent-holding session, never evicted. Every removal routes
  through `_forget_session(sid, reason)`, so *Recently finished* can say what
  a run cost; `finished_at` is the session's `last_event`, **not** the removal
  time, and the bucket is in-memory only. Both clients read
  `TerminalWhereabouts` (`DetailTab.swift`, phone copy byte-equal) for the
  `# terminal` line: an unhosted `adhoc` row says **terminal gone**, never
  "find it in the editor yourself".

A row kept after its tab vanished publishes `tab_gone` (*A Grok turn outlives its tab*, `docs/session-state-contract.md`).

- **The card's work record carries the shunt figures**: three `card_runs`
  columns (v24, `_ADDED_COLUMNS` defaults: `shunt_delegations`,
  `shunt_lines_kept_out`, `shunt_worker_cost_usd`), written by `close_run`
  alone off `work_record.read_shunt_ledger` in `_collect_work_record` (a
  missing ledger is zeros and cost unknown, never an error; the cost is
  `None` unless every delegation carried a measured USD; an `ok: false`
  record adds no lines). `run_headlines` carries the three; the record
  carries `work_record.shunt_words`, the one sentence both clients draw
  verbatim; `RECORD_KEYS` pins the Swift twins.
- **Three run-health counters ride the session state** — `permission_asks`
  (+1 at the channel relay's and the hook broker's ask registration),
  `permission_denied` (+1 in `answer_permission` on `deny`) and
  `stop_failures` (+1 on the `add` path's `StopFailure` branch) — stepped
  through `_count_on_session` **on the loop only**, never in
  `_log_permission`, which also runs on the executor under
  `_reap_permissions`; `_collect_agent_stubs` copies them onto the stub
  (`.get(..., 0)`) and `_enrich_agent_stubs` publishes them on the row
  beside `stats`. `sessions.json` carries them with the rest of the state;
  an older build reads past them. Read by `run_health.compose`
  (`docs/context-board.md`).
- **The daemon acts on a session in exactly three places**, each decided on the
  executor and performed on the loop: `autocompact.py` types `/compact` on the input
  line (never the channel, Codex excluded, one attempt per episode),
  `alerts.py` decides a banner `dark_army_menubar/notifier.py` delivers
  (transitions only, `_undelivered` drained once after the push,
  each drained alert logged to `buzz-ledger.jsonl` by `buzz_ledger.py` —
  `docs/transport-contract.md`, *The buzz is written down*; the phone leg
  is filtered (`live_activity.shown_sessions`), gated on Mac presence,
  held — *The phone leg is filtered, gated and held*),
  and `_type_answer_burst` answers an `AskUserQuestion` dialog with a keystroke
  burst that **follows the installed CLI's widgets** — a CLI bump re-reads the
  dialog before any of the tuples are trusted. Withholding a card
  (`_parked_reason`, `_finished_quietly`, `_mission_reply`) is not a fourth:
  none of them types, delivers or evicts.

- **A report's `<!-- dark-army-next: rebuild -->` line offers a button, never a
  card.** `session_stats` stamps `rebuild_marker_at` and `_enrich_agent_stubs`
  publishes `rebuild_offered` (`docs/session-state-contract.md`); it touches no
  bucket and acts on no session.
