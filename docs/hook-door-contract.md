# The hook door is a private socket

The contract for the door every Claude Code hook, status-line refresh and
channel call reaches Dark Army's daemon through. Kept apart from
`docs/transport-contract.md`, which names it in its opening paragraph and is
held under this checkout's whole-file read threshold. `docs/context-host.md`
(*dark-army-notify*) carries the short form; *The handler* below is the long
form of what the script does on the way. Plan:
the *hook socket private unix socket* plan.

## The door

**Every hook message reaches the daemon through `~/.dark-army/hook.sock`**
(`paths.HOOK_SOCK_PATH`; `DARK_ARMY_HOOK_SOCKET` overrides it for the daemon
and every client). A port is first come, first served: while Dark Army
restarted, another account could bind `127.0.0.1:19873` and hear every
message — each carries the project's enrolment key, a channel's heartbeat its
port and secret — and push `kind=user` into live sessions (review finding S1,
22 Sep 2026). `SocketServer.start()` clears a leftover file first
(`_clear_stale_socket`: probe-connect; nothing answering, or not a socket, is
unlinked; an answerer is unlinked too, with a WARNING, because `daemon.lock`
already proves no second daemon — never the pty broker's refusal), binds under
`umask(0o177)` for that one `bind()` alone, `chmod`s 0600 and records
`(st_dev, st_ino)`; `stop()` unlinks only that inode, so a late shutdown never
removes a successor's door. `hook.sock` is in `paths._PRIVATE_FILES`. Each
accepted connection is asked `getpeereid(2)` over `ctypes` (`_peer_uid`, a
capability probe): another uid is closed unread with one WARNING per process,
no reading at all is accepted — the 0700 folder is the boundary. A path over
103 bytes (`sun_path`) is one ERROR and the port serves alone. The message,
`_serve_one`'s one line up and one line back, and `_handle_message` are
unchanged: the door moved, the message did not.

## The address rule

**One address rule, in all three scripts and the channel**
(`NOTIFY_SCRIPT` and `STATUSLINE_SCRIPT` `_hook_connect`,
`channel_server.daemon_address`): `DARK_ARMY_HOOK_SOCKET` set → that socket
and nothing else; else `BOB_COMPANION_PORT` / `CLAWD_TANK_PORT` set → that
TCP port and nothing else; else the default socket, spelled under
`.dark-army`. **Explicit means exclusive** — no client falls through to a
second door, and no client dials `19873` unless an environment names it.
Hosted terminals are told the socket: `PtyHost(hook_sock=)` puts
`DARK_ARMY_HOOK_SOCKET` in the child's environment and the port only when no
socket is known; `_spawn_broker` passes `--hook-sock` and the broker's hello
carries `hook_sock`.

## The bridge

**The port is a bridge for one release.** Still on it: sessions in terminals
an older broker opened (it hands `BOB_COMPANION_PORT=19873` to every terminal
until it is replaced, and it outlives the app on purpose — the daemon logs one
WARNING at attach when the hello has no `hook_sock`), channel processes
started before the upgrade, and any shell exporting the old variable
(`clean_env` keeps it). While the bridge stands, an account that binds the
port during a restart still hears those sources. The bridge logs one INFO
line per session (`hook over the network port from session …`, twelve
characters of the id and the event, never a key); a follow-up retires the
listener and every client's TCP leg once that line has been absent a week and
the broker has been replaced. A downgrade rewrites the three scripts to its
own TCP copies and binds the port; the socket file is left behind harmlessly.

## The quiet helpers

**The quiet helpers stay quiet on both doors.** `card_prepare.env_for` and
the shunt wrappers' `worker_env` set `DARK_ARMY_HOOK_SOCKET` to
`QUIET_HOOK_SOCKET` (`/dev/null`: `connect()` fails at once) beside
`BOB_COMPANION_PORT` = `QUIET_HOOK_PORT`, so a hook from a notify script of
either age reaches nobody; an old wrapper setting only the port is still quiet
under the rule. Pinned by `test_socket_server.py`, `test_notify_script.py`,
`test_statusline.py`, `test_channel.py`, `test_ptyhost.py`,
`test_pty_persist.py`, `test_card_prepare.py` and `test_shunt_wrappers.py`.

## The handler

What `dark-army-notify` does on the way through the door — the standing
hints, the read guard, the close-out helper and the `PermissionRequest`
broker — relocated from `docs/context-host.md` on 25 Sep 2026 (the
*trim role load context host* plan; `docs/ship-efficiency.md` tracks the
paragraph as `3171734ffe6d`).

> **It carries two standing hints into every Claude session.** On
> `session_start`, for Claude only (never Grok), the handler prints
> `TLDR_HINT` and `WORK_REPORT_HINT` on stdout, unconditionally; a third,
> `NEXT_STEP_HINT` (the `<!-- dark-army-next: rebuild -->` marker, parsed by
> `session_stats._NEXT_RE`), and a fourth, `SEARCH_SCOPE_HINT`, which lists
> `search-scope.json`'s folders (`docs/agent-pack.md`), follow them. `bob-tldr`
> / `bob-actions` mark a turn that is **waiting on somebody** and are
> parsed by `session_stats._TLDR_RE` / `_parse_actions`; the `## Work done`
> report marks a turn waiting on **nobody**; `work_report` parses it for
> drawing, never state. On its
> heading, `session_stats._work_report` slices from `## Work done` to the
> end of the message into **`last_report`**, kept beside `last_text`,
> carried across every later message and cleared by the person's next
> prompt. Grok lifts the same heading from `chat_history.jsonl`. The panel
> draws it under the latest words as `# work done`
> (`StdoutPane.reportToShow`), never twice. Contract tests pin
> the marker at both ends and forbid a literal `bob-tldr`/`bob-actions`
> comment inside the report text.
>
> **`WORK_REPORT_HINT` is also the machine-wide lever for how a leftover
> check is written.** An `**Unchecked:**` item is **numbered imperative
> steps** — what to open, what to press, what the person should see —
> followed by one required line beginning `Why not automated:`, written
> to its own file at `manual-check/<YYYY-MM-DD>-<slug>/check.md`, and a
> session working a board card flags it with that path
> (`dark_army_needs_manual_check`) and **then** closes the card — the
> close is the last act whether or not a check is left. A
> machine-wide `~/.claude/CLAUDE.md`, outside the repo, carries the same
> two rules for sessions Dark Army's hooks do not reach. Contract tests pin the
> wording.
>
> **It lives as a string, not a file**: `NOTIFY_SCRIPT` in `dark_army_menubar/hooks.py`, written out to `~/.dark-army/` on install. Edit the string.
>
> **A second script guards big reads: `SHUNT_SCRIPT`** — the bytes of
> the real module `dark_army_menubar/shunt_hook.py` (ruff reads it,
> `test_shunt_script.py` imports `decide()`; edit the file and bump its
> version line), installed by `install_shunt_script()` to
> `paths.SHUNT_SCRIPT_PATH` under the notify script's rules, in a
> **second Dark Army-managed `PreToolUse` group**
> (`SHUNT_COMMAND`, `SHUNT_MATCHER`; `_command_is_ours` knows both, so
> the group is pruned and rewritten as Dark Army's beside the untouched notify
> group). Armed only where the home-skipping walk from `cwd` finds
> `.claude/skills/shunt/SKILL.md`, it denies a whole-file read over the
> dial (`BOB_SHUNT_MIN_LINES`: the environment, then the project's
> `.claude/settings.json` `env`, else 350); what passes is that
> SKILL.md's list. **Never for a reviewer**: an `agent_type` ending in
> `SHUNT_REVIEWER_SUFFIXES` or a per-session marker under
> `paths.SHUNT_EXEMPT_DIR` (0700). **Any failure is an allow**: bounded
> reads, no socket, no subprocess, exit 0, silent. Grok's file carries
> the group too; Codex gets it alone, key-merged into
> `~/.codex/hooks.json` (`CODEX_HOOKS_PATH`, foreign groups kept), run
> once hooks are trusted (`docs/codex-ship.md`, step 7). **`install_hooks()`
> writes both scripts before any group names them** — a registered
> `PreToolUse` command whose file is missing exits 2 and refuses every
> Read and Bash on the machine; Grok and Codex writes are best-effort,
> the Claude settings write must succeed. The wrappers append one line
> per delegation to a ledger under `paths.SHUNT_LEDGER_DIR` (0700, never
> content), folded into the card's work record.
>
> **The `/ship` close-out script is installed the same way** —
> `hooks.install_close_out_script()` copies `.claude/skills/ship/close-out.sh`
> (bundle resource `dark-army-close-out.sh`, copied by `build.sh`) to
> `~/.dark-army/dark-army-close-out` on every launch, compared by
> content; every project's copy is a shim handing off to it, arguments
> forwarded, under `BOB_CLOSE_OUT_SHIM` — and only to a copy carrying the
> `# close-out-contract: leaves-open` and `# close-out-mode: plan`
> markers, so a helper from before the leave-open rule or the `--plan`
> mode is never handed the job. No source found leaves the
> installed copy alone, never unlinked. The helper reads
> `~/.dark-army/api-token`, which is the **session token** and never the
> desk token: its two verbs (`close_terminal`, `close_refinement_terminal`, never
> `by_person`) are the session tier's, so an agent closing its own tab still
> works and finishes no card (`docs/transport-contract.md`, *The loopback
> door has two tokens*).
>
> **And on one event it *holds*: the `PermissionRequest` broker.** A
> board-dispatched session has no channel, so a "may I run this?" dialog
> would otherwise reach Dark Army as a bare `waiting` row nobody could answer.
> The hook mints a `request_id` + a `claim`, sends `permission_ask` (tool,
> a one-line `description` off `file_path`/`path`/`command`, an
> `input_preview`) and, on a positive `hold`, polls `permission_poll`
> until a verdict comes back, printing `hookSpecificOutput.decision` to
> answer the dialog. **The dialog and the hook run concurrently, and
> whichever answer lands first wins** — watched on 2.1.263
> (`tools/permission_hold_livefire.py`, which **a CLI bump re-runs**). Not
> universal: `awaitAutomatedChecksBeforeDialog` awaits the hooks *before*
> the dialog on the `requestDialog` branch and in the **async-subagent
> spawn** context, so **an ask raised inside a subagent is never held**.
> Bounds: 3s per socket op, a 2s poll, an **1800s** deadline under the
> `"timeout": 1860` on the hook entry — **the two move together or not at
> all**, a raised deadline under the old timeout capping the hold silently
> (`test_permission_broker.py` pins it); **every** failure — no daemon,
> no reply, `{"hold": 0}`, `"gone"`, the deadline — exits 0 having printed
> nothing. Never `updatedPermissions` and never `updatedInput`: an allow
> from Dark Army is an allow *once*. **Silence falls back to the legacy
> message** — nothing at all from the first exchange means a daemon too
> old to know the verb, so the script sends the old
> `{"event": "permission", ...}`; an explicit `{"hold": 0}` is a
> *deliberate* refusal and is never double-sent. Claude only:
> `_EVENT_ALIASES` maps Grok's `permission_request` onto the same name, but
> a Claude-shaped `hookSpecificOutput` is not Grok's stdout contract;
> Grok's hooks file carries no `timeout` key at all (`grok_hooks_config()`
> strips it); Codex asks arrive by its PermissionRequest
> (`docs/cli-permission-modes.md`). Refused for a **subagent's ask**, for
> **AskUserQuestion**, for a session with a **live channel**, for a session
> Dark Army has no state for, and for a **request id already open** — a refused
> hold still runs the ordinary `permission` event, so the row goes to
> `waiting` either way. Daemon side: `permission_ask` / `permission_poll`
> are `LIFECYCLE_EVENTS` that *reply*; the row carries `via="hook"` and no
> `port`, so `_reap_permissions` substitutes **the broker stopped polling**
> (`HOOK_PROMPT_POLL_LAPSE_SECONDS`, 15s) for "the channel is gone", with
> `hold_until` behind it; `answer_permission` **stages** the verdict for
> the next poll and refuses in words once the poll has lapsed. `claim` is a
> secret with the channel secret's discipline — stripped in
> `_permission_snapshot`, never published, asserted absent. An open prompt
> also clears `can_close`.
