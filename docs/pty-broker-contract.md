# The terminal-hosting broker

Lifted out of `CLAUDE.md` on 6 Sep 2026, which keeps the short contract and
points here. `host/dark_army_daemon/ptyhost.py` (the daemon's side) and
`host/dark_army_daemon/pty_broker.py` (the helper process) keep a hosted
agent terminal alive across a restart of Dark Army.

## The line they speak

`BROKER_LINE_LIMIT` lives in `ptyhost.py` — **one definition**, referenced as a
module global on both sides so a test can patch it. It is sized for the
greeting's worst case: `HELLO_TERMINALS_HEADROOM` full rings plus
`HELLO_SCREEN_HEADROOM_BYTES` each, because the hello and the `start` / `reap`
replies carry every terminal's whole ring as base64. asyncio's 64 KiB default
was what once left a new daemon detached from three live agents.

The broker's `data` frames are sliced at `DATA_FRAME_BYTES` **in order from one
coroutine**, so a not-yet-upgraded daemon still reads them. One rung on, the
pane's stream slices its opening paint at `terminal_stream.PAINT_CHUNK_BYTES`
(half `MAX_FRAME_BYTES`, or the codec's `max_plain` where smaller) and stops
coalescing live `D` frames at the same figure, so one further `DATA_FRAME_BYTES`
item overshoots it and still fits the frame cap.

## Connecting, and never doubling up

`_broker_connect()` answers `connected` / `refused` / `unreadable`, and
`attach()` spawns **only on `refused`** — a second broker over a live socket
unlinks its path and strands its terminals. The broker itself probes before
unlinking and exits 3 when somebody answers.

A link that drops mid-run is a WARNING and `_reconnect_loop`
(`RECONNECT_FIRST_SECONDS` doubling to `RECONNECT_MAX_SECONDS`), which
`disconnect()` alone ends (`_closing`); a reattach carries each terminal's
subscribers over and feeds the gap, so the panel's open socket keeps drawing.

The broker is always spawned with `--sock` **and** `--pidfile`.

## When a broker stops

Two rules, and the second was added on 6 Sep 2026 after four unreachable
brokers piled up on one machine in an afternoon.

**Idle.** With no live terminal and nobody connected, it quits by itself on
`IDLE_EXIT_SECONDS`, checked every `ORPHAN_CHECK_SECONDS`.

**Stranded — the moment its socket is no longer the one it bound.**
`Broker._stranded()` compares `(st_dev, st_ino)` recorded in `start()` against
the path today, so a *replaced* door counts as a deleted one: either way nobody
will ever reach this process again. That rung **outranks** the live-terminal
rung in `_should_exit()`, which made the old existence check dead code for any
broker actually doing its job; the terminals it holds go with it, through the
idle exit's own `await self.engine.close_all()`. It never fires with no
recorded identity, and never while `_writer` is set — a connected daemon **is**
reachability, whatever the path says.

**Only a stat that answers strands it.** `FileNotFoundError` /
`NotADirectoryError` — and an inode that has moved — are the whole rule. Any
other `OSError` is *cannot tell*, not *unreachable*: one WARNING per process
and the broker stays up. This rung closes live terminals, so a transient EIO
at a 30s tick must never be enough to fire it.

**Never by hand.** The broker is the one process that keeps a hosted
terminal — and the agent inside it — across a daemon restart: Quit and
Restart both go through `_shutdown(keep_terminals=True)` and leave it up on
purpose. `pkill -f pty_broker`, `kill` on its pid, `osascript quit` followed
by a sweep of "everything Dark Army" — any of these closes every hosted
terminal and kills every session inside, and the rows come back after the
relaunch as ghosts. An agent rebuilding or relaunching Dark Army from a
terminal stops the *app* (`osascript -e 'quit app "Dark Army"'`, or the
panel's Restart) and nothing else; the only sanctioned way to end the broker
is the kill switch, which a person presses.

**The `~/.bob-companion` link keeps a live broker reachable, and it is
permanent.** A broker that bound `~/.bob-companion/pty.sock` before the
folder became `~/.dark-army` stats a path that still resolves, through the
link, to the same `(st_dev, st_ino)`, so `_stranded()` never fires. No step
may restart the broker to "pick up the new path", and no code may remove the
link. Learned on 12 Sep 2026, when a
sibling agent's rebuild command ended in `pkill -TERM -f
bob_companion_daemon.pty_broker` (the module's name then) and took a colleague's ad-hoc terminal with
it.

**And the daemon notices when one has.** Every session named for a hosted
terminal carries `hosted` on its own state, persisted with `sessions.json`
(the pid is not). After the attach at startup — before the first snapshot —
`BobDaemon._retire_hostless_sessions` forgets every `hosted` session the
broker's hello no longer lists, with the `terminal closed` verdict
(`TERMINAL_CLOSED_REASON`), its restored question dropped with it;
`_check_liveness` applies the same rung to a pidless hosted row. The broker's
list is read only while the link is up (`PtyHost.connected`): an empty map
during a reconnect is not a list of zero terminals. A hosted row with a live
pid is the identity check's, as before — a session resumed in an editor after
its terminal went is not re-judged by a list it is no longer on. Pinned by
`test_hosted_terminal_gone.py`.

## The daemon's backstop sweep

For a broker too wedged to notice, `PtyHost._sweep_strays()` runs **one shot**
at the first attach that ends connected (`_swept` set first, so a failed sweep
never retries), off the loop via `asyncio.to_thread`, and its failure is logged
at WARNING and never raised into the attach.

**That includes the leg that spawns a broker, and that is the principal case.**
A stranded broker no longer listens, so `_broker_connect()` answers `refused`,
the daemon starts a fresh broker and connects to *that* — the ordinary
startup-beside-strays picture. `attach()` therefore has **one** sweep site,
below both legs, reached only once `connected` is true: by then
`_broker_connect()` has taken the live broker's pid out of its hello, so the
exclusion below is intact whichever leg got there.

It signals `SIGTERM` at a single pid — never `os.killpg`, never `SIGKILL`,
never `_signal_group`; the stray's pty children take their SIGHUP from the
master fds closing. Targets come from the pure classifier
`stray_broker_pids(entries, sock_path, live)`, which admits a `(pid, argv)`
entry only on an **exact argv match**: an element `-m` immediately followed by
`BROKER_MODULE` as an exact element, never a prefix, and this exact socket by `realpath` in either `--sock P` or
`--sock=P` form — never a substring of a joined command line, since `grep`, an
editor and this project's own tests all mention that module name. An argv
naming no socket never matches. `live` — the daemon's own pid, the pid the
broker announced in its hello (`{"hello": True, "pid": …}`), and the pid file's
— is excluded, and the list truncates to `MAX_STRAY_SWEEP`, lowest pid first.
An entry whose real uid is not this user's is skipped, and an **unreadable**
uid (psutil answers `None` where the platform refused) is not ownership: it is
skipped too, because over-excluding is the safe direction for something that
ends in a signal. `_spawn_broker` passes `self.pid_path` as `--pidfile`
whenever it is set, so the pid file the sweep reads and the one the broker
writes have **one derivation**.

**The target is proved twice.** The scan names a pid; `_signal_stray` then
re-reads that one process's argv (`psutil.Process(pid).cmdline()`) and runs the
single entry back through `stray_broker_pids` immediately before `os.kill`. A
stray that exited in between frees its number for anything on the machine to
inherit, so a mismatch — or a `psutil.Error` — returns without signalling.
`_signal_stray` returns whether it signalled: the WARNING names the pids
actually signalled, never the candidates, and a refusal (gone, recycled, not
permitted) is one INFO line saying which.

**The hello pid alone enables the sweep**, and the pid file is an exclusion
only. `self._broker_pid > 0` is the gate: the connected broker is the one
process this daemon positively identifies. A broker from a build older than
the hello's `pid` key sends none, so the sweep does nothing at all against it
— a stale `pty.pid` from a previous boot, naming a number the machine has
since recycled, must never be the thing that licenses a `SIGTERM`. Such a
broker cannot retire itself either, and the sweep starts working after one
upgrade cycle; that is the right cost. The pid file is read with
`Path(...).read_text()`, never `open(...)`:
`test_ptyhost.py::test_nothing_here_reaches_a_file_under_paths` greps for it.

Pinned by `host/tests/test_ptyhost.py` and `host/tests/test_pty_persist.py`.

## The child's environment

The broker is started by the app, and the app is routinely relaunched by a
dev rebuild — or an `open -a` — from *inside* an assistant's terminal. The
broker then keeps that environment for as long as it lives, across every
daemon restart. A child started on its pty must not be told it is that
session: with `GROK_SESSION_ID` inherited the hook stamps `provider=grok`,
liveness asks `looks_like_grok` of a claude binary and evicts a live session
as "PID gone" at the first tick; the channel announces the other session's
id and is refused as "already held"; a stale `BOB_COMPANION_ORIGIN` says the
row was opened to plan a card it never saw.

So `PtyHost.start` runs `subprocess_env.clean_env`, which strips
`SESSION_ENV_VARS` (Grok, Claude and Codex ids, `CLAUDECODE`, `CLAUDE_PID`,
the origin stamp, `TERM_PROGRAM`) and the `SESSION_ENV_PREFIXES` families
(`CLAUDE_CODE_SESSION_*`, `CLAUDE_CODE_MESSAGING_*`, `CLAUDE_CODE_BRIDGE_*`)
and keeps what a person set (`CLAUDE_CODE_DISABLE_TERMINAL_TITLE`,
`CLAUDE_CONFIG_DIR`, `ANTHROPIC_*`). The one stamp put back is the caller's
own, read off the `env` argument after the strip — `spawn_local`'s or the
broker allow-list's — never the inherited one. Pinned by
`test_subprocess_env.py` and `test_ptyhost.py`.
