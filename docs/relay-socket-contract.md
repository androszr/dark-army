# Relay socket contract

This is the away door's **fast lane** in full: one live, sealed WebSocket per
paired phone to a small relay the person runs (`relay-ws/`), held beside the
mailbox (`docs/transport-contract.md`, *The relay is the second door*), which
stays untouched underneath as the fallback. A trial, off unless switched on,
measured before it is kept (`docs/2026-09-21-relay-socket-mvp-figures.md`).
`CLAUDE.md`'s universal invariants and `docs/transport-contract.md` restate
the rules the socket inherits — the seal, the counters, `REMOTE_ACTIONS`,
the lease, nothing secret on a snapshot; this document is the socket's own
text, and the tests it names are the ones that pin it. The phone's half is
`docs/phone-contract.md`, *The phone goes relay-first once it knows it is
away*.

## The socket is the away door's fast lane (MVP)

**Beside the mailbox, one live line per paired phone — a trial, off unless
switched on, measured before it is kept.** The service is `relay-ws/`
(`server.js`, one dependency, `ws`, deployed on Fly.io by the person;
`README.md` there), and it has five properties, each pinned by
`test_relay_ws_server.py` and `relay-ws/tests/server.test.mjs`: it **reads
nothing** (text frames forwarded verbatim between the `mac` and `phone`
sides of `/ws?ch=<32 hex>&side=mac|phone`, `CH_SHAPE` the mailbox's
literal); it **logs nothing it carries** (no logging call in the file); it
**caps how fast either side may send** (`RATE_PER_MINUTE` 240 per side per
channel, a fixed window, then `4008 slow down`); it **drops anything
oversized** (`maxPayload` = `MAX_FRAME_BYTES`, the library's 1009); and it
**holds no password** that could be used against the Mac (no `process.env`,
no secret — knowing the channel id buys ciphertext and denial of service,
the mailbox's own footing). A browser-shaped upgrade (an `Origin` header)
is 403; over `MAX_CONNECTIONS` (512), or over `MAX_CONNECTIONS_PER_ADDRESS`
(16) from one `Fly-Client-IP`, is 503 — so one stranger cannot fill the
relay and lock the owner out; a frame from a client
beginning `peer:` is dropped, that prefix being the service's own word for
the other side arriving (`peer:1`) or leaving (`peer:0`); a missed pong
(`PING_MS` 25 s) terminates the line. **Newest wins**: a second socket on a
held side closes the first with `4001 replaced`, `MAX_LAN_STREAMS_PER_DEVICE`'s
precedent — an attacker with the channel id can displace either side, the
denial-of-service class the id already buys, and nothing more.

**Same key, same namespace, same counters, same channel id.** The socket
carries `relay.RELAY` frames sealed with the phone's existing relay key and
advances the existing `send_ctr` / `recv_ctr` (`sendCtr` / `recvCtr` on the
phone). A frame captured from the mailbox and replayed into the socket, or
the other way, is refused by the shared monotonic counter; a *stale* answer
crossing paths late (a mailbox reply for a request the phone had already
given up on, popped after socket frames advanced `recvCtr`) is refused as
`ctr` and skipped, which is the right fate for a stale answer — the counter
rule is not widened for it. No new namespace, info string, vector or
persisted counter exists; `test_relay_protocol.py` is untouched. The one
thing a socket frame does **not** do is stamp `relay.last_frame_at`
(`relay.note_recv_ctr(..., liveness=False)`, the socket's call alone): the
mailbox connector keeps its own pacing and idles down its ladder while the
phone lives on the socket, which is the Upstash saving the socket is meant
to show; the cost, stated on the figures page, is that the first mailbox
trip after a socket drop can wait one idle gap, inside the phone's 45 s
deadline.

**The Mac pushes only down an armed socket.** `relay_ws.RelaySocketConnector`
(`relay_client.RelayConnector`'s shape on the daemon loop: `start` / `stop`,
`_supervise` one task per `relay.channel_ids()`, `_serve` per device
connecting with `websockets.connect(url + "/ws?ch=…&side=mac", origin=None,
max_size=RELAY_FRAME_MAX_BYTES + 1024)`, `_handle_wire` inline — frame
bucket, key re-read per frame, `open_frame`, the one answered refusal `ctr`
with `ctr_expected`, `note_recv_ctr` durable for `action`, write bucket —
`_execute` as its own task calling `ApiServer._remote_run` and nothing else,
`_answer` under a per-device lock). A socket is **armed** by its first frame
`open_frame` verifies on *that* socket (a `ctr` refusal — a seal that
verified with a stale counter, a captured frame's signature — arms nothing)
and disarmed on every close; `_push_all` checks `armed` per device at the
moment of sending. The relay's `peer:` words can only **narrow** the arming,
never grant it: a `peer:1` after arming is a displacing `side=phone` socket
and a `peer:0` is the phone leaving, so either drops the device from
`_armed` (`_disarm`, a broadcast when it was armed) and nothing is pushed
until a fresh verified request re-arms the line
(`test_a_displaced_phone_gets_no_push_until_it_re_arms`); the phone reads
the same words into `RelaySocket.peerPresent` and takes the socket lane only
while the Mac is on the other side (`docs/phone-contract.md`). A stranger
holding the channel id connects and receives
nothing — `test_relay_ws.py::test_an_unverified_socket_gets_no_push_while_broadcast_fires`.
The push itself is `ApiServer.on_picture` → `nudge()` (called at the top of
`_broadcast()`, before its SSE early return, under `try`/`except`; it books
one `call_later(WS_PUSH_MIN_INTERVAL 0.5)` and nothing else) → `_push_all()`:
per armed device, the push bucket (`WS_PUSH_MAX_PER_MINUTE` 60), then
`_remote_run("state", {"done": "review", "digest": <last handed down>,
"with_usage": True})` — the same `state()` through `_state_answer` the panel
and the mailbox carry, computed no second way — `unchanged` sends nothing,
else one `push` frame `{status, content_type, body}` under
`DIR_MAC_TO_PHONE`'s next counter and the digest remembered. A phone's own
`state` answer for the review-only board down the line also sets that
digest, so a poll is not followed by a duplicate push. The picture and
its digests are built **once per push**, not once per phone
(`_state_answer`'s `prebuilt`), and a device whose remembered digest
already matches is skipped before its bucket is taken. `_push_all` never
broadcasts; a connect, arm or close does, so the Devices menu follows.

**The ladder, the health and the words.** `WS_RECONNECT_START` 1 s doubling
to `WS_RECONNECT_MAX` 60 s, reset only after `WS_STABLE_SECONDS` (30 s) open
— a relay that accepts and closes at once climbs the ladder rather than
spinning — and `4001` waits the maximum; one `_Health` per device with
`relay_client`'s discipline (one WARNING on the transition, subject `relay
socket`, consequence "the socket lane is down until this clears; the mailbox
carries the phone", a reminder per `HEALTH_REPORT_SECONDS`).
`devices_snapshot` publishes `relay_ws_enabled` (the switch), `relay_ws_url`
(the address, as `relay_url` is), `relay_ws_health` (`health_snapshot()`,
`relay_health`'s five key names so its two moving fields are already in
`_POLL_ECHO_FIELDS`, present only while the connector exists and both
switches are on) and per row `socket` ∈ `("off", "connecting", "open",
"armed")` — no key, digest, channel id or counter, the regex in
`test_the_snapshot_carries_socket_health_and_the_word_and_no_secret`.
The switch on with **no address stored** is a standing of its own rather
than nothing: `state` `no-address` (`HEALTH_NO_ADDRESS`; `bad-address` for
a stored address the connector refuses, reachable only by editing
`relay.json` by hand), `status` 0, `failures` 0, `failing_for` since the
spell began, the same five keys, one WARNING on the transition and none per
`WS_URL_RETRY_SECONDS` retry; the panel's `socketLine` draws it as "socket
link: no socket address set"
(`test_no_socket_address_is_a_standing_of_its_own_with_one_warning`,
`RelayDevicesMenuTests.testNoSocketAddressDrawsItsOwnSentence`).
An address arriving to a relay that is down ends the spell as a fresh
ok→failing transition — `_note_failure` resets a `no-address` /
`bad-address` record before `_record_failure`, so the standing says
`failing` with the status and one WARNING at once, never `no-address` with
status 0 until the reminder
(`test_an_address_arriving_to_a_down_relay_says_failing_at_once`;
`test_a_refused_stored_address_is_one_warning` pins the refused address to
one WARNING for the whole spell).
`stop()` clears every container **before** awaiting the cancelled tasks and
defers a `start()` that lands meanwhile until it returns, so a quick off→on
of either switch never has the restarted connector's fresh line wiped
(`test_a_start_racing_a_stop_leaves_the_connector_armed_capable`).
Every refused frame is `_record_access("ws", …)` and every trip is timed on
door `ws` (`access_log.DOORS`, `DOOR_WORDS["ws"]` "the relay socket",
`DOOR_LABELS["ws"]` "socket"; hops `open` / `run` / `answer` / `size`, `run`
/ `answer` for a push under kind `push`; the sentence `socket: N requests in
10 minutes — opened in … typical, … slow; handled in …; answered in …`),
so the Mac's ten-minute summaries carry a socket line beside the relay
line — the success criterion's Mac half,
`test_a_socket_trip_is_timed_on_the_ws_door`.

**The switch, the address and the pair reply.** Preference `relay_ws`
(default off, its own key, never renamed; `PANEL_ACTIONS["set_relay_ws"]` →
`BobDaemon.set_relay_ws`, `set_remote_access`'s stash-then-apply shape); the
connector is started only while `remote_access_enabled and relay_ws_enabled`
and `set_remote_access(False)` stops it too — the socket is a second way
through the away consent, never a way round it. The address is
`relay.set_ws_url` / `get_ws_url` (`relay.json`'s `ws_url`, `wss://` or
empty, refused in `WS_URL_REFUSAL`'s words: "the socket address must start
with wss://"; a pasted trailing `/ws` is stripped at the store, since the
connector appends the endpoint itself and would otherwise dial `/ws/ws`;
plain `ws://` to loopback is the connector's test seam, as
`http://127.0.0.1` is the mailbox's), set by the loopback verb `set_relay_ws`
on `_devices_request`'s allow-list — behind the token, the `Host` check and
the Origin allowlist, on **neither** `LAN_ACTIONS` nor `REMOTE_ACTIONS`
(`test_relay_lease.py::test_set_relay_ws_is_loopback_only_and_refuses_a_plain_address`).
**It rides the pair reply, never the QR and never a snapshot as a thing to
adopt**: `_mint_relay` answers `(relay_key, relay_url, relay_ws_url)` —
the first two while `remote_on`, the socket address only while `remote_on`
**and** `relay_ws_enabled` (a daemon without the attribute hands out none),
so a phone paired while Socket link is off carries no socket address — and
both pair replies (`_lan_pair`'s PAKE finish and `_lan_pair_sealed`) carry
`relay_ws_url` beside `relay_url`; a phone paired before the address was
set, or while the link was off, re-pairs at home to gain it, the relay
address's own rule, which the relay sheet states
(`test_the_pair_reply_carries_the_socket_address_only_while_socket_link_is_on`).

**A headless bot is a device of its own.** The socket relay holds one
`side=phone` per channel and the newest connection replaces the other, so
a bot that dialled the phone's channel would take the phone's line.
`pair_bot` on `_devices_request`'s loopback allow-list — the token, the
`Host` check and the Origin allowlist, on **neither** `LAN_ACTIONS` nor
`REMOTE_ACTIONS` — mints one row without a QR and without spending the
pairing code. A second call refuses while that row remains
(`devices.headless_paired`); un-pairing it is what opens the verb again.
A phone row carries no `headless` key, so the phone does not close the
door and the bot does not take the phone's channel. It refuses unless Away access is on, Socket link is on and
a socket address is stored, the same gates `_mint_relay` uses before a
pair reply carries `relay_ws_url`. The reply is the phone's pair reply
plus the home key (a headless caller has no square to have read it from),
returned once and never put on a snapshot. The row is an ordinary device:
its own channel id and key, its own counters, `unpair_device` forgets
both, and its reads and writes are the two bot grants
(`docs/transport-contract.md`, *The bot's access is two grants*), not
the phone's day window. The speaker is
`host/dark_army_daemon/relay_bot.py` (`python -m
dark_army_daemon.relay_bot`), which saves the reply at 0600 and seals
with `relay.seal_frame`. `serve` keeps that line open and answers MCP
`picture` and `act` on `127.0.0.1:19876/mcp`, behind a bearer kept in
`grok-bot-mcp-token` (0600, never printed). A tunnel in front of that
port is what a Grok account's custom connector plugs into; the relay key
stays on this Mac. Pinned by `host/tests/test_pair_bot.py`.

Pinned by `test_relay_ws.py` (the
fake socket relay under `websockets.serve`), `test_relay_ws_server.py`,
`test_access_log.py`, `test_relay_lease.py`, `test_lan_access.py`,
`test_product_name.py`, `test_phone_remote.py` and
`test_relay_latency_bench.py` (`--socket`); the phone's half is
`docs/phone-contract.md`, *The phone goes relay-first once it knows it is
away*; the figures page is `docs/2026-09-21-relay-socket-mvp-figures.md`.
