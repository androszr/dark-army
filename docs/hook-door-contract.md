# The hook door is a private socket

The contract for the door every Claude Code hook, status-line refresh and
channel call reaches Dark Army's daemon through. Kept apart from
`docs/transport-contract.md`, which names it in its opening paragraph and is
held under this checkout's whole-file read threshold. `docs/context-host.md`
(*dark-army-notify*) carries the short form. Plan:
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
