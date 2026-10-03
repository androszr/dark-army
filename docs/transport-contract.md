# Transport contract

This is the phone-to-Mac wire contract in full: both sealed doors (home over the LAN, away through the relay), the reads, the actions, the push route and the connector's health. `CLAUDE.md` restates the invariants and points here; this document is the text they were lifted from, and the tests it names are the ones that pin them. The loopback door the panel reads (`_handle_client`, `_loopback_host`, `_broadcast`, `?sections=changed`) stays described in `docs/context-host.md` (*api_server.py*): it is the panel's contract, not the phone's; which of its two tokens (desk and session) opens what is the section on the loopback door's tokens, below. The away door's socket lane — one live sealed line per paired phone beside the mailbox, a trial — is `docs/relay-socket-contract.md`. The hook door the Mac's own sessions report through, `~/.dark-army/hook.sock`, is `docs/hook-door-contract.md`, *The hook door is a private socket*.

## The LAN door is sealed

**The LAN door is sealed, and it is a chosen list, not the loopback
table.** `_handle_lan_client` (bound `0.0.0.0:19875`, preference
`lan_access`, default off) takes **sealed frames** under a per-device
**home key**: `POST /api/home` (a `relay.seal_frame` in the `relay.HOME`
namespace whose `kind` is `state`, `usage` or `action` for a name in
`LAN_ACTIONS` — a named tuple, not a prefix), `POST /api/upload` (a header
frame in `X-Bob-Frame`, kind `upload`, plus `relay.seal_blob`'s raw bytes
bound to that frame's id as the body), and `POST /api/pair` (sealed under
the QR's key, or **SRP-6a over the code** from a typed address — *The
typed-address pair is a PAKE*, below — **only** for a pairing the Pair
window armed with `allow_typed` (`devices.begin(allow_plain=True)`, one-shot,
dying with the code) — refused otherwise in `PAIR_PLAIN_REFUSAL`'s words as
access-log reason `pair_plain`, before the body is parsed and without touching
the fail counter), and `POST /api/terminal/stream` (one sealed `terminal_stream`
frame as the body, then the framed terminal stream held open under
`SealedCodec` — the section below). Pairing carries write; Origin is still refused. Five failed codes against the
live pairing window call `reset_pairing()`, with one WARNING; the fail
counter is memory-only on `_pairing`. Device identity is `X-Bob-Channel`
(the home channel id, derived from the key, not a credential) through
`devices.device_for_home_channel`; the seal is the proof, and
`_home_open` is the **one verifier** — Origin refused, a stale counter
answered as a sealed `err` with `ctr_expected`, everything else 403 in
`REFUSAL_WORDS`. The home key is minted by `devices.begin()`
unconditionally (a different key from the relay's, with a different
reason to exist), rides `begin_pairing`'s reply to the panel for the QR,
is stored in `devices.json` with two home counters of its own
(`note_home_recv_ctr` durable for `action` / `upload`,
`next_home_send_ctr` reserved), and dies with `unpair`. The three old
plaintext routes — `GET /api/state`, `GET /api/usage`, `POST /api/action`
— answer **426** in `HOME_UPDATE_REFUSAL`'s words and run nothing; no
route on this socket reads a device token, and `devices.resolve` survives
unread by any door (the phone keeps the token as its pairing-generation
guard). The loopback `X-Bob-Token` is ignored here. Unchosen actions 404
inside the sealed reply. Execution helpers only — `_sealed_run`, hoisted
out of `_remote_run`, behind `LAN_ACTIONS` with no lease check and no
remote record; never the `_foo_request` intercepts, which check the
loopback token. No SSE on LAN (`GET /api/events` stays 404); the phone
stays poll-only because iOS suspends background sockets. `wrap_up` is not
on the list; `prepare_card` is, routed to the `_prepare` **helper**, whose
"already writing" refusal is a **202** the receipt ledger does not record
(every other refusal a 409), so a phone replaying a lost reply under the
same `command_token` is handed the finished draft, never the busy sentence.
`hide_session`, `board_unqueue`, `board_queue_move`,
`board_start_project`, `set_board_autostart`,
`set_board_parallel_root` and `inbox_ack` are each explicitly
chosen on both independent home and away lists; `inbox_ack` is its own
decision on each tuple, not a copy, and its payload is a current-state
fingerprint echo. Snapshot section `inbox` carries `available` as the
version marker and the ack records. `board_manual_clear` and `board_review`
are chosen the same way — each its own line on `LAN_ACTIONS` and again on
`REMOTE_ACTIONS`, in `_LAN_BOARD`, beside `board_approve_plan` — and carry
`board_approve_plan`'s shape of echo: `expected_manual_steps`, or
`expected_closed_by` + `expected_close_note` (half a pair is refused before
any UPDATE), envelope keys the store ANDs into its own one-shot WHERE.
`board_promote` (21 Sep 2026) is chosen the same way and carries `card_id`
alone: every field of the new card comes off the scout's own row, it
dispatches nothing, and a second press is refused. Where that report opens with an answer block, the title, summary and notes come instead from the report the scout itself attached (`report_path`, never the phone's words), read bounded and containment-checked again at the press; the press still dispatches nothing.
Absent means no guard (`expected_revision`'s rule), so loopback posts of `{action, card_id}` are unchanged; the echo is a confirmation of what was on screen, never a capability, and no new secret rides the snapshot. The board states `manual_clear_writable` and `review_writable` as two independent version markers; `board_accept_outcome` and `board_request_revision` stay off both phone tuples.
`board_manual_outcome` (25 Sep 2026) is chosen the same way, its own line on both tuples and in `_LAN_BOARD`, and is keyed on the check file's `path`, never a card: `_board_action` handles it **before** the `card_id` check (`status` must be `passed` or `failed`, else 400; the note clamped at `MAX_MANUAL_OUTCOME_CHARS`). The guard is current state re-checked at the write: the path must realpath to `<enrolled root>/manual-check/<folder>/check.md`, be a regular file in a folder that is not a link, pass the checker, and still say `Status: open`, the whole check-and-write under one lock so two presses cannot both write; a second press is `MANUAL_OUTCOME_RECORDED_REFUSAL`, writes nothing, and still clears the cards flagged with that file. Dark Army is the file's single writer — the `Status`, `Outcome` and `Checked at` lines and nothing else — and every card flagged with that file has its steps cleared through `clear_manual`, one publish for the lot. `manual_checks_supported` and `manual_outcome_writable` are its two version markers. `set(REMOTE_ACTIONS) <= set(LAN_ACTIONS)` remains the rule.
`board_merge`, `board_merge_fix` and `board_review_run` (3 Oct 2026) are chosen the same way — each its own line, with its own comment and no parenthesis in the block, on `BOARD_ACTIONS`, `LAN_ACTIONS`, `REMOTE_ACTIONS` and in `_LAN_BOARD`, three decisions rather than one — and carry `card_id` alone, except that `board_merge` echoes the phone's `expected_tip` (the branch tip its Changes page was read at; absent means no guard, `expected_revision`'s rule, so the Mac's plain press is unchanged; a key that is **present** must be a whole lowercase commit hash, and an empty or short one is `TIP_CHANGED`, never read as absent; the task checks it again against the fresh tip before touching the branch). `merge_card` answers at once and merges in a detached task; `fix_merge_card` and `run_card_review` start one assistant each behind `board_dispatch` and `dispatch.helper_guard` under `_dispatch_lock`. Each re-runs `_merge_gate_sync` at the press (Done, a recorded branch, not merged, the hand-check settled in the check file, the project enrolled, nothing running in the folder) and the merge re-checks the card, the folder, the main checkout and the trunk's tip at the moment each is touched; it pushes nothing, and it moves the local main line only by a fast-forward onto the merge commit it built in the card's own folder (`docs/card-worktrees.md`, *Review and merge*). Away they ride the lease, Face ID and the receipt token like every write; the phone arms MERGE and Fix and sends Run review in one press, through the queue. `card_changes_supported`, `merge_writable` and `review_run_writable` are the three version markers on `_pipeline_writable()`, bare bools; an older Mac sends none and the phone draws none of the controls. `set(REMOTE_ACTIONS) <= set(LAN_ACTIONS)` still holds.
`board_refine_batch` (25 Sep 2026) is chosen the same way — its own line on both tuples, each its own decision, and in `_LAN_BOARD` — and is keyed on `card_ids`, never `card_id`: one comma-joined string (at most 4096 characters, split, trimmed, deduped; nothing usable is 400 `no card_ids`), handled **before** the `card_id` check. It is `board_refine` repeated under the same guards: `refine_cards` runs under `_dispatch_lock`, refuses fewer than two or more than `MAX_BATCH_CARDS` (8) in the daemon's words, runs the enrolment refusal and `dispatch.refine_guard` per card and reads each card's root and tool off the store at the press, refuses the whole press on the first card that fails before any write, and opens exactly one planning session; `board_dispatch` off refuses it and it carries no `skip_plan_gate`. Away it rides the lease, Face ID and the receipt token like every write. `refine_batch_supported` is its version marker, a bare bool; `set(REMOTE_ACTIONS) <= set(LAN_ACTIONS)` still holds. `board_start_batch` (25 Sep 2026) is chosen on both tuples and in `_LAN_BOARD` the same way, each line its own decision, and is keyed on `card_ids` exactly as `board_refine_batch` is. It is `board_dispatch` repeated under the same guards: `start_cards` runs under `_dispatch_lock`, re-runs the plan gate, the enrolment refusal, `dispatch.guard` and the dependency gate per card, skips and names a card that fails, refuses a full project, and spawns exactly one session; `board_dispatch` off refuses it and it carries no `skip_plan_gate`. `start_batch_supported` is its version marker, a bare bool. The phone sends it **synchronously** through `post` and never banks it in the receipt queue or the outbox: a batch Start opens a terminal, so it happens at the person's press or not at all (`docs/phone-contract.md`, *Several planned cards are started from the phone at once*). The board snapshot
states `queue_writable` (a version marker), `start_project_writable`
(another, for the whole-project press) and `preferences_writable`
(whether anyone implements `on_preference_request`) so the phone's
PIPELINE screen draws only controls this Mac will honour — absent, never
inert. It invokes the same revision-only Codex Hide as loopback: no signal,
deletion or persistent change. The phone decodes `can_hide` false when
absent and offers **Hide until this thread changes** only when true. Its
existing scoped request/settling flow holds the accepted press, leaves the
detail when a later snapshot removes the row, and releases with a note if
the settling timeout expires while a changed thread remains listed.
`_handle_client` and `_loopback_host` stay the loopback contract. The
phone's half is `HomeChannel` (`ios/BobPhone/HomeTransport.swift`, the
only phone file that builds a home URL) over `RelayTransport`'s
`SealNamespace.home`; `devices_snapshot` states `home_sealed: True` and
`home` per device, so the Devices menu can say "paired before sealed home
access — pair it again" of a keyless phone, and the phone's own
`.unpaired` bar says `PhoneActions.pairAgain`.

**Three bounds on the door, each answered before the request is read.** The
listener stays `0.0.0.0` and `_handle_lan_client` judges the address the
knock *landed on* (`sockname`) with `lan_hosts.arrived_on_tunnel` over a
`live_addrs()` reading memoised for `TUNNEL_ADDRS_TTL` (5s): an address in
the tunnel bucket and not the LAN one is **closed without a response**,
recorded as `tunnel` (`LAN_TUNNEL_REFUSAL` is the words the log and this
page use; the door writes nothing). No status at all, because the shipped
phone stops its walk on *any* HTTP answer and promotes that host — a 403
even un-pairs it — while a dropped connection is "nothing there, walk on"
to every phone build; the current phone additionally skips a 421 host
without promoting it (`Answer.misdirected`), belt and braces for a daemon
that one day answers. Loopback and every non-tunnel address are admitted,
and there is no subnet check. Because the door refuses them, `lan_hosts.candidates()` no longer
offers a tunnel address as a last rung: the pairing reply, the QR and the
phone's fallback list carry LAN addresses and the Bonjour name only
(`refusal()` still words the tunnel-only Mac). A phone paired before that
may still hold one, which is why the door sends nothing. An unreadable interface list **fails open** on purpose —
the seal is the boundary, this is defence in depth, and a psutil hiccup must
never lock the phone out. `LAN_MAX_OPEN` (32) in all and
`LAN_MAX_OPEN_PER_PEER` (6 — a keyholder phone holds a poll, a terminal
stream and an upload at once) are counted on the accept and released in the
handler's `finally`, before the writer is closed; over either the knock is
503 `LAN_BUSY_REFUSAL` (`busy`, weight 0 like `rate`: it fires before any
seal is opened) with nothing read, so a stranger cannot hold the door open
by never sending a byte. The typed-address pair is the third bound, above.
The Pair window's tick ("This phone can't scan") arms it through the
loopback `begin_pairing` with `allow_typed: true` — a bare `true`, never a
truthy string, on neither `LAN_ACTIONS` nor `REMOTE_ACTIONS` — and the reply
echoes `allow_typed` so the window draws the daemon's answer and derives
nothing; `devices.snapshot()` gains no key. What remains: a shared Wi-Fi
(hotel, café, tethering) sees the door while `lan_access` is on and the seal
is the boundary there, which the settings window says under the switch
(`info:lan-exposure`, drawn only while the listener is bound). Typed
pairing itself sends nothing secret — the paragraph below. Pinned by
`test_lan_door.py`, `test_lan_access.py`, `test_lan_upload.py`,
`test_event_log.py`, `test_card_sync_read.py`, `test_work_record_api.py`,
`test_phone_buzz_kinds.py` and `test_phone_reconnect_display.py`.

**The typed-address pair is a PAKE.** A phone that cannot scan the square
types the address and the eight-letter code, and the branch behind the
`allow_typed` gate is **SRP-6a** (`host/dark_army_daemon/srp.py`; the
phone's half `ios/BobPhone/SRP.swift` over `BigUInt.swift`, a Foundation-only
big integer used for nothing else) over RFC 5054's 2048-bit group, `g = 2`,
SHA-256, the code as the password, the pairing's `pairing_id` as the identity.
Two plain-JSON requests on `POST /api/pair`, no channel header: `{"pake":
"start", "A"}` → `{"pake": "start", "pairing", "salt", "B"}`
(`devices.pairing_srp_start`, one modpow on the loop); `{"pake": "finish",
"A", "M1", "name"}` → `devices.pairing_srp_finish` (two modpows,
`hmac.compare_digest` on `M1`), `devices.redeem_proven` (`redeem`'s tail
through the shared `_spend`, the same refusal order and words) and the QR
route's exact answer: one sealed `reply` under the **derived** home key at
Mac-to-phone counter 1 carrying `{token, device_id, relay_key, relay_url,
M2}`, `re` empty, never `home_key`. Every group element is padded to the
group's byte length before it is hashed (`pad`, both sides, `S` included): `k
= H(N ‖ pad(g))`, `x = H(s ‖ H(I ‖ ":" ‖ P))`, `v = g^x`, `A = g^a`,
`B = k·v + g^b`, `u = H(pad(A) ‖ pad(B))`, `K = H(pad(S))`, `M1 = H((H(N) ⊕
H(g)) ‖ H(I) ‖ s ‖ pad(A) ‖ pad(B) ‖ K)`, `M2 = H(pad(A) ‖ M1 ‖ K)`; the home key is
HKDF-SHA256 over `K`, empty salt, info `INFO_PAKE_HOME` (`bob-home v1 pake`) —
`relay._hkdf` / `RelayTransport.derive`. The verifier and salt live on
`_pairing` alone, never persisted, never on a snapshot, and die with the code.
Two bounds per code: `SRP_MAX_INFLIGHT` (4) started exchanges held, oldest
evicted; `SRP_MAX_STARTS` (32) starts in all, then `PAKE_BUSY_REFUSAL` until a
new code. **A proof that does not check out is a wrong code**: one
`_note_fail` under the unchanged `PAIRING_FAIL_LIMIT`, the wrong-code
sentence, reason `pair_code`; at the finish, used, expired and a ninth device
are the redeem's own words as `pair_code`, and at the start step the same two
sentences are reason `pake`. Everything else is reason **`pake`**, weight 1,
no fail: the old plain `{code, name}` body (a phone app from before the
exchange) is refused in `PAIR_UPDATE_REFUSAL`'s words and never handed a key;
a malformed value (`A` / `B` not the group's length in lowercase hex, `M1` not
64 hex), an `A` the pairing does not hold (a finish before its start, after a
new code, or twice — **every finish consumes its entry**), an `A` already in
flight (`A` is public, so a neighbour's replay of the phone's own start is
refused and never evicts the phone's entry; a phone draws a fresh `a` per
press) and a spent budget are `PAKE_SHAPE_REFUSAL` / `PAKE_BUSY_REFUSAL`. `A
mod N == 0` is refused on the Mac, `B mod N == 0` and `u == 0` on the phone
(`SRPError`); the phone writes a record only when the reply's `M2` verifies in
constant time, else `PhoneActions.pakeOutOfStep`; the floor is `first_ctr = 0`
(the first sealed request is counter 1, as before) and the record carries
`homeKeyCrossedInClear: false` on every route — the flag survives only for a
record an older app made. An observer sees the transcript alone, and guessing
the code from it offline is a 2048-bit discrete-log problem, not a hash to
crack. Pinned by `test_pake_pairing.py` (the shared
`fixtures/pake/cross-vector.json`, RFC 5054's Appendix B, the Swift files
under `swiftc` against Python) and `SRPTests.swift`; the QR route is
untouched.

**`mission_open` and `mission_end` are chosen on both tuples**, each its
own line on `LAN_ACTIONS` and again on `REMOTE_ACTIONS`, and neither reads
a payload field. *Open* starts one fixed executable (`claude`) in one fixed
enrolled folder (Dark Army's own checkout) with one fixed brief, a
constant opening prompt and a tool grant scoped to that folder, one scratch
file and `curl` at the loopback API alone (`mission.allowed_tools`), at
most one alive, idempotent while alive, behind
`board_dispatch`, the cooldown and the launch bounds — strictly less than
`board_dispatch` / `board_start_project`, which are on both tuples and
start arbitrary card work; away it rides the lease, Face ID and the
receipt token like every write, and what it buys a person away from the
desk — asking what the agents are doing — is exactly what the relay exists
for. *End* is `close_terminal`'s reach on one named terminal, guarded by
identity at the moment it fires; away, a mistaken End costs one reopen. On
either sealed door a fresh open answers `started` — the terminal handle is
**loopback reply-only**, to the panel, and never crosses a phone door; the
`mission` snapshot section carries no handle either. Pinned by
`test_mission_control.py`.

**`rebuild_app` is chosen at home only**: on `LAN_ACTIONS`, not `REMOTE_ACTIONS`, no payload, `post` never the queue, never replayed
(`ReceiptLedger.neverReplayed`); it builds the working tree as it stands and replaces the installed app, and a write-granted bot at
home reaches it too. `request_rebuild` hands the press to the menu-bar app, whose one in-flight flag is the gate (200 means *asked*),
and refuses while restarting or a press token the stamp says already ran; loopback twin `APP_ACTIONS`. The `rebuild` section holds no path
(`last_error` is home-redacted), seeded from `rebuild-stamp.json` (`test_rebuild_verb.py`).

## The sealed `state` read is conditional

**The sealed `state` read is conditional, on both doors at once.**
`ApiServer._state_answer` is `_sealed_run`'s whole `state` branch, so
home and away gained this once rather than twice, and `state` is still a
*read* on neither action tuple. Every full answer carries
`state_digest`; a phone may quote it back as `digest`, and when the
Mac's own picture still fingerprints the same the reply is
`{"unchanged": true, ...}` — under 200 bytes against ~80 KB. The digest
is a **cache validator, never a credential**: the frame is already
authenticated by the seal, so a plain `==` is right and
`hmac.compare_digest` would be cargo cult, and a digest of the wrong
shape is *ignored* (full payload, 200) rather than refused — the shape
check is `expected_done_token`'s without its refusal. `unchanged` is a
**present key set to true**, deliberately the opposite convention from
the panel's `?sections=changed`: the phone's `Snapshot` decodes every key
tolerantly, so an omission-marked body would decode into a valid *empty*
snapshot — a blank fleet, a blank board, a needs-you count of zero — and
`Client.swift`'s `applyState` is the one place a state body becomes the
screen, reading the marker before any `Snapshot` decode.
**`_POLL_ECHO_FIELDS` sits beside `_CLOCK_FIELDS` and is applied at the
digest alone**: `last_frame_at`, `last_ok_at` and `failing_for` move
because *this poll happened*, so without them away would never match and
the feature would silently do nothing; `_broadcast` strips none of them,
so the panel's frames stay byte-identical, and `/api/state` and
`/api/events` carry no `state_digest` at all. A stream frame whose only
movement is one of the three is deferred `BROADCAST_ECHO_INTERVAL` (15s),
never dropped and never stripped, so the panel's Devices rows stay honest
at most that stale. Unlike `_broadcast`, an
unchanged answer is not deferred — it **is** the answer — so a field
wrongly in that set costs a permanently stale screen, which is what the
phone's 60s full-resync floor (`PhoneClient.fullStateFloor`) bounds to
one minute. The held digest is memory-only and dies with the pairing;
the 4s/8s check-in cadence is untouched. An older phone quotes nothing
and an older Mac ignores the quote: both get today's full payload.

**The answer is partitioned by section (23 Sep 2026).** Every full
answer carries `section_digests` — one SHA-256 per `_OMITTABLE_SECTIONS`
key present, over the same clock- and echo-stripped canonical JSON the
whole digest and `_flush` compare (`_state_digests`, one `_news` walk) —
and a phone may quote them back as `sections` beside `digest`. A section
whose quote still matches is **left out and named in
`sections_unchanged`**, a present top-level list: the marker, never
absence, because on this door an absent key still decodes to the
section's default. The omission and the list come from one variable, so
they cannot disagree. The phone keeps a section only when it is on the
named list **and** the answer's digest for it matches the one it quoted,
carrying it from the last answer as it came off the wire (never the
screen, whose board holds the spliced Done archive) into the fresh
decode before `snapshot = decoded`; on any
mismatch it applies nothing, drops both held digests and asks for the
whole picture next time. The Mac keeps no per-phone memory — the phone
quotes what it holds every time — and a malformed or unknown quote is
ignored (`_valid_section_quotes`), never refused. The short
`unchanged: true` line is byte-identical, `_CLOCK_FIELDS` and
`_POLL_ECHO_FIELDS` are unchanged, `fleet_figures` rides every answer,
and the loopback `/api/state` and SSE carry none of it. The relay
socket push is always a whole picture: it quotes no `sections`, so it
never carries `sections_unchanged`, but it does carry `section_digests`,
which the phone holds as from any full answer. The phone quotes `sections` only
beside `digest`, so `fullStateFloor` still forces a whole picture once a
minute — the resync bound the section digests inherit — and only a whole
answer resets that floor or is remembered as the held picture. An older
phone gets the whole picture plus one key it ignores; a newer phone
talking to an older Mac receives no `section_digests` and so quotes
nothing. Pinned by `test_unchanged_state.py` and
`test_phone_section_delta.py`.

## `POST /api/upload` has its own body rule

**`POST /api/upload` is LAN-only and is the one route with its own body
rule.** `_read_request(reader, body_rule=None)` decides `(cap, timeout)`
*after* the request line **and headers** are parsed; `None` keeps
`MAX_BODY_BYTES` / `READ_TIMEOUT` for both callers, and `_lan_body_rule`
widens this route **only for a header frame that verifies** (`X-Bob-Frame`
opened under the channel's home key, kind `upload`, **without noting the
counter** — the handler opens it again through `_home_open` and would
otherwise refuse its own request as a replay) — the body is read before
any handler runs, so a rule that did not open the frame would let a
stranger on the same Wi-Fi make the daemon buffer 20 MB per connection
before its 403. It is not a second authorisation gate: `_home_open` still
runs per request. Widened to `attachments.MAX_ATTACHMENT_BYTES + 1024`
and `UPLOAD_READ_TIMEOUT` (60s) — the headroom so an oversize file is
refused *in words* rather than by a dropped connection (and it covers the
blob's 28-byte nonce and tag), and the timeout because a 20 MB body over
Wi-Fi outlives 10s. The blob is opened and stored in **one**
`run_in_executor` call (`_open_blob_and_store`) so the loop never holds a
20 MB AEAD; a blob that fails to open is an inner 403 in words. **The
frame the body rule opened is the frame the handler accepts**
(`_Request.preverified`, `_home_accept_preverified`, consumed once per
connection): the phone's 4s poll is not held during an upload, so a
`state` frame with the next counter routinely lands mid-body, and
re-running the replay test at the end of the body refused every photo
over a few seconds. The cost, stated: a captured upload header replayed
*during* the real upload's flight verifies too and buys that one
connection the widened cap; its blob cannot open without the key, so
nothing is written, and the window closes when the real upload lands.
`/api/action`'s cap is a security
boundary and is pinned unchanged. The write itself is
`attachments.store_upload` on the executor, which re-checks every bound and
returns the **stored** path post-dedupe; the caller records that, never the
name it asked for. There is deliberately **no LAN verb for reading or
deleting a staged file** — an abandoned staging folder is the 24 h orphan
sweep's job.

## The terminal stream is a sealed held-open route

**The phone reads and types into a Dark Army-hosted terminal on the
same stream the panel does, sealed.** `POST /api/terminal/stream` on the
phone door (`_serve_lan_terminal_stream`) is the one route held open
after its answer. The body is one `relay.seal_frame` in the `relay.HOME`
namespace whose `kind` is `terminal_stream` and whose `body.session`
names the session; `_home_open(..., expect_kind="terminal_stream")` is
the verifier, and **nothing is sent before it verifies**: an unpaired
channel or an Origin is a plain 403 with no head; a frame of another
kind, a missing session and a session Dark Army does not host
(`TERMINAL_NOT_OWNED_REFUSAL`) are sealed `err`s — `200 text/plain`, the
status inside — that only the keyholder can read. The head
(`SEALED_CONTENT_TYPE`, `application/x-bob-terminal-sealed`) is written
only after `terminal_attach` succeeded. `terminal_stream` is a **kind on
the verifier, not an action on `LAN_ACTIONS`**: typing through it needs
no lease because a verified home frame *is* the proof of proximity that
arms the lease (`_home_admit`, durable for this kind as for `action` and
`upload`), and the away door has no such route — `REMOTE_ACTIONS` is
unchanged.

**The codec.** `terminal_stream.serve` takes a `codec`; the loopback pane's
is `PlainCodec` (identity — the panel's contract is byte-for-byte what it
was) and this route's is `SealedCodec(key, stream_id)`, `stream_id` being
the opening frame's `id`. Every down frame (`D` bytes, `X` exited, `E` a
refusal) is `kind + payload` sealed by `relay.seal_blob` under the home
key and carried as one outer `Z` frame; every up frame (`I` keys, `S`
size, `P` ping) is unwrapped the same way. There is no counter inside a
blob: the position is the AAD — `relay.open_blob`'s
`<stream_id>|m2p|<n>` down and `<stream_id>|p2m|<n>` up, `n` counting
from 0 in each direction — so a captured frame opens only in its own
position on its own stream, and a replay, a reorder or a tampered byte
fails to open and **closes the socket** (TCP does not drop, so a gap is
tampering). The outer kind byte is always `Z`: not even the exit event
leaks. The phone half is `SealedTerminalStream`
(`ios/BobPhone/TerminalStream.swift`, whose codec and head parser are
byte-pinned to the panel's by `test_phone_terminal_stream_drift.py`),
opened through `HomeChannel.openTerminalStream`, which spends the
channel's own `sendCtr` — a poll landing between seal and send can make
the opening frame stale, answered as a sealed `err` with `ctr_expected`,
which the channel fast-forwards on and the coordinator reopens once.

**The opening paint is one or more `D` frames, in order.** The first
thing down every stream is `Screen.paint()` — the emulator's own drawing,
scrollback included — and a wide screen that drew a truecolor picture
paints past `MAX_FRAME_BYTES` (4 MiB, the outer frame cap both clients
pin). `serve` slices it at `PAINT_CHUNK_BYTES` (half the cap) or the
codec's `max_plain`, whichever is smaller, and writes the slices from the
one sender coroutine before the live queue is read, so a boundary inside
an escape sequence is safe: one socket, one writer, an incremental parser
on the other end. **The sealed budget is the wrapped frame, not the
slice**: `relay.seal_blob` adds exactly `relay.BLOB_OVERHEAD_BYTES` (a
12-byte nonce and a 16-byte tag, no deflate and no base64 on this route)
and `SealedCodec.wrap` folds the one kind byte into the plaintext, so
`SealedCodec.max_plain = MAX_FRAME_BYTES - 28 - 1` and a slice of exactly
that many bytes is a `Z` frame of exactly `MAX_FRAME_BYTES`; each slice is
one more sealed position, in order, and the phone's codec needs no change.
A send that fails for any reason other than the peer hanging up
(`CancelledError`, `ConnectionError`, `OSError` stay silent — a line there
would fire on every pane close) is one `logger.exception` naming the
handle and one best-effort `E` frame in `STREAM_SEND_FAILED_REFUSAL`'s
words; `encode` composes the whole frame before `writer.write`, so the
failed frame wrote nothing and the `E` lands in sync. The panel holds
that sentence across its own reconnect (`TerminalNoteHold`: `refused`
records, `opened` repeats what is held, `received` clears it once on the
first `D`), so it no longer blinks away once a second.

**Bounds at home.** `MAX_QUEUED_BYTES` (8 MiB queued for a client that is
not reading, then dropped — now a logged line and an `E` frame rather than
silence), the coalescer's `PAINT_CHUNK_BYTES` bound (one further
`ptyhost.DATA_FRAME_BYTES` item may overshoot it),
`STREAM_MAX_UP_FRAMES_PER_MINUTE` (600, a sliding count on this route
only; over it the socket closes), and **one stream per device**
(`MAX_LAN_STREAMS_PER_DEVICE`, `_lan_streams`, oldest first): a second
opening from the same device hangs up on the first, and the eviction reads
the constant rather than assuming the one. The phone restates its last `S` every
`STREAM_HEARTBEAT_SECONDS` (5) as the keepalive on a socket nothing else
times out; the server enforces nothing on it. Keys go to
`terminal_stream_input` under the desk's rules (no drain wait, no prompt
refusal, `TERMINAL_MAX_RAW_BYTES`). Sizes go to
`terminal_phone_resize`, which honours them only while
`_panel_focused_sessions()` does not name the session — **the width has
one owner at a time**, stated under `docs/phone-contract.md`'s terminal
section; the reverse hand-back is `_panel_pane_size`, re-applied by
`note_panel_terminal` when the pane comes back. That set is the pane's
`panel_terminal` heartbeat **alone**: the pane states its session id only
while the panel can be seen and clears it on hide, so anding in
`panel_visibility` — a verb nothing has sent `True` since the panel's
visibility heartbeat was removed — made the rule permanently inert.

**That set owns the width and nothing else.** `_alert_suppressed()` reads
the same predicate and keeps the `_panel_on_screen()` conjunct at *its* call
site, so alert behaviour is unchanged (and, in production, still inert).
Suppressing an alert is a far stronger claim than owning a width: the list
`_deliver_alerts` drains feeds the Mac's banner **and** the phone's push, and
`client.visible` is true of a panel left open on a monitor the person has
walked away from — so a live pane-focused suppression would mute an agent's
buzz on a phone in that person's pocket, indefinitely. Widening it wants a
frontmost-grade signal of its own, not this one.

**Away, the same emulator is fed by the poll.** iOS suspends background
sockets, so the stream is a foreground, at-home thing; away the phone
asks the sealed `terminal` read (a *read*: no lease, neither action
tuple) with `grid=0` and a `since_bytes` cursor, **one hop per 8 s poll
and never a `more` loop**. `BobDaemon.terminal_frame` answers `-1` — or a
cursor that fell off the ring (`ring_overflowed`), or one from another
life of the pty — with `Screen.paint()` and `painted: true`, never the
raw ring, which begins mid-sequence; otherwise the bytes since the
cursor, at most `TERMINAL_POLL_RAW_BYTES` (192 000 raw, 256 000 as
base64, inside one sealed reply under `RELAY_FRAME_MAX_BYTES`), with
`data_more` when the rest is waiting and `bytes_read` the cursor to quote
back. **The paint is bounded the same way**: a `Screen.paint()` over
`TERMINAL_POLL_RAW_BYTES` — a picture with its history — would not inflate
on the phone, so the painted leg carries the first slice with
`painted: true` and `data_more: true`, `bytes_read` the terminal's cursor
at that moment, and keeps the remainder in `BobDaemon._terminal_paint_tail`
(`(handle, viewer)` → anchor cursor, stamp, bytes; memory only, **one
entry per hosted terminal per viewer**, replaced by that viewer's next
paint). The viewer is the device id the seal already proved (`""` on
loopback), passed server-side and never a field on the wire: every paired
phone polls the same handle, and a second phone opening the terminal
mid-drain must neither replace the first's remainder nor take its next
slice. A forgotten handle drops every viewer's entry. The phone quotes that cursor back
on its next check-in — nothing new on its side, the cursor is the only
state it carries — and is answered from the tail **above** the ring test
(so a busy terminal cannot restart a drain under way): the next slice,
`painted: false`, `ring_overflowed: false`, the same `bytes_read`, and
`data_more` telling the truth until the last, after which the ring leg
resumes from the anchor. A poll quoting any other cursor drops the tail;
a handle the pty has forgotten or a tail older than
`TERMINAL_PAINT_TAIL_SECONDS` (120 s) is pruned on the next pass. A
multi-megabyte paint therefore fills in over several 8 s check-ins.
`grid=0` skips the row walk and `history` (`rows_changed: []`,
`unchanged` still stated). That reply is one of the at most five frames a
poll spends (state, usage, log, terminal, conversation, while the
conversation screen is up); no relay frame kind is added. Keys away
ride `terminal_input` with **`bytes`** (base64, raw, the desk's rules —
`BobDaemon.terminal_input` no longer refuses raw input from the phone;
the gates are pairing, the lease, Face ID, the receipt token and
`RELAY_MAX_KEY_WRITES_PER_MINUTE`), counted on the device's own key
bucket beside the writes bucket (`relay.write_bucket_kind`, both relay
doors), so keystrokes never spend an answer's allowance; the `text` line
route stays a write; a string that is
not base64 is 400 in words. The `text` route keeps every refusal for an
older phone app. `terminal_stream_supported` on `_pipeline_writable()` is
the version marker: absent decodes false and the phone draws the
terminal as one sentence, never a blank emulator. Pinned by
`test_terminal_socket.py` and `test_terminal_stream.py`.

## The loopback door has two tokens
**A local process is the loopback door's second attacker**, beside the browser page `api_server.py`'s docstring answers: every program running as this user — every agent Dark Army starts among them — reads any file the daemon writes, so the door holds two tokens, both in `X-Bob-Token`. **The desk token** (`ApiServer.token`) is minted with `secrets.token_urlsafe(32)` at every `start()`, lives in memory only and opens every loopback write and token-gated read (`_authorised`: `hmac.compare_digest`, then `_origin_ok`); it reaches the panel over the menu-bar app's private stdin pipe alone (`BobDaemon.desk_token()` → `_push_panel_context` → the `context` line's `desk_token` → `PanelContext.deskToken`), never a file, the environment, an argv, a snapshot or a log line, and a panel refused 403 sends `context_refresh` (at most once a second) for the context again. **The session token** (`ApiServer.session_token`) is the file that already existed, `~/.dark-army/api-token` (0600, `load_or_create_token`, never re-minted — so an older build after a downgrade reads the same file and works as it did), and opens `SESSION_ACTIONS` and `SESSION_READS` alone (`_session_authorised`: two `compare_digest` calls, an empty token matching nothing, the same Origin rule): `close_terminal` **without** `by_person` (with it, the board's third door into Done, the request falls through to `_route`), `close_refinement_terminal`, `reveal_panel`, and the conversation, knowledge, scout-report, plan and manual-check reads; `/api/terminal/stream`, the resizing `/api/terminal` and `/api/access-log` stay desk-only, and every other verb answers `403 {"error": "forbidden", "detail": DESK_TOKEN_REFUSAL}` to the session token and plain `forbidden` to a guessed one. **Why a pipe**: `ps -E` shows another same-user process's environment and argv, so those are the file again; a pipe's contents reach no third process; the Keychain's ACL follows the code signature, which ad-hoc builds change on every `build.sh`, and the VS Code extension (`reveal_panel` only) and the close-out helper (never `by_person`) keep the file and the session tier. **The person's own tools** read `DARK_ARMY_DESK_TOKEN` (`relay_bot.post_loopback`, `tools/permission_hold_livefire.py`, `tools/board_link_dependencies.py`), copied from Settings → Advanced → Copy desk key (armed, then confirmed), and say where to get it when it is unset. **What this is not — a sandbox**: a session-token holder can still close any session's terminal (loopback carries no peer identity, and a same-user process can `kill` any pid anyway), pop the panel and read the session-tier reads; a process that can attach a debugger to the panel or the app, the clipboard while the desk key sits on it, and a shell that exported `DARK_ARMY_DESK_TOKEN` (visible to `ps -E`) still reach the desk token. `request_id` stays on `/api/state` for the phone's lock-screen Allow; it is harmless because the verdict is a desk verb. An older panel fed by a newer app ignores `desk_token` and is refused as session tier; `build.sh`'s freshness gate keeps that pairing off an installed build. Pinned by `test_api_server.py`, `test_menubar.py`, `DeskTokenTests.swift` and `test_ship_close_out.py`.
## The relay is the second door

**The relay is the second door, and it is sealed** (`relay.py` /
`relay_client.py`; the mailbox itself lives in `relay/`, deployed on
Vercel outside this repo's build): the Mac reaches *out* and long-polls
for ChaCha20-Poly1305 envelopes keyed at pairing time, opens each with
`relay.open_frame` and answers through `ApiServer._remote_run` — the
same `state()` / `_usage_report_for` / `_lan_run`, but behind
**`REMOTE_ACTIONS`, its own tuple and never `LAN_ACTIONS`**: at home and
from anywhere are two decisions, and one shared list made them one —
`answer_questions` reached the internet that way, added for the phone at
home by a plan with no reason to think about the relay. It starts as a
copy and may never exceed `LAN_ACTIONS` (pinned). Plus one extra check:
an away window armed **only** by `relay.note_lan_proof` inside
`_home_open`, after a home frame verifies — a refused frame arms
nothing. **How long one check-in buys is a per-device grant**,
`lease_days` on the `relay.json` channel entry, chosen at this Mac from
`relay.LEASE_DAY_CHOICES` (1/3/7/14 days, `LEASE_DEFAULT_DAYS` = 1) and
dying with `forget`; an entry written before grants existed reads as the
default, never as off. `note_lan_proof` reads it and is still the **one**
site that may move an expiry forward — `set_lease_days` writes
`min(existing, now + days)` and so may only clamp down, which is why
shortening bites at once and lengthening waits for home, and why nothing
the phone does from away can extend its own window. `0` days ends away
access and coming home does not reopen it. The verb is `set_away_days` on
`_devices_request`'s loopback allow-list — behind the token, the `Host`
check and the Origin allowlist, and on **neither** `LAN_ACTIONS` nor
`REMOTE_ACTIONS` — and "End away access now" is that same verb at zero.
`lease_days` rides `devices_snapshot` beside `lease_expires_at`; the
panel decodes an absent key as `-1` (say nothing) rather than `0` (ended).
A `pair_bot` bot has such a row (`docs/relay-socket-contract.md`), but its reads and writes are the two grants below, not this window. Lapsed
means writes refuse in `relay.LEASE_REFUSAL`'s words and reads continue; un-pair
calls `relay.forget`; the connector re-reads keys, so revocation bites at once.
**The key is minted at pairing and only while `remote_access` is on** —
the switch is a consent gate, so it gates the minting; a phone paired
while it was off has no away path and re-pairs at home to get one, which
the Devices menu says. That one arming site is also the whole renewal
path, so away access with `lan_access` **off** is a window that runs
down and cannot be renewed: the two switches stay independent and the
menu states the dependency. Preference `remote_access`, its own key,
default off; `/api/upload` is deliberately not carried. **The push route is the one relay endpoint that needs a credential**: `push.js` carries plaintext titles to Apple, so it demands `Authorization: Bearer $PUSH_SECRET` (a Vercel env var, constant-time compared, 401 on mismatch, 503 `unconfigured` when unset) and a global `rate:push` cap beside the per-channel one. The Mac keeps the same value as `push_secret` in `relay.json`, set only through `relay.set_url` from the panel's Relay address… SecureField, never shown back and never in `/api/state`; a 401 is one `_Health` log line, not a retry storm.

**The bot's access is two grants, not the day lease** (26 Sep 2026). The bot (`devices.is_bot`: the ledger's `headless` key **or** any `bot_*` key on its relay channel, `relay.has_bot_grants`, so an unreadable `devices.json` fails closed) reads while its **Read** grant is on and acts while its **Write** grant is on; the phone's day window decides nothing for it. Each grant is two keys on the bot's channel entry (`bot_read_mode` / `bot_read_until`, `bot_write_mode` / `bot_write_until`), dying with `forget`; the mode is one of `relay.BOT_ACCESS_MODES` (`off`, `1h`, `6h`, `24h`, `forever`) and `relay.set_bot_access` stamps a timed mode's `until` **from now**, so the same timer again is a refresh. `pair_bot` seeds Read `forever`, Write `24h`.
`relay.bot_grant` is the one reader, and **absent and unknown mean opposite things**: a side with neither key predates the grants and reads as today's behaviour (Read `forever`; Write `24h` until `lease_expires_at` while `lease_valid`, else off); a present mode outside the tuple is a newer build's word and reads **off**; a timed grant past its `until`, or with a non-finite one, is off. **The bot is off the day lease for good**: `relay.materialise_bot_grants` — run on the bot's first frame, its first snapshot and every `set_bot_access` — writes explicit keys (a legacy Write becomes `24h` capped at the old lease, so the tick is honest and nothing widens) and clamps `lease_days` to 0, and `_home_admit` skips `note_lan_proof` for the bot, so its own home frames re-arm nothing. A store write copies the memoised store and drops it on a failed save.
**Every door the bot can reach asks one function**, `ApiServer._bot_refusal`, which picks `relay.BOT_READ_REFUSAL` or `BOT_WRITE_REFUSAL`: in `_sealed_run` (both doors) above every read kind, and in the `action` branch where the lease line was (below the `action not in actions` 404, **above** the receipt lookup, so a token minted while Write was on never replays after); on the home terminal stream, where opening is a read (a sealed `err` 403 before anything attaches) and each `I` frame a write (an `E` frame refusal, re-read per keystroke); and on the home upload, a write (inner 403, the blob never opened). A phone is judged by its lease exactly as before. Switching either grant off hangs up the bot's open home streams, and one timer at the earliest running `until` redraws the lapse and closes them then. The socket push (`relay_ws._push_all`) skips a Read-off bot on its existing non-200 check.
**One verb moves a grant**, `set_bot_access` (`device_id`, `side`, `mode`): on `_devices_request`'s loopback allow-list for the desk, and on `LAN_ACTIONS` and `REMOTE_ACTIONS`, each its own decision, for a paired phone (away it rides the phone's lease, Face ID and the receipt token; the phone never re-sends a failed one, `ReceiptEffect.botAccess`). `BobDaemon.set_bot_access` refuses the bot by its **verified** identity (`_lan_run` passes the sealed frame's `device_id` as `requester`, never a payload name; `BOT_ACCESS_SELF_REFUSAL`) and any target that is not the bot (`BOT_ACCESS_TARGET_REFUSAL`). The desk's press is filed under "Done remotely" as `desk`, an away press by the away door's recorder, a home press not at all. `devices_snapshot` carries `bot_access: {read: {mode, until}, write: {mode, until}}` on the bot's row alone (no key, digest or channel id; not a per-frame clock), and its presence is the phone's version marker. `relay_bot renew` is a home state read now: it renews nothing and prints the Mac's inner refusal.
**Downgrade, stated honestly:** the clamped lease means an older build reads the bot's writes as ended and re-arms nothing; it ignores the four keys, so its reads become ungated again, that build's own behaviour. `note_lan_proof` is still the one arming site for phones. Pinned by `host/tests/test_bot_access.py`.

## The buzz says which kind it is

**The buzz says which kind it is.** The push body is nickname-phrase-count **plus one kind word** from `alerts.KINDS` — `security`, `permission`, `question`, `attention`, `finished`, most urgent first (`security` is the one kind about the machine rather than an agent: a burst of refused knocks at the phone doors, raised by `BobDaemon._raise_access_alert` with an empty `session_id` and delivered on its own, exempt from the unlisted rung (*A buzz names only what the phone lists*); its title is "Somebody is knocking at Dark Army's door", it names no address, and `push.js` plays the permission cue for it — **no new sound file**). The kind is decided once in `AlertPolicy.evaluate` (`alerts._kind`: the entry's question slot wins over the card's hook, because the card that carries a question to the gate *is* the generic idle reminder; so does a `bob-actions` offer whose marker is current — `alerts.offered_reply`: a non-empty `reply_options` and the transcript's `marker_at` ≥ `prompt_at`, both `0.0` and so current on a Grok or Codex row; a `bob-tldr` summary alone is **`finished`** since 22 Sep 2026 (S1, the false-buzz investigation of 20 Sep 2026), and `finished` is a generic card with **none** of the three — no question slot, no current offer; a relayed `AskUserQuestion` prompt is a question too) and carried on `Alert.kind`, never re-derived — `_compose_push_title` reads `nickname` + `kind` for the four phrases and `_compose_push_kind` takes the most urgent kind present when several alerts collapse into one buzz. `relay_client.PUSH_KINDS` restates the set and sends `kind` only for a member. `push.js` maps it to a bundled sound (`SOUNDS`, a null-prototype table) — the sound is **chosen relay-side**, never named by a caller — and an absent or unknown kind is `"default"`, so an undeployed mailbox or an older phone build keeps today's buzz. The three files are generated by `tools/phone_buzz_sounds.py` (`--check` refuses byte drift) into `ios/BobPhone/Resources/sounds/`; `test_phone_buzz_kinds.py` pins the set at all four ends. The Mac banner keeps its own titles and ignores the key.

**And it says what the agent is working on.** Beside `title`, `badge` and
`kind` the push carries **`work`**, one short line composed by
`BobDaemon._compose_push_work` at push time from the bound board card's
title (`_card_titles_by_session()`, the last published board — `session_id`
and `refine_session_id` both count), else the session's own title from the
agents snapshot row's `name`, skipping `UNNAMED_SESSION`. It is **absent**,
never empty, for a collapsed buzz (`len(pending) > 1`), for a session with
no card and no name of its own, and for an alert with no session id — so a
buzz with nothing to say is byte-identical to the one this route sent before
`work` existed. Whitespace-collapsed and clamped to
`relay_client.PUSH_WORK_CHARS` (80, with a trailing `…`) on the Mac, joined
to the wire in `push_alert`'s allow-list and nowhere else, and clamped again
by `push.js`'s `MAX_WORK_CHARS` (pinned equal by
`test_phone_buzz_kinds.py`), which renders it as `aps.alert.subtitle` — the
key set only when non-empty. This **deliberately relaxes the old
wake-and-count rule**, at the user's explicit instruction on 2026-09-06:
a card or session title now transits Vercel and Apple in plaintext and shows
on a locked screen, and there is **no preference key** to switch it off. The
mailbox deploys out of band, so until `push.js` is redeployed the field is
ignored and the banner is today's; no version marker is warranted for a
field that degrades to silence.

**And one line saying what is needed.** The push's third field is **`need`**,
one sentence composed by `BobDaemon._compose_push_need` from `Alert.need`
(the question's text, else the `bob-tldr` summary — `alerts._need`) and
`Alert.tool` (the bare name). A tool approval reads "Approve running Bash"
(or "Approve a tool call"); a question is its own text, else "Answer the
question it asked", tested **before** the permission branch so a relayed
`AskUserQuestion` never reads "Approve running AskUserQuestion"; attention
is the summary where one was left. **Absent**, never empty, for a collapsed
buzz, a security alert and a bare finish — byte-identical to before `need`.
Clamped to `PUSH_NEED_CHARS` (120, trailing `…`) on the Mac and again by
`push.js`'s `MAX_NEED_CHARS` (pinned equal), joined only in `push_alert`'s
allow-list, and rendered as `aps.alert.body` only when non-empty, on the
alert leg; the live card carries no `need`. The agent's own summary now
transits Apple in plaintext (2026-09-20). What still never rides is the
command preview, the tool's `description`, a path, the project and the
branch — those stay on `body` / `subtitle`, which `_compose_push_need`
never reads (`test_alert_delivery.py` bans the keys). The phone reads none
of the three lines off `userInfo`. Pinned by `test_alerts.py`,
`test_alert_delivery.py`, `test_relay_client.py`, `test_relay_push_box.py`
and `relay/tests/push-destination.test.mjs`.

**The buzz is written down.** Every alert `_deliver_alerts` hands to the phone leg leaves one line in `~/.dark-army/buzz-ledger.jsonl` (`buzz_ledger.BuzzLedger`, `paths._PRIVATE_FILES`, 0600, ≤ 2000 lines, 30 days, `event_log`'s deque-and-rewrite shape) — the daemon's private diary of what it decided to send, never served on any door; `tail` and `jq` are the reader. The line is exactly `LINE_KEYS`: the alert's own `alert_id` / `session_id` / `rule` / `kind` / `severity` / `created_at`, a `drain` id grouping a collapsed buzz with its `collapsed` count and the `push_kind` it carried, the `badge` it carried, whether a `banner` observer existed, `evidence` (counts and booleans off the row the policy read, composed on the snapshot executor beside the decision — `questions`, `question`, `reply_options`, `summary_chars`, `text_chars`, `report`, `card_hook` by name, `cooldown_gap` since the previous alert about that session), `mac` (the frontmost cache, `panel_on_screen`, and a `Quartz` idle reading taken inside the executor callable that writes the line, `None` where the probe fails, or the phone leg's own reading when it took one) and `phone` (`outcome` — `sent:<n>`, `no_devices`, `push_off`, `remote_off`, `no_connector`, `no_loop`, `raised`, and since `SCHEMA_VERSION` 2 the phone leg's `withheld:finished`, `withheld:mac_active`, `dropped:card_gone` and `dropped:muted` — then `devices` and `held_seconds`, how long the leg held the alert, `0.0` for one sent at once). A schema-1 line on disk has no `held_seconds` and is loaded as data all the same; `jq` reads the missing key as null. **No words**: the fence is the access log's `FORBIDDEN_KEYS` widened with `title`, `body`, `subtitle`, `message`, `text`, `work`, `last_summary`, `last_text`, `last_report`, `work_report`, `question`, `questions`, `reply_options`, `options`, `input_preview`, `description`, `cwd`, `transcript_path`, `project` and `branch`, refused at any depth outside the shape-checked `evidence` dict, whose three same-named fields are counts and a bool. The summary rung above is what the ledger now measures: the false-buzz investigation of 20 Sep 2026 ranked the fixes and gives the `jq` lines. Pinned by `test_buzz_ledger.py`.

## The phone leg is filtered, gated and held

`_deliver_alerts` drains `_undelivered` once and hands the **whole batch** to the Mac banner leg (`on_alerts`) at once, exactly as before; the phone's share goes to `_start_phone_leg`, which runs it as one loop task (`_phone_leg`, kept in `_phone_leg_tasks`, cancelled and awaited by `_shutdown` before the ledger closes; a drain after shutdown began starts none) or, with no running loop, at once through `_phone_leg_now`. Three rules, applied in this order since 22 Sep 2026 (S3, S6 and S7 in the false-buzz investigation of 20 Sep 2026), each **always on with no preference key**:

- **A finished turn never buzzes the phone (S3).** A `finished`-kind alert never reaches `_push_phone_alerts`; its line reads `withheld:finished`. The phone's Needs you list, its badge (`len(waiting)` off the same snapshot) and the Live Activity are untouched, and so is the banner leg.
- **A finished work report is one quiet Mac banner and no buzz.** The `report` rule's banner (`alerts.REPORT_RULE`, `docs/session-state-contract.md`) is a `finished` kind, so it takes the same `withheld:finished` line and never reaches `_push_phone_alerts`; the push's closed field set, `PUSH_KINDS` and the Live Activity are unchanged.
- **No buzz while the Mac is in use (S6).** Right before each POST batch the leg reads `buzz_ledger.mac_idle_seconds()` **on the executor** (`run_in_executor`, never a `Quartz` call on the loop); under `MAC_PRESENT_SECONDS` (60 s) every alert of the batch is recorded `withheld:mac_active` and nothing is sent. It applies to **every** kind, `permission` and `security` included — the person is at the Mac, where the panel and the strip say the same — and it **fails open**: a `None` reading sends. The line carries the same reading the gate used.
- **A card buzz waits half a minute (S7).** An alert with `rule == "card"` (the Stop / Notification / StopFailure card branch) is held `PUSH_GRACE_SECONDS` (30 s) on the loop; at the end of the grace it is dropped when its session no longer holds a card — `sid in _active_notifications`, a membership test, never a copy of the card, which carries the enrolment key — as `dropped:card_gone` (answered, dismissed, superseded by a quiet finish, or the session forgotten), or when its session was muted, as `dropped:muted`; a card *replaced* inside the grace still sends. The rest meet the presence gate and go with `held_seconds` 30. Permission asks, `security` rows and signal alerts (`stall`, `ctx-full`, `waiting`) are not held: they are sent **before** the grace, in their own `_push_phone_alerts` call, so a drain holding both makes at most two POST batches and a permission ask never waits on a card. A grace of zero holds nothing; a cancelled leg writes nothing.

**A buzz names only what the phone lists (23 Sep 2026).** A fourth rung sits between the finished filter and the grace, so the order is `finished` → `unlisted` → hold → presence, on both `_phone_leg` and `_phone_leg_now`: an alert whose session the phone's Needs you list does not show — any kind but `security` — is recorded `withheld:unlisted` and never reaches the hold, the presence reading or `_push_phone_alerts`. The list is **one rule**, `live_activity.listed_sessions` — the phone's *One decision list* (`docs/phone-contract.md`): a live row (running, waiting, sleeping) in the `waiting` bucket, with an open prompt in `_prompts_by_session()` or with a *published* notification card (`_notification_snapshot()`, which withholds a card inside the hysteresis window), or a prompt with no row at all; a finished or abandoned row never — read once per leg, before any await, by `BobDaemon._phone_listed_sessions` from the same three inputs `_activity_subject` hands `live_activity.subject`. Since the follow-up of 23 Sep 2026 the gate reads `live_activity.shown_sessions` — that set **less every entry the person dismissed**, judged by the phone's own fingerprint rule through the same `inbox_ack.session_kind_and_fp` / `card_kind_and_fp` the Dismiss verb and Bearings use (`waiting=True` for any admitted row, the question off `inbox_ack.question_list`), against `InboxAckStore.records()`, the rows section `inbox` publishes, plus the published board's cards; a session a surviving `needs_you` / `manual_check_due` card names stays shown, because the phone's one-entry rule drops the session's entry, never the card's, and a card never admits a session `listed_sessions` did not. The word stays `withheld:unlisted`; `evidence.category == "waiting"` marks the dismissed case. `waiters` reads `listed_sessions` whole, so the acks are the one place the buzz gate and the Live Activity differ, by the accepted drift in `docs/phone-contract.md`. Why: the phone's `PushRegistrar.clearDeliveredIfQuiet` (`ios/BobPhone/Push.swift`) removes **every** delivered notification on each applied foreground snapshot whose `needsYouCount` is 0, so a buzz about a session the list does not show is an appear-then-vanish by construction — a `stall` or `ctx-full` warning on a busy agent was 96 of 247 ledger lines on 23 Sep 2026 (74 of them with a badge of 0). A `permission` alert is listed by the prompt that raised it and a `card`-rule alert by `category == "waiting"`, so both still buzz; the `security` row is never on Needs you yet is exempt (the person's decision, 23 Sep 2026): the sweep runs only on a foreground snapshot, so its buzz stays until the app is opened, and with Mac banners off it is the only interrupt for a door burst. The Mac banner leg (`on_alerts`) still receives the whole batch, the badge is still `len(waiting)`, and Needs you membership on every surface is unchanged. `_raise_access_alert` delivers its own row alone (`_deliver_batch`, the queue untouched), so agent alerts decided on a snapshot not yet published are judged after it lands. When an input cannot be read the rung gates nothing (S6's rule: a fault never silences the phone). Pinned by `test_live_activity.py`, `test_alert_delivery.py` and `test_buzz_ledger.py`.

**A withheld buzz is not retried — an accepted gap.** Alerts fire once, on a transition, so an alert recorded `withheld:mac_active` never reaches the phone later, even if the person leaves the desk while its card still stands; a card buzz reaches the phone only when the Mac has been idle `MAC_PRESENT_SECONDS` by the end of its grace. The choice is to interrupt less; the Mac banner, the strip and the phone's own Needs you list still show the ask. Re-trying a withheld card while it stands would be a new guarantee and needs its own plan.

`_push_phone_alerts` itself, the push body and the wire are unchanged. The research report's re-run block (*Re-running the measure after 2026-09-22*) counts each outcome word. Pinned by `test_alert_delivery.py` and `test_buzz_ledger.py`.

## A buzz may carry what to press

**The lock-screen leg of the push.** `BobDaemon._compose_push_act` reads the
drain: exactly one pending alert with a session, or `{}`. A `permission`
rule gives `{"act": "permission", "request_id", "session_id"}`; a
`question` or `attention` kind gives `{"act": "acknowledge",
"session_id"}`; a finish, a machine alert and a collapsed buzz give
nothing. The fields join a device's push **only where that device's
`lock_screen_actions` is on** (`relay.lock_screen_actions`, a bool on the
`relay.json` channel entry, default off, set by the loopback verb
`set_lock_screen_actions` on `_devices_request`'s allow-list — `set_away_days`'
gate, a bare bool, on neither `LAN_ACTIONS` nor `REMOTE_ACTIONS`;
`devices_snapshot` publishes it). `relay_client.push_alert` joins them only
in `PUSH_ACTS` / `PUSH_ID_SHAPE`'s shapes and `push.js` re-checks the same
two (`ACTS`, `ID_SHAPE`, pinned equal) before setting `aps.category`. What
rides is identifiers, never the ask's words; the alert's `request_id` is
stamped by `AlertPolicy._permission` and is `""` on every other rule.
Pinned by `test_lock_screen_actions.py`.

**A buzz names its subject.** Beside the receipt, a single-alert buzz carries
`session_id` and, where the last published board binds that session to a
card (`_card_ids_by_session`, the same two-pass walk `_card_titles_by_session`
names the title by, factored as `_card_field_by_session`), `card_id` —
composed by `BobDaemon._compose_push_subject` and joined to the body
**before** the per-device loop, so every phone gets it whatever its
lock-screen switch says; a collapsed buzz and a machine alert (`security`,
empty session) carry neither, and those bodies stay byte-identical.
`relay_client.push_alert` joins each only under `PUSH_ID_SHAPE` (a value out
of shape is dropped silently, `work`'s rule, never a refusal), and the act
leg now rides on the shaped `session_id` already in the fields rather than
re-assigning it, so the act fields are byte-identical to before. `push.js`
re-checks both against `ID_SHAPE` in the alert leg alone — a present but
malformed value, or both empty, is `400 "bad subject"` and nothing reaches
Apple — and sets **no** `aps.category` for them: the category stays the act
block's. What rides is identifiers, never words: the same opaque session id
the act leg already carried and a card id, no key, no digest, no claim. The
phone reads them into its notification log (`docs/phone-contract.md`, *the
phone keeps a log of the buzzes it saw*) and opens the card or agent from
the picture it already holds when the Mac is out of reach; an older phone
ignores both keys and an undeployed mailbox ignores them too (its alert leg
has no subject block), so the field degrades to silence like `work`. Pinned
by `test_push_subject.py`, `test_lock_screen_actions.py` and
`relay/tests/push-destination.test.mjs`.

**And it may name one agent's face.** A single alert of kind `question`,
`permission` or `finished` (`alerts.FACE_KINDS`) may carry `face`, the
slug already on `Alert.character`, only when it is in
`relay_client.PUSH_FACE_SLUGS` (`identity.NAMES` lowercased, every one
ASCII; the live card checks its `slug` against the same list). **Absent**, never empty, for
a collapsed buzz, attention and security. `push_alert` copies a member
and drops anything else — a miss is not a failed buzz. The alert leg of
`push.js` copies a member onto the payload and sets `aps["mutable-content"]`
to `1`; anything else sets neither and is not a 400. No image rides. The
mailbox deploys out of band, so until it is redeployed the banner is today's
icon. Pinned by `test_alert_delivery.py`, `test_relay_client.py`, `test_phone_buzz_kinds.py` and `relay/tests/push-destination.test.mjs`.

## The buzz has a live-card leg

**The same push route carries the phone's Live Activity, under the same
secret and caps.** `BobDaemon._push_live_activity` runs on the agents-push
path right after `_deliver_alerts()`, in its own `try`/`except`: the buzz
is a *transition*, this is the *standing* reading — `live_activity.subject`
over the published buckets (`running` / `waiting` / `sleeping`, admitted
**by the phone's own rule**: membership of `waiting`, an open prompt in
`_prompts_by_session()` or a *published* notification card naming the
session (`_notification_snapshot()`, the phone's `notifyIds` — never
`_active_notifications`' raw keys, which still hold a "Waiting for input"
card the `WAITING_HYSTERESIS_SECONDS` window withholds from every
surface; read raw, a stop-and-resume flicker inside the window cost an
update and an end for a card the phone never started) — a non-empty
`questions` list decides the kind and admits nothing, so a stale list on a
row nobody lists pushes no card; kind prompt → question → attention; then
the phone's one-entry rule, `live_activity.carded_sessions`: a `needs_you`
or `manual_check_due` card whose `session_id` names an *attention* row
takes that row's entry, so it is no subject, while a prompt or a question
outranks the card; ranked **exactly as the phone sorts its list** — kind,
then the row's title
(nickname, else name, else session id) as `localizedStandardCompare`
orders it (`live_activity.title_key`: case-folded, digit runs by value),
then `session_id`, never by wait, so the card the phone starts and the
Mac's first update name one agent) and `live_activity.content_state` —
**exactly** `nickname`, `slug` (`cast.character_for`), `kind`, `work`
(`_compose_push_work`'s rule), `since` and `session_id`. **`since` is the
row's own `quiet_since`** — stamped by `_collect_agent_stubs` from each
source's own clock (`_quiet_stamp`: the hook stream's `last_event`, the
roster's `started_at`, Grok's `opened_at`, Codex's `last_event`, a
tombstone's `finished_at`), whole seconds, **0.0 where the source has
none** — never `now - idle_seconds`, which stamped `now` on a stampless row
every frame and kept SSE coalescing and the phone's conditional read from
ever settling
(`test_unchanged_state.py::test_a_stampless_row_carries_no_quiet_since_and_makes_no_news_across_frames`).
**The key is a clock field** (`_CLOCK_FIELDS`, beside `last_event`, which
it is a rounding of): a hook row's stamp moves on every event, so as news
it made two same-tool events on a working row differ on this key alone —
an SSE frame per tool call and a phone digest that never settled. A
row's move to waiting is news through `state`, and every full read
carries the stamp
(`test_a_hook_rows_quiet_since_is_a_clock_field_so_a_same_tool_event_is_no_news`).
A new row key with a default on read, which the phone reads verbatim
(`Agent.quietSince`, 0 undated), so the two ends agree to the second; `now
- idle_seconds` stands in only for a row with no stamp. Per phone holding
an activity token: an unchanged
state sends nothing (`live_activity.same` ignores a `since` drift under
2 s, so a clock tick alone never costs a push); a changed one sends one
`update`; an empty list sends one `end` carrying the last state, remembered
as `"ended"` in `_live_activity_last` once it lands — memory only, keyed
on device, popped by `forget_live_activity` when the phone registers a
fresh token, because **the token is per activity, not per phone** and an
`end` kills it. **`"ended"` means no live token**: nothing more is sent to
that device, a new subject included, until the phone registers again.
**A send's answer is remembered only against the generation it was sent
in**: `_live_activity_generation` steps per device on every send's creation
and in `forget_live_activity`; `_send_live_activity` captures it before its
await and discards its answer if it moved — so an `end` in flight to the
old token, landing after the phone has ended locally, unregistered, started
a new activity and registered its token, never marks the new activity
`"ended"`, and an older update landing after a newer one was sent never
becomes the remembered state.

**A refusal is remembered, never retried per pass.** `push_activity_outcome`
answers one of `ACTIVITY_OUTCOMES`: `landed` (the state is remembered),
`refused` (a 4xx, an undeployed route — 404 / `503 unconfigured`),
`dead` (`502 apple refused`, a token iOS ended at eight hours: the
device is marked `"ended"` until it registers again), `unreachable` (no answer, any other 5xx —
including `503 unavailable`, which `push.js` answers on the activity leg
for Apple's own 5xx and 429, so a transient answer from Apple is retried
on the clock rather than remembered for the body's life; Apple's 4xx —
BadDeviceToken, an ended activity's token, an expired JWT — stays `502
apple refused`; **the alert leg keeps `502 apple refused` for every Apple
non-200**, its wire and log lines byte-identical) or
`skipped` (no token, a shape the connector would not send, the shared
bucket empty — nothing left the Mac). The last body sent and its verdict
sit in `_live_activity_attempt`: the same body (`_same_activity_body`,
event plus `live_activity.same`) **refused is not sent again until the
picture changes**; the same body **unreachable waits
`LIVE_ACTIVITY_RETRY_SECONDS`** (30 s). An `end` follows the same rule —
it is retried across passes only while the mailbox cannot be reached, and
a refusal of it (the mailbox too old) stands until the next registration. A body already in flight for a device
(`_live_activity_inflight`, one detached task per body, cleared in the
send's `finally`) is not sent twice however slowly the mailbox answers. So
an old mailbox or a dead token costs the `_push_bucket` the live card
shares with the buzz **one token per distinct state**, never one per pass,
and the buzz is never starved behind a card.

**The token.** `register_activity_token` — `register_push_token`'s twin on
**both** `LAN_ACTIONS` and `REMOTE_ACTIONS` (a token rotates per activity,
and the activity that matters is the one started away) — records the
verified caller's token on its own `relay.json` channel entry through
`relay.note_activity_token` (`activity_token` / `activity_env` /
`activity_updated_at`; empty deletes all three; `forget` deletes the
entry), same hex 32–200 shape, `prod` / `dev`, 403 / 400 / 409 in the same
words. `devices_snapshot` publishes `live_activity` as a bool beside `push`;
the token, its env and its age ride no snapshot. `live_activity_supported`
on `_pipeline_writable()` is the version marker.

**The wire.** `relay_client.push_activity` is `push_alert`'s twin: the
token and env re-read at the moment of use, the **same `_push_bucket`**
(`PUSH_MAX_PER_MINUTE` across both legs), and a body joined **only** in
shape — `tok`, `env`, `event` ∈ `ACTIVITY_EVENTS` (`update` / `end`),
`nickname`, `slug` by `ACTIVITY_SLUG_SHAPE`, `kind` ∈ `ACTIVITY_KINDS`
(`permission` / `question` / `attention` = `alerts.KINDS` minus
`security` and `finished`; an update with any other word sends nothing, an
end may carry none), `work` clamped to `PUSH_WORK_CHARS`, `since` a finite
number ≥ 0, `session_id` by `PUSH_ID_SHAPE` (a bad id is dropped, the end
still sent). **Never `title`**: a mailbox deployed before the leg existed
answers 400 `bad request`, which `push_activity_outcome` records on **the
live card's own health record** (`_activity_health`, subject `phone live
card`, consequence "the phone's live card will not be updated until this
clears" — the buzz's `_push_health` and its lines stay byte-identical, so
an old mailbox that takes the buzz and refuses the card does not flap one
record per buzz; never `_health`, never `_push_route_missing`, which stays
404 / `503 unconfigured` said once), and it is never re-shaped into an
alert — an ActivityKit token pushed as an `alert` is Apple's
BadDeviceToken. `relay/api/push.js` keys the leg on
`body.event`, validates the same closed sets (`ACTIVITY_EVENTS`,
`ACTIVITY_KINDS`, `SLUG_SHAPE`, `ID_SHAPE`, a finite `since`) and builds
`{aps: {timestamp, event, "stale-date": timestamp + ACTIVITY_STALE_SECONDS
(1800), "content-state": {nickname, slug, kind, work, since, session_id,
updated_at}}}` (`end` adds `"dismissal-date": timestamp`) in the file, so
nothing a caller sends beyond those six reaches Apple — `updated_at` is the
relay's own `timestamp`, never a caller field, and is what the card's
"N min ago" counts from (the app stamps its own local refreshes the same
way; an unstamped state draws a dash); `apnsSend` wears
`apns-push-type: liveactivity` on the topic
`${APNS_TOPIC}.push-type.liveactivity`, priority 10 and **no
`apns-collapse-id`** (an update and an end must both land, in order). The
alert leg is byte-identical to before. The card's title on a locked screen stands on the buzz subtitle's footing (`work`, 2026-09-06, no preference key), confirmed by `bc-security-reviewer` on 20 Sep 2026 (the *live activity needs you* plan): the same field at the same clamp as the buzz subtitle, bounded by the 1800 s stale date, the dismissal date on end and iOS's own "Allow Access When Locked → Live Activities" switch; `work` stays on the wire. The verdict was graded WARN: the `work` line still lacks `.privacySensitive()` (a Prep card is filed).
Pinned by `test_live_activity.py`, `test_relay_push_box.py`, `test_phone_buzz_kinds.py` and `relay/tests/push-destination.test.mjs`.

**The fleet card is the same leg with seven more short fields**, a closed
set: `working`, `needs_you`, `standing_by`, `cost_usd` / `tokens_k` when
measured and the last hour's burn `cost_usd_hour` / `tokens_k_hour`
(`fleet_figures.BurnMeter`: per-row growth since last seen, a first sighting a
baseline, scaled to an hour, absent until it has watched ten minutes) —
fourteen keys at most; a missing figure is absent, never zero.
`push_activity_outcome` clamps the ints to 0..999999 and rounds the costs to
the cent, and `push.js` checks them again (`FLEET_INT_KEYS` / `FLEET_USD_KEYS`) before they reach Apple. The phone's shape rides its own channel entry as
`activity_shape` (`relay.note_activity_token`'s optional `shape`, default
1; `register_activity_token` accepts 1 or 2, an int or a decimal string,
and stores nothing on any other) and is never on a snapshot. A shape-2
picture that differs from the last landed one only in the figures
(`FIGURE_KEYS`, the two rates included) waits `LIVE_ACTIVITY_FIGURES_INTERVAL_SECONDS` (60) so a
figures tick cannot spend the bucket the buzz shares; a count, the face
or the event is sent at once. A mailbox that learned the fields answers
the activity leg `ok shape=2` (the alert leg still answers `ok`); a landed
fleet update whose reply lacks that marker is logged once per run — the
mailbox drops the fleet figures, redeploy the relay folder — and the phone
then draws dashes rather than zeros.

## Decision history and notification destinations

**Decision history and notification destinations.** `decision_capture.py`
serializes private `decisions.db` writes off the daemon loop. Retention is
90 days / 20,000 events; question text totals 8,000 characters, outcomes
2,000, with visible truncation and coverage gaps. Exact source IDs bind
observed question results; delivery, permission staging and agent Done
remain distinct from confirmed execution or human acceptance. No historical
transcript bootstrap. `GET /api/catch-up` and sealed `catch_up` reads filter
current enrollment; device-owned receipt UUIDs are locators, not credentials.
Reads need no away-write lease. Latest episode revisions page unresolved
first under a fixed upper cursor. The phone advances its pairing-scoped
checkpoint only for a fully loaded global Since last caught up view.
APNs carries version/receipt metadata and opaque subject identifiers, never words, with receipt-scoped collapse.
`PhoneRouter` protects opaque pending routes on disk, retains them through
unlock/offline, and clears on unpair. Live controls require the exact current
episode on every snapshot; stale targets show saved records. History stays
memory-only on phone. Older peers keep generic Needs you notifications and
explicitly unavailable history. Rollout requires daemon, relay and phone
updates; this source change neither installs nor deploys them.

## Connector health and polling

**Connector health and polling.** Per-channel `_Health` logs transitions
and at most one reminder per 600 seconds. Push failures use separate
`_push_health`, logged but not published; undeployed push routes log once.
`health_snapshot()` copies scalars — `devices_snapshot()` runs
off the loop — and rides `/api/state` as `relay_health`: **statuses and
clock readings only**, never the key, its digest, or the channel id, which
is derived from the key. The speeds are decided from the later of two
moments: the last verified frame (`relay.last_frame_at(device_id)`,
persisted through `note_recv_ctr` with a `_frame_mem` overlay, stamped
at pairing) and the last buzz the mailbox accepted
(`_push_health[...].last_ok_at`, memory only — a restart or a
`remote_access` toggle forgets it, because a buzz is stale in seconds
and the phone's first frame re-arms the window).
`_next_gap(now, last_frame_at, last_push_at, idle_gap)` is the one
decision and `_serve` its one caller. Inside `ACTIVE_WINDOW_SECONDS`
(600s) the channel long-polls as it always has; outside it the poll
drops `wait=1` (built in one place, hence one occurrence in the file)
and sleeps `IDLE_GAP_START` 5s doubling to `IDLE_GAP_MAX` 30s. Nothing
periodic re-arms the window. Idle stays ≈ 6 Upstash commands a minute
(`box.js`'s `POLL_MS` 400 → 2000); each arming spends at most one
active window, ≈ 52 commands per 20-s held poll ≈ 156/min × 10 min ≈
1,560 commands, once, only while a phone is genuinely in play. A first
away request after a quiet spell can still wait one `IDLE_GAP_MAX`,
which is why the phone's `RelayChannel.request` default timeout is 45s. The socket lane (`docs/relay-socket-contract.md`) leaves this pacing untouched: a socket frame never stamps `last_frame_at`, so the mailbox idles down while the socket carries the phone. **One exception, the wake (25 Sep 2026):** the socket relay's `peer:1` — the phone opened its line, which it does the moment it comes to the front, during Face ID, when its last poll was away (`PhoneClient.prewarm`), and after a missed one-second home probe otherwise — is handed to `relay.note_phone_arrived`, a memory-only stamp that `_next_gap` reads as a fifth argument and holds for `PEER_ARM_SECONDS` (90s, a buzz's length), and whose `asyncio.Event` cuts an idle sleep short. A wake the socket cannot carry therefore finds a held mailbox, not a 30-second gap; a wake the socket does carry costs at most 90s of held polling (≈ 234 commands). `peer:1` is the relay's word, not a proof — anyone holding the channel id can connect as the phone and make the relay say it — so it arms at most once per `relay.PEER_REARM_SECONDS` (30 min) unless the phone proved itself away since the last arm: a verified mailbox frame (`last_frame_at`) or a verified socket frame (`relay.note_phone_proof`, from `relay_ws._handle_wire`, memory only). A verified home request is deliberately not a proof: a phone open at home proves itself every few seconds, so a stranger bouncing its line would re-arm on each one. The relay's `peer:0` — the phone left its line — ends the arm at once (`relay.note_phone_left`), so a phone that opened its line at home and closed it on the home answer costs seconds of held polling. A stranger reconnecting in a loop therefore buys the held rate ≈ 5% of the time, not for ever. The Mac arms only on the exact, unstripped `peer:1`: the relay drops a client frame that *starts* with `peer:`, so a padded one is the phone's own text and may disarm, never arm. Pinned by `test_relay_client.py`, `test_relay_ws.py` and `test_phone_wake_fast.py`.
**Blocking pops do not exist over Upstash's REST API** — cadence is the only lever, so do not
propose BRPOP. The phone's four failure sentences live in
`RelayChannel.Trouble`; the Mac's own sealed refusals, `LEASE_REFUSAL`
included, are shown verbatim and never reworded there.

How the phone chooses its route once it knows it is away — relay-first for writes, the direct walk's probe length, the cold-launch seed — is the phone's own rule and lives in `docs/phone-contract.md`. While the socket lane is open the phone sends every request down the socket first and falls the same request through to this mailbox; the rule is `docs/phone-contract.md`'s too.

## Phone Spenders are grouped by provider

**Phone Spenders are grouped by provider.** `/api/usage` retains legacy
`attribution.models` and adds `attribution.providers`. Stored Claude/Grok
turns use their recorded provider and each provider's priced local work as
its estimated-cost denominator. `codex_spenders.py` reads timestamped local
journal usage independently of the live roster and history database, keeping
the model at each event. Codex percentages mean **share of locally recorded
tokens**, input plus output; cached input and reasoning output are subsets.
They are neither dollars nor account-wide allowance shares. The Codex period
uses its own current matching account window, otherwise a named rolling
5h/7d window (day is rolling 24h). Parsing runs on the existing usage executor,
with a locked incremental byte cache, bounded progressive discovery, replay
deduplication and explicit partial/unavailable coverage. No persistent files
or Codex internal database are read or written. The phone decodes an absent
providers key as neutral legacy Spenders, honors an explicit empty list,
and replaces attribution on both successful transport legs; pairing reset
clears it. The desktop panel keeps its existing legacy response.

## `event_log.py` — the Mac's diary

**`event_log.py`** — the Mac's diary, for the phone to read *later*.
`/api/state` describes now; this is a bounded, append-only JSONL journal
(`~/.dark-army/event-log.jsonl`, in `paths._PRIVATE_FILES`, ≤ 500
lines, nothing older than 24 h returned or kept) of the moments that
happen while nobody is looking: a row's first live appearance in an
enriched snapshot (`_logged_starts`), a finish with a real `reason`, one
`session_error` per transition into error, a permission ask at both
row-creation sites and its one outcome (staged verdict, reap `why`, or
"the session ended"), and the board verbs — dispatch, agent close, hand
drag into Done (`_after_board_write`, so both drag doors log once), manual
flag, plan attached, and every `dispatch_error` write. **The sentence is
composed in one place**, `event_log.sentence()`, and every surface draws
`text` verbatim; `who` is nickname → title → provider → "an agent", never
an id. **Nothing secret gets in**: hooks copy named keys (`_log_permission`,
`_log_card_event`, `_log_session_event`) and `append` refuses
`FORBIDDEN_KEYS` (`claim`, `port`, `token`, `key`, `secret`) at any depth.
`_log_event` is `_history_write`'s hop with one difference: off the loop
(the bind expiries, the reap, the enrich) it appends **directly** rather
than returning, or every `card_dispatch_failed` would vanish. **The
finish rides `_record_finished`**, the seam every end shares —
`_forget_session`, the roster vanish in `_on_agent_records`, the Codex
settle and refresh, the Grok end, `stop_session`'s non-hook legs and the
terminal close — through `_log_session_end`, which pops `_logged_starts`
and so writes exactly once; logging in `_forget_session` alone left every
Codex, Grok-roster and background row started and never finished. **A
restart is not a start**: `_seed_logged_starts` rebuilds the set from the
diary when it opens (a session counts as started when its newest
start-or-end line is a start), and the enrich loop skips a row whose
`started_at` predates its own logged `session_end`
(`EventLog.last_ts`) — a stale-evicted id returning is the same run.
Loaded whole into a deque on `open()`, so `recent()` costs no I/O. The
deque is trimmed on **every** append, so a read never exceeds the bounds;
the file is rewritten atomically only when an age prune dropped a line or
the on-disk count has reached `MAX_ENTRIES + PRUNE_SLACK` (50) — never
once per append at the cap, which was a full rewrite and fsync per event
while `recent()` waited on the lock. `open()` compacts whatever it
dropped. **One read on
all three doors**: `GET /api/log` on loopback, and the sealed `log` kind
through `_sealed_run` at home and away (`_lan_home` / `_remote_run`) —
a *read* beside `state` and `usage`, so `LAN_ACTIONS` and
`REMOTE_ACTIONS` did not grow; plaintext `GET /api/log` on LAN is 426
like the other three. `since` (float ≥ 0, strictly newer) and `limit`
(1..`MAX_ENTRIES`), 400 otherwise; `available` is stated, never inferred.
Not on `/api/state`, not on SSE. The phone's Fleet tab draws it as
**RECENTLY (n)** under the process table (`RecentlyView.swift`, fold
remembered in `fleet.recently.collapsed`), fetched by `fetchLog` behind a
state 200 every 30 s on whichever leg the poll used and on tab open; the
widget and `backgroundRefresh` never fetch it (`backgroundRun`). Pinned
by `test_event_log.py`, `test_event_log_hooks.py` and
`test_phone_event_log.py`, which imports `PUBLISHED_KEYS`.

## `work_record` is the sixth sealed read

**`work_record` is the sixth sealed read**, `log`'s sibling and `log`'s
rule: `_work_record_report_for(query)` is one body behind loopback
`GET /api/work-record` and the sealed kind on both doors, **above** the
`action` branch, so neither action tuple grew, no lease is checked and no
`remote_activity` is written. `card=<id>` returns the record with
`available` **stated**; `card=<id>&file=<n>` returns one file's diff, and
`n` is an **integer index into the record's own stored list** — the caller
never supplies a path, so there is nothing to sanitise. The record's
`root` is re-checked against `enrollment.enrolled_roots()` at the moment
of the read, never trusted from the stored row, and the resolved path is
component-contained inside it (`workspace._contains`) as belt and braces.
Only the diff *text* is truncated (`MAX_DIFF_BYTES`), stated rather than
inferred. The Mac's sheet gets the whole record free as
`work_record_full` on the `GET /api/board?card=` it already makes; only a
file's diff costs a second request. The phone reads a **404 as "this
Mac's Dark Army is too old"**, never as "no record".

## `lifecycle` is a sealed read

**`lifecycle` is a sealed read**, `outcomes`' sibling and `log`'s rule: `_lifecycle_report_for(query)` is one body behind loopback `GET /api/lifecycle` and the sealed kind on both doors, **above** the `action` branch, so neither action tuple grew, **no lease is checked** and no `remote_activity` is written. The kind is on `_lan_home`'s allowlist **and** in `_sealed_run`; a kind handled only in `_sealed_run` still 404s at home. Query forms are `root` (project) or `card` (retained timeline), mutually exclusive, with the same frozen `[from,to)` UTC period (default last 30 days, at most 366) and `as_of`. Default page 25, allowed 1..100; UTF-8 JSON is capped at 300 KB before sealing. `lifecycle_supported` on `_pipeline_writable()` is the version marker. Full measurement contract: `docs/lifecycle-timing.md`. Pinned by `test_lifecycle_api.py`.

## `history_week` is a sealed read

**`history_week` is a sealed read**, `log`'s sibling and `log`'s rule: the phone's History screen, the Mac's last seven days across every project. `_history_week_for` sits **above** the `action` branch, on `_lan_home`'s allowlist **and** in `_sealed_run`, so neither `LAN_ACTIONS` nor `REMOTE_ACTIONS` grew, **no lease is checked** and no `remote_activity` is written. It takes **no query**: the body is ignored and the week is fixed — the same `agent_efficiency_report(7, "")` the Mac's `/api/history?range=7d` makes with no project. The answer is a **closed projection**, rebuilt row by row from `HISTORY_WEEK_CARD_KEYS` (`card_id`, `sessions`), `HISTORY_WEEK_SESSION_KEYS` (the session id, provider, phase, `bound_at`, `known`, the token cost and its provider split, `token_unpriced_turns`, `reported_cost_usd`) and `HISTORY_WEEK_DAY_KEYS` (the leftover day's provider token costs, reported dollars and token-unpriced counts and ids); the body adds `available`, `range_days`, `from`, `to`, `generated_at`, `partial` (the join was capped) and `codex_history_partial`. No title, person, project root, model or turn count rides it. A null or absent value is **absent**, never 0 (a 0 would price a day at $0.00); a stored 0 stays 0. Nothing is priced here: the phone folds it with `LedgerWeek`, the fold the Mac's History calls (`docs/phone-contract.md`). The page is bound at 300 KB of plaintext before sealing by halving `cards` from the tail, with `truncated: true` stated, since a cut lowers the week's total. Loopback `GET /api/history-week` is token-gated like `/api/knowledge` (`X-Bob-Token`, empty Origin allowed on GET, the Host check above the routing table). On the LAN door plaintext `GET /api/history-week` is 426 in `HOME_UPDATE_REFUSAL`'s words, recorded `plaintext`; `GET /api/history` is not a LAN route and stays 404 `not_found`. `history_week_supported` on `_pipeline_writable()` is the version marker. Pinned by `test_history_week_api.py` and `test_lan_door.py`.

## `knowledge` and `manual_checks` are sealed reads

**`knowledge` is a sealed read**, `log`'s sibling and `log`'s rule: one enrolled project's notes, behind loopback `GET /api/knowledge?root=` (token-gated, `X-Bob-Token`, empty Origin allowed) and the sealed kind on both doors, **above** the `action` branch, so neither action tuple grew, **no lease is checked** and no `remote_activity` is written. The kind is on `_lan_home`'s allowlist **and** in `_sealed_run`; a kind handled only in `_sealed_run` still 404s at home. `root` rides the JSON body, never a query string. Empty or unenrolled is refused in words. Cap 300 KB plaintext before sealing, `break` not skip-and-continue. Person writes `knowledge_confirm` / `knowledge_stale` / `knowledge_edit` are loopback `BOARD_ACTIONS` only and 404 as a sealed `action`. `knowledge_supported` on `_pipeline_writable()` is the version marker. Pinned by `test_knowledge_api.py` and `test_phone_knowledge.py`. **`manual_checks` is a sealed read**, `knowledge`'s shape and `log`'s rule: loopback `GET /api/manual-checks` (token-gated, `X-Bob-Token`, empty Origin allowed) and the sealed kind on both doors, **above** the `action` branch, on `_lan_home`'s allowlist **and** in `_sealed_run` — no lease is checked and no `remote_activity` is written. With `root` / `q` / `status` it is the Checks section's list: every `manual-check/<YYYY-MM-DD>-<slug>/check.md` under the enrolled roots (`manual_check.scan`), **`root` empty meaning every enrolled root** (the person's own list across their projects), a named root refused in words unless enrolled, `q` a casefolded search over title, check, steps, outcome and card (≤ 200 characters), `status` one of `all`, `open`, `passed`, `failed`; open first, then newest first by `Created`, each row joined to its card through `manual_check_path`. A file out of shape is listed `malformed` with its first problem, never dropped. With `path` it is one file's text, `available: false` with the reason outside an enrolled project's folder. On the sealed doors every key rides the JSON body. 300 KB of plaintext before sealing, the tail dropped with `truncated: true`. One executor hop each; nothing rides `/api/state` or SSE. `manual_checks_supported` is the version marker. Pinned by `test_manual_check_api.py`.

## `card_changes` is a sealed read

**`card_changes` is a sealed read**, `manual_checks`' sibling and `log`'s rule: a Done card's branch against the main line — its commits, its files with added and removed counts, and one file's changes — behind loopback `GET /api/card-changes?card=<id>[&file=<n>&tip=<sha>]` (token-gated, either token on `SESSION_READS`, empty Origin allowed on GET, the Host check above the routing table) and the sealed kind on both doors, **above** the `action` branch, on `_lan_home`'s allowlist **and** in `_sealed_run` — so neither action tuple grew, **no lease is checked** and no `remote_activity` is written. Both call `_card_changes_for(params)`; on the sealed doors `card`, `file` and `tip` ride the JSON body, never a query string. `file` is an **integer index** into the listing the daemon recomputes in the same hop, never a path, and `tip` is **required** and must equal the branch's current tip (`CHANGES_MOVED`, 409; missing or malformed, 400 in words), so a diff is never drawn against a listing that has moved. The answer is a closed key set (`merges.CHANGES_KEYS`): `available`, `card_id`, `branch`, `trunk`, `merge_base` (sha8), `branch_tip` (the full hash the clients echo back), `ahead`, `behind`, `commits` (`sha8`, `author`, `at`, `subject`; at most 100), `files` (`path`, `added`, `removed`, `binary`; at most `work_record.MAX_FILES`), `files_total`, `files_truncated`, `commits_truncated`, `merge_offered` and `merge_refusal` (the gate's words minus the busy rungs), `merge_state`, `merge_note`, `review` (`verdict`, `tip` sha8, `current`), `generated_at`, `reason`. A file's changes are `{available, path, text, truncated, reason}`, `text` cut at `MAX_DIFF_BYTES` and the cut stated, the path component-contained in the real root (`workspace._contains`). No board, no card, no branch, an unenrolled root, a non-checkout or a git failure is a 200 `available: false` with the reason, never a 500; the page is bound at 300 KB of plaintext before sealing by dropping `files` from the tail with `files_truncated: true`. Read-only git from the project's root (a few calls, serialised by `_changes_lock`, one executor hop), fetched when a client's CHANGES section opens — never from `/api/state`, SSE, the phone's poll, `backgroundRefresh` or the widget. `card_changes_supported` is the version marker. Pinned by `test_card_changes_api.py`.
**`review_offer` is a sealed read**, `knowledge`'s sibling (`root` in the body); `review_start`, `review_continue` and `review_end` are on both phone tuples, each its own decision: `docs/review-runs.md`.

## `scout_reports` and `scout_report` are sealed reads

**`scout_reports` and `scout_report` are sealed reads**, `log`'s siblings and `log`'s rule: the list of every scout report Dark Army knows about and one report's text. Both sit **above** the `action` branch, on `_lan_home`'s allowlist **and** in `_sealed_run`, so neither `LAN_ACTIONS` nor `REMOTE_ACTIONS` grew, **no lease is checked** and no `remote_activity` is written. On loopback they are `GET /api/scout-reports` (optional `root=`) and `GET /api/scout-report?path=`, token-gated like `/api/knowledge` (`X-Bob-Token`, empty Origin allowed on GET, the Host check above the routing table). On the sealed doors `root` / `path` ride the JSON body, never a query string: a path must not reach the relay's logs.

The list (`host/dark_army_daemon/scout_index.py`, `build`) is derived from two facts alone — the dated folders `scout/<YYYY-MM-DD>-<slug>/report.md` under every enrolled root and the cards' `report_path` (which may name an older prose report) — newest first by the file's time, each row its title, verdict, question, project, day and card, and **never a body**. A `root` present but empty, or not enrolled, is refused in words. The page is bound at 300 KB of plaintext before sealing by dropping from the tail — the **oldest** rows, since the list is newest first — naming the count in `omitted` with `truncated: true`, never skip-and-continue.

The body read accepts a **closed set**, re-checked at the moment of the read (`scout_index.locate`): a card's stored `report_path` inside that card's enrolled root, or `<enrolled root>/scout/<dated folder>/report.md`, both through the attach's own `_plan_path_refusal` realpath rule. Anything else is a 200 `available: false` with "that report is not one Dark Army lists"; a missing `path` or a repeated parameter is a 400 in words. The text is split into the answer block (`scout_report.parse_header`'s dict) and the body with the H1 and the block removed. Both reads are one `run_in_executor` hop each; nothing rides `/api/state`, SSE or the sealed `state` read. `scout_reports_supported` on `_pipeline_writable()` is the version marker. Pinned by `test_scout_reports_api.py`, `test_scout_index.py` and `test_scout_reports_surface.py`.

**The list read also searches the reports' text** (`scout_index.search`): `?q=` on loopback, `"q"` in the JSON body on both sealed doors — never a query string there, a search term must not reach the relay's logs any more than a path. The candidates are exactly `build`'s (one function, `_candidates`, both read), so a hit is always a report the index lists and the body read would open; only the **body** is searched — `_split`'s text with the H1 and the answer block removed, since title, verdict, question and the rest are the index's fields and the clients' instant filter's. Whitespace in `q` is collapsed to single spaces (the clients send it that way, cut to 200); a `q` under `MIN_QUERY_CHARS` (3), before or after folding, or over `MAX_QUERY_CHARS` (200) is a 400 in words. At most `SEARCH_MAX_BODIES` (400) bodies are read, newest first, each through `scout_report.read_text`'s 64 KiB bound — a report over it, or unreadable, is counted in `unsearched` and skipped, **never clipped** — and the search stops at `SEARCH_MAX_HITS` (50). Case and combining marks are folded (`scout_index.fold`). The reply is `build`'s shape with only the matching rows, each carrying `snippet` (one whitespace-collapsed body line of at most 160 characters around the match — a snippet, not a citation), `match_line` (1-based, in the body) and `match: "body"`, never `text` or `body`; the page adds `query`, `searched`, `unsearched`, `search_truncated` (the reading bound bit) and `hits_truncated` (the hit cap did), and the 300 KB page bound still applies. The search runs inside the same single `run_in_executor` hop as the list, **one text search at a time per daemon** (`_scout_search_lock`; the plain list never waits on it); neither action tuple grew and nothing new rides a snapshot. `scout_reports_body_search_supported` on `_pipeline_writable()` is its marker: a phone against an older Mac decodes false and sends no `q`. Pinned by the same three tests.

## `plans` and `plan` are sealed reads

**`plans` and `plan` are sealed reads**, `scout_reports`' siblings and its rule: the list of every written plan Dark Army knows about and one plan's text. Both sit **above** the `action` branch, on `_lan_home`'s allowlist **and** in `_sealed_run`, so neither `LAN_ACTIONS` nor `REMOTE_ACTIONS` grew, **no lease is checked** and no `remote_activity` is written. On loopback they are `GET /api/plans` (optional `root=`) and `GET /api/plan?path=`, token-gated like `/api/scout-reports` (`X-Bob-Token`, empty Origin allowed on GET, the Host check above the routing table). On the sealed doors `root` / `path` ride the JSON body, never a query string: a path must not reach the relay's logs.

**`image` is a sealed read**, `plan`'s sibling and its rule: one picture from a session's project, for the phone's Conversation tab. It sits **above** the `action` branch and below the bot's Read grant, on `_lan_home`'s allowlist and in `_sealed_run`; neither action tuple grew, no lease is checked, no `remote_activity` is written, and there is no loopback route. `session` and `path` ride the JSON body. `image_preview.locate` confines the path to the session's project (the nearest `.git` folder above its working folder, never the home folder, `/` or any folder holding the home folder, judged by on-disk identity so `/users` and `/System/Volumes/Data/Users` count; and a file whose folders up to the root pass through home or anything holding it is refused, so a root that holds home under another name serves nothing), and the session's working folder must lie in Dark Army's own checkout or an enrolled project (`_onboarded` + `_in_projects`, by on-disk identity, walking up from the working folder), the root then narrowed to the nearer of the git root and that project so an enrolled subfolder of a bigger repository opens only itself, and a worktree outside every enrolled folder shows nothing (a corrupt ledger entry is skipped; an unreadable ledger shows nothing and an enrolled entry that is home, holds it or is a whole disk (`os.path.ismount`) is ignored, so Documents, Desktop and the home folder are never readable); it refuses a `..` walk-out, a symlink leaving the project, any hidden path part and any extension outside PNG, JPEG, GIF, SVG, WebP and HEIC; a relative path is tried against the working folder, the root, then as the tail of a file inside the project (bounded, hidden and build folders skipped). `render` never sends a file's own bytes except an animated GIF ImageIO itself identifies that already fits: everything else is decoded and re-encoded (at most 2048 pixels, at most `PREVIEW_MAX_BYTES` = 450 000 so the base64 fits one 900 000-byte frame), so a secret named `.png` is refused as unreadable; a picture stating more than 100 million pixels is never decoded, and an animated GIF travels as itself only while its frames together stay under 40 million pixels (the phone holds the same cap). Reads are served one at a time (`BobDaemon._image_gate`). An SVG that is not plain UTF-8, or naming any external `href`, `src`, `url()`, `@import`, entity or `foreignObject` is refused before AppKit draws it. Refusals are `available: false` with `reason` in words; a missing or oversized `session` / `path` is a 400. `host/tests/test_image_preview.py` pins it.

The list (`host/dark_army_daemon/plan_index.py`, `build`) is derived from two facts alone — the dated files `plans/<YYYY-MM-DD>-<slug>.md` under every enrolled root (the name rule alone leaves the folder's `README.md`, its question list and any directory out; at most 600 names per root, the newest) and the cards' `plan_path` (which may name a plan outside `plans/`; the first card in `CARD_ORDER_SQL` annotates a shared plan's row) — newest first by the **file name's day**, then the file's time, then the path, so a plan iterated today keeps its place; each row its title, `Status:` and `Area:` from the header list, project, name, slug, day and card (`card_id`, `card_title`, `card_column`), and **never a body**. Only a 16 KiB head of each file is read. A `root` present but empty, or not enrolled, is refused in words. The page is bound at 300 KB of plaintext before sealing by `_scout_reports_page_bytes`, dropping from the tail — the **oldest** rows — naming the count in `omitted` with `truncated: true`.

The body read accepts a **closed set**, re-checked at the moment of the read (`plan_index.locate`): a card's stored `plan_path` inside that card's enrolled root, or `<enrolled root>/plans/<dated name>.md` — exactly two components under the root after realpath, so a symlinked file or folder resolving elsewhere is refused — both through the attach's own `_plan_path_refusal` realpath rule; never "any `.md` under an enrolled root". Anything else is a 200 `available: false` with "that plan is not one Dark Army lists"; a missing or empty `path`, one over 4096 characters or a repeated parameter is a 400 in words. The text comes back whole with its first H1 line removed (the client draws the title) through `scout_report.read_text`, bounded at 64 KiB — the same number as `MAX_PLAN_BYTES` — and a plan over it is "that plan could not be read", never clipped. Both reads are one `run_in_executor` hop each; nothing rides `/api/state`, SSE or the sealed `state` read. `plans_supported` on `_pipeline_writable()` is the version marker. Pinned by `test_plans_api.py`, `test_plan_index.py` and `test_phone_plans.py`.

## The phone doors keep an access log

**Every refused knock on either phone door is written down, after the door has answered.** `access_log.py` is `event_log.py`'s shape on its own file (`~/.dark-army/access-log.jsonl`, `paths._PRIVATE_FILES`, ≤ 2000 lines, 30 days — **the cap evicts `refusal` lines only**, so a flood from one peer cannot push the `burst` line that keeps its own alert open off the end; only the TTL or an ack closes an alert, and `MAX_ALERT_ENTRIES` (200) is the belt on the lines the cap spares, which takes **closed alerts first** — an acknowledged or TTL-expired burst with its ack, oldest first — and falls back to the oldest open one only when the belt is still exceeded, so two hundred closed alerts can never evict an unacknowledged, in-date one): one `refusal` line per turned-away request — `door` in `access_log.DOORS` (`lan`, `pairing`, `upload`, `relay`), `peer` (the LAN address, or for the relay the paired **device id**, since the internet gives no sender), `reason` in the closed `REASONS`, `device_id` where the door knew one, and the sentence composed once in `access_log.sentence()`. The recorder, `ApiServer._record_access`, is **fire-and-forget and sits on the line before each existing refusal `return`** — in `_handle_lan_client` (the five 426 routes, the 404, and the body rule's oversize refusal through `_read_request(on_oversize=)`), `_home_open`, `_home_accept_preverified`, `_lan_pair` (its `pair_plain` gate and the typed exchange's `pake` refusals included), `_lan_pair_sealed`, `_lan_upload`, the two accept-time refusals — `tunnel` for a knock that landed on a VPN address and `busy` for one over a connection cap, both recorded with nothing read — and `relay_client._drop` — and **never changes a verdict**: `test_access_log_doors.py` compares each refusal with the recorder spied out. What is never logged: the raw `X-Bob-Channel` (derived from the home key), a channel id, a mailbox URL, a pairing code, a key — `access_log.FORBIDDEN_KEYS` widens the diary's fence with those names as the second fence.

**A burst is an alert.** `BurstDetector` (pure, in memory, LRU-bounded to
`MAX_TRACKED_PEERS`) counts `WEIGHTS`-weighted refusals per peer:
`BURST_THRESHOLD` (5) inside `BURST_WINDOW_SECONDS` (600) raises one alert,
then none for `ALERT_COOLDOWN_SECONDS` (3600) about that peer. `ctr`, `ts`
and `kind` — refusals of a frame whose seal **verified**, the person's own
phone — weigh zero and can never alert. So does the relay's `rate`: its
bucket fires before the seal is opened, a quantity signal and not an
authenticity one (a stranger flooding the mailbox still produces counted
`seal` / `shape` refusals), so it is logged and never a hit. And an
`oversize` refusal **carrying a device id** — the body rule opened the
upload frame before refusing the body, so `_read_request` hands
`on_oversize` the id beside the path — is a keyholder's too-large photo and
weighs nothing at `BurstDetector.note(device_id=)`; the same reason with no
id, a stranger's body, still counts. The raise **awaits** the `burst`
append before its delivery and the wake, so no frame carries the banner
beside an empty `security.alerts`. The raise writes a `burst` line
(`id` = the alert id), one `access_burst` diary line, and one
`kind == "security"` row delivered at once through `_deliver_batch` — the
Mac banner and the phone buzz, the path `_deliver_alerts` drains through —
without draining the queued agent alerts, which wait for the snapshot's
own drain.

**One raise per `ALERT_FLOOR_SECONDS` (600), whatever the peer.** Source
addresses are free (IPv6 temporaries), so the per-peer cooldown alone let
one LAN machine raise two hundred alerts. `BurstDetector` keeps a
machine-wide `_raised_at` beside the per-peer `_alerted_at`: a burst that
clears its own peer's cooldown but lands inside the floor spends that
cooldown as usual and answers `{"fold": True, ...}` instead of the alert
dict (the raise answer carries no `fold` key). The floor is on **raising**
— the detector knows nothing of acknowledgements, so an ack never resets
it, and a burst right after an ack folds into the acked alert (on the log,
off the list) — and it is **memory only**: a restart lets the next burst
raise. `BobDaemon._fold_access_alert` writes one **`burst_more`** line —
`alert_id` naming the open alert, the folding knock's `door` and `peer`,
the running `count` and distinct-source `peers` (bounded by
`MAX_FOLDED_PEERS`, 1024 — at or past it the daemon counts no further and
both sentences render the figure as "at least N"; below it the wording is
exact and byte-identical) off the loop-only `_access_fold` record
`_raise_access_alert` sets — and **nothing else**: no diary line, no
`security` row, no banner, no wake (a wake per fold is a per-knock cost the
attacker controls; the count rides the next frame). A fold with no open
alert to fold into is dropped, never promoted to a raise. **The decision
and its journal write are serialised on the loop**: `_record_access` fires
one task per knock, so `note_access_refusal` takes `BobDaemon.
_access_alert_lock` around `BurstDetector.note` and the raise or fold it
leads to — the raise sets `_access_fold` only after its own executor hop,
and a sibling burst resuming inside that hop would otherwise find no open
alert and drop its fold; the refusal append stays outside the lock, so
every knock is still logged without waiting. The `burst` line is never rewritten: `AccessLog.open_alerts` is the
**one merge point**, giving each row the newest fold's `count` / `peers`,
its `ts` as `last_seen` (a `_CLOCK_FIELDS` name on purpose) and a recomposed
`text` — `N refused attempts from P and M other sources at the Wi-Fi door`,
no window clause — while a row with no fold reads `peers = 1`,
`last_seen = ts` and the byte-identical original sentence. The belt keeps
**the newest fold per alert**, drops one naming no `burst` on the file,
never counts a fold against `MAX_ALERT_ENTRIES`, and evicts a fold with its
alert in the same pass, so a flood of folds can neither grow the file nor
push an older open alert off the end.

The open alerts ride the
snapshot as the **`security`** section (`security_snapshot()`: `available`
stated, `alerts` from `AccessLog.open_alerts` **capped at the newest
`MAX_PUBLISHED_ALERTS` (8), oldest first, plus `hidden` for the rest** —
`ack_access_alert` checks the uncapped list, so a hidden alert still acks,
and the unavailable shape stays the two-key one — each carrying `door_word`
— the person's word for the door, `access_log.door_label` — beside the
`door` token, so neither Inbox draws `lan`, plus `peers` and `last_seen`;
omittable under `?sections=changed`), both Inboxes consume it and re-derive
nothing (the Mac banner for a `security` row is `notifier.post_notice`, no
buttons and no thread), and `access_alert_ack` `{id}` — on `LAN_ACTIONS`
and `REMOTE_ACTIONS`, a loopback `/api/action`, clear-never-set — writes
the `burst_ack` line. The log is read by token-gated loopback `GET
/api/access-log` (`since`, `limit`) and the sealed kind **`access_log`** on
both doors, `log`'s rule: a read above the `action` branch, no lease, no
record; **both stay uncapped** (`open_alerts` whole, merged). `access_log_supported` on
`_pipeline_writable()` is the version marker. Pinned by `test_access_log.py`
and `test_access_log_doors.py`.

## Every phone request is timed

**Every sealed request on either phone door is timed on the Mac, its
legs are echoed to the phone, and ten minutes of them land on the access
log as one line.** `link_timing.py` is pure and stdlib-only (`now` is
handed in): `LinkTimer.add` per request, a closed rollup every
`ROLLUP_SECONDS` (600) per `(door, device)` over a `MAX_SAMPLES` (512)
ring, nearest-rank `percentile`. The relay's hops are `dwell` (Mac wall
clock at pickup minus the phone's `ts` inside the opened frame — two
clocks, **approximate**, clamped at zero, never corrected;
`RELAY_SKEW_SECONDS` bounds it), `hold` (the held GET's wait, monotonic),
`open`, `run` and `answer` (seal + POST); the LAN door's are `open`
(`_home_open`), `run` and `seal` (`_home_answer`); both record `size`.
`_serve` stamps pickup and hold, `_handle_wire` times the opening and
hands `_execute` a `_Hops` record; `_lan_home` stamps inline. **The echo
rides the reply envelope, never the state body**: `"timing"` beside
`body` (`{dwell, hold, open, run}` away, `{open, run}` at home), so
`_state_digest` and `_POLL_ECHO_FIELDS` are untouched and an `unchanged`
poll stays short; every shipped phone ignores the sibling key. The record
is fire-and-forget: `ApiServer.note_link_timing` is `_record_access`'s
shape (never raising into a door; `_execute` wraps it as `_drop` does, a
failed reply POST filed under status 0), and `BobDaemon.note_link_timing`
runs `LinkTimer.add` on the loop and appends one **`timing`** line on the
executor only when a window closed.

**A timing line never weighs and never alerts** — no `reason`, no
`WEIGHTS` entry, `BurstDetector.note` never called (a spy asserts it).
Its `hops` is the closed `HOP_KEYS` set (`n`, then `p50` / `p90` / `max`
per hop), stated literally in `access_log.py` and pinned equal to what
`LinkTimer.rollup` emits; `append` coerces values to `float` and refuses
a stray key; `PUBLISHED_KEYS` gains `hops` (`{}` on every other kind).
`sentence()` names each door's own hops (`relay: 74 requests in 10
minutes — picked up in 1.9 s typical, 3.8 s slow; handled in 0.04 s;
answered in 0.3 s`). The lines sit on **their own belt**,
`MAX_TIMING_ENTRIES` (600), oldest first as the first rung of
`_prune_locked`, counted against neither `MAX_ENTRIES` nor
`MAX_ALERT_ENTRIES`, and the file compacts once it runs `PRUNE_SLACK`
lines past what is held, never per append. `recent(include_timing=False)`
hides them unless asked: the loopback `GET /api/access-log` and the sealed
`access_log` kind take a fourth query key **`timing`** (`0` / `1`, default
`0`, 400 otherwise) — the rollups ride **beside** `limit`, at most
`TIMING_READ_LIMIT` (100) newest, so days of them never crowd a refusal
out of the phone's read — so an older phone and the panel's window never
see a line; `link_timing_supported` on `_pipeline_writable()` is the marker.
A line carries floats, a count and the `device_id` the log already
publishes — **no key, digest, claim, channel id or mailbox URL**.

**The phone keeps its own side.** `RelayChannel` and `HomeChannel` stamp
`sentAt`, `postSeconds` and `totalSeconds`, read `payload["timing"] as?
[String: Double] ?? [:]` tolerantly and fire `onTiming` beside
`onCounters` on every completed request (the `ctr_expected` retry reports
once, timed from the first POST; a cancelled or unanswered request reports
nothing); `PhoneClient` records into `LinkTimingStore`
(`docs/phone-contract.md`) and asks for `&timing=1` only where
`board.linkTimingSupported`; `backgroundRefresh` wires none of it.
Measured by `tools/relay_latency_bench.py`; the audit is
`docs/2026-09-20-relay-latency-audit.md`. Pinned by `test_link_timing.py`,
`test_access_log.py`, `test_access_log_doors.py`, `test_relay_client.py`,
`test_lan_access.py`, `test_unchanged_state.py`, `test_phone_remote.py`
and `test_relay_latency_bench.py`.

## `conversation`

**`conversation` is the eleventh sealed read**, `log`'s sibling and `log`'s
rule: `_conversation_report_for` sits above `action` — neither tuple grew,
no lease, no record. Query: `session` (required, ≤ 200), `since` (0..10**9,
default 0), `key` (≤ 64 `[A-Za-z0-9:_-]`). A repeated key is 400 in words.
Page bounded at 150 turns / 200 000 bytes, with `more` / `next_seq` /
`reset`. Unknown session is 404 in words; missing journal is 200
`available: false`. No path on the page. A reply typed in Dark Army's
panel or on the phone — the `isMeta` `user` record with `origin.kind`
`channel` whose text opens `<channel source="…" kind="user">`, read by
`session_stats._is_channel_user_message` — is a `user` turn with both tags stripped; `kind="fleet"`, a look-alike without that origin and every
other `isMeta` record are no turn (23 Sep 2026). `conversation_supported` absent
decodes false. Loopback GET is token-gated like `/api/knowledge`. Pinned
by `test_conversation.py`.

**`agent`** (1–64 `[A-Za-z0-9_-]`, else 400) pages that Claude session's own `subagents/agent-<id>.jsonl`, sidechain lines kept; a missing file or a non-Claude session is `available: false` in words. `subagent_conversation_supported` absent decodes false: no helper tabs.

## `done` is the tenth sealed read

**`done` is the tenth sealed read**, `log`'s sibling and `log`'s rule:
`_done_archive_for(payload)` sits **above** the `action` branch, so neither
`LAN_ACTIONS` nor `REMOTE_ACTIONS` grew, **no lease is checked** and no
`remote_activity` is written — expiry bounds what the phone may *do*, never
what it may see. It answers `ApiServer._done_archive()`, the one builder
behind loopback `GET /api/board?column=done` and this kind, so the two doors
can never drift into two answers about the same column. The cards come back
in the **snapshot's own shape** — trimmed, decorated, `thread_count` and
`work_record` included — because a client splices them into `board.cards`,
and a second shape would be a second idea of what a card is. Bounded at
`CARD_SYNC_MAX_BYTES` (300 000, `_outcome_page_bytes`' own figure and its
reason) with an integer `offset` from the payload and `more` / `next_offset`
**stated**. Any `column` other than `done` on the loopback read is a **400 in
words**, `BOARD_RANGES`' precedent; `?range=` and `?card=` are untouched.

**`done=review` on the sealed `state` read is the phone's half of the same
opt-in.** The phone sends `"done": "review"` in the state body
unconditionally, and `_state_answer` builds `self.state(done_review=…)`
**before** `_state_digest(state)`, so the digest a phone quotes always
fingerprints the picture it was actually sent. The board that comes back
still carries every finished card that wants a person — `board.review_only`
drops exactly the cards stamped `done_preview`, which is the record of which
store read produced the row and never a second judgment — and the rest is
this kind's to fetch. `unchanged: true` stays a **present key**; nothing
here turns any part of that answer into an omission.

**Two tokens ride the board, and they are not interchangeable.**
`done_clear_token` is exact membership over the Done ids and is the **only**
thing `clear_done` ever compares — a content-sensitive token there would
refuse a Clear Done because somebody retitled a finished card.
`done_view_token` is what a *held* copy can go stale against: membership,
any revised column, and a review. `BoardStore.done_tokens()` reads both
under one lock, so they can never disagree about which cards exist.

## `bearings` is a sealed read

**`bearings` is a sealed read**, `log`'s sibling and `log`'s rule:
`_bearings_report_for(query)` sits **above** the `action` branch, on
`_lan_home`'s allowlist **and** in `_sealed_run`, so neither
`LAN_ACTIONS` nor `REMOTE_ACTIONS` grew, **no lease is checked** and no
`remote_activity` is written. `since` rides the query string and narrows
Recently landed alone. The body copies no key, digest, claim, port or
channel id. Loopback `GET /api/bearings` and `/api/bearings/text`; LAN
plaintext is 426 in `HOME_UPDATE_REFUSAL`'s words. Composer:
`docs/context-host.md`.

## `card_sync` is the delta card read

**`card_sync` is the delta card read, and it is a kind for `log`'s
reason.** `_card_sync_for(payload)` parses
`ids=<id>,…&plans=<id>:<sha256hex>,…` (`_log_report_for`'s discipline:
400 on a repeated key, an over-long id or more than `CARD_SYNC_MAX_IDS`
(50) of either; a malformed stamp is **dropped, never fatal**) and makes
exactly one executor hop into `_cards_sync_page`, whose per-card body is
**`_card_collect(with_plan=True)` and nothing else** — one reader for a
card and its plan, one containment rule (`_plan_path_refusal`), pinned by
a grep in `test_card_sync_read.py`. A plan whose digest the caller
already holds comes back `unchanged` with **no text**, which is what
makes a re-check of 50 unchanged plans cost 50 stats and zero bytes.
Bounded at `CARD_SYNC_MAX_BYTES` (300 000, `_outcome_page_bytes`' own
figure and its reason), with `more` / `unserved` **stated**. It is
served at home *and* away, checks no lease, writes no `remote_activity`
and appears on **neither** action tuple: expiry bounds what the phone
may *do*, never what it may see. A Mac too old to know the kind 404s it,
and the phone **latches that** (`PhoneClient.cardSyncUnsupported`): an
unfillable cache would otherwise report every card stale on every pass
and ask again for ever. A card carrying a real `revision` lifts it.

## A press carries a one-time mark

**A press carries a one-time mark.** `command_receipts.CommandReceipts`
is a bounded (256), TTL'd (900s), **memory-only** `(device, token) ->
(status, ctype, body)` map on `ApiServer`, consulted inside
`_sealed_run`'s `action` branch **below** the allow-list and **below**
the lease check — a lookup above either would let a token minted at
home replay from a lapsed away window. 200 and 409 are both recorded
(both are final answers about that press; a RETRY mints a fresh token);
an absent or malformed token is no dedupe and today's behaviour exactly.
A restart forgets every receipt, and what covers that window is the
phone's own effect test, `OutboxStore.alreadyLanded`'s argument one rung
on: never resend what you can check.
