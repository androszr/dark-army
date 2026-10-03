"""Local HTTP + SSE API — the the panel panel's back end.

Hand-rolled HTTP/1.1 on asyncio rather than a framework, for the same reason the
rest of the host layer has no dependencies it can avoid: this ships inside a
py2app bundle, and every runtime import is one more thing that can fail to freeze.
The surface is four routes, and SSE is line-oriented text — no framing, unlike
WebSocket, so there is nothing here worth a library.

Threat model. The listener is bound to 127.0.0.1, so the only remote attacker is a
web page in the user's own browser. Such a page can *issue* requests to localhost
but cannot *read* the responses: we emit no CORS headers, so the browser withholds
the body. That makes reads safe to leave open (and keeps `curl :19874/api/state |
jq` working), while anything that changes state requires a token the page cannot
obtain plus a same-origin check.

That argument has a hole in it, and `_loopback_host` is the patch. It holds for an
ordinary cross-origin request and fails for **DNS rebinding**: a page on
`evil.com` whose name is re-pointed at 127.0.0.1 is, as far as the browser is
concerned, *same-origin* with this server, so the no-CORS-headers defence stops
applying and the body is handed over — which for `/api/state` is every project
path, branch, question and cost on the machine. The defence against rebinding is
not CORS at all but the `Host` header, which still says `evil.com` because that is
what the user's browser was told to ask for. So every request must arrive
addressed to loopback by name. Dark Army's own VS Code extension has always done this;
this server did not, and the gap was the whole of the read surface.

A local process is the second attacker, and the browser argument says nothing
about it: any program running as this user — every agent Dark Army starts
among them — can read any file the daemon writes. So the loopback door holds
**two tokens** (`docs/transport-contract.md`, *The loopback door has two
tokens*). The **desk token** (`self.token`) is minted fresh in memory at every
`start()`, never written to disk, an environment, an argv, a snapshot or a log,
and handed to the panel over the menu-bar app's private stdin pipe; it opens
every write. The **session token** (`self.session_token`) is the file that
already existed, `~/.dark-army/api-token`, and it opens only `SESSION_ACTIONS`
(closing a terminal without finishing a card, bringing the panel forward) and
`SESSION_READS`. Everything else refuses it with `DESK_TOKEN_REFUSAL`. This is
not a sandbox: a same-user process can still read the person's files, kill any
pid and read the clipboard while the desk key sits on it.
"""

from __future__ import annotations

import asyncio
import base64
import functools
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import socket
import time
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import unquote, parse_qs

from . import (access_log, agent_report, attachments, bearings, board,
               board_workflow, card_timeline, claude_usage, codex_spenders,
               command_receipts, conversation, daemon_board,
               devices, enrollment, event_log, fleet_figures, grok_billing,
               image_preview,
               lan_hosts, limits, live_activity, manual_check,
               mission, relay, scout_index, srp, terminal_stream, vtgrid,
               work_record, workspace)
from .paths import STATE_DIR, ensure_state_dir

logger = logging.getLogger("dark-army.api")

# "localhost", not "127.0.0.1": it is what the URL says, and asyncio binds every
# address it resolves to — which on macOS is ::1 *first*, then 127.0.0.1. Binding
# only the IPv4 literal while handing out a localhost URL would send the browser
# to ::1 and have the connection refused. Still loopback-only either way.
API_HOST = "localhost"
API_PORT = int(os.environ.get("BOB_COMPANION_API_PORT", "19874"))
# The **session token**'s file. Readable by every process running as this user —
# every agent included — so it opens only the verbs and reads below. The desk
# token that opens everything else lives in memory alone (`ApiServer.start`).
API_TOKEN_PATH = STATE_DIR / "api-token"
# What the on-disk session token may do on `/api/action`. `close_terminal`
# only *without* `by_person` — the flag is the third door into Done
# (`docs/context-board.md`), and an agent's finishing script never sends it.
# `reveal_panel` is untrusted aim, never a verb (`_reveal_panel_request`).
SESSION_ACTIONS = ("close_terminal", "close_refinement_terminal", "reveal_panel")
# The token-gated GETs the session token may read. `/api/terminal/stream`
# (it types) and `/api/access-log` (every paired phone's name) stay desk-only,
# as does the resizing `/api/terminal`.
SESSION_READS = ("/api/conversation", "/api/knowledge", "/api/scout-reports",
                 "/api/scout-report", "/api/plans", "/api/plan",
                 "/api/manual-checks")
# The 403 detail a session-token holder gets for a desk verb. Words for a
# person, never the token.
DESK_TOKEN_REFUSAL = ("that needs Dark Army's desk token — the one on disk only "
                      "closes terminals and reads")
# Second listener, opt-in, off by default. Bound to 0.0.0.0 so a phone on the
# same Wi-Fi can reach it; every request on it is a sealed frame under the
# device's home key (`relay.HOME`), and the AEAD is the entire boundary on
# that socket. The interface is judged **per connection**, not at the bind:
# `_handle_lan_client` reads the local address the knock landed on and turns
# a tunnel address away unread (`lan_hosts.arrived_on_tunnel`). Env-overridable
# for the second-user-on-one-machine case the other ports already handle.
LAN_API_PORT = int(os.environ.get("BOB_COMPANION_LAN_PORT", "19875"))

# How long a usage body folded into a sealed `state` answer is believed.
# The fold is what a phone check-in pays for: the state body is memory
# (2 ms) while the usage body is a ~100 KB parse plus SQLite attribution
# (135-186 ms measured, 21 Sep 2026), rebuilt on every poll for bars that
# move once a turn. Only `_state_with_usage` reads the memo; the loopback
# `/api/usage` and the sealed `usage` read stay fresh.
USAGE_MEMO_SECONDS = 20.0

# Three bounds on the phone door, each answered before the request is read.
# The typed-address pair branch runs only for a pairing the Pair window armed
# (`devices.begin(allow_plain=True)`); otherwise this sentence, and the
# access log's `pair_plain`.
PAIR_PLAIN_REFUSAL = ("type-in pairing is off — tick 'This phone can't scan' in "
                      "the Mac's pairing window, or scan the code")
# The typed-address pair is SRP-6a over the code (`srp.py`, `devices.
# pairing_srp_start` / `pairing_srp_finish`): two plain-JSON requests,
# `pake: start` and `pake: finish`, the reply sealed under the key both
# sides derived. Three refusals of its own, all access-log reason `pake`
# and none counting a fail: a body in the old plain `{code, name}` shape
# (a phone app from before the exchange — refused in words that say to
# update it, and never handed the keys), a value out of shape or an `A`
# the pairing does not hold (a finish before its start, after a new code
# was issued, or twice; a start replaying an `A` already in flight), and a
# code whose start budget is spent.
PAIR_UPDATE_REFUSAL = ("update the Dark Army phone app — this Mac no longer sends "
                       "its keys over the Wi-Fi when you type the address")
PAKE_SHAPE_REFUSAL = "that pairing exchange was out of step — try the code again"
PAKE_BUSY_REFUSAL = ("too many pairing attempts on this code — start a new one "
                     "from the Mac")
# A knock that landed on one of this Mac's VPN / tunnel addresses (`tunnel`).
# The words the access log and the docs use — the door itself **sends
# nothing**: it closes the connection unwritten. Any HTTP answer is one the
# shipped phone stops its walk on and promotes the host for (a 403 even
# un-pairs it), and a phone paired before tunnels left the candidate list
# still carries one; a transport error is "nothing there, walk on" to every
# phone generation.
LAN_TUNNEL_REFUSAL = ("Dark Army's phone door does not answer over a VPN — join "
                      "the Mac's Wi-Fi")
# Over either connection cap (`busy`, weight 0 on the access log).
LAN_BUSY_REFUSAL = "Dark Army's phone door is busy — try again in a moment"
# How many connections the door holds open at once, in all and per address.
# A keyholder phone holds a poll, a terminal stream and an upload at once —
# the per-peer headroom is deliberate. Read through the module at call time
# so a monkeypatch bites.
LAN_MAX_OPEN = 32
LAN_MAX_OPEN_PER_PEER = 6
# How long one reading of this Mac's interfaces is believed, so a knock does
# not enumerate them every time.
TUNNEL_ADDRS_TTL = 5.0
# TCP keepalive on every accepted phone-door socket, so a phone that walks
# out of range with a terminal stream open — a connection the caps count for
# its whole life, and whose sealed read has no deadline — is noticed by the
# kernel and its slot released, instead of held until the next reboot. Idle
# seconds before the first probe, seconds between probes, probes before the
# connection is declared dead: about two minutes of silence in all.
LAN_KEEPALIVE_IDLE_SECONDS = 60
LAN_KEEPALIVE_INTERVAL_SECONDS = 15
LAN_KEEPALIVE_COUNT = 4

# What the three plain routes the phone used to call answer now. An old phone
# build shows "The Mac refused (HTTP 426)."; the sentence is in the body for
# anyone reading the log or curl.
HOME_UPDATE_REFUSAL = ("update the Dark Army phone app and pair it again — this Mac "
                       "only takes sealed requests")

# The sealed `history_week` read is a **closed** projection of
# `agent_efficiency_report(7, "")`: every row is rebuilt from these tuples,
# never copied through, so a card's title, a person's name or a project root
# (a path) can never ride the relay by accident. They are exactly the fields
# the shared week fold (`LedgerWeek`, byte-pinned Mac/phone) reads for the
# token cost, the reported dollar and the not-priced marks.
HISTORY_WEEK_CARD_KEYS = ("card_id", "sessions")
HISTORY_WEEK_SESSION_KEYS = (
    "session_id", "provider", "phase", "bound_at", "known",
    "token_cost_usd", "claude_token_cost_usd", "grok_token_cost_usd",
    "codex_token_cost_usd", "token_unpriced_turns", "reported_cost_usd",
)
HISTORY_WEEK_DAY_KEYS = (
    "day", "claude_token_cost_usd", "grok_token_cost_usd",
    "codex_token_cost_usd", "claude_reported_cost_usd",
    "grok_reported_cost_usd", "claude_token_unpriced_sessions",
    "grok_token_unpriced_sessions", "codex_token_unpriced_sessions",
    "claude_token_unpriced_session_ids", "grok_token_unpriced_session_ids",
    "codex_token_unpriced_session_ids",
)
# The body's own keys. `truncated` is present only when the page cut cards.
HISTORY_WEEK_BODY_KEYS = (
    "supported", "available", "range_days", "from", "to", "generated_at",
    "partial", "codex_history_partial", "cards", "other_days", "truncated",
)

# A request line plus headers. Anything larger is not a client of ours.
MAX_HEADER_BYTES = 16 * 1024
MAX_BODY_BYTES = 256 * 1024
READ_TIMEOUT = 10.0
# The upload route alone. A 20 MB body over kitchen Wi-Fi outlives
# `READ_TIMEOUT`, and raising that globally would hand every other
# route a minute of patience it has no use for.
UPLOAD_READ_TIMEOUT = 60.0
# The delta card read's two bounds. `CARD_SYNC_MAX_BYTES` carries
# `_outcome_page_bytes`' own argument verbatim: the sealed envelope embeds
# this JSON as a string, and a 300 KB budget leaves room for doubled escapes,
# encryption/base64 overhead and envelope fields beneath the phone's wire and
# inflate limits. `CARD_SYNC_MAX_IDS` is what one poll may ask for; the rest
# stays stale for the next pass.
CARD_SYNC_MAX_BYTES = 300_000
CARD_SYNC_MAX_IDS = 50
# SSE keep-alive: without traffic, an idle connection can be dropped by the OS or
# a proxy and a reader would silently stop updating while looking connected.
HEARTBEAT_SECONDS = 20.0
# How long a Jump is held open for before the answer stops being worth waiting
# for. Long enough for the extension fan-out plus both raisers on a busy machine,
# short enough that a wedged one does not sit on a button press.
REVEAL_TIMEOUT = 12.0

# The floor between two SSE frames, and the wider floor when a frame carries
# nothing but moving clocks. See `_broadcast`.
BROADCAST_MIN_INTERVAL = 0.2
BROADCAST_QUIET_INTERVAL = 1.0
# How long after a bot grant's end the lapse frame is sent, so the grant
# has certainly read as off when the frame is built.
BOT_EXPIRY_GRACE_SECONDS = 0.5
# The floor for a frame whose only movement is a poll-echo field
# (`_POLL_ECHO_FIELDS`, all three inside `devices`): a phone checking in every
# four seconds must not buy a panel frame of its own each time. Deferred this
# long, never stripped, so the Devices rows' "last carried a message N ago"
# (`SettingsDeviceRows.awayHealthLine`) is at most this stale; real news pulls
# the booking forward as it does past the quiet floor.
BROADCAST_ECHO_INTERVAL = 15.0

# How many frames one live-update client may have waiting. A panel that
# stopped reading needs only the newest few: `_enqueue_client` drops the
# oldest on overflow and puts a *whole* frame in its place, so a slim client
# is never left holding a stale section, and the daemon's memory stays flat
# however long the reader is stuck.
SSE_CLIENT_QUEUE_MAX = 8

# Fields whose value moves on its own, without anything about the fleet having
# changed: wall clocks, elapsed times and the rates derived from them. Two frames
# that differ only in these are the same news, and the panel redraws itself for
# nothing when both are sent. Deliberately short — a field left out of this set
# costs a suppressed frame, a field wrongly *in* it costs a stale panel, so
# anything that could carry news (`five_hour_resets_at`, `trend`) stays out.
_CLOCK_FIELDS = frozenset({
    "generated_at", "idle_seconds", "duration_seconds", "duration_ms",
    "api_duration_ms", "received_at", "output_tokens_per_sec",
    "finished_at", "last_event",
    # `last_event` rounded to the second, published per row for the phone's
    # Live Activity clock (`_collect_agent_stubs`). It moves on every hook
    # event the way `last_event` does, so left out of this set two
    # consecutive same-tool events on a working row were news on this key
    # alone: an SSE frame per tool call and a phone digest that never
    # settled. A row's transition to waiting is still news through `state`,
    # and a full read still carries the value.
    "quiet_since",
    # last_event's sibling: recency of a project's last session, rounded to
    # the minute on the way in. The strip is recursive by key name — a future
    # field called last_active anywhere in the state would be silently dropped
    # from news, so do not reuse this name.
    "last_active",
    # A folder Dark Army turned away last says nothing new when only the clock on it
    # moved — and a busy unenrolled project emits hook events continuously, so
    # without this every refused message would be news.
    "last_seen",
})


# Fields that move *because this poll happened* rather than because anything
# about the fleet did. `last_frame_at` (per device, from `relay.last_frame_at`)
# is stamped by `relay.note_recv_ctr` on every accepted relay frame;
# `last_ok_at` and `failing_for` inside `relay_health` move on every successful
# relay round trip. Away, a phone quoting a digest would therefore never match
# its own previous answer and the conditional read would silently do nothing.
#
# Stripped for the **phone's digest only** and never for `_broadcast`, so the
# panel's SSE frames stay byte-identical. The phone draws none of the three —
# `PhoneDeviceRow` reads `id` + `lease_expires_at`, and `relay_health` is not in
# the phone's `Snapshot` at all — and `test_phone_unchanged_state.py` fails the
# day one of them is drawn. Unlike `_broadcast`, an unchanged answer is not
# deferred but *is* the answer, so this set must stay at these three names and
# never grow to chase a higher hit rate. `_flush` still strips none of them:
# a frame whose only movement is one of these is deferred
# `BROADCAST_ECHO_INTERVAL` instead — the echo floor — because the panel's
# Devices rows draw `last_frame_at`.
_POLL_ECHO_FIELDS = frozenset({"last_frame_at", "last_ok_at", "failing_for"})


# The top-level sections of `state()` a `?sections=changed` client may have left
# out of a frame when they read the same as the last one it was sent (clocks
# discounted). Two things in the frame are deliberately not here:
#
# * `generated_at` rides every frame — it is the frame's identity and the
#   panel's one clock authority, and a frame without it could not be aged.
# * `reconciler_available` / `reconciler_error` are top-level **scalars, not
#   sections**: a few bytes each, no "carry" story in a decoder, and
#   `reconcilerAvailable` is a tri-state where absent already means "the daemon
#   did not say". They ride every frame that is sent — and a slim client is
#   sent no frame at all when nothing but the clocks moved (`_flush`).
# One spelling of the published figures key. It is not on the slim-frame
# list below — that list is the panel's section set — so a slim frame may
# repeat the section. A full `state()` always publishes it.
_FLEET_FIGURES_KEY = "fleet_figures"
# The third opt-in, `?cards=delta`: a present board carrying
# `cards_delta: true` holds only the cards whose news moved, and
# `card_order` names every card of the variant in the daemon's order, so the
# panel rebuilds its list from the ids it holds (`BoardCardDelta.merge`).
# Never absence and never a short list alone: the marker is the key.
CARDS_DELTA_KEY = "cards_delta"
CARD_ORDER_KEY = "card_order"
# The phone's sealed `state` read partitions its delta answer on this same
# tuple (`_state_digests`, `_state_answer`), and the phone ignores a name it
# does not decode.
_OMITTABLE_SECTIONS = (
    "counts", "notifications", "agents", "signals", "mesh",
    "collaboration", "permissions",
    "board", "enrollment", "devices", "inbox", "security", "mission",
    "power", "rebuild",
)


def _news(value, drop=_CLOCK_FIELDS):
    """`value` with the clock fields dropped, recursively — what is left is the
    part a surface would actually have to redraw.

    ``drop`` defaults to `_CLOCK_FIELDS`, which is what `_broadcast` and its
    tests mean by news; the phone's conditional `state` read passes the wider
    `_CLOCK_FIELDS | _POLL_ECHO_FIELDS`."""
    if isinstance(value, dict):
        return {k: _news(v, drop) for k, v in value.items() if k not in drop}
    if isinstance(value, list):
        return [_news(v, drop) for v in value]
    return value


def _state_digests(state) -> tuple[str, dict[str, str]]:
    """The whole-picture fingerprint and one fingerprint per section.

    One `_news` walk with both the moving clocks and the poll-echo fields set
    aside; the whole digest is exactly `_state_digest`'s, and each section of
    `_OMITTABLE_SECTIONS` present in `state` gets its own SHA-256 over the
    same stripped, canonical JSON — the comparison `_flush` makes for the
    panel, taken per section so the phone can keep the ones it holds."""
    stripped = _news(state, _CLOCK_FIELDS | _POLL_ECHO_FIELDS)
    whole = hashlib.sha256(
        json.dumps(stripped, sort_keys=True).encode()).hexdigest()
    sections = {
        k: hashlib.sha256(
            json.dumps(stripped[k], sort_keys=True).encode()).hexdigest()
        for k in _OMITTABLE_SECTIONS if k in state
    }
    return whole, sections


def _state_digest(state) -> str:
    """A fingerprint of everything in `state` a phone would have to redraw.

    Both the moving clocks and the fields that move because the poll itself
    happened are set aside, so two quiet polls agree."""
    return _state_digests(state)[0]


def _valid_state_digest(value) -> str:
    """`value` when it is a plain 64-character lowercase hex digest, else `""`.

    `expected_done_token`'s shape check without its refusal: a bad token there
    is a destructive write to refuse, a bad digest here is a cache validator to
    ignore, so this never raises and never becomes a 400."""
    if not isinstance(value, str) or len(value) != 64:
        return ""
    if any(c not in "0123456789abcdef" for c in value):
        return ""
    return value


def _valid_section_quotes(value) -> dict[str, str]:
    """The per-section digests a phone quoted, kept only where they are one.

    `{}` unless `value` is a dict; a name outside `_OMITTABLE_SECTIONS` or a
    digest `_valid_state_digest` rejects is dropped. `_valid_state_digest`'s
    rule: a cache validator to ignore, so this never raises and never
    becomes a 400."""
    if not isinstance(value, dict):
        return {}
    quotes: dict[str, str] = {}
    for name in _OMITTABLE_SECTIONS:
        digest = _valid_state_digest(value.get(name))
        if digest:
            quotes[name] = digest
    return quotes


class _Frame:
    """One SSE frame's picture, built once per `_flush` and read by every
    kind of listener.

    Holds the `state()` dict, its one `_news` walk, the clock-stripped JSON
    of each top-level key, and memoised wire strings. `news` is assembled
    from the per-key strings and is byte-identical to
    `json.dumps(_news(state), sort_keys=True)` — the old whole-state dump —
    so `_last_news` keeps its meaning without a second serialisation.
    `render` assembles a frame from per-key wire strings in `state`'s own
    key order with `json.dumps`' default separators, so `render(every key)`
    is byte-identical to `json.dumps(state)`; `overrides` swaps one key's
    wire string (the review-only board, a delta board). No I/O, loop-local,
    discarded after the flush."""

    __slots__ = ("state", "stripped", "key_news", "section_news",
                 "scalar_news", "news", "_wires")

    def __init__(self, state: dict):
        self.state = state
        self.stripped = _news(state)
        self.key_news = {k: json.dumps(v, sort_keys=True)
                         for k, v in self.stripped.items()}
        self.section_news = {k: self.key_news.get(k, "null")
                             for k in _OMITTABLE_SECTIONS}
        self.scalar_news = self._assemble_sorted(
            [k for k in self.stripped if k not in _OMITTABLE_SECTIONS])
        self.news = self._assemble_sorted(list(self.stripped))
        self._wires: dict[str, str] = {}

    def _assemble_sorted(self, keys: list) -> str:
        return "{" + ", ".join(
            json.dumps(k) + ": " + self.key_news[k]
            for k in sorted(keys)) + "}"

    def devices_echo_news(self) -> str:
        """The `devices` section with the poll-echo fields set aside too —
        what the echo floor compares."""
        return json.dumps(_news(self.stripped.get("devices"),
                                _POLL_ECHO_FIELDS), sort_keys=True)

    def wire(self, key: str) -> str:
        text = self._wires.get(key)
        if text is None:
            text = json.dumps(self.state[key])
            self._wires[key] = text
        return text

    def render(self, omit=(), overrides=None) -> str:
        overrides = overrides or {}
        return "{" + ", ".join(
            json.dumps(k) + ": " + (overrides[k] if k in overrides
                                   else self.wire(k))
            for k in self.state if k not in omit) + "}"

_STATUS_TEXT = {200: "OK", 204: "No Content", 400: "Bad Request", 403: "Forbidden",
                404: "Not Found", 405: "Method Not Allowed", 409: "Conflict",
                426: "Upgrade Required", 500: "Internal Server Error",
                503: "Service Unavailable"}

def _log_broadcast(payload: str, clients: int) -> None:
    """Say what went out on the SSE stream, in the same shape the panel logs on
    the way in (`Trace.digest`).

    The pair is the point. A panel showing a stale fleet has three possible
    causes — the daemon never pushed, the push never arrived, or it arrived and
    was rejected — and until both ends said so in the same log, all three looked
    identical from the outside. Only the rows the caption path is about are
    named, so this stays one short line per push.

    At DEBUG, not INFO: this fires per frame and was 86% of a day's log
    (measured 2026-08-23: 25,763 of 29,893 lines were this line and the panel's
    matching pair). The pair remains obtainable — quit Dark Army, run
    `launchctl setenv BOB_COMPANION_LOG_LEVEL DEBUG` and
    `launchctl setenv BOB_PANEL_TRACE 1`, relaunch (the panel inherits the menu
    bar's environment); unset both and relaunch to quiet it again.

    Never raises: this is a log line on the path every surface update takes.
    """
    try:
        state = json.loads(payload)
        agents = state.get("agents") or {}
        parts = [f"clients={clients}", f"{len(payload)}B"]
        for bucket in ("running", "waiting", "sleeping", "finished", "abandoned"):
            parts.append(f"{bucket[:5]}={len(agents.get(bucket) or [])}")
        parts.append(f"cards={len(state.get('notifications') or [])}")
        says = []
        for bucket in ("waiting", "running", "sleeping"):
            for row in agents.get(bucket) or []:
                summary = row.get("last_summary") or ""
                question = (row.get("question") or {}).get("text") or ""
                if not summary and not question:
                    continue
                who = row.get("nickname") or str(row.get("session_id", ""))[:8]
                says.append(f"{who}:{bucket[:5]}/{int(row.get('idle_seconds') or 0)}s"
                            + ("/tldr" if summary else "")
                            + ("/q" if question else ""))
        if says:
            parts.append("says=[" + " ".join(says) + "]")
        logger.debug("SSE push: %s", " ".join(parts))
    except Exception:  # pragma: no cover - a log line must not break a push
        logger.debug("unloggable SSE payload", exc_info=True)


def load_or_create_token() -> str:
    """Read the API token, creating it on first use. 0600 — it is the only thing
    standing between a stray browser tab and an action endpoint."""
    ensure_state_dir()
    try:
        token = API_TOKEN_PATH.read_text(encoding="utf-8").strip()
        if token:
            return token
    except OSError:
        pass
    token = secrets.token_urlsafe(32)
    # Created 0600, not created-then-chmodded: between the write and the chmod
    # the token sat at whatever the umask allows — 0644 on a stock macOS — and
    # the whole point of the file is that only this user can read it. `O_EXCL`
    # so a token that appeared between the read above and this line is not
    # overwritten; whoever wrote it is the daemon we would have to agree with.
    try:
        fd = os.open(API_TOKEN_PATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        existing = API_TOKEN_PATH.read_text(encoding="utf-8").strip()
        if existing:
            return existing
        fd = os.open(API_TOKEN_PATH, os.O_WRONLY | os.O_TRUNC, 0o600)
    except OSError:
        logger.warning("Could not create %s", API_TOKEN_PATH)
        return token
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(token + "\n")
    return token


class _Request:
    __slots__ = ("method", "path", "query", "headers", "body", "preverified",
                 "peer")

    def __init__(self, method, path, query, headers, body, preverified=None,
                 peer=""):
        self.method = method
        self.path = path
        self.query = query
        self.headers = headers
        self.body = body
        # The address the request came from, stamped by `_handle_lan_client`
        # alone for the access log; `""` on loopback, where nothing reads it.
        self.peer = peer
        # A frame the body rule verified *before* the body was read —
        # `(device_id, key, frame)` for a sealed upload — bound to this one
        # request and consumed once by the handler. Never a module-level
        # cache: the point is that the verdict cannot outlive the connection.
        self.preverified = preverified

    def json(self) -> dict:
        try:
            data = json.loads(self.body or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}
        return data if isinstance(data, dict) else {}


class ApiServer:
    """Serves the panel and streams daemon state. Registers itself as a
    DaemonObserver, so it is a peer of the menu bar rather than a hanger-on."""

    def __init__(self, daemon, host: str = API_HOST, port: int = API_PORT):
        self._daemon = daemon
        self._host = host
        self._port = port
        self._server: Optional[asyncio.AbstractServer] = None
        # The phone listener. None unless `start_lan` succeeded. A failed bind
        # leaves this None so a collision on 19875 cannot take the loopback
        # listener down with it.
        self._lan_server: Optional[asyncio.AbstractServer] = None
        # Every open terminal stream's writer (`_serve_terminal_stream` and
        # `_serve_lan_terminal_stream`), so `stop()` can hang up on them.
        self._stream_writers: set = set()
        # The phone door's open terminal streams per device id, oldest
        # first: at most `MAX_LAN_STREAMS_PER_DEVICE`, a new one evicting
        # the oldest. A list rather than a scalar so the bound is the
        # constant's to state and not the code's to imply.
        self._lan_streams: dict[str, list] = {}
        # The one timer at the earliest running bot grant's end
        # (`_arm_bot_expiry`), and the moment it is set for.
        self._bot_expiry_handle = None
        self._bot_expiry_at = 0.0
        # Open connections on the phone door, per peer address and in all,
        # counted on the accept and released in `_handle_lan_client`'s
        # `finally`. Plain ints touched only on the loop: no lock.
        self._lan_open: dict[str, int] = {}
        self._lan_open_total: int = 0
        # `(monotonic, addrs)` — one reading of `lan_hosts.live_addrs()`
        # believed for `TUNNEL_ADDRS_TTL`, for `_arrived_on_tunnel`.
        self._tunnel_addrs_cache: Optional[tuple] = None
        self._lan_port: int = LAN_API_PORT
        # Last successful LAN request, per device id. Memory only — a restart
        # forgets, which is the honest thing (the phone will be seen again on
        # its next poll). Never a cache of the *verdict*: `_home_open` reads
        # the home key out of the ledger on every request.
        self._device_last_seen: dict[str, float] = {}
        # queue -> slim: clients that asked for `?sections=changed` get frames
        # whose unchanged sections are omitted (see `_flush`). A dict rather
        # than a set of queues so the flag rides with the queue; iteration
        # everywhere else is over keys, which is what a set gave.
        self._clients: dict[asyncio.Queue, bool] = {}
        # The subset of `self._clients` that also asked for `?done=review`:
        # frames whose board carries only the finished cards still waiting on
        # a person, the rest fetched once from `/api/board?column=done` and
        # held. A separate set rather than a second value on `_clients`
        # because that value is a bool other code assigns directly.
        self._done_review_clients: set = set()
        # `id(queue)` of every client whose queue has overflowed at least once,
        # so the DEBUG line in `_enqueue_client` is written once per client
        # rather than once per dropped frame. Ints, not queues: a set of Queue
        # objects would pin a departed client's queue. Memory only.
        self._overflow_logged: set[int] = set()
        # A replayed press returns its original answer. Bounded, TTL'd,
        # memory only, touched from the loop inside `_sealed_run` alone —
        # `command_receipts`' module docstring is where the ordering rule and
        # the restart cost are stated.
        self._receipts = command_receipts.CommandReceipts()
        # query string -> (monotonic, parsed usage body) for the state fold
        # alone, believed for `USAGE_MEMO_SECONDS`. Memory only, loop only.
        self._usage_memo: dict[str, tuple[float, dict]] = {}
        # The desk token: minted in `start()`, memory only. `session_token` is
        # the file's value. Both are emptied on a failed bind, and an empty
        # token authorises nothing on either tier.
        self.token = ""
        self.session_token = ""
        # The socket lane's listener (`relay_ws.RelaySocketConnector.nudge`),
        # called at the top of `_broadcast` on every observer callback,
        # before the SSE early return: the picture changed, whether or not
        # a panel is attached to hear it. Assigned by the daemon while the
        # socket connector runs; `None` otherwise. Under `try`/`except` at
        # the call: a listener must never break a broadcast.
        self.on_picture: Optional[Callable[[], None]] = None

        # Latest pushed state. The API never recomputes the agent snapshot on a
        # request: enrichment does blocking transcript I/O, and doing that on the
        # event loop for every poll would stall the daemon.
        self._agents: dict = {"running": [], "sleeping": [], "waiting": [],
                              "abandoned": [], "finished": []}
        self._notifications: list = []
        self._counts: dict = {"working": 0, "idle": 0, "attention": 0, "subagents": 0}
        # The Kanban board, pushed by `on_board_change`. Held like everything
        # else here so `state()` never touches SQLite — it is called once per
        # SSE frame, and the board lives in a database.
        self._board: dict = {}

        # The broadcast rate limiter. `_last_news` is the last frame *with its
        # clocks discounted*, which is what decides whether the next one is news.
        self._flush_handle: Optional[asyncio.TimerHandle] = None
        self._flush_due: float = 0.0
        self._last_sent: float = 0.0
        self._last_news: str = ""
        # Per section, the last version a slim client was sent, clocks
        # discounted — what decides whether the next frame may omit it. Empty
        # so the first frame after startup carries everything.
        self._last_section_news: dict[str, str] = {}
        # The same, for the review-only variant of the board. A **second**
        # slot and not a reuse: the two variants go stale at different
        # moments — an edit to a withheld Done card moves the full board and
        # not the review-only one — and sharing one slot would either
        # suppress a frame a client needed or send one it did not, the
        # expensive way to be wrong here.
        self._last_review_board_news: str = ""
        # The echo floor (`BROADCAST_ECHO_INTERVAL`): the top-level scalars'
        # news and the `devices` section with the poll-echo fields set aside,
        # as last sent, and when a frame last went out. Stamped on every
        # frame that is sent, so a busy fleet keeps the floor fresh.
        self._last_scalar_news: str = ""
        self._last_devices_echo_news: str = ""
        self._last_echo_sent: float = 0.0
        # The subset of slim `self._clients` that also asked for
        # `?cards=delta` — a set beside `_done_review_clients` for the same
        # reason. Per board variant, card id -> the clock-stripped card news
        # last sent to them; replaced (never updated) on every frame that
        # carries that variant's board, cleared on any delta client's attach
        # and whenever no delta client of the variant listens, so an empty
        # map means "send the whole board".
        self._card_delta_clients: set = set()
        self._last_cards_sent_full: dict[str, str] = {}
        self._last_cards_sent_review: dict[str, str] = {}

    # --- lifecycle ---

    async def start(self) -> None:
        # The desk token is never written anywhere: it reaches the panel over
        # the menu-bar app's stdin pipe alone (`BobDaemon.desk_token`). The
        # file keeps its value — never re-minted — and becomes the session
        # token, which is exactly what an older build reads after a downgrade.
        self.token = secrets.token_urlsafe(32)
        self.session_token = load_or_create_token()
        try:
            self._server = await asyncio.start_server(
                self._handle_client, self._host, self._port, reuse_address=True,
            )
        except OSError:
            # Port taken — most likely a previous daemon outliving its app. Drop
            # the token again: callers test it to decide whether the panel
            # is reachable, and a token with no listener behind it sends the menu
            # off to open a URL that refuses the connection.
            self.token = ""
            self.session_token = ""
            raise
        self._daemon.add_observer(self)
        self._daemon._schedule_agents_push()   # populate before the first request
        logger.info("the panel on http://%s:%d", self._host, self._port)

    @property
    def lan_listening(self) -> bool:
        return self._lan_server is not None

    async def start_lan(self) -> None:
        """Bind the phone listener. Idempotent; a failed bind logs and leaves
        LAN off rather than raising into the loopback path. Always broadcasts
        so the panel's Pair row follows the actual bind, not the preference."""
        if self._lan_server is None:
            port = LAN_API_PORT
            try:
                self._lan_server = await asyncio.start_server(
                    self._handle_lan_client, "0.0.0.0", port, reuse_address=True,
                )
                self._lan_port = port
                logger.info("phone access on 0.0.0.0:%d", port)
            except OSError:
                logger.warning("phone access could not bind port %s", port,
                               exc_info=True)
                self._lan_server = None
        self._broadcast()

    async def stop_lan(self) -> None:
        """Close the phone listener. Idempotent; safe to call twice.

        Burns the in-flight pairing code so a QR left on screen cannot be
        redeemed after Phone access is turned off — even if it is turned
        back on inside the two-minute window.
        """
        devices.reset_pairing()
        if self._lan_server is not None:
            self._lan_server.close()
            await self._lan_server.wait_closed()
            self._lan_server = None
            logger.info("phone access off")
        self._broadcast()

    async def stop(self) -> None:
        self._daemon.remove_observer(self)
        if self._bot_expiry_handle is not None:
            self._bot_expiry_handle.cancel()
            self._bot_expiry_handle = None
        if self._flush_handle is not None:
            self._flush_handle.cancel()
            self._flush_handle = None
        for queue in list(self._clients):
            # Unblock the writers so they exit — through the drop-oldest put,
            # because a full queue would otherwise raise here and leave the
            # rest of the writers parked on `get()` for `wait_closed` to hang on.
            self._enqueue_client(queue, None)
        self._clients.clear()
        self._done_review_clients.clear()
        self._card_delta_clients.clear()
        self._overflow_logged.clear()
        # The panes' streams hold their sockets open until told otherwise;
        # `wait_closed` below waits for every connection.
        for stream_writer in list(self._stream_writers):
            try:
                stream_writer.close()
            except Exception:
                pass
        self._stream_writers.clear()
        await self.stop_lan()
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    @property
    def url(self) -> str:
        return f"http://{self._host}:{self._port}/"

    # --- DaemonObserver ---

    def on_agents_change(self, snapshot: dict) -> None:
        self._agents = snapshot or {}
        self._broadcast()

    def on_notification_change(self, notifications: list) -> None:
        self._notifications = notifications or []
        self._broadcast()

    def on_activity_change(self, working, idle, attention, subagents) -> None:
        self._counts = {"working": working, "idle": idle,
                        "attention": attention, "subagents": subagents}
        self._broadcast()

    def on_board_change(self, board: dict) -> None:
        """The board changed — a card was written, moved, or its session came or
        went. Stored and broadcast, exactly like the notifications above.

        **This does not defeat the limiter.** Every field in the board payload is
        either static text or a stamp that moves only when something moved, so
        `_news()` sees an edit as news and an idle board as nothing at all. The
        board's own `generated_at` is already in `_CLOCK_FIELDS` and is stripped
        with the outer one, so a reconcile that changed nothing buys no frame.
        Nothing else here belongs in that set: a board timestamp wrongly in it
        would cost a stale board, which is the more expensive way to be wrong.
        """
        self._board = board or {}
        self._broadcast()

    def on_rebuild_change(self, _facts) -> None:
        """The rebuild's facts moved. `state()` reads them off the daemon, so
        nothing is stored here: the frame is the news."""
        self._broadcast()

    # --- state ---

    def state(self, *, done_review: bool = False) -> dict:
        """The whole frame.

        ``done_review`` narrows the board section alone, to the cards a
        client that opted into `?done=review` asked for: everything except
        the finished cards nobody is waiting on. Nothing else in the frame
        changes, and a caller that does not ask gets today's bytes.
        """
        board_section = (
            self._board or getattr(self._daemon, "_board_state", {}) or {})
        if done_review:
            board_section = board.review_only(board_section)
        return {
            "generated_at": time.time(),
            "counts": self._counts,
            # The live fleet's cost and tokens, composed from the same
            # published rows as `agents` — never a second cache, so the
            # two cannot disagree. The phone reads it verbatim.
            _FLEET_FIGURES_KEY: fleet_figures.compose(
                self._agents, getattr(self._daemon, "_burn_meter", None),
                time.time()),
            "notifications": self._notifications,
            "agents": self._agents,
            # Fleet-level signals only — the per-agent ones ride inside each entry.
            # Account-wide facts like "5h budget at 84%" belong here rather than
            # repeated down every row, where they read as noise.
            "signals": getattr(self._daemon, "_global_signals", []),
            # Who has messaged whom, resolved against the fleet's own addresses.
            # Fleet-level for the same reason the signals above are: an edge
            # belongs to neither of the two rows it joins.
            "mesh": getattr(self._daemon, "_mesh", []),
            "collaboration": getattr(self._daemon, "_collaboration",
                                     {"version": 1, "available": False}),
            # Tool-approval prompts relayed out of a session by Dark Army's channel.
            # Not folded into `notifications`: a card is dismissible and these
            # are not — a prompt dismissed off the panel would leave the session
            # blocked with nothing on screen saying why.
            "permissions": self._daemon._permission_snapshot()
            if hasattr(self._daemon, "_permission_snapshot") else [],
            # The Kanban board. The daemon's held copy is the fallback so the
            # very first frame — before any `on_board_change` has fired — still
            # carries the cards rather than an empty board that fills in a
            # second later.
            "board": board_section,
            # Which projects Dark Army is allowed to watch, and what it has turned
            # away. `available` is stated by the daemon rather than inferred
            # from an empty `enrolled` list: an older daemon sends no section
            # at all, and an empty list decoding as "nothing is enrolled" would
            # draw the enrolment prompt over a working fleet.
            "enrollment": self._daemon.enrollment_snapshot()
            if hasattr(self._daemon, "enrollment_snapshot") else {},
            # Paired phones, and whether the LAN door is actually listening.
            # `available` is stated by the daemon rather than inferred from an
            # empty list — enrollment's shape, same reason.
            "devices": self._devices_section()
            if hasattr(self._daemon, "devices_snapshot") else {},
            # Needs you acknowledgements. `available` is stated, never
            # inferred: an older daemon sends no section at all.
            "inbox": self._daemon.inbox_snapshot()
            if hasattr(self._daemon, "inbox_snapshot") else {},
            # Open burst alerts off the phone doors' access log. `available`
            # is stated, never inferred: an older daemon sends no section.
            "security": self._daemon.security_snapshot()
            if hasattr(self._daemon, "security_snapshot") else {},
            # Mission Control: whether the standing chief-of-staff session
            # is alive, which session it is and where it lives. `available`
            # is stated, never inferred; no handle rides here.
            "mission": self._daemon.mission_snapshot()
            if hasattr(self._daemon, "mission_snapshot") else {},
            # The host Mac's power source for the phone's Fleet tab.
            # `available` is stated; no clock and no secret ride here.
            "power": self._daemon.power_snapshot()
            if hasattr(self._daemon, "power_snapshot") else {},
            # Rebuild & restart for the phone's Menu tab: whether this Mac can
            # rebuild, the button's label, whether one is running and how the
            # last one ended. No path or key rides here; the error is a
            # home-redacted tail.
            "rebuild": self._daemon.rebuild_snapshot()
            if hasattr(self._daemon, "rebuild_snapshot") else {},
            "reconciler_available": getattr(
                self._daemon._agents_poller, "available", None
            ),
            # Why the reconciler is down, if it is. Surfaced rather than logged at
            # debug: "background agents just never show up" is not a diagnosis.
            "reconciler_error": getattr(self._daemon._agents_poller, "last_error", ""),
        }

    def _broadcast(self) -> None:
        """Ask for a frame. Whether one goes out now, shortly, or not at all is
        decided by `_flush`.

        Rate-limited rather than immediate, and that is a fix rather than a
        tidy-up. Four observer callbacks land on this method and a single hook
        event trips two of them, so the stream carried pairs of full ~19 KB
        snapshots 8 ms apart — measured: 61 frames in 32 s, median gap 8 ms, and
        **67 % of them identical once the clocks are discounted**. The panel
        decodes and re-lays-out the whole list for each one, which is where its
        idle CPU went, and a list that relays out under the pointer several
        times a second loses clicks: a mouse-down and the mouse-up 100 ms later
        have to hit-test to the same view, and the rows in `Needs you` — the
        section with the most conditional furniture, hence the most geometry to
        redo — were the ones that stopped opening.

        Trailing-edge: the request is never dropped on the floor, only deferred,
        so the newest state always arrives — at most `BROADCAST_MIN_INTERVAL`
        late."""
        listener = self.on_picture
        if listener is not None:
            try:
                listener()
            except Exception:  # noqa: BLE001 - a listener never breaks a broadcast
                logger.debug("on_picture listener failed", exc_info=True)
        if not self._clients:
            return
        now = time.monotonic()
        soonest = max(now, self._last_sent + BROADCAST_MIN_INTERVAL)
        if self._flush_handle is not None:
            # A frame is already booked — but it may be booked for the far side
            # of the *quiet* floor, because the last thing to arrive was nothing
            # but moving clocks. News must not wait behind that, so the booking
            # is pulled forward rather than deferred to. Without this the fix
            # would be worse than the fault it corrects: a busy fleet's clock
            # ticks would hold every real change for a second.
            if self._flush_due <= soonest:
                return
            self._flush_handle.cancel()
            self._flush_handle = None
        if soonest <= now:
            self._flush()
        else:
            self._book_flush(soonest - now)

    def _book_flush(self, delay: float) -> None:
        """Schedule one flush. Outside a running loop — unit tests build servers
        freely — there is nothing to schedule on and nobody connected to hear it."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._flush_due = time.monotonic() + delay
        self._flush_handle = loop.call_later(delay, self._flush)

    def _flush(self) -> None:
        self._flush_handle = None
        if not self._clients:
            return
        # One picture per frame: `state()` once, one `_news` walk, one
        # clock-stripped string per top-level key, and every payload variant
        # assembled from those strings on demand (`_Frame`). The review-only
        # board is derived from this same picture, never a second `state()`,
        # so both variants of one frame share its `generated_at`.
        frame = _Frame(self.state())
        now = time.monotonic()
        # Nothing moved but the clocks, and the panel had that a moment ago.
        # Deferred to the quiet floor rather than sent at once — which turns
        # a busy fleet's 4 frames a second into one. Past the floor it goes
        # to full clients alone (below).
        clock_only = frame.news == self._last_news
        if clock_only and now - self._last_sent < BROADCAST_QUIET_INTERVAL:
            self._book_flush(self._last_sent + BROADCAST_QUIET_INTERVAL - now)
            return
        # Nothing moved but a phone's check-in stamp (`_POLL_ECHO_FIELDS`,
        # all inside `devices`): deferred to the echo floor, never stripped,
        # so a phone polling every four seconds does not buy a panel frame
        # each time and the Devices rows are at most that stale. The same
        # trailing-edge shape as the quiet floor; `_broadcast`'s pull-forward
        # brings real news in ahead of it.
        echo_news = frame.devices_echo_news()
        if (not clock_only and self._echo_only(frame, echo_news)
                and now - self._last_echo_sent < BROADCAST_ECHO_INTERVAL):
            self._book_flush(self._last_echo_sent + BROADCAST_ECHO_INTERVAL - now)
            return
        self._last_news = frame.news
        self._last_sent = now
        clients = list(self._clients.items())
        if clock_only:
            # Past the quiet floor a clock-only frame goes to **full** clients
            # alone — today's bytes, for an older panel or a curl reader. A
            # `?sections=changed` client is sent nothing: every section would
            # be omitted, and no clock on its screen depends on `generated_at`
            # advancing (every duration is aged from an absolute stamp), so
            # the old ~89-byte frame of the clock and the two reconciler
            # scalars said nothing it could use. With no full client attached
            # nothing is rendered at all.
            clients = [(queue, slim) for queue, slim in clients if not slim]
            if not clients:
                return
        self._last_scalar_news = frame.scalar_news
        self._last_echo_sent = now
        self._last_devices_echo_news = echo_news

        # A client that opted in with `?sections=changed` is not sent a section
        # that is byte-identical, clocks aside, to the last one that went out.
        # Key *absence* is the omission marker — `available: false` and
        # `cards: []` are real states that only ever appear inside a present
        # section — and the attach frame in `_serve_events` is always a full
        # `state()`, so a slim client holds every section from its first frame
        # onward. A default client's frames are unchanged byte for byte.
        # `_last_section_news` is updated on every frame that goes out.
        omitted = {k for k in _OMITTABLE_SECTIONS
                   if self._last_section_news.get(k) == frame.section_news[k]}
        self._last_section_news.update(frame.section_news)

        # The review-only variant, derived from this picture for the clients
        # that asked for it. Its own news slot, for the reason stated where
        # that slot is declared; every other section's omission decision is
        # shared.
        review_omitted = omitted
        review_board: Optional[dict] = None
        if self._done_review_clients:
            review_board = board.review_only(frame.state["board"])
            stripped_board = frame.stripped.get("board")
            review_board_news = json.dumps(
                board.review_only(stripped_board)
                if isinstance(stripped_board, dict) else _news(review_board),
                sort_keys=True)
            review_omitted = omitted - {"board"}
            if review_board_news == self._last_review_board_news:
                review_omitted = review_omitted | {"board"}
            self._last_review_board_news = review_board_news

        # The per-card delta, the third opt-in (`?cards=delta`, slim clients
        # only). A per-row `agents` delta is deliberately not done: a row's
        # `idle_seconds` / `duration_seconds` are relative to the frame and
        # aged on both clients from one per-section stamp
        # (`Snapshot.agentsStamp`, `AgentFacts.aged`), so a frame carrying
        # some rows and not others would freeze the rows it skipped, and rows
        # move between five buckets. Its prerequisite is every row's timing
        # made absolute and aged from its own stamp on both clients.
        plain_delta_listens = review_delta_listens = False
        for queue, slim in self._clients.items():
            if slim and queue in self._card_delta_clients:
                if queue in self._done_review_clients:
                    review_delta_listens = True
                else:
                    plain_delta_listens = True
        plain_delta_wire = review_delta_wire = None
        # A clock-only frame reaches full clients alone: whole boards, and
        # the per-card maps are left as they were.
        if not clock_only:
            card_news = None
            if (plain_delta_listens and "board" not in omitted) or (
                    review_delta_listens and "board" not in review_omitted):
                card_news = self._card_news(frame.stripped.get("board"))
            if not plain_delta_listens:
                self._last_cards_sent_full = {}
            elif "board" not in omitted:
                plain_delta_wire = self._card_delta_wire(
                    frame.state["board"], card_news, "_last_cards_sent_full")
            if not review_delta_listens:
                self._last_cards_sent_review = {}
            elif "board" not in review_omitted:
                review_delta_wire = self._card_delta_wire(
                    review_board, card_news, "_last_cards_sent_review")

        # Every payload is rendered lazily, at most once per flush, and only
        # when a client of that kind is attached or overflows.
        memo: dict[str, str] = {}

        def lazy(name: str, build: Callable[[], str]) -> Callable[[], str]:
            def get() -> str:
                text = memo.get(name)
                if text is None:
                    text = memo[name] = build()
                return text
            return get

        review_board_wire = lazy("review_board",
                                 lambda: json.dumps(review_board))
        payload = lazy("payload", lambda: frame.render())
        slim_payload = lazy(
            "slim", lambda: frame.render(omit=omitted)) if omitted else payload
        review_payload = lazy("review", lambda: frame.render(
            overrides={"board": review_board_wire()}))
        review_slim_payload = lazy("review_slim", lambda: frame.render(
            omit=review_omitted, overrides={"board": review_board_wire()}))
        delta_payload = lazy("delta", lambda: frame.render(
            omit=omitted, overrides={"board": plain_delta_wire})) \
            if plain_delta_wire is not None else slim_payload
        review_delta_payload = lazy("review_delta", lambda: frame.render(
            omit=review_omitted, overrides={"board": review_delta_wire})) \
            if review_delta_wire is not None else review_slim_payload

        if logger.isEnabledFor(logging.DEBUG):
            _log_broadcast(payload(), len(self._clients))
        for queue, slim in clients:
            review = queue in self._done_review_clients
            # The overflow replacement is always the whole variant: a present
            # board without the delta marker replaces a delta client's board
            # wholesale, exactly as a whole frame refills a slim client.
            whole = review_payload if review else payload
            if not slim:
                text = whole()
            elif queue in self._card_delta_clients:
                text = (review_delta_payload if review else delta_payload)()
            else:
                text = (review_slim_payload if review else slim_payload)()
            self._enqueue_client(queue, text, whole)

    def _echo_only(self, frame: "_Frame", echo_news: str) -> bool:
        """Whether this frame differs from the last one sent in poll-echo
        fields alone: the scalars and every section but `devices` unchanged,
        and `devices` unchanged once `_POLL_ECHO_FIELDS` are set aside."""
        if frame.scalar_news != self._last_scalar_news:
            return False
        for k in _OMITTABLE_SECTIONS:
            if k != "devices" and \
                    frame.section_news[k] != self._last_section_news.get(k):
                return False
        return echo_news == self._last_devices_echo_news

    @staticmethod
    def _card_news(stripped_board) -> Optional[dict]:
        """Card id -> the clock-stripped news of that card, off the frame's
        one `_news` walk; `None` when any card lacks a string id or an id
        repeats, which sends every delta client the whole board."""
        if not isinstance(stripped_board, dict):
            return None
        cards = stripped_board.get("cards")
        if not isinstance(cards, list):
            return None
        news: dict[str, str] = {}
        for card in cards:
            card_id = card.get("id") if isinstance(card, dict) else None
            if not isinstance(card_id, str) or not card_id or card_id in news:
                return None
            news[card_id] = json.dumps(card, sort_keys=True)
        return news

    def _card_delta_wire(self, variant_board, card_news: Optional[dict],
                         slot: str) -> Optional[str]:
        """The board a `?cards=delta` client of one variant is sent, as a
        wire string — or `None`, meaning the whole variant board rides.

        Whole when that variant's map is empty (after an attach, or the first
        frame since a delta client of this variant listened) or when the
        cards cannot be keyed. Otherwise `cards` holds only the cards whose
        news moved, `card_order` every id of the variant in the daemon's
        order, and `cards_delta: true` is the marker. The map is **replaced**
        with this frame's cards either way, so a removed card falls out."""
        held: dict = getattr(self, slot)
        cards = variant_board.get("cards") if isinstance(variant_board, dict) \
            else None
        if card_news is None or not isinstance(cards, list):
            setattr(self, slot, {})
            return None
        order = [c["id"] for c in cards]
        current = {card_id: card_news[card_id] for card_id in order}
        setattr(self, slot, current)
        if not held:
            return None
        delta = dict(variant_board,
                     cards=[c for c in cards
                            if current[c["id"]] != held.get(c["id"])])
        delta[CARD_ORDER_KEY] = order
        delta[CARDS_DELTA_KEY] = True
        return json.dumps(delta)

    def _enqueue_client(self, queue: asyncio.Queue, payload,
                        full_payload: Optional[Callable[[], str]] = None) -> None:
        """Put one frame (or the `None` shutdown sentinel) on a client's queue,
        dropping the oldest waiting frame when the queue is full.

        A full client loses nothing by losing its backlog — every frame it
        gets is the whole state. A slim client's frames omit sections against
        `_last_section_news`, the last frame flushed server-wide (and a delta
        client's board carries only the cards that moved), so the one
        dropped frame may be the only one carrying a change; on overflow the
        frame put in its place is therefore `full_payload()`, the whole state
        (or the whole review variant) — rendered only then — and the tail is
        always the last put, so a reader that catches up is whole and every
        later slim frame is safe. Never `await queue.put`: that would stall
        every client's flush on the daemon loop, not just the stuck one. The
        first overflow per client is one DEBUG line naming the queue's id —
        never the write token and never the payload, which is the state
        itself."""
        try:
            queue.put_nowait(payload)
            return
        except asyncio.QueueFull:
            pass
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            pass
        queue.put_nowait(payload if full_payload is None else full_payload())
        if payload is not None and id(queue) not in self._overflow_logged:
            self._overflow_logged.add(id(queue))
            logger.debug(
                "SSE client %d stopped reading; dropping its oldest frames "
                "(queue capped at %d)", id(queue), queue.maxsize)

    # --- HTTP ---

    async def _handle_client(self, reader: asyncio.StreamReader,
                             writer: asyncio.StreamWriter) -> None:
        try:
            request = await self._read_request(reader)
            if request is None:
                return
            if not self._loopback_host(request):
                # Before the routing table, so it covers the SSE stream and the
                # two report endpoints as well as everything `_route` serves.
                await self._respond(writer, 403, "application/json",
                                    b'{"error":"non-loopback host"}')
                return
            if request.path == "/api/events" and request.method == "GET":
                await self._serve_events(writer, request)
                return
            if request.path == "/api/history" and request.method == "GET":
                status, ctype, body = await self._history_report(request)
                await self._respond(writer, status, ctype, body)
                return
            if request.path == "/api/usage" and request.method == "GET":
                status, ctype, body = await self._usage_report(request)
                await self._respond(writer, status, ctype, body)
                return
            if request.path == "/api/catch-up" and request.method == "GET":
                status, ctype, body = await self._catch_up_report(request.query)
                await self._respond(writer, status, ctype, body)
                return
            if request.path == "/api/bearings" and request.method == "GET":
                status, ctype, body = self._bearings_report_for(request.query)
                await self._respond(writer, status, ctype, body)
                return
            if request.path == "/api/bearings/text" and request.method == "GET":
                status, ctype, body = self._bearings_report_for(request.query)
                if status == 200:
                    payload = json.loads(body)
                    body = str(payload.get("text") or "").encode()
                    ctype = "text/plain; charset=utf-8"
                await self._respond(writer, status, ctype, body)
                return
            if request.path == "/api/log" and request.method == "GET":
                status, ctype, body = await self._log_report(request)
                await self._respond(writer, status, ctype, body)
                return
            if request.path == "/api/terminal/stream" and request.method == "GET":
                # The panel's pane: one connection, keys up and bytes down,
                # held open after the head (`terminal_stream`). Token-gated
                # like the read below, and for the same reason plus one: it
                # types.
                if not self._authorised(request):
                    await self._respond(writer, 403, "application/json",
                                        b'{"error":"forbidden"}')
                    return
                await self._serve_terminal_stream(request, reader, writer)
                return
            if request.path == "/api/terminal" and request.method == "GET":
                # Token-gated although a read: this one also *resizes* the
                # pty when the panel says its size, and the panel is the one
                # owner of that size.
                if not self._authorised(request):
                    await self._respond(writer, 403, "application/json",
                                        b'{"error":"forbidden"}')
                    return
                status, ctype, body = self._terminal_report_for(
                    request.query, resize=True)
                await self._respond(writer, status, ctype, body)
                return
            if request.path == "/api/conversation" and request.method == "GET":
                # Token-gated although a read: a person's whole conversation,
                # like `/api/knowledge`. Either token (`_session_authorised`); empty
                # Origin is allowed on GET. Host already ran above.
                if not self._session_authorised(request):
                    await self._respond(writer, 403, "application/json",
                                        b'{"error":"forbidden"}')
                    return
                status, ctype, body = await self._conversation_report_for(
                    request.query)
                await self._respond(writer, status, ctype, body)
                return
            if request.path == "/api/work-record" and request.method == "GET":
                status, ctype, body = await self._work_record_report_for(
                    request.query)
                await self._respond(writer, status, ctype, body)
                return
            if request.path == "/api/outcomes" and request.method == "GET":
                status, ctype, body = await self._outcome_report_for(request.query)
                await self._respond(writer, status, ctype, body)
                return
            if request.path == "/api/knowledge" and request.method == "GET":
                # Token-gated although a read: standing policy agents treat
                # as source of truth, never on `/api/state`, and another
                # project's notes. Either token (`_session_authorised`); empty Origin
                # is allowed on GET. Host already ran above.
                if not self._session_authorised(request):
                    await self._respond(writer, 403, "application/json",
                                        b'{"error":"forbidden"}')
                    return
                status, ctype, body = await self._knowledge_report_for(
                    request.query)
                await self._respond(writer, status, ctype, body)
                return
            if request.path == "/api/history-week" and request.method == "GET":
                # The sealed `history_week` read's loopback twin, token-gated
                # exactly as `/api/knowledge`: `_authorised`'s token half;
                # empty Origin is allowed on GET. Host already ran above.
                if not self._authorised(request):
                    await self._respond(writer, 403, "application/json",
                                        b'{"error":"forbidden"}')
                    return
                status, ctype, body = await self._history_week_for({})
                await self._respond(writer, status, ctype, body)
                return
            if request.path == "/api/scout-reports" and request.method == "GET":
                # Token-gated although a read, exactly as `/api/knowledge`:
                # every watched project's report titles and verdicts, or
                # with `?q=` the reports whose body holds the term (a text
                # search, `scout_index.search`). Either token
                # (`_session_authorised`); empty Origin is allowed on GET.
                # Host already ran above.
                if not self._session_authorised(request):
                    await self._respond(writer, 403, "application/json",
                                        b'{"error":"forbidden"}')
                    return
                status, ctype, body = await self._scout_reports_for(
                    request.query)
                await self._respond(writer, status, ctype, body)
                return
            if request.path == "/api/scout-report" and request.method == "GET":
                # One report's text — token-gated like `/api/knowledge`, and
                # only for a path in `scout_index.locate`'s closed set.
                if not self._session_authorised(request):
                    await self._respond(writer, 403, "application/json",
                                        b'{"error":"forbidden"}')
                    return
                status, ctype, body = await self._scout_report_for(
                    request.query)
                await self._respond(writer, status, ctype, body)
                return
            if request.path == "/api/plans" and request.method == "GET":
                # Token-gated although a read, exactly as
                # `/api/scout-reports`: every watched project's plan titles.
                # Either token (`_session_authorised`); empty Origin is allowed on
                # GET. Host already ran above.
                if not self._session_authorised(request):
                    await self._respond(writer, 403, "application/json",
                                        b'{"error":"forbidden"}')
                    return
                status, ctype, body = await self._plans_for(request.query)
                await self._respond(writer, status, ctype, body)
                return
            if request.path == "/api/plan" and request.method == "GET":
                # One plan's text — token-gated like `/api/scout-report`, and
                # only for a path in `plan_index.locate`'s closed set.
                if not self._session_authorised(request):
                    await self._respond(writer, 403, "application/json",
                                        b'{"error":"forbidden"}')
                    return
                status, ctype, body = await self._plan_for(request.query)
                await self._respond(writer, status, ctype, body)
                return
            if request.path == "/api/manual-checks" and request.method == "GET":
                # Token-gated although a read, exactly as `/api/knowledge`:
                # every watched project's leftover checks and their steps.
                # `?root=&q=&status=` is the list, `?path=` one file's text.
                # Either token (`_session_authorised`); empty Origin is allowed on
                # GET. Host already ran above.
                if not self._session_authorised(request):
                    await self._respond(writer, 403, "application/json",
                                        b'{"error":"forbidden"}')
                    return
                status, ctype, body = await self._manual_checks_for(
                    request.query)
                await self._respond(writer, status, ctype, body)
                return
            if request.path == "/api/access-log" and request.method == "GET":
                # Token-gated although a read, exactly as `/api/knowledge`:
                # every address that knocked and every paired phone's name.
                # Desk token only (`_authorised`); empty Origin is allowed on
                # GET. Host already ran above.
                if not self._authorised(request):
                    await self._respond(writer, 403, "application/json",
                                        b'{"error":"forbidden"}')
                    return
                status, ctype, body = self._access_log_report_for(request.query)
                await self._respond(writer, status, ctype, body)
                return
            if request.path == "/api/lifecycle" and request.method == "GET":
                status, ctype, body = await self._lifecycle_report_for(request.query)
                await self._respond(writer, status, ctype, body)
                return
            if request.path == "/api/board" and request.method == "GET":
                status, ctype, body = await self._board_report(request)
                await self._respond(writer, status, ctype, body)
                return
            # The four board writes. Awaited rather than answered inside
            # `_action`, for two reasons that both apply. The board is SQLite and
            # every statement owes the executor a hop — a write on the event loop
            # is a stall in the thing driving every surface. And `board_dispatch`
            # opens a window and spends money, so "it did not land" is news the
            # presser must have *before* pressing again; that is wrap-up's
            # argument, one notch sharper.
            board_call = self._board_request(request)
            if board_call is not None:
                status, ctype, body = await self._board_action(*board_call)
                await self._respond(writer, status, ctype, body)
                return
            # Reveal is the one action that has to be awaited (it reports whether
            # the window actually came forward), and it is handled here rather
            # than inside `_action` so that `_route` stays synchronous — every
            # other action either answers instantly or is genuinely fire-and-forget.
            reveal = self._reveal_request(request)
            if reveal is not None:
                status, ctype, body = await self._reveal(reveal)
                await self._respond(writer, status, ctype, body)
                return
            # The reverse jump: a VS Code window asking Dark Army to come forward on
            # whatever session its terminal holds. Awaited for `_reveal`'s own
            # reason — the press happens in the editor, and "Dark Army is not
            # reachable" has to be sayable there rather than inferred from a
            # panel that never appeared.
            reveal_panel = self._reveal_panel_request(request)
            if reveal_panel is not None:
                status, ctype, body = await self._reveal_panel(*reveal_panel)
                await self._respond(writer, status, ctype, body)
                return
            # A permission verdict is awaited for a sharper reason than reveal:
            # the same prompt is live in the session's own terminal and either
            # answer wins, so "too late" is a real outcome the presser has to
            # see rather than a row that quietly stops changing.
            verdict = self._permission_request(request)
            if verdict is not None:
                status, ctype, body = await self._permission(*verdict)
                await self._respond(writer, status, ctype, body)
                return
            # A reply is awaited for the same reason: the common refusal is
            # "that session has no channel", and it is invisible on the row.
            reply = self._reply_request(request)
            if reply is not None:
                status, ctype, body = await self._reply(*reply)
                await self._respond(writer, status, ctype, body)
                return
            # Wrap up is awaited for the same reason again, and one more: it
            # closes somebody's terminal where it can (typing `/clear` only
            # on a non-prompt refusal — the daemon's close-or-clear verb), so "it
            # did not land" is news the presser must get before they press
            # again. Only stale `close-out.sh` copies still send it.
            wrap = self._wrap_up_request(request)
            if wrap is not None:
                status, ctype, body = await self._wrap_up(wrap)
                await self._respond(writer, status, ctype, body)
                return
            # Close-terminal is awaited for wrap-up's reason: disposing a tab
            # is the act, and "it did not land" is news the presser must get
            # before they press again. A separate action from wrap_up — typing
            # `/clear` and disposing a tab are two verbs with two reaches.
            refinement = self._refinement_close_request(request)
            if refinement is not None:
                ok, detail = await self._daemon.close_refinement_terminal(refinement)
                await self._respond(writer, 200 if ok else 409, "application/json",
                                    json.dumps({"ok": ok, "detail": detail}).encode())
                return
            close = self._close_terminal_request(request)
            if close is not None:
                status, ctype, body = await self._close_terminal(*close)
                await self._respond(writer, status, ctype, body)
                return
            # Low priority is awaited for the same reason: the refusal is
            # news the presser needs before pressing again, and the command
            # is a toggle, so a blind second press is the one thing to avoid.
            low = self._low_priority_request(request)
            if low is not None:
                status, ctype, body = await self._low_priority(low)
                await self._respond(writer, status, ctype, body)
                return
            # Acknowledge is awaited for 409-is-news: a stale fingerprint
            # writes nothing, and the presser must hear that before pressing
            # again. Never `_action()` — dismiss is fire-and-forget.
            ack = self._inbox_ack_request(request)
            if ack is not None:
                status, ctype, body = await self._inbox_ack(*ack)
                await self._respond(writer, status, ctype, body)
                return
            # And the access alert's own acknowledgement, on the same
            # argument: a refusal is news the presser must hear.
            alert_ack = self._access_alert_ack_request(request)
            if alert_ack is not None:
                status, ctype, body = await self._access_alert_ack(alert_ack)
                await self._respond(writer, status, ctype, body)
                return
            typed = self._terminal_input_request(request)
            if typed is not None:
                status, ctype, body = await self._terminal_input(
                    typed[0], typed[1], from_phone=False,
                    raw=typed[2], data=typed[3])
                await self._respond(writer, status, ctype, body)
                return
            # Prepare is awaited for wrap-up's reason: the refusal is invisible
            # on the surface, so the presser must have it before pressing
            # again — here plus a result that only the caller can use.
            # Enrolment is awaited for wrap-up's reason: it writes a file into
            # somebody's project, so "it did not land" is news the presser must
            # have before pressing again.
            enrol = self._enrollment_request(request)
            if enrol is not None:
                status, ctype, body = await self._enrollment(*enrol)
                await self._respond(writer, status, ctype, body)
                return
            # Pairing is begun and revoked from the Mac, never from the
            # network side — this intercept is on the loopback listener only.
            device_call = self._devices_request(request)
            if device_call is not None:
                status, ctype, body = await self._devices(*device_call)
                await self._respond(writer, status, ctype, body)
                return
            prepare = self._prepare_request(request)
            if prepare is not None:
                status, ctype, body = await self._prepare(prepare)
                await self._respond(writer, status, ctype, body)
                return
            # A terminal with no card behind it is awaited for
            # `board_dispatch`'s own reason: it opens a terminal and spends
            # money, so "it did not land" is news the presser must have
            # before pressing again.
            spawn = self._spawn_terminal_request(request)
            if spawn is not None:
                status, ctype, body = await self._spawn_terminal(*spawn)
                await self._respond(writer, status, ctype, body)
                return
            # Mission Control's two verbs are awaited for the same reason:
            # open starts a session and spends money, End closes one.
            mission_action = self._mission_request(request)
            if mission_action is not None:
                status, ctype, body = await self._mission(mission_action)
                await self._respond(writer, status, ctype, body)
                return
            # Rebuild & restart is awaited for the same reason as the Mission
            # Control verbs: "nobody is listening" is news the presser gets.
            app_action = self._app_request(request)
            if app_action is not None:
                status, ctype, body = await self._app_action(app_action)
                await self._respond(writer, status, ctype, body)
                return
            # A question answer is awaited for wrap-up's reason: it types into
            # somebody's session, so "it did not land" is news the presser must
            # get before pressing again.
            answer = self._answer_question_request(request)
            if answer is not None:
                status, ctype, body = await self._answer_question(*answer)
                await self._respond(writer, status, ctype, body)
                return
            answers = self._answer_questions_request(request)
            if answers is not None:
                status, ctype, body = await self._answer_questions(*answers)
                await self._respond(writer, status, ctype, body)
                return
            status, ctype, body = self._route(request)
            await self._respond(writer, status, ctype, body)
        except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError):
            pass
        except Exception:
            logger.exception("API request failed")
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except (ConnectionResetError, BrokenPipeError, OSError):
                pass

    async def _read_request(self, reader, body_rule=None, *,
                            on_oversize: Optional[Callable[[str, str], None]] = None
                            ) -> Optional[_Request]:
        """One request off the wire.

        ``on_oversize`` is called with the parsed ``path`` — the door is not
        known before the request line is — and the **device id** the body
        rule resolved the header frame to (``""`` for a stranger), on the
        one branch that refuses a body over the rule's cap, so the phone
        door can record the refusal and weigh a keyholder's too-large upload
        at nothing; ``None`` (the loopback caller) keeps this byte-for-byte
        what it was.

        ``body_rule`` is an optional ``(method, path, headers) -> (cap,
        timeout)`` — or ``(cap, timeout, preverified)``, the third riding
        onto the request for the handler — applied *after* the request line
        and headers are parsed —
        the upload route needs a larger body and a longer read, and deciding
        that before the path is known would hand the same slack to every other
        route. It is given the headers because the widened cap must not be
        available to a sender who has not proved anything: the body is read
        before any handler runs, so the rule is the only place that can refuse
        a stranger the 20 MB. ``None`` keeps ``MAX_BODY_BYTES`` /
        ``READ_TIMEOUT`` for both callers, so the loopback handler is
        byte-for-byte what it was.
        """
        try:
            head = await asyncio.wait_for(
                reader.readuntil(b"\r\n\r\n"), timeout=READ_TIMEOUT
            )
        except (asyncio.TimeoutError, asyncio.IncompleteReadError):
            return None
        except asyncio.LimitOverrunError:
            return None
        if len(head) > MAX_HEADER_BYTES:
            return None

        lines = head.decode("latin-1").split("\r\n")
        parts = lines[0].split(" ")
        if len(parts) < 2:
            return None
        method, target = parts[0], parts[1]
        path, _, query = target.partition("?")

        headers = {}
        for line in lines[1:]:
            name, sep, value = line.partition(":")
            if sep:
                headers[name.strip().lower()] = value.strip()

        cap, body_timeout, preverified = MAX_BODY_BYTES, READ_TIMEOUT, None
        if body_rule is not None:
            rule = body_rule(method, path, headers)
            cap, body_timeout = rule[0], rule[1]
            preverified = rule[2] if len(rule) > 2 else None

        body = b""
        try:
            length = int(headers.get("content-length", "0"))
        except ValueError:
            length = 0
        if 0 < length <= cap:
            body = await asyncio.wait_for(reader.readexactly(length),
                                          timeout=body_timeout)
        elif length > cap:
            if on_oversize is not None:
                # `preverified` is the body rule's `(device_id, key, frame)`
                # where the upload header opened; only the id travels.
                on_oversize(path, str(preverified[0]) if preverified else "")
            return None

        return _Request(method, path, query, headers, body, preverified)

    #: Every name that legitimately reaches this listener. A request addressed
    #: to anything else is a browser that was sent here by a rebound DNS name.
    LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]", ""})

    @classmethod
    def _loopback_host(cls, request: _Request) -> bool:
        """Whether the request was addressed to this machine by a loopback name.

        The anti-rebinding check, and it applies to *reads* as well as writes —
        reads are the only thing rebinding buys an attacker, since writes still
        fail the token. A native client (the panel, `curl`) sends `Host:
        localhost:19874` or `127.0.0.1:19874` and passes without noticing.

        An absent Host is allowed: HTTP/1.0 clients and hand-rolled sockets omit
        it, a browser never does, and refusing them would break `curl --http1.0`
        for no security gain.
        """
        host = request.headers.get("host", "").strip().lower()
        if host.startswith("["):
            # An IPv6 literal keeps its brackets and its own colons; only a
            # colon *after* the closing bracket is the port separator.
            name = host[:host.index("]") + 1] if "]" in host else host
        else:
            name = host.split(":", 1)[0]
        return name in cls.LOOPBACK_HOSTS

    def _authorised(self, request: _Request) -> bool:
        """Writes need the token *and* an origin that isn't someone else's page.

        The token here is the **desk token** alone — memory only, handed to the
        panel over its stdin pipe. The file's session token never passes this
        gate; `_session_authorised` is the narrower one that accepts it.
        """
        # compare_digest, not `!=`: this is the gate, and `!=` on a str returns
        # as soon as two bytes differ. Guarded on an empty token because a
        # daemon that failed to bind drops it to "" (see `start`), and an empty
        # secret that compares equal to an absent header would open every write.
        if not self.token:
            return False
        if not hmac.compare_digest(request.headers.get("x-bob-token", ""),
                                   self.token):
            return False
        return self._origin_ok(request)

    def _session_authorised(self, request: _Request) -> bool:
        """Either token, plus the same Origin rule — the gate for the
        session tier alone (`SESSION_ACTIONS`, `SESSION_READS`).

        Two `compare_digest` calls, never `in` on a tuple, and an empty token
        on either side matches nothing: a failed bind empties both, and an
        empty secret equal to an absent header would open the tier. Every
        other write keeps `_authorised`, so a session token there falls
        through to `_route`, which answers 403 with `DESK_TOKEN_REFUSAL`.
        """
        presented = request.headers.get("x-bob-token", "")
        if not presented:
            return False
        desk = bool(self.token) and hmac.compare_digest(presented, self.token)
        session = (bool(self.session_token)
                   and hmac.compare_digest(presented, self.session_token))
        if not (desk or session):
            return False
        return self._origin_ok(request)

    def _origin_ok(self, request: _Request) -> bool:
        """The Origin half of both gates: absent, or one spelling of this server."""
        origin = request.headers.get("origin", "")
        # Every spelling of "this server": the browser sends whichever the user
        # typed, and they all reach the same loopback listener.
        allowed = {
            f"http://{self._host}:{self._port}",
            f"http://localhost:{self._port}",
            f"http://127.0.0.1:{self._port}",
            f"http://[::1]:{self._port}",
        }
        if origin and origin not in allowed:
            return False
        return True

    def _route(self, request: _Request):
        if request.path == "/api/state" and request.method == "GET":
            return 200, "application/json", json.dumps(self.state()).encode()
        if request.path == "/api/state/pretty" and request.method == "GET":
            # The same picture, indented — for a reader that works in lines.
            # `/api/state` is one line of ~100 KB, which Mission Control's
            # `Read` refuses whole (a token ceiling, not a permission) and
            # whose only `Grep` hit is "[Omitted long matching line]"; the
            # brief's pre-approved curl (`mission.CURL_RULE`) cannot carry a
            # `?` (zsh globs it unquoted, the rule denies it quoted), so the
            # newline-bearing form is a **path**, not a query. Loopback
            # only — `_loopback_host` runs above the routing table — and the
            # SSE frame stays one line (`_serve_events` is untouched).
            body = json.dumps(self.state(), indent=2).encode()
            return 200, "application/json", body
        if request.path == "/api/action":
            if request.method != "POST":
                return 405, "application/json", b'{"error":"method not allowed"}'
            if not self._authorised(request):
                body = {"error": "forbidden"}
                # A session-token holder is told, in words, why: the verb
                # needs the desk token. A guessed token hears nothing more.
                if self._session_authorised(request):
                    body["detail"] = DESK_TOKEN_REFUSAL
                return 403, "application/json", json.dumps(body).encode()
            return self._action(request.json())
        return 404, "application/json", b'{"error":"not found"}'

    def _action(self, payload: dict):
        action = payload.get("action", "")
        session_id = payload.get("session_id", "")

        if action == "dismiss" and session_id:
            asyncio.ensure_future(self._daemon.dismiss_notification(session_id))
            return 200, "application/json", b'{"ok":true}'

        if action == "delete_agent" and session_id:
            # The only action here that destroys something, and — with
            # stop_session — one of the two that answer synchronously: the
            # others are fire-and-forget because their result shows up in the
            # next push anyway. This one has to say whether it worked, since
            # "the row is still there" is also what a stale frame looks like.
            # 409 for a refusal: the request was well-formed, the daemon
            # declined it (not abandoned, no such agent, bad job id).
            ok, detail = self._daemon.delete_abandoned_agent(session_id)
            body = json.dumps({"ok": ok, "detail": detail}).encode()
            return (200 if ok else 409), "application/json", body

        if action == "stop_session" and session_id:
            # Synchronous for the same reason as delete_agent: it either signalled
            # something or it refused, and the row alone cannot tell those apart.
            # 409 on refusal — a well-formed request the daemon declined (no PID
            # on record, or the PID is no longer a Claude process).
            if session_id.startswith("codex:"):
                ok, detail = self._daemon.stop_codex_session(session_id)
            else:
                ok, detail = self._daemon.stop_session(session_id)
            body = json.dumps({"ok": ok, "detail": detail}).encode()
            return (200 if ok else 409), "application/json", body

        if action == "hide_session" and session_id:
            ok, detail = self._daemon.hide_codex_session(session_id)
            body = json.dumps({"ok": ok, "detail": detail}).encode()
            return (200 if ok else 409), "application/json", body
        return 400, "application/json", json.dumps(
            {"error": "unknown action", "action": action}
        ).encode()

    def _reveal_request(self, request: _Request) -> Optional[str]:
        """The session id if this request is an authorised reveal, else None.

        Returning None sends the request back down the ordinary `_route` path,
        which is what answers 405/403/400 — the gate is duplicated nowhere.
        """
        if request.path != "/api/action" or request.method != "POST":
            return None
        if not self._authorised(request):
            return None
        payload = request.json()
        if payload.get("action") != "reveal_session":
            return None
        session_id = payload.get("session_id", "")
        return session_id or None

    async def _reveal(self, session_id: str):
        """Jump, and answer with whether it worked.

        This used to be fire-and-forget on the theory that a reveal has its own
        visible effect. It does not when it fails — the window simply stays where
        it was, which is pixel-for-pixel identical to the press not registering,
        and the log line saying why is somewhere nobody is looking. (The reveal
        being broken machine-wide showed up as a user pressing Jump fourteen times
        in one minute against a UI that never said a word.)

        Bounded, and the task outlives the bound: the fan-out and both raisers are
        individually capped but a pathological reveal can still outlast anyone's
        patience, so past `REVEAL_TIMEOUT` we stop *waiting* rather than cancel —
        it may well be one slow raise away from succeeding, and killing it mid-way
        to report a failure we do not know about is worse than saying nothing.
        """
        task = asyncio.ensure_future(self._daemon.reveal_in_vscode(session_id))
        try:
            ok, detail = await asyncio.wait_for(
                asyncio.shield(task), timeout=REVEAL_TIMEOUT)
        except asyncio.TimeoutError:
            return 200, "application/json", b'{"ok":true,"detail":""}'
        body = json.dumps({"ok": ok, "detail": detail}).encode()
        return (200 if ok else 409), "application/json", body

    def _reveal_panel_request(self, request: _Request):
        """`(shell_pid, tty)` if this is an authorised reverse jump, else None.

        `_reveal_request`'s exact shape, and gated identically: None falls back
        through `_route`, which is what answers 405/403/400 — the gate is
        duplicated nowhere. Both fields are coerced rather than validated: the
        daemon treats them as untrusted *aim* (they can select a row, never run
        a verb), and a pair that names nothing simply opens the panel plainly.
        On the session tier (`SESSION_ACTIONS`): an agent may pop the panel
        with the file's token, a nuisance and not a capability.
        """
        if request.path != "/api/action" or request.method != "POST":
            return None
        if not self._session_authorised(request):
            return None
        payload = request.json()
        if payload.get("action") != "reveal_panel":
            return None
        try:
            shell_pid = int(payload.get("shell_pid") or 0)
        except (TypeError, ValueError):
            shell_pid = 0
        tty = str(payload.get("tty") or "")
        return shell_pid, tty

    async def _reveal_panel(self, shell_pid: int, tty: str):
        """Bring the panel forward, on that terminal's session where there is one."""
        ok, detail = await self._daemon.reveal_panel_for_terminal(shell_pid, tty)
        body = json.dumps({"ok": ok, "detail": detail}).encode()
        return (200 if ok else 409), "application/json", body

    def _permission_request(self, request: _Request):
        """`(request_id, behavior)` if this is an authorised verdict, else None.

        Same shape as `_reveal_request`, and gated the same way: writes carry
        `X-Bob-Token`, reads do not, and answering someone's permission prompt
        is emphatically a write.
        """
        if request.path != "/api/action" or request.method != "POST":
            return None
        if not self._authorised(request):
            return None
        payload = request.json()
        if payload.get("action") != "permission_verdict":
            return None
        request_id = str(payload.get("request_id") or "")
        behavior = str(payload.get("behavior") or "")
        if not request_id or behavior not in ("allow", "deny"):
            return None
        return request_id, behavior

    def _reply_request(self, request: _Request):
        """`(session_id, text)` if this is an authorised reply, else None."""
        if request.path != "/api/action" or request.method != "POST":
            return None
        if not self._authorised(request):
            return None
        payload = request.json()
        if payload.get("action") != "reply":
            return None
        session_id = payload.get("session_id", "")
        text = payload.get("text", "")
        if not session_id or not isinstance(text, str) or not text.strip():
            return None
        return session_id, text

    def _wrap_up_request(self, request: _Request) -> Optional[str]:
        """The session id if this is an authorised wrap-up, else None."""
        if request.path != "/api/action" or request.method != "POST":
            return None
        if not self._authorised(request):
            return None
        payload = request.json()
        if payload.get("action") != "wrap_up":
            return None
        session_id = payload.get("session_id", "")
        return session_id or None

    def _refinement_close_request(self, request: _Request) -> Optional[str]:
        # Only the authenticated loopback handler calls this; never a sealed door.
        # Session tier (`SESSION_ACTIONS`): the planning run's own close-out
        # sends it with the file's token.
        if (request.path != "/api/action" or request.method != "POST"
                or not self._session_authorised(request)):
            return None
        payload = request.json()
        sid = payload.get("session_id")
        return sid if (payload.get("action") == "close_refinement_terminal"
                       and isinstance(sid, str) and sid) else None

    def _close_terminal_request(self, request: _Request):
        """`(session_id, by_person)` for an authorised close-terminal, else None.

        `by_person` is the caller's assertion that a human pressed a button.
        **Absent is False**, which is today's behaviour exactly: `close-out.sh`
        sends this same action at the end of an agent's own turn and carries
        no such key, and an older panel or phone build simply does not finish
        cards.

        Session tier (`SESSION_ACTIONS`), **without** `by_person`: the file's
        token closes a terminal and finishes nothing. `by_person` with only
        the session token returns None, so `_route` refuses it with
        `DESK_TOKEN_REFUSAL` — the board's third door into Done stays the
        desk's.
        """
        if request.path != "/api/action" or request.method != "POST":
            return None
        if not self._session_authorised(request):
            return None
        payload = request.json()
        if payload.get("action") != "close_terminal":
            return None
        session_id = payload.get("session_id", "")
        if not session_id:
            return None
        by_person = payload.get("by_person") in (True, "1", "true")
        if by_person and not self._authorised(request):
            return None
        return session_id, by_person

    def _low_priority_request(self, request: _Request) -> Optional[str]:
        """The session id if this is an authorised low-priority press, else None."""
        if request.path != "/api/action" or request.method != "POST":
            return None
        if not self._authorised(request):
            return None
        payload = request.json()
        if payload.get("action") != "low_priority":
            return None
        session_id = payload.get("session_id", "")
        return session_id or None

    def _inbox_ack_request(self, request: _Request):
        """`(key, kind, fingerprint)` for an authorised inbox_ack, else None.

        `_low_priority_request`'s shape: path, POST, token and Origin.
        Returning None sends the request back down `_route`.
        """
        if request.path != "/api/action" or request.method != "POST":
            return None
        if not self._authorised(request):
            return None
        payload = request.json()
        if payload.get("action") != "inbox_ack":
            return None
        return (payload.get("key"), payload.get("kind"),
                payload.get("fingerprint"))

    async def _inbox_ack(self, key, kind, fingerprint):
        if not isinstance(key, str) or not isinstance(kind, str) \
                or not isinstance(fingerprint, str):
            return 400, "application/json", json.dumps(
                {"error": "unknown action", "action": "inbox_ack"}
            ).encode()
        ok, detail = await self._daemon.ack_inbox(key, kind, fingerprint)
        body = json.dumps({"ok": ok, "detail": detail}).encode()
        return (200 if ok else 409), "application/json", body

    def _access_alert_ack_request(self, request: _Request):
        """The alert id for an authorised `access_alert_ack`, else None.
        `_inbox_ack_request`'s shape: path, POST, token and Origin."""
        if request.path != "/api/action" or request.method != "POST":
            return None
        if not self._authorised(request):
            return None
        payload = request.json()
        if payload.get("action") != "access_alert_ack":
            return None
        return payload.get("id")

    async def _access_alert_ack(self, alert_id):
        """Close one burst alert. Clear-never-set: it can only take an open
        alert off both surfaces, and the log entry stays."""
        if not isinstance(alert_id, str):
            return 400, "application/json", json.dumps(
                {"error": "unknown action", "action": "access_alert_ack"}
            ).encode()
        if not hasattr(self._daemon, "ack_access_alert"):
            return 404, "application/json", b'{"error":"not found"}'
        ok, detail = await self._daemon.ack_access_alert(alert_id)
        body = json.dumps({"ok": ok, "detail": detail}).encode()
        return (200 if ok else 409), "application/json", body

    # -- the phone doors' access log --------------------------------------------

    @staticmethod
    def _door_for(path: str) -> str:
        """Which `access_log.DOORS` member a LAN path knocks on."""
        if path == "/api/pair":
            return "pairing"
        if path == "/api/upload":
            return "upload"
        return "lan"

    def _record_access(self, door: str, peer: str, reason: str,
                       device_id: str = "") -> None:
        """Write one refused knock to the access log, fire-and-forget.

        The one caller of `BobDaemon.note_access_refusal`. Sits on the line
        before an existing refusal `return`, takes nothing from the answer
        and returns nothing: the verdict never depends on it. Off the loop
        (no running loop) it records nothing; an exception inside is logged
        and swallowed, never raised into the door.
        """
        note = getattr(self._daemon, "note_access_refusal", None)
        if note is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        try:
            loop.create_task(note(door, peer, reason, device_id))
        except Exception:
            logger.debug("access record failed", exc_info=True)

    def note_link_timing(self, door: str, device_id: str, kind: str,
                         hops: dict, status: int, size: int) -> None:
        """Hand one timed request to `BobDaemon.note_link_timing`,
        fire-and-forget — `_record_access`'s shape exactly: after the door
        has answered, taking nothing from the answer, returning nothing,
        recording nothing off the loop, and never raising into a door."""
        note = getattr(self._daemon, "note_link_timing", None)
        if note is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        try:
            loop.create_task(note(door, device_id, kind, hops, status, size))
        except Exception:
            logger.debug("link timing record failed", exc_info=True)

    def _access_log_report_for(self, query: str):
        """The access log, newest first, on the query string so all three
        doors share one report: `since` and `limit`
        (1..`access_log.MAX_ENTRIES`), 400 on anything else — `_log_report_for`'s
        shape. Answered from memory. `available` is stated. `timing` (`0`
        or `1`, default `0`) is the opt-in for the ten-minute link-timing
        rollups: a reader that never asks — an older phone, the panel's
        window — never sees a `timing` line."""
        params = dict(
            pair.split("=", 1) for pair in (query or "").split("&") if "=" in pair
        )
        since = None
        limit = None
        include_timing = False
        if "timing" in params:
            if params["timing"] not in ("0", "1"):
                return 400, "application/json", json.dumps(
                    {"error": "timing must be 0 or 1"}).encode()
            include_timing = params["timing"] == "1"
        if "since" in params:
            try:
                since = float(params["since"])
            except ValueError:
                since = -1.0
            if not (since >= 0.0) or since == float("inf"):
                return 400, "application/json", json.dumps(
                    {"error": "since must be a number of seconds ≥ 0"}).encode()
        if "limit" in params:
            try:
                limit = int(params["limit"])
            except ValueError:
                limit = 0
            if not (1 <= limit <= access_log.MAX_ENTRIES):
                return 400, "application/json", json.dumps(
                    {"error": f"limit must be 1..{access_log.MAX_ENTRIES}"}).encode()
        log = getattr(self._daemon, "_access_log", None)
        if log is None:
            body = {"available": False, "generated_at": time.time(),
                    "entries": [], "open_alerts": []}
        else:
            body = {"available": True, "generated_at": time.time(),
                    "entries": log.recent(since=since, limit=limit,
                                          include_timing=include_timing),
                    "open_alerts": log.open_alerts(time.time())}
        return 200, "application/json", json.dumps(body).encode()

    def _terminal_input_request(self, request: _Request):
        """`(session_id, text, raw, data)` if this is an authorised
        `terminal_input` press from the panel, else None.
        `_low_priority_request`'s shape."""
        if request.path != "/api/action" or request.method != "POST":
            return None
        if not self._authorised(request):
            return None
        payload = request.json()
        if payload.get("action") != "terminal_input":
            return None
        session_id = payload.get("session_id", "")
        if not session_id:
            return None
        raw_b64 = payload.get("bytes")
        blob = None
        if isinstance(raw_b64, str) and raw_b64:
            try:
                blob = base64.b64decode(raw_b64)
            except (ValueError, TypeError):
                blob = None
        return (str(session_id), str(payload.get("text") or ""),
                bool(payload.get("raw")), blob)

    def _prepare_request(self, request: _Request):
        """The payload if this is an authorised `prepare_card`, else None.

        A verb, not a board write: it is deliberately not in `BOARD_ACTIONS`
        (that list is writes to `board.db`) and not a new endpoint (`/api/action`
        already carries every authenticated verb). Returning None sends the
        request back down `_route`, which is what answers 403/405/400.
        """
        if request.path != "/api/action" or request.method != "POST":
            return None
        if not self._authorised(request):
            return None
        payload = request.json()
        if payload.get("action") != "prepare_card":
            return None
        return payload

    def _spawn_terminal_request(self, request: _Request):
        """`(root, tool)` if this is an authorised `spawn_terminal`, else None.

        `_prepare_request`'s shape verbatim, and on the same door for the same
        reason: a verb, not a board write. It is deliberately **not** in
        `BOARD_ACTIONS` (that list is writes to `board.db`, and this writes
        none) and deliberately not on `LAN_ACTIONS` or `REMOTE_ACTIONS` — a
        phone-reachable "start an assistant in a folder, no card, no prompt"
        is strictly more capability than `board_dispatch`, which at least
        names a card a person wrote, and the phone has no gesture asking for
        it. Behind the existing `_authorised` — `X-Bob-Token` **and** the
        Origin allowlist, neither re-implemented here. Returning None sends
        the request back down `_route`, which is what answers 403/405/400.
        """
        if request.path != "/api/action" or request.method != "POST":
            return None
        if not self._authorised(request):
            return None
        payload = request.json()
        if payload.get("action") != "spawn_terminal":
            return None
        return str(payload.get("root") or ""), str(payload.get("tool") or "")

    async def _spawn_terminal(self, root: str, tool: str):
        """Open an assistant terminal with no card behind it. `_enrollment`'s
        answer shape: 400 for a request that did not say what to start or
        where, 409 for one the daemon declined, in its own words."""
        if not root or not tool:
            body = json.dumps({
                "ok": False,
                "detail": "say which folder and which assistant",
            }).encode()
            return 400, "application/json", body
        ok, detail = await self._daemon.open_adhoc_terminal(root, tool)
        body = json.dumps({"ok": ok, "detail": detail or ""}).encode()
        return (200 if ok else 409), "application/json", body

    #: The two Mission Control verbs, one name each.
    MISSION_ACTIONS = ("mission_open", "mission_end")

    def _mission_request(self, request: _Request):
        """The action name if this is an authorised `mission_open` /
        `mission_end` POST, else None. `_spawn_terminal_request`'s shape:
        a verb, not a board write, behind the existing `_authorised` —
        `X-Bob-Token` **and** the Origin allowlist. Neither verb reads a
        payload field. Returning None sends the request back down `_route`,
        which is what answers 403/405/400."""
        if request.path != "/api/action" or request.method != "POST":
            return None
        if not self._authorised(request):
            return None
        payload = request.json()
        action = payload.get("action")
        if action not in self.MISSION_ACTIONS:
            return None
        return str(action)

    async def _mission(self, action: str):
        """Open or end Mission Control. `_spawn_terminal`'s answer shape:
        200 with the daemon's detail, 409 for a refusal in its own words."""
        if action == "mission_open":
            ok, detail = await self._daemon.open_mission()
        elif action == "mission_end":
            ok, detail = await self._daemon.end_mission()
        else:
            return 404, "application/json", b'{"error":"not found"}'
        body = json.dumps({"ok": ok, "detail": detail or ""}).encode()
        return (200 if ok else 409), "application/json", body

    #: The one app verb: Rebuild & restart.
    APP_ACTIONS = ("rebuild_app",)

    def _app_request(self, request: _Request):
        """The action name if this is an authorised `rebuild_app` POST, else
        None. `_mission_request`'s shape: a verb behind the existing
        `_authorised` (desk token **and** Origin), no payload field read."""
        if request.path != "/api/action" or request.method != "POST":
            return None
        if not self._authorised(request):
            return None
        payload = request.json()
        action = payload.get("action")
        if action not in self.APP_ACTIONS:
            return None
        return str(action)

    async def _app_action(self, action: str):
        """Ask the menu-bar app to rebuild and restart. 200 means *asked*, not
        *built*; 409 carries a refusal in its own words."""
        if action not in self.APP_ACTIONS:
            return 404, "application/json", b'{"error":"not found"}'
        ok, detail = self._daemon.request_rebuild()
        body = json.dumps({"ok": ok, "detail": detail or ""}).encode()
        return (200 if ok else 409), "application/json", body

    def _enrollment_request(self, request: _Request):
        """`(action, root)` if this is an authorised enrolment write, else None.

        Behind the existing `_authorised` — `X-Bob-Token` **and** the Origin
        allowlist, neither re-implemented here — like every other write.
        Returning None sends the request back down `_route`, which is what
        answers 403/405/400, so the gate exists in exactly one place.
        """
        if request.path != "/api/action" or request.method != "POST":
            return None
        if not self._authorised(request):
            return None
        payload = request.json()
        action = payload.get("action", "")
        if action not in ("enroll_project", "unenroll_project"):
            return None
        return action, str(payload.get("root") or "")

    async def _enrollment(self, action: str, root: str):
        """Enrol or un-enrol one folder, and answer with the module's own words.

        Executed on the executor: both write files inside somebody's project,
        which is blocking I/O and must never run on the loop. The message is
        returned verbatim so the panel and the daemon cannot disagree about a
        refusal.
        """
        if not root:
            return 400, "application/json", b'{"ok":false,"detail":"no folder"}'
        loop = asyncio.get_running_loop()
        fn = (enrollment.enroll if action == "enroll_project"
              else enrollment.unenroll)
        ok, detail = await loop.run_in_executor(None, fn, root)
        body = json.dumps({"ok": ok, "detail": detail}).encode()
        return (200 if ok else 409), "application/json", body

    def _devices_request(self, request: _Request):
        """`(action, payload)` if this is an authorised devices write, else None.

        Behind the existing `_authorised` — `X-Bob-Token` **and** the Origin
        allowlist, neither re-implemented here. Loopback only: this is never
        consulted from `_handle_lan_client`. Returning None sends the request
        back down `_route`, which is what answers 403/405/400.
        """
        if request.path != "/api/action" or request.method != "POST":
            return None
        if not self._authorised(request):
            return None
        payload = request.json()
        action = payload.get("action", "")
        if action not in ("begin_pairing", "unpair_device", "set_relay",
                          "set_relay_ws", "set_away_days",
                          "set_lock_screen_actions", "pair_bot",
                          "set_bot_access"):
            return None
        return action, payload

    async def _devices(self, action: str, payload: dict):
        """Begin pairing or un-pair one phone, and answer with the daemon's
        own words. Pairing is a small JSON write; run inline like un-enrol's
        executor hop is not owed."""
        if action == "begin_pairing":
            # A bare `True`, never a truthy string: `"false"` is truthy.
            allow_typed = payload.get("allow_typed") is True
            result = self._daemon.begin_pairing(allow_typed=allow_typed)
            ok = bool(result.get("ok"))
            if ok:
                # Send pairing_open true now. `_broadcast` can defer past a
                # burst of misses that already closed the window, and the
                # booked flush would then look identical to the last sent
                # frame (still closed). `_flush` no-ops with no SSE clients.
                if self._flush_handle is not None:
                    self._flush_handle.cancel()
                    self._flush_handle = None
                self._flush()
            body = json.dumps(result).encode()
            return (200 if ok else 409), "application/json", body
        if action == "set_relay":
            # Loopback only, behind `_authorised` like its siblings. The
            # refusal (a non-https address) is the store's own sentence,
            # shown verbatim in the panel's relay sheet.
            ok, detail = relay.set_url(
                str(payload.get("url") or ""),
                str(payload.get("push_secret") or ""))
            if ok:
                self._broadcast()
            body = json.dumps({"ok": ok, "detail": detail}).encode()
            return (200 if ok else 409), "application/json", body
        if action == "set_relay_ws":
            # `set_relay`'s shape for the socket relay's address: loopback
            # only, behind `_authorised`, the store's own refusal (a
            # non-`wss://` address) shown verbatim in the relay sheet.
            ok, detail = relay.set_ws_url(str(payload.get("url") or ""))
            if ok:
                self._broadcast()
            body = json.dumps({"ok": ok, "detail": detail}).encode()
            return (200 if ok else 409), "application/json", body
        if action == "set_away_days":
            # Loopback only, like its siblings: `_devices_request` is never
            # consulted from `_handle_lan_client`, so the token, the `Host`
            # check and the Origin allowlist are the gate and this verb is on
            # neither `LAN_ACTIONS` nor `REMOTE_ACTIONS`. "End away access
            # now" is this same verb with `days = 0` — one verb, so there is
            # no second path to the store to keep honest.
            days = payload.get("days")
            # `isinstance(True, int)` is True, and `{"days": true}` meant
            # something other than a one-day grant.
            if isinstance(days, bool) or not isinstance(days, int):
                body = json.dumps({
                    "ok": False,
                    "detail": "that is not a length Dark Army offers"}).encode()
                return 409, "application/json", body
            ok, detail = relay.set_lease_days(
                str(payload.get("device_id") or ""), days)
            if ok:
                self._broadcast()
            body = json.dumps({"ok": ok, "detail": detail}).encode()
            return (200 if ok else 409), "application/json", body
        if action == "set_lock_screen_actions":
            # Loopback only, `set_away_days`' shape: the desk's consent for
            # a phone to answer a buzz from its lock screen. A bare bool,
            # never a truthy string.
            enabled = payload.get("enabled")
            if not isinstance(enabled, bool):
                body = json.dumps({
                    "ok": False,
                    "detail": "that is not a switch position"}).encode()
                return 409, "application/json", body
            ok, detail = relay.set_lock_screen_actions(
                str(payload.get("device_id") or ""), enabled)
            if ok:
                self._broadcast()
            body = json.dumps({"ok": ok, "detail": detail}).encode()
            return (200 if ok else 409), "application/json", body
        if action == "set_bot_access":
            # `set_away_days`' shape: the desk switching one side of the
            # bot's access. The daemon method refuses any target but the
            # headless device; the desk is never the bot, so no requester.
            side = payload.get("side")
            mode = payload.get("mode")
            if not isinstance(side, str) or not isinstance(mode, str):
                body = json.dumps({
                    "ok": False,
                    "detail": relay.BOT_ACCESS_MODE_REFUSAL}).encode()
                return 409, "application/json", body
            target = str(payload.get("device_id") or "")
            ok, detail = self._daemon.set_bot_access(target, side, mode)
            if ok:
                self._bot_grants_moved(target)
            body = json.dumps({"ok": ok, "detail": detail}).encode()
            return (200 if ok else 409), "application/json", body
        if action == "pair_bot":
            # Loopback only, beside `begin_pairing`: a headless device,
            # its own channel, the pair reply returned once. On neither
            # phone door. The reply carries keys, so it is not logged.
            result = self._daemon.pair_bot(str(payload.get("name") or ""))
            if result.get("ok"):
                self._broadcast()
            body = json.dumps(result).encode()
            return (200 if result.get("ok") else 409), "application/json", body
        device_id = str(payload.get("device_id") or "")
        ok, detail = self._daemon.unpair_device(device_id)
        if ok:
            self._device_last_seen.pop(device_id, None)
            self._broadcast()
        body = json.dumps({"ok": ok, "detail": detail}).encode()
        return (200 if ok else 409), "application/json", body

    async def _handle_lan_client(self, reader: asyncio.StreamReader,
                                 writer: asyncio.StreamWriter) -> None:
        """The phone door. Its own function so `_handle_client` and
        `_loopback_host` stay byte-for-byte the loopback contract.

        Every request after pairing is a **sealed frame** under the device's
        home key: `POST /api/home` (a frame whose `kind` is `state`, `usage`
        or `action` — the three things `_sealed_run` does), `POST /api/upload`
        (a header frame in `X-Bob-Frame` and a sealed blob as the body) and
        `POST /api/pair` (sealed under the QR's key, or plain from a typed
        address). Device identity is the `X-Bob-Channel` header, resolved
        through `devices.device_for_home_channel`; the seal is the proof. The
        three routes the old plaintext phone called — `GET /api/state`,
        `GET /api/usage`, `POST /api/action` — answer 426 in
        `HOME_UPDATE_REFUSAL`'s words and run nothing. A `POST` to the
        terminal stream path is the one route held open after its answer:
        a sealed `terminal_stream` frame in the body, verified by
        `_home_open`, then the framed terminal stream under `SealedCodec`
        (`_serve_lan_terminal_stream`). Everything else, `/api/events`
        included, is 404. No route here reads a device token.

        Two refusals come **before the request is read**, on the accept
        alone: over `LAN_MAX_OPEN` / `LAN_MAX_OPEN_PER_PEER` open
        connections the knock is 503 `LAN_BUSY_REFUSAL` (`busy`) with
        nothing read — a stranger cannot hold the door open by never
        sending a byte; and a knock that landed on one of this Mac's tunnel
        addresses (`sockname` against `lan_hosts.arrived_on_tunnel`) is
        **closed without a response**, recorded as `tunnel` — no status,
        because every phone build stops its walk on any HTTP answer and
        promotes the host (a 403 un-pairs it), while a dropped connection
        is "nothing there, walk on" to all of them. Loopback and every non-tunnel
        address are admitted; there is no subnet check. The counters are
        released in the `finally`, before the writer is closed — and every
        admitted socket carries TCP keepalive (`_arm_keepalive`), so a
        phone that vanishes mid-stream gives its slot back.
        """
        # The address that knocked, for the access log alone. Read first:
        # a body over the cap is refused inside `_read_request`, before
        # there is a request to stamp.
        peer = str((writer.get_extra_info("peername") or ("",))[0])
        self._arm_keepalive(writer)
        if (self._lan_open_total >= LAN_MAX_OPEN
                or self._lan_open.get(peer, 0) >= LAN_MAX_OPEN_PER_PEER):
            self._record_access("lan", peer, "busy")
            try:
                await self._respond(writer, 503, "application/json",
                                    json.dumps({"error": LAN_BUSY_REFUSAL}).encode())
            except (ConnectionResetError, BrokenPipeError, OSError):
                pass
            finally:
                try:
                    writer.close()
                    await writer.wait_closed()
                except (ConnectionResetError, BrokenPipeError, OSError):
                    pass
            return
        # Admitted: count it. Exactly one increment per decrement below.
        self._lan_open_total += 1
        self._lan_open[peer] = self._lan_open.get(peer, 0) + 1
        try:
            local = str((writer.get_extra_info("sockname") or ("",))[0])
            if self._arrived_on_tunnel(local):
                self._record_access("lan", peer, "tunnel")
                # Nothing written: the `finally` closes the socket unread.
                return
            request = await self._read_request(
                reader, self._lan_body_rule_with_frame,
                on_oversize=lambda path, device_id: self._record_access(
                    self._door_for(path), peer, "oversize",
                    device_id=device_id))
            if request is None:
                return
            request.peer = peer
            if request.path == "/api/upload" and request.method == "POST":
                status, ctype, body = await self._lan_upload(request)
                await self._respond(writer, status, ctype, body)
                return
            if request.path == "/api/pair" and request.method == "POST":
                status, ctype, body = self._lan_pair(request)
                await self._respond(writer, status, ctype, body)
                return
            if request.path == "/api/home" and request.method == "POST":
                status, ctype, body = await self._lan_home(request)
                await self._respond(writer, status, ctype, body)
                return
            if request.path == "/api/terminal/stream" and request.method == "POST":
                await self._serve_lan_terminal_stream(request, reader, writer)
                return
            if request.path == "/api/state" and request.method == "GET":
                self._record_access("lan", peer, "plaintext")
                await self._respond(writer, 426, "application/json",
                                    json.dumps(
                                        {"error": HOME_UPDATE_REFUSAL}).encode())
                return
            if request.path == "/api/usage" and request.method == "GET":
                self._record_access("lan", peer, "plaintext")
                await self._respond(writer, 426, "application/json",
                                    json.dumps(
                                        {"error": HOME_UPDATE_REFUSAL}).encode())
                return
            if request.path == "/api/log" and request.method == "GET":
                # The diary rides the sealed `log` kind on `/api/home`, never
                # plaintext — the same answer the other three reads give.
                self._record_access("lan", peer, "plaintext")
                await self._respond(writer, 426, "application/json",
                                    json.dumps(
                                        {"error": HOME_UPDATE_REFUSAL}).encode())
                return
            if request.path == "/api/bearings" and request.method == "GET":
                self._record_access("lan", peer, "plaintext")
                await self._respond(writer, 426, "application/json",
                                    json.dumps(
                                        {"error": HOME_UPDATE_REFUSAL}).encode())
                return
            if request.path == "/api/bearings/text" and request.method == "GET":
                self._record_access("lan", peer, "plaintext")
                await self._respond(writer, 426, "application/json",
                                    json.dumps(
                                        {"error": HOME_UPDATE_REFUSAL}).encode())
                return
            if request.path == "/api/history-week" and request.method == "GET":
                # The week rides the sealed `history_week` kind on
                # `/api/home`, never plaintext. `/api/history` is not listed
                # here and keeps its 404.
                self._record_access("lan", peer, "plaintext")
                await self._respond(writer, 426, "application/json",
                                    json.dumps(
                                        {"error": HOME_UPDATE_REFUSAL}).encode())
                return
            if request.path == "/api/terminal" and request.method == "GET":
                # A terminal's screen rides the sealed `terminal` kind, never
                # plaintext: it is every line an agent printed.
                self._record_access("lan", peer, "plaintext")
                await self._respond(writer, 426, "application/json",
                                    json.dumps(
                                        {"error": HOME_UPDATE_REFUSAL}).encode())
                return
            if request.path == "/api/action" and request.method == "POST":
                self._record_access("lan", peer, "plaintext")
                await self._respond(writer, 426, "application/json",
                                    json.dumps(
                                        {"error": HOME_UPDATE_REFUSAL}).encode())
                return
            self._record_access("lan", peer, "not_found")
            await self._respond(writer, 404, "application/json",
                                b'{"error":"not found"}')
        except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError):
            pass
        except Exception:
            logger.exception("LAN API request failed")
        finally:
            # Release the slot **before** the close: a stalled
            # `wait_closed` must not hold a place at the door.
            self._lan_open_total = max(0, self._lan_open_total - 1)
            left = self._lan_open.get(peer, 0) - 1
            if left > 0:
                self._lan_open[peer] = left
            else:
                self._lan_open.pop(peer, None)
            try:
                writer.close()
                await writer.wait_closed()
            except (ConnectionResetError, BrokenPipeError, OSError):
                pass

    @staticmethod
    def _arm_keepalive(writer) -> None:
        """TCP keepalive on one accepted phone-door socket, the LAN door
        only — the loopback handler is byte-for-byte what it was. Every
        option is set under its own guard: a socket asyncio cannot hand
        back, or an option this kernel lacks, is a debug line and never a
        refusal. `SO_KEEPALIVE` first, then macOS's `TCP_KEEPALIVE` (idle
        seconds), `TCP_KEEPINTVL` and `TCP_KEEPCNT` where the module names
        them."""
        sock = writer.get_extra_info("socket")
        if sock is None:
            return
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        except (OSError, AttributeError, TypeError):
            logger.debug("phone door: keepalive not armed", exc_info=True)
            return
        for name, value in (("TCP_KEEPALIVE", LAN_KEEPALIVE_IDLE_SECONDS),
                            ("TCP_KEEPINTVL", LAN_KEEPALIVE_INTERVAL_SECONDS),
                            ("TCP_KEEPCNT", LAN_KEEPALIVE_COUNT)):
            option = getattr(socket, name, None)
            if option is None:
                continue
            try:
                sock.setsockopt(socket.IPPROTO_TCP, option, int(value))
            except (OSError, TypeError):
                logger.debug("phone door: %s not set", name, exc_info=True)

    def _arrived_on_tunnel(self, local: str) -> bool:
        """Whether the address a LAN knock landed on is one of this Mac's
        tunnel addresses. `lan_hosts.live_addrs()` (psutil, no socket) is
        read on the loop and believed for `TUNNEL_ADDRS_TTL`; the reader is
        resolved through the module at call time so a test's stub bites.
        A `127.x` local address — every test knock, and a curl from the Mac
        itself — is never a tunnel. Fails open on an unreadable list."""
        now = time.monotonic()
        cached = self._tunnel_addrs_cache
        if cached is None or now - cached[0] >= TUNNEL_ADDRS_TTL:
            cached = (now, lan_hosts.live_addrs())
            self._tunnel_addrs_cache = cached
        return lan_hosts.arrived_on_tunnel(local, cached[1])

    def _home_open(self, request: _Request, *, wire: str, expect_kind=None):
        """The one verifier on the phone door. ``(device_id, key, frame)`` on
        success, ``(None, None, (status, content_type, body))`` — a whole
        answer, ready to send — on every refusal.

        Refuses an Origin (a native client never sends one), an unknown
        `X-Bob-Channel`, and every `relay.open_frame` refusal, each in words.
        **Which way it says so depends on whether the frame authenticated.**
        `seal` and `shape` failures — and an unknown channel — could have
        come from anyone, and are a plain HTTP 403; the phone treats a plain
        403 as "not paired here". `ctr`, `ts` and an `expect_kind` mismatch
        are refusals of a frame whose seal *verified*, so they are answered
        as a **sealed** `err` (HTTP 200) with the status inside — `ctr`
        carrying `ctr_expected`, the relay's exact shape, so the phone's
        fast-forward is reused and only the other keyholder learns the
        counter. A plain 403 for a skewed clock would have a paired phone
        forgetting a valid pairing on every poll. Success notes the counter (durably for a write, an
        upload or a terminal stream, coalesced for a read), stamps
        `last_seen`, and arms the away window: **this is the one
        lease-arming site**, and it sits after the successful open, never
        before, so a refused frame arms nothing.

        `terminal_stream` is a **kind on this verifier, not an action on
        `LAN_ACTIONS`**: typing through the stream needs no lease because a
        verified home frame *is* the proof of proximity that arms the
        lease, and the away door has no such route (`REMOTE_ACTIONS` is
        unchanged — away, keys ride `terminal_input` with `bytes`).
        """
        # For the access log: the door and the address. Every recorder
        # below sits on the line before an existing refusal and changes
        # nothing about the answer; the success path records nothing.
        door = self._door_for(request.path)
        peer = getattr(request, "peer", "")
        if "origin" in request.headers:
            self._record_access(door, peer, "origin")
            return None, None, (403, "application/json",
                                b'{"error":"forbidden"}')
        chan = request.headers.get("x-bob-channel", "")
        device_id = devices.device_for_home_channel(chan)
        key = devices.home_key(device_id) if device_id else None
        if not device_id or key is None:
            # No device id: the raw header value is derived from the home
            # key and is never logged.
            self._record_access(door, peer, "unpaired")
            return None, None, (403, "application/json", json.dumps(
                {"error": "that phone is not paired"}).encode())
        last = devices.last_home_recv_ctr(device_id)
        frame, err = relay.open_frame(key, relay.DIR_PHONE_TO_MAC, wire, last,
                                      ns=relay.HOME)
        if err == "ctr":
            body = self._home_answer(device_id, key, {
                "re": "", "status": 409,
                "error": relay.REFUSAL_WORDS[err],
                "ctr_expected": last + 1,
            }, kind="err")
            self._record_access(door, peer, "ctr", device_id=device_id)
            return None, None, (200, "text/plain", body)
        if err == "ts":
            body = self._home_answer(device_id, key, {
                "re": "", "status": 409,
                "error": relay.REFUSAL_WORDS[err],
            }, kind="err")
            self._record_access(door, peer, "ts", device_id=device_id)
            return None, None, (200, "text/plain", body)
        if err:
            self._record_access(door, peer, err, device_id=device_id)
            return None, None, (403, "application/json", json.dumps(
                {"error": relay.REFUSAL_WORDS[err]}).encode())
        kind = str(frame.get("kind") or "")
        if expect_kind is not None and kind != expect_kind:
            body = self._home_answer(device_id, key, {
                "re": str(frame.get("id") or ""), "status": 400,
                "error": f"that frame is not {expect_kind!r}",
            }, kind="err")
            self._record_access(door, peer, "kind", device_id=device_id)
            return None, None, (200, "text/plain", body)
        self._home_admit(device_id, int(frame["ctr"]),
                         durable=(kind in ("action", "upload", "terminal_stream")))
        return device_id, key, frame

    def _home_admit(self, device_id: str, ctr: int, *, durable: bool) -> None:
        """What accepting one verified home frame does — the counter note,
        `last_seen`, and the lease. Shared by `_home_open` and
        `_home_accept_preverified` so the arming stays textually **one
        site**: a verified home frame is the proof of proximity that arms the
        away window — for however many days this device was granted — and a
        remote frame must never extend its own lease."""
        devices.note_home_recv_ctr(device_id, ctr, durable=durable)
        self._device_last_seen[device_id] = time.time()
        # The bot is not on the day lease: its access is the two grants
        # a person sets, and its own home frames must never re-arm anything.
        if not devices.is_bot(device_id):
            relay.note_lan_proof(device_id)

    @staticmethod
    def _home_answer(device_id: str, key: bytes, body: dict, *,
                     kind: str) -> bytes:
        """One sealed Mac-to-phone frame, ready to be the HTTP body. Every
        sealed answer is HTTP 200 `text/plain`; the real status is inside.
        `relay_client._answer`'s oversize fallback: a `reply` that outgrows
        the frame cap becomes a small sealed `err` the phone can open."""
        ctr = devices.next_home_send_ctr(device_id)
        if ctr <= 0:
            return b""
        wire = relay.seal_frame(key, relay.DIR_MAC_TO_PHONE, ctr, kind, body,
                                ns=relay.HOME)
        if not wire and kind == "reply":
            ctr = devices.next_home_send_ctr(device_id)
            if ctr <= 0:
                return b""
            wire = relay.seal_frame(
                key, relay.DIR_MAC_TO_PHONE, ctr, "err", {
                    "re": str(body.get("re") or ""), "status": 500,
                    "error": relay.REFUSAL_WORDS["oversize"],
                }, ns=relay.HOME)
        return wire.encode("ascii")

    async def _lan_home(self, request: _Request):
        """`POST /api/home`: one sealed request, one sealed answer. The body
        is the frame; `kind` is `state`, `usage` or `action`, anything else an
        inner 404. Runs through `_sealed_run` behind `LAN_ACTIONS` — no lease
        check and no remote record, because this *is* home."""
        wire = request.body.decode("ascii", "replace").strip()
        # Three monotonic stamps — the seal's opening, the run, the reply's
        # sealing — are the LAN door's link timing (`link_timing.py`). The
        # first two ride the reply envelope as `timing` beside `body`
        # (never inside the state body, so `_state_digest` is untouched);
        # all three go to the rollup after the answer is built.
        opened_at = time.monotonic()
        device_id, key, frame = self._home_open(request, wire=wire)
        open_seconds = time.monotonic() - opened_at
        if device_id is None:
            return frame
        kind = str(frame.get("kind") or "")
        payload = frame.get("body")
        payload = payload if isinstance(payload, dict) else {}
        run_at = time.monotonic()
        if kind in ("state", "usage", "log", "card", "card_sync", "catch_up",
                    "outcomes", "work_record", "agent_report", "lifecycle",
                    "terminal", "conversation", "done", "knowledge",
                    "access_log", "bearings", "scout_reports",
                    "scout_report", "manual_checks", "plans", "plan", "image",
                    "history_week", "action"):
            status, ctype, out = await self._sealed_run(
                kind, payload, device_id, actions=self.LAN_ACTIONS,
                check_lease=False, record=False)
        else:
            status, ctype, out = 404, "application/json", b'{"error":"not found"}'
        run_seconds = time.monotonic() - run_at
        seal_at = time.monotonic()
        body = self._home_answer(device_id, key, {
            "re": str(frame.get("id") or ""),
            "status": int(status),
            "content_type": str(ctype),
            "body": out.decode("utf-8", "replace"),
            "timing": {"open": open_seconds, "run": run_seconds},
        }, kind="reply")
        seal_seconds = time.monotonic() - seal_at
        self.note_link_timing("lan", device_id, kind, {
            "open": open_seconds, "run": run_seconds, "seal": seal_seconds,
        }, int(status), len(out))
        return 200, "text/plain", body

    @staticmethod
    def _upload_preverify(headers: dict):
        """``(device_id, key, frame)`` when `X-Bob-Frame` is an `upload`
        frame that opens under `X-Bob-Channel`'s home key against the
        counter floor *now*, else ``None``. Notes nothing."""
        chan = headers.get("x-bob-channel", "")
        wire = headers.get("x-bob-frame", "")
        device_id = devices.device_for_home_channel(chan) if chan else ""
        key = devices.home_key(device_id) if device_id else None
        if key is None or not wire:
            return None
        frame, _err = relay.open_frame(
            key, relay.DIR_PHONE_TO_MAC, wire,
            devices.last_home_recv_ctr(device_id), ns=relay.HOME)
        if frame is None or frame.get("kind") != "upload":
            return None
        return device_id, key, frame

    @classmethod
    def _lan_body_rule_with_frame(cls, method: str, path: str, headers: dict):
        """`_lan_body_rule` plus the verified frame itself as a third element,
        so the handler accepts exactly the frame that bought the widened cap
        even if the counter floor has moved past it while the body arrived
        — the phone's 4s poll runs on its own task and is not held during
        an upload, so a `state` frame with the *next* counter routinely
        lands mid-body. Without this every photo over a few seconds on
        Wi-Fi was refused as a replay."""
        if method == "POST" and path == "/api/upload":
            verified = cls._upload_preverify(headers)
            if verified is not None:
                return (attachments.MAX_ATTACHMENT_BYTES + 1024,
                        UPLOAD_READ_TIMEOUT, verified)
        return MAX_BODY_BYTES, READ_TIMEOUT, None

    @classmethod
    def _lan_body_rule(cls, method: str, path: str, headers: dict):
        """The body cap and read timeout for one LAN request.

        `POST /api/upload` **with a header frame that verifies** gets an
        attachment-sized body and a minute to deliver it; the ``+ 1024``
        headroom means an exactly-oversize file still reaches
        `attachments.store_upload` and is refused *in words* rather than by a
        dropped connection, and still covers the blob's 28-byte nonce and
        tag. Everything else — `/api/home` included — keeps `MAX_BODY_BYTES`,
        which is a security boundary.

        The frame is opened *here* because the body is read before any
        handler runs: without this, an unpaired host on the same Wi-Fi could
        make the daemon buffer 20 MB per connection and only then be refused.
        It is not the verifier — `_lan_upload` opens the same frame again
        through `_home_open` — and it **must not note the counter**: noting
        it here would make the handler refuse its own request as a replay.
        """
        cap, timeout, _verified = cls._lan_body_rule_with_frame(
            method, path, headers)
        return cap, timeout

    def _home_accept_preverified(self, request: _Request):
        """`_home_open`'s success path for the one frame the body rule
        already opened on this connection — `_home_open`'s shape, and
        **consumed**: the slot is cleared so it is accepted at most once.

        The counter floor may have moved past this frame's ``ctr`` while the
        body arrived (a fleet poll landing mid-upload); that is exactly the
        case this exists for, so the replay test is *not* re-run — the frame
        was judged fresh when its headers arrived, on this connection, and
        no other connection can present it (a replayed copy on a fresh
        connection meets the ordinary floor in `_upload_preverify`). Origin,
        the key re-read (an un-pair during the body read fails closed), the
        durable note at ``max(ctr, floor)``, `last_seen` and the lease arming
        all still run.
        """
        pre = request.preverified
        request.preverified = None
        door = self._door_for(request.path)
        peer = getattr(request, "peer", "")
        if pre is None:
            self._record_access(door, peer, "unpaired")
            return None, None, (403, "application/json", json.dumps(
                {"error": "that phone is not paired"}).encode())
        if "origin" in request.headers:
            self._record_access(door, peer, "origin")
            return None, None, (403, "application/json",
                                b'{"error":"forbidden"}')
        device_id, key, frame = pre
        fresh = devices.home_key(device_id)
        if fresh is None or fresh != key:
            self._record_access(door, peer, "unpaired")
            return None, None, (403, "application/json", json.dumps(
                {"error": "that phone is not paired"}).encode())
        self._home_admit(device_id, int(frame["ctr"]), durable=True)
        return device_id, key, frame

    @staticmethod
    def _open_blob_and_store(key: bytes, frame_id: str, raw: bytes,
                             staging: str, name: str):
        """One executor call: open the sealed blob, then store it. Together
        so the loop never holds a 20 MB AEAD. ``(opened, rel, detail, size)``
        — `opened` False means the blob did not verify and nothing was
        written; otherwise `rel` / `detail` are `store_upload`'s own."""
        plain = relay.open_blob(key, relay.DIR_PHONE_TO_MAC, frame_id, raw,
                                ns=relay.HOME)
        if plain is None:
            return False, None, "that upload could not be opened", 0
        rel, detail = attachments.store_upload(staging, name, plain)
        return True, rel, detail, len(plain)

    async def _lan_upload(self, request: _Request):
        """One staged attachment, sealed, LAN only.

        `X-Bob-Frame` is a sealed header frame (kind `upload`, body
        `{staging, name}`) and the HTTP body is `relay.seal_blob`'s raw bytes
        under the same key, bound to that frame's id. Every bound is
        `attachments.store_upload`'s — this only opens and parses. The answer
        is a sealed `reply` whose body is today's `{ok, path, detail}`: the
        *stored* path, post-dedupe, because the phone must record what the
        Mac wrote and never the name it asked for.
        """
        if request.preverified is not None:
            device_id, key, frame = self._home_accept_preverified(request)
        else:
            # No verdict from the body rule (the frame did not open, or was
            # not an upload): the ordinary verifier, ordinary replay refusal.
            device_id, key, frame = self._home_open(
                request, wire=request.headers.get("x-bob-frame", ""),
                expect_kind="upload")
        if device_id is None:
            return frame
        payload = frame.get("body")
        payload = payload if isinstance(payload, dict) else {}
        staging = str(payload.get("staging") or "")
        refusal = self._bot_refusal(device_id, "write")
        name = str(payload.get("name") or "")
        frame_id = str(frame.get("id") or "")
        if refusal:
            # Staging a file is a write: the bot's Write grant decides, and
            # the blob is never opened.
            inner = 403, {"ok": False, "detail": refusal}
        elif not staging or not name:
            inner = 400, {"ok": False, "detail": "that upload named no file"}
        else:
            loop = asyncio.get_running_loop()
            opened, rel, detail, size = await loop.run_in_executor(
                None, self._open_blob_and_store, key, frame_id,
                request.body, staging, name)
            if not opened:
                logger.info("attachment refused: %s", detail)
                self._record_access("upload", getattr(request, "peer", ""), "blob",
                                    device_id=device_id)
                inner = 403, {"ok": False, "detail": detail}
            elif rel is None:
                # Logged because this route is otherwise silent: a photo
                # that never reached a card leaves no trace on the board, in
                # the attachments folder, or here — which is a bug report
                # nobody can answer. The refusal is Dark Army's own sentence.
                logger.info("attachment refused: %s",
                            detail or "that file was refused")
                inner = 409, {"ok": False,
                              "detail": detail or "that file was refused"}
            else:
                logger.info("attachment stored: %s (%d bytes)", rel, size)
                inner = 200, {"ok": True, "path": rel, "detail": ""}
        status, out = inner
        body = self._home_answer(device_id, key, {
            "re": frame_id,
            "status": int(status),
            "content_type": "application/json",
            "body": json.dumps(out),
        }, kind="reply")
        return 200, "text/plain", body

    def _lan_pair(self, request: _Request):
        """Spend the active pairing code. Origin is refused *before* redeem
        so a browser-shaped request cannot burn the code.

        **Sealed branch** (`X-Bob-Channel` present): the phone scanned the
        QR and already holds the pairing's home key, so its first request is
        a frame kind `pair` under it. A channel id that is not the active
        pairing's is refused with the wrong-code sentence, so a probe learns
        nothing. A redeem refusal (expired, used, ninth device) is a sealed
        `err` the phone can open and show; success is a sealed `reply`
        carrying `{token, device_id, relay_key, relay_url}` and **no
        `home_key`** — the phone already holds it.

        **Typed branch** (no channel header): runs **only** for a pairing
        the Pair window armed with `allow_typed` (`devices.begin(allow_plain=
        True)`, one-shot, dying with the code) — otherwise 403 in
        `PAIR_PLAIN_REFUSAL`'s words before the body is parsed, logged as
        `pair_plain`, with no `_note_fail` (no code was judged) and no
        broadcast. Armed, it is **SRP-6a over the code** (`srp.py`): two
        plain-JSON requests. `{"pake": "start", "A"}` answers `{"pake":
        "start", "pairing", "salt", "B"}` (`devices.pairing_srp_start`);
        `{"pake": "finish", "A", "M1", "name"}` checks the proof
        (`devices.pairing_srp_finish`) — a proof that does not check out
        counts one fail under `PAIRING_FAIL_LIMIT` and answers the
        wrong-code sentence as `pair_code`, exactly as a wrong code did —
        then redeems (`devices.redeem_proven`, the redeem's own refusals as
        `pair_code`) and answers the **sealed branch's shape verbatim**: a
        `reply` under the derived home key carrying `{token, device_id,
        relay_key, relay_url, M2}` and no `home_key`, `re` empty because
        no phone frame preceded it. The old plain `{code, name}` body, a
        malformed value, an unknown `A` and a spent start budget are
        refused as `pake` with no fail counted (`PAIR_UPDATE_REFUSAL`,
        `PAKE_SHAPE_REFUSAL`, `PAKE_BUSY_REFUSAL`).
        """
        peer = getattr(request, "peer", "")
        if "origin" in request.headers:
            self._record_access("pairing", peer, "origin")
            return 403, "application/json", b'{"error":"forbidden"}'
        chan = request.headers.get("x-bob-channel", "")
        if chan:
            return self._lan_pair_sealed(request, chan)
        if not devices.pairing_allows_plain():
            self._record_access("pairing", peer, "pair_plain")
            return 403, "application/json", json.dumps(
                {"error": PAIR_PLAIN_REFUSAL}).encode()
        payload = request.json()
        payload = payload if isinstance(payload, dict) else {}
        step = payload.get("pake")
        if step == "start":
            reply, why = devices.pairing_srp_start(str(payload.get("A") or ""))
            if reply is None:
                if why == "shape":
                    words = PAKE_SHAPE_REFUSAL
                elif why:
                    words = why
                else:
                    words = PAKE_BUSY_REFUSAL
                self._record_access("pairing", peer, "pake")
                return 403, "application/json", json.dumps(
                    {"error": words}).encode()
            body = json.dumps({"pake": "start", **reply}).encode()
            return 200, "application/json", body
        if step == "finish":
            A_hex = str(payload.get("A") or "")
            name = str(payload.get("name") or "")
            was_open = devices.pairing_open()
            K, M2, why = devices.pairing_srp_finish(
                A_hex, str(payload.get("M1") or ""))
            if K is None:
                if why == "shape":
                    self._record_access("pairing", peer, "pake")
                    return 403, "application/json", json.dumps(
                        {"error": PAKE_SHAPE_REFUSAL}).encode()
                if was_open and not devices.pairing_open():
                    self._broadcast()
                self._record_access("pairing", peer, "pair_code")
                return 403, "application/json", json.dumps(
                    {"error": "that pairing code is not valid"}).encode()
            home = srp.home_key(K)
            token, device_id, detail = devices.redeem_proven(
                name, home_key=home, first_ctr=0)
            if not token:
                if was_open and not devices.pairing_open():
                    self._broadcast()
                self._record_access("pairing", peer, "pair_code")
                return 403, "application/json", json.dumps(
                    {"error": detail or "forbidden"}).encode()
            relay_key, relay_url, relay_ws_url = self._mint_relay(device_id)
            self._broadcast()
            body = self._home_answer(device_id, home, {
                "re": "",
                "status": 200,
                "content_type": "application/json",
                "body": json.dumps({
                    "token": token,
                    "device_id": device_id,
                    "relay_key": relay_key,
                    "relay_url": relay_url,
                    "relay_ws_url": relay_ws_url,
                    "M2": M2.hex(),
                }),
            }, kind="reply")
            return 200, "text/plain", body
        # The old plain `{code, name}` shape, no `pake` key, an unknown
        # step: words that say to update the phone, and no key.
        self._record_access("pairing", peer, "pake")
        return 403, "application/json", json.dumps(
            {"error": PAIR_UPDATE_REFUSAL}).encode()

    def _lan_pair_sealed(self, request: _Request, chan: str):
        pk = devices.pairing_home_key()
        refused = json.dumps({"error": "that pairing code is not valid"}).encode()
        # Headers are latin-1 decoded; `compare_digest` on two `str`s raises
        # on a non-ASCII one, so a stray byte is refused *before* the compare
        # rather than by a dropped connection and a traceback.
        text = str(chan or "")
        if pk is None or not text.isascii() or not hmac.compare_digest(
                relay.channel_id(pk, ns=relay.HOME).encode("ascii"),
                text.encode("ascii")):
            self._record_access("pairing", getattr(request, "peer", ""), "pair_channel")
            return 403, "application/json", refused
        wire = request.body.decode("ascii", "replace").strip()
        frame, err = relay.open_frame(pk, relay.DIR_PHONE_TO_MAC, wire, 0,
                                      ns=relay.HOME)
        if err:
            self._record_access("pairing", getattr(request, "peer", ""), err)
            return 403, "application/json", json.dumps(
                {"error": relay.REFUSAL_WORDS[err]}).encode()
        if str(frame.get("kind") or "") != "pair":
            self._record_access("pairing", getattr(request, "peer", ""), "not_pair")
            return 403, "application/json", json.dumps(
                {"error": "that is not a pairing request"}).encode()
        payload = frame.get("body")
        payload = payload if isinstance(payload, dict) else {}
        frame_id = str(frame.get("id") or "")
        was_open = devices.pairing_open()
        token, device_id, detail = devices.redeem(
            str(payload.get("code") or ""), str(payload.get("name") or ""),
            first_ctr=int(frame["ctr"]))
        if not token:
            if was_open and not devices.pairing_open():
                self._broadcast()
            wire = relay.seal_frame(pk, relay.DIR_MAC_TO_PHONE, 1, "err", {
                "re": frame_id, "status": 403,
                "error": detail or "forbidden",
            }, ns=relay.HOME)
            self._record_access("pairing", getattr(request, "peer", ""), "pair_code")
            return 200, "text/plain", wire.encode("ascii")
        relay_key, relay_url, relay_ws_url = self._mint_relay(device_id)
        self._broadcast()
        key = devices.home_key(device_id)
        if key is None:
            return 403, "application/json", refused
        body = self._home_answer(device_id, key, {
            "re": frame_id,
            "status": 200,
            "content_type": "application/json",
            "body": json.dumps({
                "token": token,
                "device_id": device_id,
                "relay_key": relay_key,
                "relay_url": relay_url,
                "relay_ws_url": relay_ws_url,
            }),
        }, kind="reply")
        return 200, "text/plain", body

    def _mint_relay(self, device_id: str) -> tuple:
        """``(relay_key, relay_url, relay_ws_url)`` for a freshly paired
        device. The socket relay's address rides the pair reply beside the
        mailbox's, never the QR and never a snapshot: a phone paired before
        the address was set re-pairs at home to gain it, the relay
        address's own rule.

        The away channel's shared secret, minted here because pairing *is*
        the at-home proof. It crosses the LAN once, inside the pair response
        — sealed under the QR's key, or under the PAKE-derived key from a
        typed address — and is never written anywhere but relay.json and
        the phone's Keychain.

        Minted **only while the away switch is on**, and that is the whole
        point of the switch. Minting regardless was inert on the day but
        undid the consent the second preference key exists to ask for: a
        phone paired months before, while away access was off, would have
        become able to act from anywhere the moment somebody flipped it —
        no re-pair, no prompt, no notice. Re-pairing at home is the
        documented upgrade path for a keyless record and it is the one
        used here; the panel says so on a device with no away key.
        """
        remote_on = bool(getattr(self._daemon, "remote_access_enabled", False))
        # The socket address rides only while Socket link is on as well: a
        # phone paired while the trial lane is off carries no socket
        # address, and gains one by pairing again once it is on.
        socket_on = remote_on and bool(
            getattr(self._daemon, "relay_ws_enabled", False))
        relay_key = relay.create_channel(device_id) if remote_on else ""
        return (relay_key, (relay.get_url() if remote_on else ""),
                (relay.get_ws_url() if socket_on else ""))

    #: The board writes, and the whole set of them. Named here rather than
    #: matched with a prefix: a prefix test would enrol whatever a future
    #: action happens to be called, and the point of this gate is that it is a
    #: list somebody chose.
    BOARD_ACTIONS = ("board_create", "board_update", "board_reset",
                     "board_delete", "board_dispatch", "board_reorder",
                     "board_refine", "board_clear_done", "board_unqueue",
                     "board_queue_move", "board_manual_clear", "board_review",
                     "board_ask",
                     # Typing a person's own words onto the input line of the
                     # session working one named card. Card-scoped, never
                     # session-scoped: see `BoardVerbsMixin.message_card`.
                     "board_message", "board_approve_plan",
                     # One press, one project: Start on every startable
                     # Backlog card that project has, in board order.
                     "board_start_project",
                     "board_accept_outcome", "board_request_revision",
                     # Person-only knowledge review. Loopback only: not on
                     # LAN_ACTIONS / REMOTE_ACTIONS / _LAN_BOARD. Handled
                     # before the card_id check.
                     "knowledge_confirm", "knowledge_stale", "knowledge_edit",
                     # Turning a finished scout's report into a Prep build
                     # card. Loopback-only in v1; chosen for the phone on
                     # 21 Sep 2026 — see `LAN_ACTIONS`.
                     "board_promote",
                     # A person's Passed / Failed on a manual check file.
                     # Keyed on the file, never a card: handled before the
                     # card_id check. On both phone tuples — see
                     # `LAN_ACTIONS`.
                     "board_manual_outcome",
                     # Refine on several Prep cards with one planning
                     # session. Keyed on a list of cards (`card_ids`), so
                     # handled before the card_id check. On both phone
                     # tuples since 25 Sep 2026 — see `LAN_ACTIONS`.
                     "board_refine_batch",
                     # Start several planned Backlog cards in one session,
                     # worked one at a time. Keyed on `card_ids`, handled
                     # before the card_id check. On both phone tuples since
                     # 25 Sep 2026 — see `LAN_ACTIONS`.
                     "board_start_batch")

    #: The phone writes, and the whole set of them. Named here rather than
    #: matched with a prefix: a prefix test would enrol whatever a future
    #: action happens to be called, and the point of this gate is that it is a
    #: list somebody chose. Pairing carries write; unchosen names 404 even
    #: with a valid device token. wrap_up is deliberately absent.
    LAN_ACTIONS = (
        "board_create", "board_update", "board_reset", "board_delete",
        "board_dispatch", "board_refine",
        # The Mac's existing exact-set sweep, now chosen for a paired phone at home.
        "board_clear_done",
        # Saying "yes, that wording" about a plan somebody just read on the
        # phone. It starts nothing and only ever *narrows* what a later Start
        # will do without a confirmation.
        "board_approve_plan",
        # The queue's order, from the phone. Both are clear-never-set:
        # `queue_state` / `queued_at` / `queue_rank` stay outside
        # `_BOARD_FIELDS`, so a phone may take a card out of the line or
        # reorder cards already in it and may never put one in. Opening them
        # here therefore widens no field ring.
        "board_unqueue", "board_queue_move",
        # Getting a whole project moving in one press. It is `board_dispatch`
        # repeated under the same guards — the plan gate, the enrolment
        # refusal, `dispatch.guard` and the slot rule all run per card — it
        # carries no `skip_plan_gate`, it spawns at most one process per
        # press, and it is refused outright when `board_dispatch` is off.
        "board_start_project",
        # And the two pipeline dials. Neither is applied here: both are
        # handed to the menu-bar app through `on_preference_request`, which
        # is the one process that owns `preferences.json`.
        "set_board_autostart", "set_board_parallel_root",
        # Saying "also update the readme" to the assistant already working a
        # card, from the sofa. At home this is the obvious half: the reach is
        # narrower than `reply`, which is on this tuple already.
        "board_message",
        # Typing one line into a terminal Dark Army itself hosts, from the sofa.
        # The reach is already licensed — `reply`, `answer_question` and
        # `board_message` above all carry free text a person typed into a
        # live session — and this verb reaches a **narrower** set of
        # sessions than any of them: only ones Dark Army owns the pty of — a
        # dispatched card, an ad-hoc terminal or Mission Control. A
        # leading slash is typed, the same
        # way the desk types one. An open permission prompt is
        # `TERMINAL_PROMPT_REFUSAL`. A **control character** — any C0 byte
        # or DEL; two Ctrl-Cs quit the CLI, Escape cancels a turn, an arrow
        # sequence recalls history — is `TERMINAL_CONTROL_REFUSAL`,
        # `message_card`'s own test. From the phone the verb types printable
        # text and one Enter, and nothing else. No parenthesis in this block:
        # `test_phone_needs_you` slices the tuple at the first one.
        "terminal_input",
        "reply", "permission_verdict", "dismiss", "close_terminal", "low_priority",
        "stop_session", "hide_session", "delete_agent", "answer_question", "answer_questions",
        "prepare_card", "register_push_token",
        # The Live Activity update token, `register_push_token`'s twin:
        # the caller's own channel alone, an empty token unregisters.
        "register_activity_token",
        # Hide a Needs you subject until it changes; on a session subject
        # the card is dropped too, so the row goes quiet everywhere. Not an
        # answer. No parenthesis in this block:
        # test_phone_needs_you slices the tuple at the first one.
        "inbox_ack",
        # Saying "I did the leftover check" from the phone. Clear-never-set,
        # board_unqueue's shape: manual_steps stays outside _BOARD_FIELDS, so
        # a phone may empty a check and can never hang one on a card. The
        # press echoes the steps it drew as expected_manual_steps and the
        # store ANDs that into its own WHERE, so a stale screen clears
        # nothing else. Its own line, chosen on purpose; no parenthesis in
        # this block.
        "board_manual_clear",
        # Recording Passed or Failed on a leftover check from the phone,
        # keyed on the check file and never a card. Dark Army writes the
        # three status lines of that file and nothing else, only into a
        # regular file under an enrolled project's manual-check folder that
        # still says open, re-checked at the write; a second press is
        # refused and writes nothing. Its own line, chosen on purpose; no
        # parenthesis in this block.
        "board_manual_outcome",
        # Saying "I have read this close" from the phone. The field is
        # closed, the act is open: reviewed_at is outside _BOARD_FIELDS and
        # only this named verb stamps it. The press echoes closed_by and
        # close_note as expected_closed_by / expected_close_note, ANDed into
        # the store's one-way WHERE. Reading a result never accepts an
        # outcome, and board_accept_outcome stays off this tuple. No
        # parenthesis in this block.
        "board_review",
        # Closing one burst alert off the phone doors' access log from the
        # phone. Clear-never-set: it takes an open alert off both surfaces
        # and can raise none; the log entry stays. No parenthesis in this
        # block.
        "access_alert_ack",
        # Opening Mission Control from the phone's Comm tab. Strictly less
        # than board_dispatch, which is on this tuple and starts arbitrary
        # card work: one fixed executable in one fixed enrolled folder with
        # one fixed brief and a constant opening prompt, at most one alive,
        # idempotent while alive, behind board_dispatch, the cooldown and
        # the launch bounds. Nothing the caller sends reaches the argv. No
        # parenthesis in this block.
        "mission_open",
        # Ending it. close_terminal's reach on one named terminal, guarded
        # by identity at the moment it fires; a mistaken End costs one
        # reopen. No parenthesis in this block.
        "mission_end",
        # Turning a finished scout's report into a Prep build card from the
        # phone, where the report is now read. Strictly less than
        # board_create, which is on this tuple already: the new card's
        # every field is copied off the scout's own row and the report path
        # the scout itself attached, nothing the caller sends reaches it,
        # it dispatches nothing, and the store refuses a second press. No
        # parenthesis in this block.
        "board_promote",
        # Refine on several Prep cards in one press, from the phone. It is
        # board_refine repeated under the same guards: refine_cards re-runs
        # dispatch.refine_guard and the enrolment refusal per card under
        # _dispatch_lock, the root, the tool and eligibility are read off
        # the store at the press, the first card that fails refuses the
        # whole press before any write, it spawns exactly one planning
        # process, it carries no skip_plan_gate, and it is refused outright
        # when board_dispatch is off. Keyed on card_ids. No parenthesis in
        # this block.
        "board_refine_batch",
        # Start several planned Backlog cards in one session from the
        # phone, worked one at a time. It is board_dispatch repeated under
        # the same guards: start_cards re-runs the plan gate, the enrolment
        # refusal, dispatch.guard and the dependency gate per card under
        # _dispatch_lock, skips and names a card that fails, refuses a full
        # project, spawns exactly one process, carries no skip_plan_gate and
        # is refused outright when board_dispatch is off. Keyed on card_ids.
        # No parenthesis in this block.
        "board_start_batch",
        # Switching the bot's read or write access on, off or onto a timer
        # from the phone. It reaches one row only: BobDaemon.set_bot_access
        # refuses any target that is not the headless device, and refuses
        # the bot itself by its verified identity, never by a payload name.
        # Its own line, chosen on purpose; no parenthesis in this block.
        "set_bot_access",
        # Rebuild and restart the Mac's Dark Army from the phone, at home
        # only. It runs build.sh from the working tree as it stands, changes
        # included, and replaces the installed app: strictly more than any
        # verb on REMOTE_ACTIONS does, so it is deliberately not on that
        # tuple. A failed build away from the desk leaves nobody able to read
        # the log, and the mailbox cadence plus a restart gap makes the
        # outcome murky. Carries no payload field. A write-granted bot at
        # home reaches it too. No parenthesis in this block.
        "rebuild_app",
    )

    #: The phone writes **from away**, and the whole set of them. It starts
    #: as a copy of `LAN_ACTIONS` and is deliberately a *second* list rather
    #: than a reuse of the first: "the phone may do X on the home Wi-Fi" and
    #: "the phone may do X from anywhere in the world" are two decisions, and
    #: sharing one tuple silently made them one. `answer_questions` reached
    #: the internet that way — added here for the phone at home by a plan
    #: that had no reason to think about the relay at all. Adding a verb to
    #: the away path is now its own act, and `test_lan_access` pins that a
    #: name absent from this tuple 404s over the relay even inside a live
    #: lease.
    REMOTE_ACTIONS = (
        "board_create", "board_update", "board_reset", "board_delete",
        "board_dispatch", "board_refine",
        # Away as well as at home, and that is its own decision: board_delete
        # already destroys one card from away; Face ID already gates the away
        # path; the store still re-checks count+token. set(REMOTE_ACTIONS) <=
        # set(LAN_ACTIONS) remains the rule and this addition keeps it.
        "board_clear_done",
        # Away as well as at home, and its own decision rather than a copy of
        # the line above: reading a plan away from the desk and then not being
        # able to say "yes, that one" would leave the feature half-built.
        # Approval starts nothing, and away it rides the lease like every
        # other write.
        "board_approve_plan",
        # Away as well as at home, and that is its own decision rather than a
        # copy of the line above: choosing what runs next is exactly the thing
        # somebody away from the desk cannot otherwise do, and neither verb
        # can start work that a person had not already pressed Start on — the
        # queue is only ever joined by a person's own gesture meeting a full
        # project.
        "board_unqueue", "board_queue_move",
        # Away as well as at home, and its own decision: getting a morning's
        # work moving without being at the desk is precisely the thing the
        # relay exists for, and the press can start nothing a person could
        # not have started one card at a time. Away it rides the lease like
        # every other write.
        "board_start_project",
        # Same again for the two dials. Away this rides the 24-hour lease like
        # every other write, so a phone that has not been home in a day can
        # read the pipeline and change nothing on it.
        "set_board_autostart", "set_board_parallel_root",
        # Away as well as at home, and its own decision rather than a copy of
        # the line above. The reach is already licensed — `reply` and
        # `answer_question` both carry free text a person typed into a session
        # Dark Army is running — and this one reaches a *narrower* set of sessions.
        # The two things a terminal can do that a channel cannot (run a slash
        # command, confirm a permission dialog) are each refused by a named
        # constant rather than by hope. Away it rides the lease.
        "board_message",
        # Away as well as at home, and its own decision rather than a copy
        # of the line above: answering an agent in its own words from
        # wherever you are is the whole point of the terminal stream, and
        # the verb reaches only sessions on a pty Dark Army hosts — a dispatched
        # card, an ad-hoc terminal or Mission Control. A leading
        # slash is typed, the same way the desk types one. An open
        # permission prompt is `TERMINAL_PROMPT_REFUSAL`; a control
        # character is `TERMINAL_CONTROL_REFUSAL`. Away it rides the lease
        # like every other write. A keystroke is not idempotent, so the
        # phone always carries a receipt token on this verb — see
        # `command_receipts`.
        "terminal_input",
        "reply", "permission_verdict", "dismiss", "close_terminal", "low_priority",
        "stop_session", "hide_session", "delete_agent", "answer_question", "answer_questions",
        "prepare_card", "register_push_token",
        # Away too: ActivityKit mints a token per activity, and the
        # activity that matters is the one started away from the desk.
        "register_activity_token",
        # Away as well as at home, and its own decision
        "inbox_ack",
        # Away as well as at home, and its own decision rather than a copy
        # of the line above: a leftover check is exactly the thing somebody
        # finishes on the sofa and then wants off Needs you, the verb only
        # ever empties a note, and the echo it carries still has to match
        # the store. Away it rides the lease like every other write. No
        # parenthesis in this block.
        "board_manual_clear",
        # Away as well as at home, and its own decision: the check a person
        # finishes on the sofa is the one they want to record there. It
        # rewrites three lines of one check file that still says open, and
        # away it rides the lease like every other write. No parenthesis in
        # this block.
        "board_manual_outcome",
        # Away as well as at home, and its own decision: reading a result on
        # the train and not being able to say "seen" would leave the inbox
        # half-built. It stamps one timestamp, accepts no outcome, and away
        # it rides the lease like every other write. No parenthesis in this
        # block.
        "board_review",
        # Away as well as at home, and its own decision: the alert about a
        # burst at the doors is exactly the thing a person away from the
        # desk reads on the phone, and the verb can only close it. Away it
        # rides the lease like every other write. No parenthesis in this
        # block.
        "access_alert_ack",
        # Away as well as at home, and its own decision: asking Mission
        # Control what the agents are doing from the train is exactly what
        # the relay exists for, and the verb can start nothing but that one
        # session in that one folder. Away it rides the lease, Face ID and
        # the receipt token like every write. No parenthesis in this block.
        "mission_open",
        # Away as well as at home, and its own decision: it closes one
        # named terminal, guarded by identity, and a mistaken End costs one
        # reopen. Away it rides the lease like every other write. No
        # parenthesis in this block.
        "mission_end",
        # Away as well as at home, and its own decision: a scout's report
        # is the thing a person reads on the train, and "make this a build
        # card" is the one verb the report asks for. It creates one Prep
        # card off the scout's own row and starts nothing; away it rides
        # the lease like every other write. No parenthesis in this block.
        "board_promote",
        # Away as well as at home, and its own decision rather than a copy
        # of the line above: triaging a pile of Prep ideas is a sofa job,
        # and the press can start nothing a person could not start one
        # Refine at a time — every Refine guard runs per card, one planning
        # session opens. Away it rides the lease, Face ID and the receipt
        # token like every write. No parenthesis in this block.
        "board_refine_batch",
        # Away as well as at home, and its own decision: the morning's pile
        # of planned cards is exactly what somebody away from the desk wants
        # moving, and the press can start nothing a person could not start
        # one card at a time — every Start guard runs per card, one session
        # opens. Away it rides the lease, Face ID and the receipt token like
        # every write. No parenthesis in this block.
        "board_start_batch",
        # Away as well as at home, and its own decision: refreshing the
        # bot's timer from the train is exactly the thing the person asked
        # for. Away it rides the phone's lease, Face ID and the receipt
        # token; the bot itself is refused by identity in
        # BobDaemon.set_bot_access, so it can never lengthen its own grant.
        # No parenthesis in this block.
        "set_bot_access",
    )

    #: The board names inside `LAN_ACTIONS`. Membership, not a prefix.
    _LAN_BOARD = frozenset({
        "board_create", "board_update", "board_reset", "board_delete",
        "board_dispatch", "board_refine", "board_clear_done",
        "board_approve_plan",
        "board_unqueue", "board_queue_move", "board_start_project",
        # `_lan_run` hands these straight to `_board_action` with the payload
        # intact, and `text` is not filtered by `_door_payload`.
        "board_message",
        # The two acknowledgements; their echo keys ride the payload intact
        # and are read by `_board_action` with `"k" in payload`.
        "board_manual_clear", "board_review",
        # Keyed on `path`, handled above the `card_id` check.
        "board_manual_outcome",
        # `_board_action` reads `card_id` alone for it.
        "board_promote",
        # Keyed on `card_ids`, handled above the `card_id` check.
        "board_refine_batch",
        # Keyed on `card_ids`, handled above the `card_id` check.
        "board_start_batch",
    })

    async def _lan_run(self, action: str, payload: dict,
                       device_id: str = ""):
        """Execute one chosen LAN action. Auth and allowlisting already ran.

        Calls the execution helpers, never the `_foo_request` intercepts —
        those check the loopback token. Malformed chosen payloads 400 the
        same way a loopback intercept falling through does.

        ``device_id`` is the already-verified caller — the sealed frame's
        identity, at home (`_home_open`) or away (`relay_client`) — and only
        `register_push_token`, `register_activity_token` and
        `set_bot_access` read it: a token must land on the channel of the
        phone that *sent* it, never one the payload names, and the bot must
        be refused by who it is when it aims at its own grants.
        """
        if action in self._LAN_BOARD:
            return await self._board_action(action, payload)
        if action == "hide_session":
            session_id = payload.get("session_id")
            if not isinstance(session_id, str) or not session_id.strip():
                return 400, "application/json", json.dumps(
                    {"error": "unknown action", "action": action}
                ).encode()
            ok, detail = self._daemon.hide_codex_session(session_id)
            return (200 if ok else 409), "application/json", json.dumps(
                {"ok": ok, "detail": detail}).encode()
        if action == "reply":
            session_id = payload.get("session_id", "")
            text = payload.get("text", "")
            if not session_id or not isinstance(text, str) or not text.strip():
                return 400, "application/json", json.dumps(
                    {"error": "unknown action", "action": action}
                ).encode()
            return await self._reply(session_id, text)
        if action == "permission_verdict":
            request_id = str(payload.get("request_id") or "")
            behavior = str(payload.get("behavior") or "")
            if not request_id or behavior not in ("allow", "deny"):
                return 400, "application/json", json.dumps(
                    {"error": "unknown action", "action": action}
                ).encode()
            return await self._permission(request_id, behavior)
        if action in ("set_board_autostart", "set_board_parallel_root"):
            # `permission_verdict`'s shape: parse, refuse malformed with 400,
            # trust nothing. Every payload value on this wire is a string
            # (`PhoneClient.post` is `[String: String]`), so the numbers are
            # parsed here rather than assumed. Neither verb is applied by the
            # daemon — `request_preference` hands both to the menu-bar app,
            # the one process that owns `preferences.json` — so a 200 here
            # means accepted, not stored.
            if action == "set_board_autostart":
                enabled = str(payload.get("enabled") or "")
                if enabled not in ("on", "off"):
                    return 400, "application/json", json.dumps(
                        {"error": "unknown action", "action": action}
                    ).encode()
                ok, detail = self._daemon.request_preference(
                    "board_autostart", enabled == "on")
            else:
                root = str(payload.get("root") or "").strip()
                limit = str(payload.get("limit") or "").strip()
                # 0..4, as digits. The daemon clamps anyway, but a garbage
                # value is a malformed request rather than a silent 1 — and
                # 0 is the wire's "back to the shared default", exactly as
                # the panel's own verb defines it.
                if not root or limit not in ("0", "1", "2", "3", "4"):
                    return 400, "application/json", json.dumps(
                        {"error": "unknown action", "action": action}
                    ).encode()
                ok, detail = self._daemon.request_preference(
                    "board_parallel_root", {"root": root, "limit": int(limit)})
            return (200 if ok else 409), "application/json", json.dumps(
                {"ok": ok, "detail": detail}).encode()
        if action == "close_terminal":
            session_id = payload.get("session_id", "")
            if not session_id:
                return 400, "application/json", json.dumps(
                    {"error": "unknown action", "action": action}
                ).encode()
            return await self._close_terminal(
                session_id, payload.get("by_person") in (True, "1", "true"))
        if action == "low_priority":
            session_id = payload.get("session_id", "")
            if not session_id:
                return 400, "application/json", json.dumps(
                    {"error": "unknown action", "action": action}
                ).encode()
            return await self._low_priority(session_id)
        if action == "terminal_input":
            session_id = payload.get("session_id", "")
            if not session_id:
                return 400, "application/json", json.dumps(
                    {"error": "unknown action", "action": action}
                ).encode()
            raw_b64 = payload.get("bytes")
            if isinstance(raw_b64, str) and raw_b64:
                # The phone's own emulator typing from away: raw keys, the
                # desk's rules (`BobDaemon.terminal_input`). A string that
                # is not base64 is 400 in words, never typed as text.
                try:
                    blob = base64.b64decode(raw_b64, validate=True)
                except (ValueError, TypeError):
                    return 400, "application/json", json.dumps(
                        {"error": "bytes must be base64"}).encode()
                return await self._terminal_input(
                    str(session_id), "", from_phone=True, raw=True, data=blob)
            return await self._terminal_input(
                str(session_id), str(payload.get("text") or ""),
                from_phone=True)
        if action == "answer_question":
            # `_answer_question_request`'s parse, minus the loopback token —
            # the chosen-list membership above is this socket's gate. The
            # guards that matter are `BobDaemon.answer_question`'s own and are
            # deliberately not re-implemented here.
            session_id = payload.get("session_id", "")
            question_id = str(payload.get("question_id") or "")
            try:
                option_index = int(str(payload.get("option_index")))
            except (TypeError, ValueError):
                option_index = -1
            if not session_id or option_index < 0:
                return 400, "application/json", json.dumps(
                    {"error": "unknown action", "action": action}
                ).encode()
            return await self._answer_question(
                session_id, question_id, option_index)
        if action == "answer_questions":
            # The batch sibling: one ordered burst for a multi-question
            # dialog. Same shape as the singular branch — the guards that
            # matter are `BobDaemon.answer_questions`' own.
            session_id = payload.get("session_id", "")
            question_id = str(payload.get("question_id") or "")
            indexes = self._parse_option_indexes(payload.get("option_indexes"))
            if not session_id or indexes is None:
                return 400, "application/json", json.dumps(
                    {"error": "unknown action", "action": action}
                ).encode()
            return await self._answer_questions(
                session_id, question_id, indexes)
        if action == "prepare_card":
            # The execution helper, never `_prepare_request` — that one checks
            # the loopback token. `prepare_card_text`'s own guards (the
            # preference, the attachment refusals, the single-flight lock) are
            # the safety of this verb, exactly the `answer_question` precedent.
            return await self._prepare(payload)
        if action == "register_push_token":
            return self._register_push_token(payload, device_id)
        if action == "register_activity_token":
            return self._register_activity_token(payload, device_id)
        if action == "set_bot_access":
            # The payload names the *target*; ``device_id`` is the verified
            # sender, handed on as the requester so the bot is refused by
            # identity. An away press is filed under "Done remotely" by
            # `_sealed_run`'s own recorder; a home press is not filed.
            target = payload.get("device_id")
            side = payload.get("side")
            mode = payload.get("mode")
            if not isinstance(target, str) or not isinstance(side, str) \
                    or not isinstance(mode, str):
                return 400, "application/json", json.dumps(
                    {"error": "unknown action", "action": action}
                ).encode()
            ok, detail = self._daemon.set_bot_access(
                target, side, mode, requester=device_id)
            if ok:
                self._bot_grants_moved(target)
            return (200 if ok else 409), "application/json", json.dumps(
                {"ok": ok, "detail": detail}).encode()
        if action == "inbox_ack":
            key = payload.get("key")
            kind = payload.get("kind")
            fingerprint = payload.get("fingerprint")
            if not isinstance(key, str) or not isinstance(kind, str) \
                    or not isinstance(fingerprint, str):
                return 400, "application/json", json.dumps(
                    {"error": "unknown action", "action": action}
                ).encode()
            return await self._inbox_ack(key, kind, fingerprint)
        if action == "access_alert_ack":
            alert_id = payload.get("id")
            if not isinstance(alert_id, str):
                return 400, "application/json", json.dumps(
                    {"error": "unknown action", "action": action}
                ).encode()
            return await self._access_alert_ack(alert_id)
        if action == "mission_open":
            # No payload field is read: the verb names everything itself.
            # A fresh spawn's detail is the terminal handle on loopback;
            # on a phone door it is the word `started` — the handle never
            # crosses either sealed door.
            ok, detail = await self._daemon.open_mission()
            if ok and detail != mission.ALREADY_RUNNING:
                detail = mission.STARTED
            return (200 if ok else 409), "application/json", json.dumps(
                {"ok": ok, "detail": detail or ""}).encode()
        if action == "mission_end":
            ok, detail = await self._daemon.end_mission()
            return (200 if ok else 409), "application/json", json.dumps(
                {"ok": ok, "detail": detail or ""}).encode()
        if action == "rebuild_app":
            # No payload field is read and no path crosses the door: the
            # menu-bar app builds what it was launched from.
            ok, detail = self._daemon.request_rebuild(
                self._receipts.usable(payload.get("command_token")) or "")
            return (200 if ok else 409), "application/json", json.dumps(
                {"ok": ok, "detail": detail or ""}).encode()
        if action in ("dismiss", "stop_session", "delete_agent"):
            return self._action(payload)
        return 404, "application/json", b'{"error":"not found"}'

    def _register_push_token(self, payload: dict, device_id: str):
        """Record the calling phone's APNs token, or clear it.

        The device is the verified caller alone — there is deliberately no
        device field in the payload, so one paired phone can never point
        Dark Army's buzzes at (or away from) another. Token shape is validated
        here (hex, 32–200 chars; Apple's are 64 today but the length is not
        contractual), env is `prod`/`dev` (which APNs host the token is
        valid against), and an **empty token unregisters** — the phone's own
        opt-out on forget. A phone whose pairing predates the away channel
        has nothing to hang a push on; that refusal names the fix.
        """
        if not device_id:
            return 403, "application/json", b'{"error":"forbidden"}'
        token = str(payload.get("token") or "").strip().lower()
        env = str(payload.get("env") or "prod")
        if token == "":
            relay.note_push_token(device_id, "", "")
            return 200, "application/json", b'{"ok": true}'
        if env not in ("prod", "dev"):
            return 400, "application/json", json.dumps(
                {"error": "unknown push environment"}).encode()
        if not (32 <= len(token) <= 200) \
                or any(c not in "0123456789abcdef" for c in token):
            return 400, "application/json", json.dumps(
                {"error": "that is not a push token"}).encode()
        if not relay.note_push_token(device_id, token, env):
            return 409, "application/json", json.dumps(
                {"error": "this phone has no away channel — re-pair on "
                          "home Wi-Fi with away access on"}).encode()
        return 200, "application/json", b'{"ok": true}'

    def _register_activity_token(self, payload: dict, device_id: str):
        """Record the calling phone's Live Activity update token, or clear it.

        `_register_push_token`'s body against `relay.note_activity_token`:
        the device is the verified caller alone (no device field in the
        payload), the same hex 32–200 shape, `prod`/`dev`, an empty token
        unregisters, and a phone with no away channel is refused in the
        same words. One extra effect: a fresh token is a fresh activity, so
        the daemon forgets the `ended` mark it may hold for this device —
        through its own method, never by reaching into its dict.
        """
        if not device_id:
            return 403, "application/json", b'{"error":"forbidden"}'
        token = str(payload.get("token") or "").strip().lower()
        env = str(payload.get("env") or "prod")
        if token == "":
            relay.note_activity_token(device_id, "", "")
            self._daemon.forget_live_activity(device_id)
            return 200, "application/json", b'{"ok": true}'
        if env not in ("prod", "dev"):
            return 400, "application/json", json.dumps(
                {"error": "unknown push environment"}).encode()
        if not (32 <= len(token) <= 200) \
                or any(c not in "0123456789abcdef" for c in token):
            return 400, "application/json", json.dumps(
                {"error": "that is not a push token"}).encode()
        # Which card this phone draws. Absent is 1 (an older build). An int
        # or a decimal string in `live_activity.SHAPES`; anything else is
        # refused and nothing is stored. The empty-token branch above does
        # not read it.
        raw_shape = payload.get("shape", 1)
        if isinstance(raw_shape, str) and raw_shape.strip().isdecimal():
            raw_shape = int(raw_shape.strip())
        if (isinstance(raw_shape, bool) or not isinstance(raw_shape, int)
                or raw_shape not in live_activity.SHAPES):
            return 400, "application/json", json.dumps(
                {"error": "unknown live card shape"}).encode()
        if not relay.note_activity_token(device_id, token, env, raw_shape):
            return 409, "application/json", json.dumps(
                {"error": "this phone has no away channel — re-pair on "
                          "home Wi-Fi with away access on"}).encode()
        self._daemon.forget_live_activity(device_id)
        return 200, "application/json", b'{"ok": true}'

    def _state_answer(self, payload: dict, *, prebuilt=None):
        """The sealed `state` read, answered conditionally.

        The phone may quote the digest of the picture it is already holding.
        When ours still fingerprints the same — moving clocks and the fields
        that move because the poll happened both set aside — the answer is one
        short line meaning "keep what you have" instead of the whole fleet and
        board.

        A plain `==` is right: the frame arrived inside a verified sealed
        envelope (`_home_open` at home, `relay.open_frame` away), so the digest
        is a cache validator, not a credential, and `hmac.compare_digest` here
        would be cargo cult.

        `unchanged` is a **present key set to true**, never an omission — the
        deliberate opposite of the panel's `?sections=changed`. The phone's
        `Snapshot` decodes every key tolerantly, so an omission-marked body
        would decode into a valid *empty* snapshot: a blank fleet, a blank
        board, a needs-you count of zero.

        The answer is also **partitioned by section**. Every full answer
        carries `section_digests`, one per `_OMITTABLE_SECTIONS` key present,
        and a phone may quote them back as `sections` beside `digest`. A
        section whose quote still matches is left out of the answer **and
        named in `sections_unchanged`**, a present top-level list — the
        marker. On this door absence is never a marker: the phone's tolerant
        decoder would read a silently missing section as an empty one. The
        list and the omission come from the same variable, so they cannot
        disagree. The Mac keeps no per-phone memory: the phone quotes what it
        holds every time, and a malformed or unknown quote is ignored.

        ``prebuilt`` is the socket push's `(state, digest, section_digests)`
        triple, built once per push from the same `state(done_review=True)`
        this method would build, so every armed phone is answered off one
        picture; the answer's bytes are unchanged.
        """
        # The digest a phone quotes must fingerprint the picture it was
        # actually sent: a phone that asked for the review-only board and got
        # the full board's digest would hold one and quote the other for ever.
        if prebuilt is not None:
            state, digest, section_digests = prebuilt
        else:
            state = self.state(done_review=payload.get("done") == "review")
            digest, section_digests = _state_digests(state)
        if _valid_state_digest(payload.get("digest")) == digest:
            return 200, "application/json", json.dumps({
                "unchanged": True,
                "state_digest": digest,
                "generated_at": state.get("generated_at"),
            }).encode()
        quotes = _valid_section_quotes(payload.get("sections"))
        unchanged = [k for k in _OMITTABLE_SECTIONS
                     if k in state and quotes.get(k) == section_digests[k]]
        answer = {k: v for k, v in state.items() if k not in unchanged}
        # Added after hashing, so no digest is ever part of its own input.
        answer["state_digest"] = digest
        answer["section_digests"] = section_digests
        if unchanged:
            answer["sections_unchanged"] = unchanged
        return 200, "application/json", json.dumps(answer).encode()

    async def _state_with_usage(self, body: bytes, query: str) -> bytes:
        """The usage bars, folded into a sealed `state` answer.

        Away, a check-in used to be two mailbox trips in series — `state`,
        then `usage` — and each trip pays the mailbox's tick twice
        (`docs/2026-09-20-relay-latency-audit.md`, proposal 1). A phone that
        sends `with_usage` gets `_usage_report_for`'s whole body as a
        **sibling key** `usage` on the state answer — the full picture and
        the short `unchanged` line alike, so the bars stay fresh while the
        picture stands — and makes no second trip. Folded in **after**
        `_state_digest` has been taken, so the bars, which move every turn,
        never make a standing picture look changed; the phone's `Snapshot`
        ignores the key. A usage read that fails leaves the state answer as
        it was, and the phone's own fallback leg asks separately. An older
        phone never sends the marker and sees nothing new.

        The usage body is memoised per query string for `USAGE_MEMO_SECONDS`
        — the SQLite attribution behind it cost the check-in its whole
        latency budget while the picture it rode beside was memory — and
        parsed once at build, so the fold composes the sibling from a dict
        rather than reading the usage bytes again per check-in.
        """
        try:
            usage = await self._usage_for_state(query)
            if usage is None:
                return body
            answer = json.loads(body)
            answer["usage"] = usage
            return json.dumps(answer).encode()
        except Exception:  # noqa: BLE001 - a read beside a read never breaks it
            return body

    async def _usage_for_state(self, query: str):
        """`_usage_report_for`'s parsed body for the state fold, believed
        for `USAGE_MEMO_SECONDS` per query string; `None` on a refusal.
        Expired entries are dropped as a fresh one is written, so the memo
        never outgrows the handful of queries a phone actually sends."""
        now = time.monotonic()
        memo = self._usage_memo.get(query)
        if memo is not None and now - memo[0] <= USAGE_MEMO_SECONDS:
            return memo[1]
        status, _, raw = await self._usage_report_for(query)
        if status != 200:
            return None
        usage = json.loads(raw)
        self._usage_memo = {
            q: entry for q, entry in self._usage_memo.items()
            if now - entry[0] <= USAGE_MEMO_SECONDS}
        self._usage_memo[query] = (now, usage)
        return usage

    async def _sealed_run(self, kind: str, payload: dict, device_id: str,
                          *, actions: tuple, check_lease: bool, record: bool,
                          prebuilt=None):
        """One request that arrived as a sealed envelope, already verified,
        from either door.

        The sealed paths compute nothing of their own: `state` is the same
        `state()` the loopback serves, `usage` is `_usage_report_for`, and
        `action` is the same `_lan_run` behind ``actions`` — `LAN_ACTIONS`
        at home, `REMOTE_ACTIONS` away, two lists so that widening the
        phone's reach at home is never silently a widening of its reach from
        anywhere. ``check_lease`` is the away path's one extra check: a
        lapsed away window refuses every write in `relay.LEASE_REFUSAL`'s
        words and executes nothing; reads keep working, because expiry
        bounds what the phone may *do*, never what it may *see*. ``record``
        writes every executed write to the daemon (`remote_activity`) and
        the log, so what was driven from outside the house is always listed
        on the Mac.
        """
        payload = payload if isinstance(payload, dict) else {}
        # The bot's read grant, above **every** read kind: one `if` here is
        # the whole read gate, so no kind below can be left open. A phone is
        # never headless and passes untouched; expiry of *its* lease still
        # bounds doing, never seeing.
        headless = devices.is_bot(device_id)
        if headless:
            # A bot paired before the grants existed is made explicit on
            # its first frame and taken off the day lease for good.
            relay.materialise_bot_grants(device_id)
        refusal = self._bot_refusal(device_id, "read") \
            if headless and kind != "action" else ""
        if refusal:
            body = json.dumps({"error": refusal, "detail": refusal}).encode()
            return 403, "application/json", body
        if kind == "state":
            status, content_type, body = self._state_answer(
                payload, prebuilt=prebuilt)
            if status == 200 and payload.get("with_usage"):
                body = await self._state_with_usage(
                    body, str(payload.get("usage_query") or ""))
            return status, content_type, body
        if kind == "usage":
            return await self._usage_report_for(str(payload.get("query") or ""))
        if kind == "outcomes":
            return await self._outcome_report_for(str(payload.get("query") or ""))
        if kind == "catch_up":
            return await self._catch_up_report(str(payload.get("query") or ""), device_id)
        if kind == "log":
            # A read, like `state` and `usage`: no lease, no action list, no
            # record. `LAN_ACTIONS` / `REMOTE_ACTIONS` gate `action` alone.
            return self._log_report_for(str(payload.get("query") or ""))
        if kind == "bearings":
            # `log`'s sibling and `log`'s rule: a **read**, above the
            # `action` branch, so it touches neither action tuple, takes no
            # lease check and writes no `remote_activity` record.
            return self._bearings_report_for(str(payload.get("query") or ""))
        if kind == "access_log":
            # `log`'s sibling and `log`'s rule: a **read**, above the
            # `action` branch, so it touches neither action tuple, takes no
            # lease check and writes no `remote_activity` record.
            return self._access_log_report_for(str(payload.get("query") or ""))
        if kind == "work_record":
            # `log`'s sibling and `log`'s rule: a **read**, above the `action`
            # branch, so it touches neither action tuple, takes no lease check
            # and writes no `remote_activity` record. Expiry bounds what the
            # phone may *do*, never what it may see.
            return await self._work_record_report_for(
                str(payload.get("query") or ""))
        if kind == "agent_report":
            # `log`'s rule again: a **read**, above the `action` branch, so it
            # touches neither action tuple, takes no lease check and writes no
            # `remote_activity` record. Expiry bounds what the phone may *do*,
            # never what it may see.
            return await self._agent_report_for(str(payload.get("query") or ""))
        if kind == "lifecycle":
            # `outcomes`' sibling and `log`'s rule: a **read**, above the
            # `action` branch, so it touches neither action tuple, takes no
            # lease check and writes no `remote_activity` record.
            return await self._lifecycle_report_for(str(payload.get("query") or ""))
        if kind == "card":
            # One card, in full, with the plan it points at. A read for the
            # same reason `log` is one — it widens no action tuple, checks no
            # lease, and records nothing — and it exists because the live
            # frame deliberately carries a *preview* of every prompt, so a
            # phone that lived off the frame could start work it could not
            # read.
            return await self._card_report_for(str(payload.get("query") or ""))
        if kind == "card_sync":
            # The batch sibling of `card`, and the same rule the `log` branch
            # states: a **read**, above the `action` branch, so it consults
            # neither action tuple, checks no lease and writes no
            # `remote_activity` record. Away as well as at home is deliberate
            # and free rather than a widening — a phone that could read the
            # board's titles from a hotel and not the instructions behind
            # them would be the half-built thing this exists to remove.
            return await self._card_sync_for(payload)
        if kind == "terminal":
            # One frame of a Dark Army-owned terminal's screen — `card_sync`'s
            # sibling and `log`'s rule: a **read**, above the `action`
            # branch, so it consults neither action tuple, checks no lease
            # and writes no `remote_activity` record. Carries the phone's
            # own `cols` / `rows` as a *phone* resize — the stream's `S`
            # frame's verb, `terminal_phone_resize`, which yields to the
            # panel's pane — never the panel's unconditional one
            # (`BobDaemon.terminal_frame`).
            # `viewer` is the sealed device id the seal already proved,
            # so a paint remainder is kept per phone, never per handle —
            # nothing new rides the wire.
            return self._terminal_report_for(
                str(payload.get("query") or ""), resize=False,
                viewer=device_id)
        if kind == "conversation":
            # A read, like `log`: neither action tuple, no lease, no
            # `remote_activity` record. Expiry bounds what the phone may
            # *do*, never what it may see.
            return await self._conversation_report_for(
                str(payload.get("query") or ""))
        if kind == "done":
            # The whole finished column, on demand — `log`'s rule again: a
            # **read**, above the `action` branch, so it consults neither
            # action tuple, checks no lease and writes no `remote_activity`
            # record. Expiry bounds what the phone may *do*, never what it
            # may see, and a phone that could not read its own finished
            # column from a hotel would be the half-built thing this exists
            # to remove.
            return await self._done_archive_for(payload)
        if kind == "knowledge":
            # One enrolled project's notes. `log`'s rule: a **read**, above
            # the `action` branch, so it consults neither action tuple,
            # checks no lease and writes no `remote_activity` record. `root`
            # rides the JSON body, never a query string — logs and envelope
            # fields must not leak a path on the relay.
            return await self._knowledge_report_for(payload)
        if kind == "scout_reports":
            # Every watched project's scout reports, newest first, no
            # bodies. `knowledge`'s rule: a **read**, above the `action`
            # branch — neither action tuple, no lease check, no
            # `remote_activity` record. An optional `root` and an optional
            # text search `q` ride the JSON body, never a query string — a
            # search term must not reach the relay's logs any more than a
            # path.
            return await self._scout_reports_for(payload)
        if kind == "scout_report":
            # One report's text, fetched when the phone opens it. The same
            # read rule; `path` rides the JSON body (a path on a query
            # string would reach the relay's logs) and is re-checked
            # against `scout_index.locate`'s closed set at the read.
            return await self._scout_report_for(payload)
        if kind == "manual_checks":
            # The Checks section: every enrolled project's manual checks, or
            # one file's text when `path` is in the body. `knowledge`'s
            # rule: a **read**, above the `action` branch — neither action
            # tuple, no lease check, no `remote_activity` record. `root`,
            # `q`, `status` and `path` ride the JSON body, never a query
            # string.
            return await self._manual_checks_for(payload)
        if kind == "plans":
            # Every watched project's plans, newest first, no bodies.
            # `scout_reports`' rule: a **read**, above the `action` branch —
            # neither action tuple, no lease check, no `remote_activity`
            # record. An optional `root` rides the JSON body, never a query
            # string.
            return await self._plans_for(payload)
        if kind == "plan":
            # One plan's text, fetched when the phone opens it. The same
            # read rule; `path` rides the JSON body (a path on a query
            # string would reach the relay's logs) and is re-checked
            # against `plan_index.locate`'s closed set at the read.
            return await self._plan_for(payload)
        if kind == "image":
            # One picture from a session's project, shrunk to fit one
            # answer (`image_preview`). `plan`'s rule: a **read**, above the
            # `action` branch — neither action tuple, no lease check, no
            # `remote_activity` record — and the bot's Read grant above
            # decides for the bot. `session` and `path` ride the JSON body,
            # never a query string, and the path is confined to that
            # session's own project at the read.
            return await self._image_for(payload)
        if kind == "history_week":
            # The phone's History: the last seven days, machine-wide, cut to
            # a closed key set. `log`'s rule: a **read**, above the `action`
            # branch — neither action tuple, no lease check, no
            # `remote_activity` record. It takes no query; the week is fixed.
            return await self._history_week_for(payload)
        if kind == "action":
            action = str(payload.get("action") or "")
            # `board_create` alone is exempt: a card may be written *with*
            # its objective from the phone, and `BoardStore.create` bounds
            # the values with `objective_refusal`. Editing one that exists
            # stays a Mac-side act — `board_update` with any of the four
            # keys is still refused here, before the action tuple.
            if action != "board_create" and any(k in payload for k in ("beneficiary", "intended_benefit", "success_criterion", "outcome_check_on")):
                return 403, "application/json", b'{"error":"outcome editing is available on the Mac"}'
            if action not in actions:
                return 404, "application/json", b'{"error":"not found"}'
            # The bot's write grant decides for the bot on both doors, and
            # the day lease no longer does; a phone keeps its day lease away
            # and nothing at home, exactly as before.
            if headless:
                refusal = self._bot_refusal(device_id, "write")
                if refusal:
                    body = json.dumps(
                        {"error": refusal, "detail": refusal}).encode()
                    return 403, "application/json", body
            elif check_lease and not relay.lease_valid(device_id):
                body = json.dumps({"error": relay.LEASE_REFUSAL,
                                   "detail": relay.LEASE_REFUSAL}).encode()
                return 403, "application/json", body
            # The one-time mark, **below** the allow-list and **below** the
            # lease check (the bot's write grant, for the bot), both
            # deliberately: a token minted at home must
            # never replay out of a lapsed away window, and an unchosen verb
            # must 404 before anything is looked up. An absent or malformed
            # token means no dedupe and today's behaviour exactly, which is
            # what keeps a phone older than the ledger working.
            token = self._receipts.usable(payload.get("command_token"))
            if token:
                replayed = self._receipts.look_up(device_id, token)
                if replayed is not None:
                    logger.info("replayed action %s from device %s",
                                action, device_id)
                    return replayed
            status, ctype, body = await self._lan_run(
                action, self._door_payload(action, payload, actions),
                device_id=device_id)
            if token and status in (200, 409):
                # Both are final answers about this press; a person's RETRY
                # mints a new token, so recording a refusal freezes nobody
                # out. Anything else (a transport-shaped failure, a 403) is
                # deliberately not recorded — it is not an answer yet.
                self._receipts.record(device_id, token, (status, ctype, body))
            if record:
                ok = status == 200
                recorder = getattr(self._daemon, "record_remote_action", None)
                if recorder is not None:
                    recorder(device_id, action, ok)
                logger.info("remote action %s from device %s -> %d",
                            action, device_id, status)
            return status, ctype, body
        return 404, "application/json", b'{"error":"not found"}'

    @staticmethod
    def _bot_refusal(device_id: str, side: str) -> str:
        """The bot's refusal for ``side`` (``"read"`` / ``"write"``), or
        ``""`` where the device is not the bot or that grant is on. **The
        one place both refusals are chosen**, used by every door the bot
        can reach: `_sealed_run` (every read kind and every action, home and
        away), the home terminal stream (opening it is a read, each
        keystroke a write) and the home upload (a write). `devices.is_bot`
        fails closed; the grant is re-read per call, so an Off bites on the
        very next frame or keystroke."""
        if not devices.is_bot(device_id) \
                or relay.bot_grant_valid(device_id, side):
            return ""
        return (relay.BOT_READ_REFUSAL if side == "read"
                else relay.BOT_WRITE_REFUSAL)

    def _close_bot_streams(self, device_id: str) -> None:
        """Hang up the bot's open home terminal streams once either of its
        grants is off, so a stream opened while Read was on does not keep
        painting after it went off. A phone's streams are never touched."""
        if not devices.is_bot(device_id):
            return
        if relay.bot_grant_valid(device_id, "read") \
                and relay.bot_grant_valid(device_id, "write"):
            return
        for writer in list(self._lan_streams.get(device_id, [])):
            try:
                writer.close()
            except OSError:
                pass

    def _bot_grants_moved(self, device_id: str) -> None:
        """After a grant changed: close what it now forbids and redraw."""
        self._close_bot_streams(device_id)
        self._arm_bot_expiry_from_store()
        self._broadcast()

    def _arm_bot_expiry_from_store(self) -> None:
        """Set the lapse timer from the store itself. `_broadcast` builds no
        picture while nobody listens, so a grant change or a bot stream
        opening with no panel and no phone attached would otherwise leave a
        timed grant running on an open stream past its end."""
        try:
            self._arm_bot_expiry(self._daemon.devices_snapshot())
        except Exception:  # noqa: BLE001 — a timer, never a refusal path
            logger.exception("could not set the bot grant timer")

    def _devices_section(self) -> dict:
        """`state()`'s devices section, plus one timer at the moment the
        earliest running bot grant ends, so a lapsed timer is redrawn (and
        the bot's streams closed) when it lapses rather than at the next
        unrelated frame."""
        section = self._daemon.devices_snapshot()
        self._arm_bot_expiry(section)
        return section

    def _arm_bot_expiry(self, section: dict) -> None:
        now = time.time()
        ends = []
        for row in (section or {}).get("devices") or []:
            access = row.get("bot_access") if isinstance(row, dict) else None
            if not isinstance(access, dict):
                continue
            for grant in access.values():
                until = grant.get("until") if isinstance(grant, dict) else None
                if isinstance(until, (int, float)) and until > now:
                    ends.append(float(until))
        earliest = min(ends) if ends else 0.0
        if earliest == self._bot_expiry_at:
            return
        if self._bot_expiry_handle is not None:
            self._bot_expiry_handle.cancel()
            self._bot_expiry_handle = None
        self._bot_expiry_at = earliest
        if not earliest:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._bot_expiry_at = 0.0
            return
        self._bot_expiry_handle = loop.call_later(
            earliest - now + BOT_EXPIRY_GRACE_SECONDS, self._bot_grant_lapsed)

    def _bot_grant_lapsed(self) -> None:
        self._bot_expiry_handle = None
        self._bot_expiry_at = 0.0
        for device_id in list(self._lan_streams):
            self._close_bot_streams(device_id)
        self._broadcast()

    @staticmethod
    def _door_payload(action: str, payload: dict, actions: tuple) -> dict:
        """The `refine` flag on `board_create` is a `board_refine` in
        disguise, so a door honours it only where that door also allows
        `board_refine`. Elsewhere it is **dropped, not refused**: the create
        half is allowed on that door and the phone must not lose the card.
        Every door allows the verb today, so nothing changes on the wire;
        the rule exists so that narrowing `REMOTE_ACTIONS` later cannot
        leave the flag as a back door."""
        if action == "board_create" and "refine" in payload \
                and "board_refine" not in actions:
            return {k: v for k, v in payload.items() if k != "refine"}
        return payload

    async def _remote_run(self, kind: str, payload: dict, device_id: str,
                          *, prebuilt=None):
        """The away door's run: `_sealed_run` behind `REMOTE_ACTIONS`, with
        the lease checked and every write recorded. `relay_client` calls this
        and nothing else; the socket push alone passes ``prebuilt``
        (`_state_answer`)."""
        return await self._sealed_run(
            kind, payload, device_id, actions=self.REMOTE_ACTIONS,
            check_lease=True, record=True, prebuilt=prebuilt)

    @staticmethod
    def _outcome_integer(value):
        if type(value) is int and value >= 0:
            return value
        if isinstance(value, str) and value.isascii() and value.isdigit() and len(value) < 12:
            return int(value)
        return None

    async def _knowledge_report_for(self, query_or_payload):
        """One enrolled project's notes. Loopback GET uses the query string;
        the sealed kind uses the JSON body (`root` there, never a query
        string). Missing/empty root is a refusal in words, never
        `knowledge_for("")`.
        """
        try:
            if isinstance(query_or_payload, dict):
                payload = query_or_payload
                if "root" not in payload:
                    raise ValueError("Dark Army needs an enrolled project")
                root = str(payload.get("root") or "").strip()
                if "offset" in payload:
                    offset = self._outcome_integer(payload.get("offset"))
                    if offset is None:
                        raise ValueError(
                            "offset must be a nonnegative whole number")
                else:
                    offset = 0
            else:
                params = parse_qs(query_or_payload or "",
                                  keep_blank_values=True)
                if any(len(v) != 1 for v in params.values()):
                    raise ValueError("report parameters must not repeat")
                if "root" not in params:
                    raise ValueError("Dark Army needs an enrolled project")
                root = str(params.get("root", [""])[0] or "").strip()
                offset = self._outcome_integer(params.get("offset", ["0"])[0])
                if offset is None:
                    raise ValueError(
                        "offset must be a nonnegative whole number")
            if not root:
                raise ValueError("Dark Army needs an enrolled project")
            if len(root) > 1024:
                raise ValueError(
                    "a project root must be at most 1024 characters")
            handler = getattr(self._daemon, "knowledge_report", None)
            report = (await handler(root) if handler else {
                "supported": True, "available": False,
                "root": root, "entries": [],
                "truncated": False, "omitted_keys": []})
            entries = report.get("entries")
            if not isinstance(entries, list):
                entries = []
                report["entries"] = entries
            if offset:
                report["entries"] = entries[offset:]
            report["offset"] = offset
            return 200, "application/json", self._knowledge_page_bytes(report)
        except (ValueError, TypeError) as exc:
            return 400, "application/json", json.dumps(
                {"error": str(exc)}).encode()

    async def _manual_checks_for(self, query_or_payload):
        """The Checks section — loopback `GET /api/manual-checks` and the
        sealed `manual_checks` kind. With `path`, one check's text
        (`manual_check_text`); otherwise the list, `root` empty meaning
        every enrolled root, `q` a search (≤ 200 characters), `status` one
        of `all`, `open`, `passed`, `failed` or empty. A malformed request is
        400 in words."""
        try:
            if isinstance(query_or_payload, dict):
                params = {k: v for k, v in query_or_payload.items()
                          if k in ("root", "q", "status", "path")}
            else:
                raw = parse_qs(query_or_payload or "", keep_blank_values=True)
                if any(len(v) != 1 for v in raw.values()):
                    raise ValueError("report parameters must not repeat")
                params = {k: v[0] for k, v in raw.items()}
            if "path" in params:
                path = str(params.get("path") or "").strip()
                if not path:
                    raise ValueError("a manual check needs its path")
                if len(path) > 1024:
                    raise ValueError("a path must be at most 1024 characters")
                handler = getattr(self._daemon, "manual_check_text", None)
                document = (await handler(path) if handler else {
                    "available": False, "path": path, "text": "",
                    "reason": "Dark Army cannot read manual checks"})
                return 200, "application/json", json.dumps(
                    document, allow_nan=False).encode()
            root = str(params.get("root") or "").strip()
            query = str(params.get("q") or "").strip()
            status = str(params.get("status") or "").strip().lower()
            if len(root) > 1024:
                raise ValueError(
                    "a project root must be at most 1024 characters")
            if len(query) > 200:
                raise ValueError("a search must be at most 200 characters")
            if status not in daemon_board.BoardVerbsMixin.MANUAL_CHECK_FILTERS:
                raise ValueError(
                    "status must be all, open, passed or failed")
            handler = getattr(self._daemon, "manual_checks_report", None)
            report = (await handler(root, query, status) if handler else {
                "supported": True, "available": False, "root": root,
                "checks": [], "truncated": False,
                "reason": "Dark Army cannot read manual checks"})
            return 200, "application/json", self._manual_checks_page_bytes(
                report)
        except (ValueError, TypeError) as exc:
            return 400, "application/json", json.dumps(
                {"error": str(exc)}).encode()

    @staticmethod
    def _manual_checks_page_bytes(report):
        """Bound plaintext at 300 000 bytes before sealing,
        `_knowledge_page_bytes`' rule: oversize drops the *last* checks of
        the page (open first, newest first, so the oldest settled go) and
        sets `truncated: true`; an included check is never shortened."""
        checks = report.get("checks")
        if not isinstance(checks, list):
            report["checks"] = []
            checks = report["checks"]
        while True:
            body = json.dumps(report, allow_nan=False).encode()
            if len(body) <= 300_000 or len(checks) <= 1:
                return body
            checks.pop()
            report["truncated"] = True

    @staticmethod
    def _knowledge_page_bytes(report):
        """Bound plaintext at 300_000 bytes before sealing.

        `_outcome_page_bytes`' budget: the sealed envelope embeds this JSON
        as a string. Oversize drops later entries of this page (`break`, not
        skip-and-continue), names `omitted_keys`, sets `truncated: true`,
        and never silently shortens an included answer (the store already
        clamped at 4000).
        """
        entries = report.get("entries")
        if not isinstance(entries, list):
            report["entries"] = []
            entries = report["entries"]
        omitted = [str(k) for k in (report.get("omitted_keys") or [])]
        while True:
            body = json.dumps(report, allow_nan=False).encode()
            if len(body) <= 300_000:
                return body
            if len(entries) <= 1:
                return body
            last = entries.pop()
            key = ""
            if isinstance(last, dict):
                key = str(last.get("key") or "")
            omitted.insert(0, key)
            report["truncated"] = True
            report["omitted_keys"] = omitted

    @staticmethod
    def _scout_params(query_or_payload) -> dict:
        """The parameters of a scout-report read: the JSON body on the
        sealed doors, the query string on loopback (`parse_qs` with blanks
        kept, a repeated key refused in words)."""
        if isinstance(query_or_payload, dict):
            return dict(query_or_payload)
        params = parse_qs(query_or_payload or "", keep_blank_values=True)
        if any(len(v) != 1 for v in params.values()):
            raise ValueError("report parameters must not repeat")
        return {k: v[0] for k, v in params.items()}

    async def _scout_reports_for(self, query_or_payload):
        """The scout-report list — loopback `GET /api/scout-reports` and the
        sealed `"scout_reports"` kind. No `root` means every enrolled root;
        a `root` that is present but empty, or not enrolled, is a 400 in
        words (`_knowledge_enrolled_root`'s), never an empty list. A `q`
        asks for a text search over the bodies (`scout_index.search`):
        under `MIN_QUERY_CHARS` or over `MAX_QUERY_CHARS` is a 400 in
        words; the page bound applies to its reply the same."""
        try:
            params = self._scout_params(query_or_payload)
            root = str(params.get("root") or "").strip()
            if "root" in params and not root:
                raise ValueError("Dark Army needs an enrolled project")
            if len(root) > 1024:
                raise ValueError(
                    "a project root must be at most 1024 characters")
            query = " ".join(str(params.get("q") or "").split())
            if "q" in params:
                if len(query) < scout_index.MIN_QUERY_CHARS:
                    raise ValueError(scout_index.QUERY_TOO_SHORT)
                if len(query) > scout_index.MAX_QUERY_CHARS:
                    raise ValueError(scout_index.QUERY_TOO_LONG)
            handler = getattr(self._daemon, "scout_reports_index", None)
            if handler is None:
                report = {
                    "supported": True, "available": False, "rows": [],
                    "truncated": False, "omitted": 0, "roots": 0}
            elif query:
                report = await handler(root, query)
            else:
                report = await handler(root)
            return 200, "application/json", \
                self._scout_reports_page_bytes(report)
        except (ValueError, TypeError) as exc:
            return 400, "application/json", json.dumps(
                {"error": str(exc)}).encode()

    async def _scout_report_for(self, query_or_payload):
        """One report's text — loopback `GET /api/scout-report?path=` and the
        sealed `scout_report` kind (`path` in the JSON body there). A
        missing or empty `path` is a 400 in words; a path outside the
        closed set is a 200 `available: false` with the refusal in
        `reason` (`scout_index.locate`)."""
        try:
            params = self._scout_params(query_or_payload)
            if "path" not in params:
                raise ValueError("Dark Army needs the report's path")
            path = str(params.get("path") or "").strip()
            if not path:
                raise ValueError("Dark Army needs the report's path")
            if len(path) > 4096:
                raise ValueError(
                    "a report path must be at most 4096 characters")
            handler = getattr(self._daemon, "scout_report_body", None)
            report = (await handler(path) if handler else {
                "available": False, "path": path, "header": {}, "body": "",
                "has_header": False, "reason": "Dark Army cannot read reports"})
            return 200, "application/json", json.dumps(
                report, allow_nan=False).encode()
        except (ValueError, TypeError) as exc:
            return 400, "application/json", json.dumps(
                {"error": str(exc)}).encode()

    async def _plans_for(self, query_or_payload):
        """The plan list — loopback `GET /api/plans` and the sealed
        `"plans"` kind. `_scout_reports_for`' rule: no `root` means every
        enrolled root; a `root` that is present but empty, or not enrolled,
        is a 400 in words, never an empty list. The page is bounded by
        `_scout_reports_page_bytes`, dropping the oldest rows."""
        try:
            params = self._scout_params(query_or_payload)
            root = str(params.get("root") or "").strip()
            if "root" in params and not root:
                raise ValueError("Dark Army needs an enrolled project")
            if len(root) > 1024:
                raise ValueError(
                    "a project root must be at most 1024 characters")
            handler = getattr(self._daemon, "plans_index", None)
            report = (await handler(root) if handler else {
                "supported": True, "available": False, "rows": [],
                "truncated": False, "omitted": 0, "roots": 0})
            return 200, "application/json", \
                self._scout_reports_page_bytes(report)
        except (ValueError, TypeError) as exc:
            return 400, "application/json", json.dumps(
                {"error": str(exc)}).encode()

    async def _plan_for(self, query_or_payload):
        """One plan's text — loopback `GET /api/plan?path=` and the sealed
        `plan` kind (`path` in the JSON body there). A missing or empty
        `path` is a 400 in words; a path outside the closed set is a 200
        `available: false` with the refusal in `reason`
        (`plan_index.locate`)."""
        try:
            params = self._scout_params(query_or_payload)
            if "path" not in params:
                raise ValueError("Dark Army needs the plan's path")
            path = str(params.get("path") or "").strip()
            if not path:
                raise ValueError("Dark Army needs the plan's path")
            if len(path) > 4096:
                raise ValueError(
                    "a plan path must be at most 4096 characters")
            handler = getattr(self._daemon, "plan_body", None)
            report = (await handler(path) if handler else {
                "available": False, "path": path, "body": "",
                "reason": "Dark Army cannot read plans"})
            return 200, "application/json", json.dumps(
                report, allow_nan=False).encode()
        except (ValueError, TypeError) as exc:
            return 400, "application/json", json.dumps(
                {"error": str(exc)}).encode()

    async def _image_for(self, payload):
        """One picture for the sealed `image` kind: `session` (≤ 200
        characters) and `path` (≤ 4096) in the JSON body, both required, a
        400 in words otherwise. An unknown session, a path outside its
        project and a file that is not a picture are each a 200
        `available: false` with the refusal in `reason`
        (`image_preview.locate`)."""
        payload = payload if isinstance(payload, dict) else {}
        session_id = payload.get("session")
        path = payload.get("path")
        if not isinstance(session_id, str) or not session_id \
                or len(session_id) > 200:
            return 400, "application/json", json.dumps(
                {"error": "session is required"}).encode()
        if not isinstance(path, str) or not path.strip():
            return 400, "application/json", json.dumps(
                {"error": "Dark Army needs the picture's path"}).encode()
        if len(path) > image_preview.PATH_MAX_CHARS:
            return 400, "application/json", json.dumps(
                {"error": "a picture path must be at most 4096 characters"}
            ).encode()
        handler = getattr(self._daemon, "image_preview", None)
        report = (await handler(session_id, path) if handler else {
            "available": False, "path": path,
            "reason": "Dark Army cannot show pictures"})
        return 200, "application/json", json.dumps(
            report, allow_nan=False).encode()

    @staticmethod
    def _scout_reports_page_bytes(report):
        """Bound plaintext at 300_000 bytes before sealing —
        `_knowledge_page_bytes`' loop. The list is **newest first**, so
        popping the tail drops the **oldest** rows, which is the right end
        to lose; `omitted` counts them, `truncated` says so, and nothing is
        skipped-and-continued past."""
        rows = report.get("rows")
        if not isinstance(rows, list):
            report["rows"] = []
            rows = report["rows"]
        try:
            omitted = int(report.get("omitted") or 0)
        except (TypeError, ValueError):
            omitted = 0
        while True:
            body = json.dumps(report, allow_nan=False).encode()
            if len(body) <= 300_000 or len(rows) <= 1:
                return body
            rows.pop()
            omitted += 1
            report["truncated"] = True
            report["omitted"] = omitted

    async def _outcome_report_for(self, query):
        import math
        try:
            params = parse_qs(query, keep_blank_values=True)
            if any(len(v) != 1 for v in params.values()):
                raise ValueError("report parameters must not repeat")
            limit = self._outcome_integer(params.get("limit", ["100"])[0])
            offset = self._outcome_integer(params.get("offset", ["0"])[0])
            if limit is None or not 1 <= limit <= 100 or offset is None:
                raise ValueError("limit must be 1..100 and offset nonnegative")
            card_id = params.get("card", [""])[0]
            if card_id:
                if len(card_id) > 200:
                    raise ValueError("invalid card id")
                kwargs = dict(card_id=card_id, limit=limit, offset=offset)
            else:
                root = params.get("root", [""])[0]
                if not root or len(root) > 1024:
                    raise ValueError("a project report needs its canonical root")
                end = float(params.get("to", [str(time.time())])[0])
                start = float(params.get("from", [str(end-30*86400)])[0])
                if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start or end-start > 366*86400:
                    raise ValueError("report needs a finite period of at most 366 days")
                kwargs = dict(root=root, start=start, end=end, limit=limit, offset=offset)
            handler = getattr(self._daemon, "card_outcome_report", None)
            report = await handler(**kwargs) if handler else {"supported": True, "available": False}
            return 200, "application/json", self._outcome_page_bytes(report, offset)
        except (ValueError, TypeError) as exc:
            return 400, "application/json", json.dumps({"error": str(exc)}).encode()

    async def _lifecycle_report_for(self, query):
        import math
        try:
            params = parse_qs(query, keep_blank_values=True)
            if any(len(v) != 1 for v in params.values()):
                raise ValueError("report parameters must not repeat")
            known = {"card", "root", "from", "to", "limit", "offset", "sort",
                     "generation", "as_of"}
            extra = set(params) - known
            if extra:
                raise ValueError("unknown report parameter")
            limit = self._outcome_integer(params.get("limit", ["25"])[0])
            offset = self._outcome_integer(params.get("offset", ["0"])[0])
            if limit is None or not 1 <= limit <= 100 or offset is None:
                raise ValueError("limit must be 1..100 and offset nonnegative")
            card_id = params.get("card", [""])[0]
            root = params.get("root", [""])[0]
            if card_id and root:
                raise ValueError("a report names a card or a project, not both")
            if card_id and len(card_id) > 200:
                raise ValueError("invalid card id")
            if not card_id and (not root or len(root) > 1024):
                raise ValueError("a project report needs its canonical root")
            end = float(params.get("to", [str(time.time())])[0])
            start = float(params.get("from", [str(end - 30 * 86400)])[0])
            if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start or end - start > 366 * 86400:
                raise ValueError("report needs a finite period of at most 366 days")
            as_of = None
            if "as_of" in params:
                as_of = float(params["as_of"][0])
                if not math.isfinite(as_of):
                    raise ValueError("as_of must be a finite UTC timestamp")
            sort = params.get("sort", ["queue"])[0]
            if sort not in ("queue", "execution", "review", "rework"):
                raise ValueError("sort must be a lifecycle category")
            generation = params.get("generation", [None])[0]
            kwargs = dict(start=start, end=end, as_of=as_of, limit=limit,
                          offset=offset, sort=sort, generation=generation)
            if card_id:
                kwargs["card_id"] = card_id
            else:
                kwargs["root"] = root
            handler = getattr(self._daemon, "lifecycle_report", None)
            report = await handler(**kwargs) if handler else {
                "supported": True, "available": False}
            return 200, "application/json", self._lifecycle_page_bytes(
                report, offset)
        except (ValueError, TypeError) as exc:
            return 400, "application/json", json.dumps({"error": str(exc)}).encode()

    async def _agent_report_for(self, query):
        """The sealed `agent_report` read: one period, one optional project.

        A read on both doors — no lease, no action tuple, no
        `remote_activity` record — reached through the same `_sealed_run`, so
        the away door needs no list of its own.
        """
        try:
            params = parse_qs(query, keep_blank_values=True)
            if any(len(v) != 1 for v in params.values()):
                raise ValueError("report parameters must not repeat")
            key = params.get("range", ["30d"])[0]
            if key not in self.HISTORY_RANGES:
                raise ValueError("unknown range")
            root = params.get("root", [""])[0]
            if len(root) > 1024:
                raise ValueError("a project root must be at most 1024 characters")
            offset = self._outcome_integer(params.get("offset", ["0"])[0])
            if offset is None:
                raise ValueError("offset must be a nonnegative whole number")
            handler = getattr(self._daemon, "agent_efficiency_report", None)
            report = (await handler(self.HISTORY_RANGES[key], root) if handler
                      else {"supported": True, "available": False})
            # The offset is a real slice, taken here before the body is
            # bounded. Validating it and then paging from the top would hand a
            # client following `next_offset` page one for ever — the wire has
            # to mean what it says.
            if offset:
                for name in ("agents", "cards"):
                    rows = report.get(name)
                    if isinstance(rows, list):
                        report[name] = rows[offset:]
            report["offset"] = offset
            return 200, "application/json", self._agent_report_page_bytes(
                report, offset)
        except (ValueError, TypeError) as exc:
            return 400, "application/json", json.dumps({"error": str(exc)}).encode()

    async def _history_week_for(self, payload=None):
        """The sealed `history_week` read and its loopback twin: the last
        seven days across the whole Mac, the same `agent_efficiency_report(7,
        "")` call the Mac's `/api/history?range=7d` makes with no project.

        No query: ``payload`` is ignored. The answer is a **projection**, not
        the report: every card, session and leftover day is rebuilt from
        `HISTORY_WEEK_*_KEYS` through `dict.get`, so a key the report did not
        carry (or carried as null) stays absent — never a 0, which would
        price a day at $0.00 — and a stored 0 stays 0. Nothing is priced
        here; the phone folds it with `LedgerWeek`, the Mac's own rule.
        Runs on the daemon loop; the report's store reads already hop to the
        executor.
        """
        handler = getattr(self._daemon, "agent_efficiency_report", None)
        if handler is None:
            body = {"supported": True, "available": False,
                    "reason": "this Mac's Dark Army does not keep the week"}
            return 200, "application/json", json.dumps(body).encode()
        report = await handler(7, "")
        if not isinstance(report, dict) or not report.get("available"):
            reason = ""
            if isinstance(report, dict):
                reason = str(report.get("reason") or "")
            body = {"supported": True, "available": False,
                    "reason": reason or "the week could not be read"}
            return 200, "application/json", json.dumps(body).encode()

        def pick(row, keys):
            out = {}
            for key in keys:
                value = row.get(key) if isinstance(row, dict) else None
                if value is not None:
                    out[key] = value
            return out

        cards = []
        for card in report.get("cards") or []:
            if not isinstance(card, dict):
                continue
            row = pick(card, [k for k in HISTORY_WEEK_CARD_KEYS if k != "sessions"])
            row["sessions"] = [pick(session, HISTORY_WEEK_SESSION_KEYS)
                               for session in card.get("sessions") or []
                               if isinstance(session, dict)]
            cards.append(row)
        days = [pick(day, HISTORY_WEEK_DAY_KEYS)
                for day in report.get("other_days") or []
                if isinstance(day, dict)]
        body = {
            "supported": True,
            "available": True,
            "range_days": 7,
            "from": report.get("from"),
            "to": report.get("to"),
            "generated_at": report.get("generated_at"),
            # The week is a floor, not the whole, when the join was capped
            # (`MAX_JOINED_SESSIONS`) or a card list was cut upstream.
            "partial": bool(report.get("sessions_truncated")
                            or report.get("cards_truncated")),
            "codex_history_partial": bool(report.get("codex_history_partial")),
            "cards": cards,
            "other_days": days,
        }
        return 200, "application/json", self._history_week_page_bytes(body)

    @staticmethod
    def _history_week_page_bytes(body):
        """`_agent_report_page_bytes`' loop over `cards` alone: the same
        300 KB plaintext budget before sealing, for the same reason. Cards
        are halved from the tail until the page fits, and `truncated: True`
        says so — a cut lowers the week's total, so it is stated, never
        inferred. Should the leftover days alone still overflow, their
        session-id lists (a de-duplication aid, not money) go next."""
        cards = body.get("cards") or []
        while True:
            out = json.dumps(body, allow_nan=False).encode()
            if len(out) <= 300_000:
                return out
            if cards:
                keep = len(cards) // 2
                del cards[keep:]
                body["truncated"] = True
                continue
            stripped = False
            for day in body.get("other_days") or []:
                for key in ("claude_token_unpriced_session_ids",
                            "grok_token_unpriced_session_ids",
                            "codex_token_unpriced_session_ids"):
                    if day.pop(key, None) is not None:
                        stripped = True
            if not stripped:
                return json.dumps({
                    "supported": True, "available": False,
                    "reason": "the week exceeds the response limit"}).encode()
            body["truncated"] = True

    @staticmethod
    def _agent_report_page_bytes(report, offset):
        """`_outcome_page_bytes`' sibling, and deliberately not a shared generic.

        Same 300 KB plaintext budget, for the same reason — the sealed
        envelope embeds this JSON as a string, and the budget leaves room for
        doubled escapes, encryption and base64 beneath the phone's wire
        limits. Kept separate because `_outcome_page_bytes`' collection keys
        are pinned by `test_outcome_transport.py`, and refactoring a working
        sealed path to serve two callers buys nothing.

        Both collections are trimmed at the **same** offset, or a page would
        skip rows on one of them.

        Which of them the trim actually *reached* is said separately, per
        collection. `next_offset` alone cannot answer it: the offset is shared,
        `cards` is routinely the long one, and a client that read a bare
        `next_offset` as "your list was cut" would tell a reader their complete
        helper list had been shortened whenever a card list they never draw
        was. `agents_truncated` / `cards_truncated` are absent where nothing
        was removed — absent means whole.
        """
        named = [(key, report[key]) for key in ("agents", "cards")
                 if isinstance(report.get(key), list)]
        while True:
            body = json.dumps(report, allow_nan=False).encode()
            if len(body) <= 300_000:
                return body
            count = max((len(rows) for _, rows in named), default=0)
            if count <= 1:
                return json.dumps({"supported": True, "available": False,
                                   "reason": "Agent report exceeds the response limit."}).encode()
            count = max(1, count // 2)
            for key, rows in named:
                if len(rows) > count:
                    report[f"{key}_truncated"] = True
                del rows[count:]
            report["next_offset"] = offset + count

    @staticmethod
    def _outcome_page_bytes(report, offset):
        """Bound plaintext before sealing, including worst-case string escaping.

        The sealed envelope embeds this JSON as a string. A 300 KB ASCII JSON
        budget leaves room for doubled escapes, encryption/base64 overhead and
        envelope fields beneath the phone's 900 KB wire and inflate limits.
        Trim both parallel card collections at the same offset so no event or
        run is skipped when Unicode evidence makes a count-bounded page huge.
        """
        collections = [report[key] for key in ("events", "cards") if key in report]
        if isinstance(report.get("card"), dict):
            collections.append(report["card"].get("runs", []))
        while True:
            body = json.dumps(report, allow_nan=False).encode()
            if len(body) <= 300_000:
                return body
            count = max((len(rows) for rows in collections), default=0)
            if count <= 1:
                return json.dumps({"supported": True, "available": False,
                                   "reason": "Outcome report exceeds the response limit."}).encode()
            count = max(1, count // 2)
            for rows in collections:
                del rows[count:]
            report["next_offset"] = offset + count

    @staticmethod
    def _lifecycle_page_bytes(report, offset):
        """Bound plaintext before sealing. Trim the one paged collection."""
        key = "episodes" if report.get("card_id") else "cards"
        rows = report.get(key)
        if not isinstance(rows, list):
            rows = None
        while True:
            body = json.dumps(report, allow_nan=False).encode()
            if len(body) <= 300_000:
                return body
            if not rows or len(rows) <= 1:
                return json.dumps({
                    "supported": True, "available": False,
                    "reason": "Lifecycle report exceeds the response limit.",
                }).encode()
            count = max(1, len(rows) // 2)
            del rows[count:]
            report["next_offset"] = offset + count

    def _board_request(self, request: _Request):
        """`(action, payload)` if this is an authorised board write, else None.

        Behind the existing `_authorised` — `X-Bob-Token` **and** the Origin
        allowlist, neither of them re-implemented here. Returning None sends the
        request back down `_route`, which is what answers 403/405/400, so the
        gate exists in exactly one place.
        """
        if request.path != "/api/action" or request.method != "POST":
            return None
        if not self._authorised(request):
            return None
        payload = request.json()
        action = payload.get("action", "")
        if action not in self.BOARD_ACTIONS:
            return None
        return action, payload

    #: Card fields a request may set, and the type each is parsed to. Every
    #: value arrives as a string — `DaemonClient.post` is `[String: String]` —
    #: so the handler parses rather than trusting, and an unlisted key is
    #: dropped instead of reaching the store.
    #:
    #: `session_id`, `link_state`, `dispatched_at` and `session_ended_at` are
    #: **deliberately absent**: they are Dark Army's own bookkeeping, written by the
    #: dispatch and by the reconcile, and a request that could set them could
    #: claim a card was live on a session it has nothing to do with. Clearing
    #: them is the only edit a surface needs and it has its own verb,
    #: `board_reset`.
    #: `workflow` is here and `agent_trail` is not, for the same reason
    #: `link_state` is not: the first is what a card *declares* it expects and
    #: belongs to whoever writes the card, the second is Dark Army's record of what it
    #: actually saw run. A surface that could write the trail could claim a
    #: stage had happened that never did, which is precisely the lie the stage
    #: track is built to be incapable of.
    #: `closed_by` and `close_note` fall on the `agent_trail` side and are
    #: **deliberately absent** for that same reason. They are Dark Army's record of a
    #: statement the session bound to the card made about its own work, written
    #: only by `BoardStore.declare_done`; a surface that could set them could
    #: paint a card somebody dragged across by hand with a verifier's signature,
    #: which is the exact lie. Clearing them is the only edit a surface needs
    #: and it happens automatically when a card leaves Done — Reopen is the undo.
    #: Note that `column_name` **is** here, so a human moving a card to Done goes
    #: on working through `board_update` and is untouched by any of this.
    #: `blocked_by` is **here** again (card dependencies,
    #: `docs/card-dependencies.md`): the cards this one waits on are a thing a
    #: person states about their own card, `model`'s side of the line. The
    #: joined ids ride as one string (`str(...)` below, newline-separated);
    #: a self-wait, a cycle and a card in another project are refused at the
    #: store, in `_update_locked`, so every writer inherits them. `create`
    #: never writes it — a link is set on a card that exists, through
    #: `board_update`. `position` is **not** here: a client sending an
    #: arbitrary float is the thing `board_reorder` exists to prevent.
    #: `plan_path`, `refine_session_id` and `refine_state` are **deliberately
    #: absent** too, each on the side of the line it belongs to: the refine
    #: pair is `session_id`'s case exactly (a request that could set them
    #: could claim a refinement is running that is not), and `plan_path` is
    #: the Prep column's exit condition — a surface that could write it could
    #: stamp a card "planned" and walk it past the plan gate with no plan
    #: behind it. The one writer is `BoardStore.attach_plan`.
    #: `manual_steps` is **deliberately absent**, on `closed_by`'s side of the
    #: line: it is a statement the session that did the work made about its own
    #: work, written only by `BoardStore.flag_manual`. A surface that could set
    #: it could hang a chore on a card nobody flagged — and the badge's whole
    #: value is that it appears only where the session that did the work put it.
    #: Clearing it is the only edit a surface needs and it has its own verb,
    #: `board_manual_clear`, which is `board_unqueue`'s clear-never-set shape.
    #: `reviewed_at` is **deliberately absent** on its own argument: it records
    #: a *human's* acknowledgement of an assistant's close — presumptively the
    #: token-holder's side of the line — but the card sheet saves several
    #: fields on every edit, and a generically writable review field could
    #: ride a draft: a Save must never silently acknowledge a review the
    #: person did not make. The gesture arrives as the named verb
    #: `board_review` instead; the field is closed, the act is open.
    _BOARD_FIELDS = ("title", "summary", "prompt", "project", "root", "tool",
                     "column_name", "author", "workflow",
                     "attachments", "model",
                     # The standing "start it when its plan lands" tick.
                     # Here on `model`'s side of the line: a thing a person
                     # states about their own card. It arms nothing on its
                     # own — the auto-start it later triggers is the
                     # ordinary `dispatch_card`, which re-runs every guard.
                     "start_when_planned",
                     # The importance number, 0..100. `model`'s side of the
                     # line again: a thing a person states about their own
                     # card, and the field Dark Army's own suggestion writes
                     # through the ordinary update.
                     #
                     # **Send a string.** `_board_fields` does
                     # `str(payload.get(key) or "")`, so a client sending the
                     # JSON integer `0` stores `""` — unscored, not zero.
                     # Both clients therefore send `String(...)`.
                     "priority", "area", "kind",
                     "beneficiary", "intended_benefit", "success_criterion", "outcome_check_on",
                     # The cards this one waits on, as one string of
                     # newline-joined ids (comment above).
                     "blocked_by")

    #: Keys in a board payload that are the envelope rather than the card, and
    #: so do not count as "the caller named a field". `skip_plan_gate` is the
    #: plan gate's confirmed-press flag: envelope, not card — a reorder
    #: payload carrying only the flag must not be judged "named fields, all
    #: dropped" and 400 on the press that was supposed to go through.
    #: `refine` is the composer's one-press flag on `board_create`: envelope
    #: for the same reason, and never a card field — `_board_fields` would
    #: drop it anyway, but `_board_named_fields` must not count it.
    #: `create_token` is the same shape: create-only, never a Save field.
    _BOARD_ENVELOPE = ("action", "card_id", "skip_plan_gate",
                       # Per-press override of `board_own_terminal`: spawn
                       # Dark Army's own terminal for this card. Envelope,
                       # not a card field — a payload carrying only this
                       # flag must not be judged "named fields, all dropped".
                       "own_terminal",
                       "refine", "create_token",
                       # The approval's two arguments. Envelope, not card
                       # fields: `plan_approved` is outside `_BOARD_FIELDS`
                       # and always will be, and a payload carrying only
                       # these two must not be judged "named fields, all
                       # dropped" and 400 on the press meant to go through.
                       "plan_path", "plan_digest",
                       "expected_count", "expected_done_token",
                       # The card guard's expected change number and the
                       # press's one-time mark. Envelope for the same
                       # reason: neither is a card field, and a payload
                       # naming only one of them must not be judged "named
                       # fields, all dropped" and 400'd.
                       "expected_revision", "command_token",
                       # The two acknowledgements' current-state echo —
                       # `plan_path` / `plan_digest`'s shape. Envelope, never
                       # card fields: `manual_steps`, `closed_by` and
                       # `close_note` stay outside `_BOARD_FIELDS`, and a
                       # payload naming only these must not be judged
                       # "named fields, all dropped".
                       "expected_manual_steps", "expected_closed_by",
                       "expected_close_note",
                       "expected_outcome_revision", "confirm_outcome_scope_change",
                       # The batch verbs' list of cards, comma-joined
                       # (`_card_ids`). Envelope, never a card field.
                       "card_ids")

    def _board_fields(self, payload: dict) -> dict:
        out = {}
        for key in self._BOARD_FIELDS:
            if key in payload:
                out[key] = payload[key] if key in ("beneficiary", "intended_benefit", "success_criterion", "outcome_check_on") else str(payload.get(key) or "")
        # `author` is not the caller's to state: a card written through the API
        # is written by the person at the keyboard, and the only thing that may
        # claim otherwise is the channel path, which sets it on the daemon side.
        out.pop("author", None)
        if any(k in out for k in ("beneficiary", "intended_benefit", "success_criterion", "outcome_check_on")):
            out["expected_outcome_revision"] = self._outcome_integer(payload.get("expected_outcome_revision"))
            out["confirm_outcome_scope_change"] = str(payload.get("confirm_outcome_scope_change", "")).lower() in ("true", "1")
        # The card guard, the outcome pair's route exactly and with the same
        # one rule: carried **only when the key is present**, never
        # defaulted. A default of 0 would guard every write in the codebase
        # — the reconcile, the auto-filer, the channel verbs — against a
        # value none of them holds, and the board would stop moving. Gated on
        # `out` as well so a payload naming nothing but the expectation stays
        # the "no fields to change" 400 it is today rather than a store
        # refusal about writability.
        if out and "expected_revision" in payload:
            raw = payload.get("expected_revision")
            parsed = self._outcome_integer(raw)
            # A value that will not parse is **not** silently dropped: the
            # key being there says the caller meant to guard, and dropping
            # it would turn a malformed guard into no guard at all. It rides
            # through as something the store's `type(...) is not int` test
            # refuses, in `CARD_CHANGED_REFUSAL`'s words.
            out["expected_revision"] = parsed if parsed is not None \
                else (raw if raw is not None else "")
        return out

    def _board_named_fields(self, payload: dict) -> bool:
        """Did the caller name anything at all beyond the envelope?

        An update that named fields and had **every one** of them dropped is a
        client error, not a success. It used to answer `200 ok:true` — the store
        returns "nothing to change" for an empty update — so a panel sending a
        field this gate does not admit was told the write had landed and the
        card was silently unchanged. The genuinely idempotent case (a field
        named with the value it already holds) is untouched: that field survives
        the allow-list, so the update is not empty.
        """
        return any(k not in self._BOARD_ENVELOPE for k in payload)

    @staticmethod
    def _card_ids(payload: dict):
        """The batch verbs' `card_ids`, parsed once: a comma-joined string
        (`DaemonClient.post` is `[String: String]`), split, stripped, empties
        dropped and duplicates dropped in order. `None` when the value is not
        a string or names no card — the caller's 400. Bounds are the
        daemon's (`board.MAX_BATCH_CARDS`), refused in words there."""
        raw = payload.get("card_ids")
        if not isinstance(raw, str) or len(raw) > 4096:
            return None
        ids: list = []
        for part in raw.split(","):
            cid = part.strip()
            if cid and cid not in ids:
                ids.append(cid)
        return ids or None

    @staticmethod
    def _echo_kwargs(payload: dict, keys: tuple) -> dict:
        """The current-state echo a phone sends with an acknowledgement, as
        keyword arguments for the daemon verb.

        Gated on `"k" in payload` and **never defaulted**: an absent key is
        an absent argument, so the Mac's own `{action, card_id}` press keeps
        today's statement, and a present key rides as `str(... or "")` —
        empty is a real, refusable echo, not a missing one.
        """
        return {k: str(payload.get(k) or "") for k in keys if k in payload}

    async def _board_action(self, action: str, payload: dict):
        """Run one board write and answer with its outcome.

        409 on a refusal, in the shape `delete_agent` uses: the request was
        well-formed and the daemon declined it (a card that has moved, a project
        with no window open, dispatch switched off).
        """
        card_id = str(payload.get("card_id") or "")
        # The plan gate's confirmed-press flag. Every value in a board payload
        # is a string (`DaemonClient.post` is `[String: String]`), so this is
        # parsed rather than trusted, and it reaches only the three verbs that
        # can move a card into In progress — everything else ignores it.
        skip_plan_gate = str(payload.get("skip_plan_gate") or "") \
            .strip().lower() in ("1", "true", "yes")
        # Same shape as `skip_plan_gate`: every board payload value is a
        # string. Absent is ordinary Start (the preference decides). Present
        # and truthy is START HERE — spawn locally even when the preference
        # is off. The drain never comes through this door.
        own_terminal = str(payload.get("own_terminal") or "") \
            .strip().lower() in ("1", "true", "yes")
        # The composer's "and refine it" mark, same shape. Only `board_create`
        # reads it. It is a `board_refine` in disguise, and every door that
        # reaches here (loopback, home, away) allows that verb today; a door
        # that stops allowing it must drop the flag before `_lan_run`.
        refine = str(payload.get("refine") or "") \
            .strip().lower() in ("1", "true", "yes")
        if action == "board_create":
            fields = self._board_fields(payload)
            if "create_token" in payload:
                fields["create_token"] = str(
                    payload.get("create_token") or "").strip()
            if refine:
                card, detail, refine_ok, refine_detail = \
                    await self._daemon.create_card_and_refine(fields)
            else:
                card, detail = await self._daemon.create_card(fields)
                refine_ok, refine_detail = False, ""
            # Status follows the *create* alone: a refused refinement is
            # still a written card, answered 200 with `refine_ok: false`.
            # Neither client decodes the two refine keys (both parse only
            # `detail`); the card's `dispatch_error` is their surface.
            body = json.dumps({"ok": card is not None, "detail": detail,
                               "card_id": (card or {}).get("id", ""),
                               "refine_ok": refine_ok,
                               "refine_detail": refine_detail}).encode()
            return (200 if card is not None else 409), "application/json", body
        if action == "board_clear_done":
            raw_count = payload.get("expected_count")
            if isinstance(raw_count, bool):
                expected_count = None
            elif isinstance(raw_count, int):
                expected_count = raw_count
            elif isinstance(raw_count, str) and raw_count.strip().isdigit():
                expected_count = int(raw_count.strip())
            else:
                expected_count = None
            if expected_count is None or expected_count < 0:
                return (400, "application/json",
                        b'{"error":"expected_count must be a non-negative integer"}')
            expected_token = payload.get("expected_done_token")
            if not (isinstance(expected_token, str)
                    and len(expected_token) == 64
                    and all(ch in "0123456789abcdef"
                            for ch in expected_token)):
                return (400, "application/json",
                        b'{"error":"expected_done_token must be a lowercase SHA-256 digest"}')
            ok, deleted_count, detail = await self._daemon.clear_done_cards(
                expected_count, expected_token)
            body = json.dumps({"ok": ok, "detail": detail,
                               "deleted_count": deleted_count}).encode()
            return (200 if ok else 409), "application/json", body
        if action == "board_start_project":
            # Project-scoped, so it is handled **above** the `no card_id`
            # line: below it the verb would 400 and read as a client bug.
            root = str(payload.get("root") or "").strip()
            if not root:
                return 400, "application/json", b'{"error":"no root"}'
            ok, detail = await self._daemon.start_project(root)
            body = json.dumps({"ok": ok, "detail": detail}).encode()
            return (200 if ok else 409), "application/json", body
        if action in ("knowledge_confirm", "knowledge_stale", "knowledge_edit"):
            # Project+key, never a card. Above the `no card_id` line for
            # `board_start_project`'s reason.
            root = str(payload.get("root") or "").strip()
            key = str(payload.get("key") or "").strip()
            if not root:
                return 400, "application/json", b'{"error":"no root"}'
            if not key:
                return 400, "application/json", b'{"error":"no key"}'
            try:
                if action == "knowledge_confirm":
                    ok, detail = await self._daemon.knowledge_confirm(root, key)
                elif action == "knowledge_stale":
                    ok, detail = await self._daemon.knowledge_mark_stale(
                        root, key)
                else:
                    ok, detail = await self._daemon.knowledge_edit(
                        root, key,
                        str(payload.get("question") or ""),
                        str(payload.get("answer") or ""))
            except ValueError as exc:
                body = json.dumps({"ok": False, "error": str(exc),
                                   "detail": str(exc)}).encode()
                return 400, "application/json", body
            body = json.dumps({"ok": ok, "detail": detail}).encode()
            return (200 if ok else 409), "application/json", body
        if action == "board_manual_outcome":
            # Keyed on the check file, never a card: a check may outlive or
            # predate its card. Above the `no card_id` line for
            # `board_start_project`'s reason. The daemon re-checks the place,
            # the shape and `Status: open` at the write.
            path = str(payload.get("path") or "").strip()
            outcome = str(payload.get("status") or "").strip().lower()
            if not path or len(path) > 1024:
                return 400, "application/json", b'{"error":"no path"}'
            if outcome not in manual_check.OUTCOMES:
                return (400, "application/json",
                        b'{"error":"status must be passed or failed"}')
            note = str(payload.get("note") or "")[
                :board.MAX_MANUAL_OUTCOME_CHARS]
            ok, detail = await self._daemon.record_manual_outcome(
                path, outcome, note)
            body = json.dumps({"ok": ok, "detail": detail}).encode()
            return (200 if ok else 409), "application/json", body
        if action == "board_refine_batch":
            # Several cards, one session: keyed on `card_ids`, so handled
            # **above** the `no card_id` line for `board_start_project`'s
            # reason.
            ids = self._card_ids(payload)
            if ids is None:
                return 400, "application/json", b'{"error":"no card_ids"}'
            ok, detail = await self._daemon.refine_cards(ids)
            body = json.dumps({"ok": ok, "detail": detail}).encode()
            return (200 if ok else 409), "application/json", body
        if action == "board_start_batch":
            # `board_refine_batch`'s shape for the Backlog row: one session,
            # several planned cards, worked one at a time (`start_cards`).
            ids = self._card_ids(payload)
            if ids is None:
                return 400, "application/json", b'{"error":"no card_ids"}'
            ok, detail = await self._daemon.start_cards(ids)
            body = json.dumps({"ok": ok, "detail": detail}).encode()
            return (200 if ok else 409), "application/json", body
        if not card_id:
            return 400, "application/json", b'{"error":"no card_id"}'
        if action == "board_promote":
            card, detail = await self._daemon.promote_card(card_id)
            body = json.dumps({
                "ok": card is not None, "detail": detail,
                "card_id": (card or {}).get("id", ""),
            }).encode()
            return (200 if card is not None else 409), "application/json", body
        if action in ("board_accept_outcome", "board_request_revision"):
            result, detail = await self._daemon.decide_card_outcome(
                card_id, self._outcome_integer(payload.get("expected_outcome_revision")),
                payload.get("request_key"), payload.get("evidence"),
                accept=action == "board_accept_outcome")
            body = json.dumps({"ok": result is not None, "detail": detail, **(result or {})}).encode()
            return (200 if result is not None else 409), "application/json", body
        if action == "board_update":
            fields = self._board_fields(payload)
            if not fields:
                # Named nothing, or named only things this gate does not admit.
                # Either way there is no write to do and saying `ok` would be a
                # lie the caller acts on.
                detail = ("no writable card fields in that update"
                          if self._board_named_fields(payload)
                          else "no fields to change")
                return 400, "application/json", json.dumps(
                    {"ok": False, "error": detail, "detail": detail}).encode()
            card, detail = await self._daemon.update_card(
                card_id, fields, allow_unplanned=skip_plan_gate)
            answer = {"ok": card is not None, "detail": detail,
                      "revision": (card or {}).get("revision"),
                      "outcome_revision": (card or {}).get("outcome_revision")}
            if card is None and detail == board.CARD_CHANGED_REFUSAL:
                # The "show both" half: the refusal is routinely the first
                # thing a phone hears after being out of signal, and a
                # second fetch may not be possible. Only on *this* refusal,
                # and only the stated fields — the body is sealed.
                answer["current"] = await self._daemon.card_stated_fields(card_id)
            body = json.dumps(answer).encode()
            return (200 if card is not None else 409), "application/json", body
        if action == "board_reset":
            card, detail = await self._daemon.reset_card(
                card_id, self._board_fields(payload))
            body = json.dumps({"ok": card is not None, "detail": detail}).encode()
            return (200 if card is not None else 409), "application/json", body
        if action == "board_delete":
            ok, detail = await self._daemon.delete_card(card_id)
            body = json.dumps({"ok": ok, "detail": detail}).encode()
            return (200 if ok else 409), "application/json", body
        if action == "board_reorder":
            column = str(payload.get("column_name") or "")
            before_id = str(payload.get("before_id") or "")
            # Anything else the drop changed — the lane, today — rides the
            # same store write as the slot, so a card can never land in the
            # right column and the wrong folder. `_BOARD_FIELDS` gates what
            # may arrive; the column and the slot travel as themselves.
            extra = self._board_fields(payload)
            extra.pop("column_name", None)
            card, detail = await self._daemon.reorder_card(
                card_id, column, before_id, allow_unplanned=skip_plan_gate,
                fields=extra)
            body = json.dumps({"ok": card is not None, "detail": detail}).encode()
            return (200 if card is not None else 409), "application/json", body
        if action == "board_dispatch":
            ok, detail = await self._daemon.dispatch_card(
                card_id, allow_unplanned=skip_plan_gate,
                own_terminal=own_terminal)
            body = json.dumps({"ok": ok, "detail": detail}).encode()
            return (200 if ok else 409), "application/json", body
        if action == "board_refine":
            ok, detail = await self._daemon.refine_card(card_id)
            body = json.dumps({"ok": ok, "detail": detail}).encode()
            return (200 if ok else 409), "application/json", body
        if action == "board_unqueue":
            # Clear-never-set, `board_reset`'s precedent: `queue_state` and
            # `queued_at` are absent from `_BOARD_FIELDS`, so a surface can
            # take a card out of the queue and can never put one in — a
            # surface that could set them would be manufacturing a claim on
            # Dark Army's future auto-start, or reordering somebody else's queue.
            ok, detail = await self._daemon.unqueue_card(card_id)
            body = json.dumps({"ok": ok, "detail": detail}).encode()
            return (200 if ok else 409), "application/json", body
        if action == "board_queue_move":
            # The other half of `board_unqueue`'s asymmetry: a surface may
            # now reorder cards already queued, and still may never put
            # one in — `queue_state` / `queued_at` / `queue_rank` all stay
            # out of `_BOARD_FIELDS`, and the verb's store guard refuses
            # any card that is not `queue_state = 'queued'`. `before_id`
            # is not in `_BOARD_ENVELOPE`; this verb does not go through
            # `_board_fields`.
            before_id = str(payload.get("before_id") or "")
            ok, detail = await self._daemon.move_queued_card(
                card_id, before_id)
            body = json.dumps({"ok": ok, "detail": detail}).encode()
            return (200 if ok else 409), "application/json", body
        if action == "board_manual_clear":
            # Clear-never-set, `board_unqueue`'s precedent exactly:
            # `manual_steps` is absent from `_BOARD_FIELDS`, so a surface can
            # say a hand-check has been done and can never claim one is
            # outstanding — a surface that could set it would be hanging a
            # chore on somebody else's card, and the badge would stop meaning
            # that the session which did the work asked for the check.
            #
            # The phone's echo of the steps it drew. Read with `"k" in
            # payload`, `expected_revision`'s rule: absent means no guard
            # — the Mac's own press sends none, and a default of `""` would
            # turn every panel press into a stale-empty refusal.
            ok, detail = await self._daemon.clear_manual_check(
                card_id, **self._echo_kwargs(
                    payload, ("expected_manual_steps",)))
            body = json.dumps({"ok": ok, "detail": detail}).encode()
            return (200 if ok else 409), "application/json", body
        if action == "board_ask":
            text = str(payload.get("text") or "")
            ok, detail = await self._daemon.ask_card(card_id, text)
            body = json.dumps({"ok": ok, "detail": detail}).encode()
            return (200 if ok else 409), "application/json", body
        if action == "board_message":
            # `board_ask`'s sibling and a different act: this types the words
            # onto the input line of the session already working the card,
            # rather than starting a helper or pushing down a channel. Every
            # bound — empty, one line, a leading slash, the length, the open
            # permission prompt — is the daemon's, so this branch adds none
            # of its own and hands the text over unfiltered.
            text = str(payload.get("text") or "")
            ok, detail = await self._daemon.message_card(card_id, text)
            body = json.dumps({"ok": ok, "detail": detail}).encode()
            return (200 if ok else 409), "application/json", body
        if action == "board_review":
            # The verb is open where the field is closed: `reviewed_at` is
            # deliberately absent from `_BOARD_FIELDS` (a card-sheet Save that
            # could carry it would silently acknowledge a review the person
            # did not make), so a human's acknowledgement arrives only as this
            # named gesture, and the one-way transition's race answer is the
            # WHERE clause in `BoardStore.mark_reviewed` itself.
            #
            # The phone's echo of the close it drew, both halves, on the
            # same present-key rule as `expected_manual_steps` above; the
            # store refuses half a pair before any UPDATE.
            card, detail = await self._daemon.review_card(
                card_id, **self._echo_kwargs(
                    payload, ("expected_closed_by", "expected_close_note")))
            body = json.dumps({"ok": card is not None,
                               "detail": detail}).encode()
            return (200 if card is not None else 409), "application/json", body
        if action == "board_approve_plan":
            # The verb is open where the field is closed, `board_review`'s
            # own shape: `plan_approved` / `plan_approved_at` are absent from
            # `_BOARD_FIELDS` and written by `approve_plan` alone, so a card
            # can never be shown as approved against a plan nobody read. Both
            # arguments come off the payload rather than through
            # `_board_fields` — they are an echo of what the card read handed
            # back, not card state the caller is setting.
            card, detail = await self._daemon.approve_card_plan(
                card_id, str(payload.get("plan_path") or ""),
                str(payload.get("plan_digest") or ""))
            body = json.dumps({"ok": card is not None,
                               "detail": detail}).encode()
            return (200 if card is not None else 409), "application/json", body
        return 400, "application/json", b'{"error":"unknown board action"}'

    #: Ranges the Done archive offers. Days, or None for everything on record.
    BOARD_RANGES = {"30d": 30, "90d": 90, "all": None}

    async def _board_report(self, request: _Request):
        """The Done archive, on demand.

        An ungated read like `/api/state`, and defensible for the same reason:
        `_loopback_host` runs above the routing table in `_handle_client`, so a
        rebound DNS name never reaches this. Served from the executor because it
        is SQLite.
        """
        params = dict(
            pair.split("=", 1) for pair in request.query.split("&") if "=" in pair
        )
        # The whole Done column, in the snapshot's own shape — what a client
        # that opted into `?done=review` fetches once and holds. Distinct from
        # `?range=`, which serves raw store rows: a client splicing these into
        # `board.cards` needs the frame's shape or it needs a second decode
        # path and two ideas of what a card is.
        column = params.get("column")
        if column is not None:
            if column != "done":
                return 400, "application/json", json.dumps(
                    {"error": "unknown column", "allowed": ["done"]}).encode()
            return 200, "application/json", json.dumps(
                await self._done_archive()).encode()
        key = params.get("range", "30d")
        if key not in self.BOARD_RANGES:
            return 400, "application/json", json.dumps(
                {"error": "unknown range", "allowed": sorted(self.BOARD_RANGES)}
            ).encode()
        days = self.BOARD_RANGES[key]
        store = getattr(self._daemon, "_board", None)
        if store is None:
            return 200, "application/json", json.dumps(
                {"available": False, "range": key, "cards": [],
                 "reason": "the board is not open"}).encode()
        since = 0.0 if days is None else time.time() - days * 86400
        # One card, in full. The live snapshot carries a *preview* of every
        # prompt (`BOARD_SNAPSHOT_PROMPT_CHARS`) because the whole text on every
        # SSE frame is what made that payload 8x its budget; the editor needs the
        # real thing before it may save, and this is where it gets it.
        # Deliberately not a new endpoint: this one already exists to serve what
        # the frame cannot carry, and that is exactly the job.
        card_id = str(params.get("card") or "")

        def collect() -> dict:
            if card_id:
                return self._card_collect(card_id, with_plan=False, range_key=key,
                                          with_timeline=True)
            return {"available": True, "range": key,
                    "generated_at": time.time(),
                    "cards": store.done_since(since, limit=1000)}

        report = await asyncio.get_running_loop().run_in_executor(None, collect)
        return 200, "application/json", json.dumps(report).encode()

    def _card_collect(self, card_id: str, *, with_plan: bool,
                      range_key: str = "30d", with_timeline: bool = False) -> dict:
        """One card, in full. **Blocking (SQLite, and with a plan two stats
        and a bounded read) — executor only.**

        Two shapes from one place, because the loopback read and the sealed
        read want the same card and must never drift into two answers about
        it. `with_plan=False` is byte-for-byte today's `GET /api/board?card=`
        payload — the card with its `messages` attached. `with_plan=True`
        omits `messages` (the phone has no surface for them) and adds the plan
        document itself, whose absence is **stated rather than inferred**: a
        card with no `plan_path`, or one whose path fails the daemon's own
        containment rule, comes back `available: false` carrying that rule's
        own words as `reason`, never an empty string a client has to guess at.

        Asking for the timeline adds it (`card_timeline.compose`) as a
        **top-level sibling of `plan`**, never on the card dict, so no
        card-shaped decoder or snapshot sees it. Only the two on-open readers
        ask for it; the phone's background sweep (`_cards_sync_page`) does
        not, so nothing gets heavier per frame.
        """
        store = getattr(self._daemon, "_board", None)
        if store is None:
            return {"available": False, "range": range_key,
                    "generated_at": time.time(), "cards": [],
                    "reason": "the board is not open"}
        card = store.get(card_id)
        if card is not None and not with_plan:
            messages = []
            peek = self._daemon._identities.peek
            for row in store.messages(card_id):
                item = dict(row)
                author = str(item.get("author") or "")
                if author in ("", "user"):
                    item["author_name"] = ""
                else:
                    item["author_name"] = peek(author)
                messages.append(item)
            card["messages"] = messages
            # What Dark Army observed the last run do, in full — the closing words
            # and the file list the snapshot's eight-scalar headline
            # deliberately does not carry. Attached to the fetch the sheet
            # already makes, so reading a card costs no second request; only
            # one file's diff does.
            try:
                full = store.run_for(card_id)
            except Exception:
                logger.debug("could not read the work record for %s",
                             card_id[:8], exc_info=True)
                full = None
            if isinstance(full, dict) and full.get("recorded_at"):
                # The helper's sentence is the daemon's, composed once
                # (`work_record.shunt_words`); both clients draw it verbatim
                # and count nothing themselves.
                full["shunt_words"] = work_record.shunt_words(full)
                card["work_record_full"] = full
        report = {"available": True, "range": range_key,
                  "generated_at": time.time(),
                  "cards": [card] if card else []}
        if with_plan:
            report["plan"] = self._card_plan(card)
            report["report"] = self._card_report(card)
            report["manual_check"] = self._card_manual_check(card)
        if with_timeline:
            report["timeline"] = self._card_timeline(card)
        return report

    def _card_timeline(self, card) -> dict:
        """The card's timeline, for the two on-open reads. **Blocking —
        executor only**, and reached only from `_card_collect`.

        One store read under `BoardStore._lock` (`card_timeline_facts`),
        the diary's recent rows (its own lock, copies), then the pure
        composition. Any failure is stated rather than raised: the card read
        the timeline rides on must still land.
        """
        if not isinstance(card, dict):
            return card_timeline.unavailable("no such card")
        try:
            store = self._daemon._board
            facts = store.card_timeline_facts(str(card.get("id") or ""))
            log = getattr(self._daemon, "_event_log", None)
            rows = log.recent() if log is not None else []
            return card_timeline.compose(card, facts, rows, time.time())
        except Exception:
            logger.debug("could not read the timeline for %s",
                         str(card.get("id") or "")[:8], exc_info=True)
            return card_timeline.unavailable("the timeline could not be read")

    def _card_plan(self, card) -> dict:
        """The plan document a card points at, for the sealed read.
        **Blocking — executor only**, and reached only from `_card_collect`.

        Reuses the daemon's `_plan_path_refusal` verbatim: there is exactly
        one containment rule for a plan path in this codebase and a second one
        here is how they drift. `digest` is what an approval echoes back, so
        it is computed off the same bytes that were read out.
        """
        if not isinstance(card, dict):
            return {"available": False, "path": "", "text": "", "digest": "",
                    "reason": "no such card"}
        path = str(card.get("plan_path") or "")
        if not path:
            return {"available": False, "path": "", "text": "", "digest": "",
                    "reason": "this card has no plan"}
        resolved, refusal = daemon_board.BoardVerbsMixin._plan_path_refusal(
            str(card.get("root") or ""), path)
        if refusal:
            return {"available": False, "path": path, "text": "",
                    "digest": "", "reason": refusal}
        text = board_workflow.read_plan_text(Path(resolved))
        if not text:
            return {"available": False, "path": path, "text": "",
                    "digest": "", "reason": "that plan could not be read"}
        return {"available": True, "path": path, "text": text,
                "digest": daemon_board._plan_digest(resolved), "reason": ""}

    def _card_report(self, card) -> dict:
        """A scout's written report, for the sealed read — `_card_plan`'s
        twin on `report_path`, the same containment rule (a Markdown file
        inside the card's own project) and the same stated `available`.
        **Blocking — executor only**, reached only from `_card_collect`.
        Until 21 Sep 2026 the phone was handed the path alone and drew it
        as a line of text; a scout is an investigation ending in a report,
        so the report is what its card shows.
        """
        if not isinstance(card, dict):
            return {"available": False, "path": "", "text": "",
                    "reason": "no such card"}
        path = str(card.get("report_path") or "")
        if not path:
            return {"available": False, "path": "", "text": "",
                    "reason": "this card has no report"}
        resolved, refusal = daemon_board.BoardVerbsMixin._plan_path_refusal(
            str(card.get("root") or ""), path)
        if refusal:
            return {"available": False, "path": path, "text": "",
                    "reason": refusal.replace("a plan", "a report")
                    .replace("the plan", "the report")}
        text = board_workflow.read_plan_text(Path(resolved))
        if not text:
            return {"available": False, "path": path, "text": "",
                    "reason": "that report could not be read"}
        return {"available": True, "path": path, "text": text, "reason": ""}

    def _card_manual_check(self, card) -> dict:
        """The check file a card was flagged with, for the sealed read —
        `_card_report`'s twin on `manual_check_path`, the flag's own
        containment and shape rule (`_manual_check_path_refusal`) and the same
        stated `available`. **Blocking — executor only**, reached only from
        `_card_collect`."""
        if not isinstance(card, dict):
            return {"available": False, "path": "", "text": "",
                    "reason": "no such card"}
        path = str(card.get("manual_check_path") or "")
        if not path:
            return {"available": False, "path": "", "text": "",
                    "reason": "this card has no manual check file"}
        resolved, refusal = \
            daemon_board.BoardVerbsMixin._manual_check_path_refusal(
                str(card.get("root") or ""), path)
        if refusal:
            return {"available": False, "path": path, "text": "",
                    "reason": refusal}
        text = manual_check.read_text(resolved)
        if not text:
            return {"available": False, "path": path, "text": "",
                    "reason": "that check could not be read"}
        return {"available": True, "path": path, "text": text, "reason": ""}

    async def _card_report_for(self, query: str):
        """`GET`-shaped card read for the sealed doors: `card=<id>` on the
        query string, `_log_report_for`'s discipline — 400 on a malformed
        query, `available` stated. The whole read runs in one executor hop."""
        params = dict(
            pair.split("=", 1) for pair in (query or "").split("&") if "=" in pair
        )
        card_id = unquote(str(params.get("card") or ""))
        if not card_id or len(card_id) > 200:
            return 400, "application/json", json.dumps(
                {"error": "card must be a card id"}).encode()
        report = await asyncio.get_running_loop().run_in_executor(
            None, functools.partial(self._card_collect, card_id, with_plan=True,
                                    with_timeline=True))
        return 200, "application/json", json.dumps(report).encode()

    def _cards_sync_page(self, ids, plan_stamps, budget: int) -> dict:
        """Many cards, in full, plus the plan documents that moved.
        **Blocking (SQLite, and per plan two stats and a bounded read) —
        executor only, one hop for the whole page.**

        The batch body is `_card_collect(with_plan=True)` **per id** and
        deliberately nothing else: there is one reader for a card and its
        plan in this codebase and a second one here is how they drift. Every
        containment decision therefore stays inside `daemon_board`'s
        `_plan_path_refusal` and every plan byte still comes off
        `board_workflow`'s own bounded plan reader.

        `plan_stamps` is `{card_id: sha256hex}` — what the caller already
        holds. A plan whose digest matches is **omitted**, which is what
        makes a re-check of 50 unchanged plans cost 50 stats and zero bytes
        on the wire. A plan that is absent is *stated, never inferred*: the
        refusal's own words ride as `reason`, and a card with no `plan_path`
        says "this card has no plan".

        The budget is `_outcome_page_bytes`' 300 KB, for its reason. A plan
        is at most `MAX_PLAN_BYTES` (64 KiB) and a prompt at most
        `MAX_PROMPT_CHARS` (8 000), so a single item always fits and there is
        no degenerate "cannot serve one row" case — stated here rather than
        written as an unreachable branch. Anything the budget did not reach
        is listed in `unserved` with `more: true`, and the caller asks again.
        """
        cards: list = []
        plans: list = []
        unserved: list = []
        stamps = plan_stamps if isinstance(plan_stamps, dict) else {}
        # The envelope's own cost, so `more` is decided against what actually
        # goes on the wire rather than against the rows alone.
        used = 200
        stopped = False
        for card_id in ids:
            if stopped:
                unserved.append(card_id)
                continue
            report = self._card_collect(card_id, with_plan=True)
            row = (report.get("cards") or [None])[0]
            if not isinstance(row, dict):
                # An unknown id is dropped rather than fatal: a card deleted
                # between the snapshot and this read is an ordinary event.
                continue
            plan = report.get("plan") or {}
            item = {"card_id": card_id, "path": str(plan.get("path") or ""),
                    "available": bool(plan.get("available")),
                    "digest": str(plan.get("digest") or ""),
                    "reason": str(plan.get("reason") or ""),
                    "text": str(plan.get("text") or "")}
            if item["available"] and item["digest"] \
                    and stamps.get(card_id) == item["digest"]:
                item["text"] = ""
                item["unchanged"] = True
            cost = len(json.dumps(row)) + len(json.dumps(item))
            if cards and used + cost > budget:
                stopped = True
                unserved.append(card_id)
                continue
            used += cost
            cards.append(row)
            plans.append(item)
        # Plans the caller asked about that were not in `ids` at all: the
        # digest re-check, which is the only way the phone can ever see a
        # plan file edited under a card whose fields never moved.
        #
        # **`unserved` names each id at most once, and never one already
        # served.** An id in both `ids` and `plans` is finished by the first
        # loop, so the second must skip it whether it was served *or*
        # deferred — appending it twice, or serving its plan while its card
        # row sits in `unserved`, would make the paging contract say two
        # different things about one card.
        asked = set(ids)
        deferred = set(unserved)
        for card_id, digest in stamps.items():
            if card_id in asked:
                continue
            if stopped:
                if card_id not in deferred:
                    deferred.add(card_id)
                    unserved.append(card_id)
                continue
            if any(p["card_id"] == card_id for p in plans):
                continue
            row = self._card_collect(card_id, with_plan=True)
            plan = row.get("plan") or {}
            if not (row.get("cards") or []):
                continue
            item = {"card_id": card_id, "path": str(plan.get("path") or ""),
                    "available": bool(plan.get("available")),
                    "digest": str(plan.get("digest") or ""),
                    "reason": str(plan.get("reason") or ""),
                    "text": str(plan.get("text") or "")}
            if item["available"] and item["digest"] == str(digest or ""):
                # Unchanged: say so and send none of it.
                item["text"] = ""
                item["unchanged"] = True
            cost = len(json.dumps(item))
            if used + cost > budget and plans:
                stopped = True
                if card_id not in deferred:
                    deferred.add(card_id)
                    unserved.append(card_id)
                continue
            used += cost
            plans.append(item)
        return {"available": True, "generated_at": time.time(),
                "cards": cards, "plans": plans,
                "unserved": unserved, "more": bool(unserved)}

    async def _card_sync_for(self, payload: dict):
        """The sealed delta read: `ids=<id>,<id>…&plans=<id>:<sha256hex>,…`.

        `_log_report_for`'s query discipline — `parse_qs`, 400 in words on a
        repeated key, an over-long id or too many of either; a malformed
        stamp is **dropped, never fatal**, because one bad entry must not
        cost the phone a whole page. The whole read is one executor hop.
        """
        query = str((payload or {}).get("query") or "")
        try:
            params = parse_qs(query, keep_blank_values=True)
            if any(len(v) != 1 for v in params.values()):
                raise ValueError("card_sync parameters must not repeat")
            raw_ids = [p.strip() for p in
                       (params.get("ids", [""])[0] or "").split(",")]
            ids: list = []
            for item in raw_ids:
                if not item:
                    continue
                if len(item) > 200:
                    raise ValueError("invalid card id")
                if item not in ids:
                    ids.append(item)
            if len(ids) > CARD_SYNC_MAX_IDS:
                raise ValueError(
                    f"at most {CARD_SYNC_MAX_IDS} cards in one sync")
            stamps: dict = {}
            raw_plans = [p.strip() for p in
                         (params.get("plans", [""])[0] or "").split(",")]
            for item in raw_plans:
                if not item or ":" not in item:
                    continue
                card_id, _, digest = item.partition(":")
                card_id = card_id.strip()
                digest = digest.strip()
                if not card_id or len(card_id) > 200 or len(digest) != 64:
                    continue
                if not all(ch in "0123456789abcdef" for ch in digest):
                    continue
                stamps[card_id] = digest
            if len(stamps) > CARD_SYNC_MAX_IDS:
                raise ValueError(
                    f"at most {CARD_SYNC_MAX_IDS} plan stamps in one sync")
        except (ValueError, TypeError) as exc:
            return 400, "application/json", json.dumps(
                {"error": str(exc)}).encode()
        report = await asyncio.get_running_loop().run_in_executor(
            None, functools.partial(self._cards_sync_page, ids, stamps,
                                    CARD_SYNC_MAX_BYTES))
        return 200, "application/json", json.dumps(report).encode()

    async def _done_archive(self) -> dict:
        """The finished column in the frame's own shape, off the executor.

        One place, so the loopback read and the sealed read can never drift
        into two answers about the same column. An older daemon shape — a
        fake in a test, a mixin not inherited — answers "not available"
        rather than raising, `_board_report`'s own rule for a shut board.
        """
        builder = getattr(self._daemon, "done_archive_cards", None)
        if builder is None:
            return {"available": False, "cards": [], "count": 0,
                    "generated_at": time.time(), "more": False,
                    "done_clear_token": "", "done_view_token": "",
                    "reason": "the board is not open"}
        return await asyncio.get_running_loop().run_in_executor(None, builder)

    async def _done_archive_for(self, payload: dict):
        """The sealed `done` read: the finished column, paged by bytes.

        `_outcome_page_bytes`' bound and `_outcome_page_bytes`' reason — the
        sealed envelope embeds this JSON as a string, so 300 KB of ASCII JSON
        leaves room for doubled escapes, encryption and base64 beneath the
        phone's wire limit. `offset` is the caller's place in the card list;
        the answer says `more` and `next_offset` when it had to cut.
        """
        try:
            offset = int(payload.get("offset") or 0)
        except (TypeError, ValueError):
            return 400, "application/json", json.dumps(
                {"error": "offset must be a whole number"}).encode()
        if offset < 0:
            return 400, "application/json", json.dumps(
                {"error": "offset must be a whole number"}).encode()
        report = await self._done_archive()
        cards = report.get("cards") or []
        report["cards"] = cards[offset:]
        body = self._outcome_page_bytes(report, offset)
        report = json.loads(body.decode())
        report["more"] = bool(report.get("next_offset")) or bool(
            report.get("more"))
        return 200, "application/json", json.dumps(report).encode()

    async def _wrap_up(self, session_id: str):
        ok, detail = await self._daemon.wrap_up_or_close_session(session_id)
        body = json.dumps({"ok": ok, "detail": detail}).encode()
        return (200 if ok else 409), "application/json", body

    async def _close_terminal(self, session_id: str, by_person: bool = False):
        ok, detail = await self._daemon.close_session_terminal(
            session_id, by_person=by_person)
        body = json.dumps({"ok": ok, "detail": detail}).encode()
        return (200 if ok else 409), "application/json", body

    async def _low_priority(self, session_id: str):
        ok, detail = await self._daemon.low_priority_session(session_id)
        body = json.dumps({"ok": ok, "detail": detail}).encode()
        return (200 if ok else 409), "application/json", body

    async def _terminal_input(self, session_id: str, text: str, *,
                              from_phone: bool, raw: bool = False,
                              data: Optional[bytes] = None):
        """One line (or a raw keystroke burst) into a Dark Army-owned terminal.
        409 on a refusal, in the daemon's own words. `from_phone` is
        decided by the door, never by the payload: the sealed doors
        always say True, the loopback intercept always False. Raw bytes
        are the panel's native terminal; the phone stays line-oriented."""
        ok, detail = await self._daemon.terminal_input(
            session_id, text, from_phone=from_phone, raw=raw, data=data)
        body = json.dumps({"ok": ok, "detail": detail}).encode()
        return (200 if ok else 409), "application/json", body

    async def _serve_terminal_stream(self, request: _Request, reader, writer) -> None:
        """The loopback stream route (`GET`, `?session=<id>`): a 409 in
        words when Dark Army does not host that session, else a `200` whose body
        is the framed stream until either side hangs up. `PlainCodec`, no
        rate cap — byte-for-byte the panel's contract."""
        multi = parse_qs(request.query or "", keep_blank_values=True)
        session_id = ""
        if len(multi.get("session", [])) == 1:
            session_id = str(multi["session"][0])
        if not session_id or len(session_id) > 200:
            await self._respond(writer, 400, "application/json", json.dumps(
                {"error": "session is required"}).encode())
            return
        handle, paint, exited, refusal = self._daemon.terminal_attach(session_id)
        if handle is None:
            await self._respond(writer, 409, "application/json", json.dumps(
                {"ok": False, "detail": refusal}).encode())
            return
        writer.write((
            "HTTP/1.1 200 OK\r\n"
            f"Content-Type: {terminal_stream.CONTENT_TYPE}\r\n"
            "Cache-Control: no-store\r\n"
            "X-Frame-Options: DENY\r\n"
            "Content-Security-Policy: frame-ancestors 'none'\r\n"
            "Connection: keep-alive\r\n\r\n").encode("latin-1"))
        await writer.drain()
        daemon = self._daemon
        self._stream_writers.add(writer)
        try:
            await terminal_stream.serve(
                daemon._pty, handle, paint, exited, reader, writer,
                on_input=lambda blob: daemon.terminal_stream_input(handle, blob),
                on_size=lambda c, r: daemon.terminal_stream_resize(handle, c, r))
        finally:
            self._stream_writers.discard(writer)

    #: Held-open terminal streams one paired phone may have at once; a new
    #: one hangs up on the old.
    MAX_LAN_STREAMS_PER_DEVICE = 1

    async def _serve_lan_terminal_stream(self, request: _Request, reader,
                                         writer) -> None:
        """The phone door's terminal stream (`POST`, the same path as the
        loopback `GET`): the body is one sealed `terminal_stream` frame
        (`body.session`), verified by `_home_open` — the one verifier — and
        the answer is the head with `SEALED_CONTENT_TYPE` followed by the
        framed stream under `SealedCodec(key, frame id)` until either side
        hangs up.

        Nothing is sent before the frame verifies: an unpaired channel is a
        plain 403 with no head; a frame of another kind, and a session Dark Army
        does not host, are sealed `err`s (200 `text/plain`, the status
        inside) that only the keyholder can read. The head is written only
        after `terminal_attach` succeeded. One stream per device: a second
        closes the first. Keys go to `terminal_stream_input` (the desk's
        rules), sizes to `terminal_phone_resize` (honoured only while the
        panel's pane is not showing the session), and the up-frame rate is
        capped at `STREAM_MAX_UP_FRAMES_PER_MINUTE`.
        """
        wire = request.body.decode("ascii", "replace").strip()
        device_id, key, frame = self._home_open(
            request, wire=wire, expect_kind="terminal_stream")
        if device_id is None:
            await self._respond(writer, *frame)
            return
        body = frame.get("body")
        body = body if isinstance(body, dict) else {}
        session_id = str(body.get("session") or "")
        stream_id = str(frame.get("id") or "")
        if not session_id or len(session_id) > 200 or not stream_id:
            answer = self._home_answer(device_id, key, {
                "re": stream_id, "status": 400,
                "error": "session is required"}, kind="err")
            await self._respond(writer, 200, "text/plain", answer)
            return
        bot_refusal = self._bot_refusal(device_id, "read")
        if bot_refusal:
            # Watching a terminal is a read: the bot's Read grant decides,
            # before anything is attached.
            answer = self._home_answer(device_id, key, {
                "re": stream_id, "status": 403,
                "error": bot_refusal}, kind="err")
            await self._respond(writer, 200, "text/plain", answer)
            return
        handle, paint, exited, refusal = self._daemon.terminal_attach(session_id)
        if handle is None:
            answer = self._home_answer(device_id, key, {
                "re": stream_id, "status": 409, "error": refusal}, kind="err")
            await self._respond(writer, 200, "text/plain", answer)
            return
        # The bound is the constant, read here: the eviction used to close
        # "the previous one" and the number beside it was decoration.
        open_streams = [w for w in self._lan_streams.get(device_id, [])
                        if w is not writer]
        while len(open_streams) >= self.MAX_LAN_STREAMS_PER_DEVICE:
            oldest = open_streams.pop(0)
            try:
                oldest.close()
            except OSError:
                pass
        open_streams.append(writer)
        self._lan_streams[device_id] = open_streams
        if devices.is_bot(device_id):
            self._arm_bot_expiry_from_store()
        # **The `try` opens on the same statement as the registration.** The
        # drain below awaits, so a phone that vanished between the admit and
        # the head raises here — and a `finally` that started after it would
        # leave this writer in `_lan_streams` for ever, spending one of the
        # device's `MAX_LAN_STREAMS_PER_DEVICE` places on a dead socket until
        # the daemon restarts.
        try:
            writer.write((
                "HTTP/1.1 200 OK\r\n"
                f"Content-Type: {terminal_stream.SEALED_CONTENT_TYPE}\r\n"
                "Cache-Control: no-store\r\n"
                "X-Frame-Options: DENY\r\n"
                "Content-Security-Policy: frame-ancestors 'none'\r\n"
                "Connection: keep-alive\r\n\r\n").encode("latin-1"))
            await writer.drain()
            daemon = self._daemon
            self._stream_writers.add(writer)
            logger.info("terminal stream for %s opened by device %s",
                        session_id[:12], device_id)
            await terminal_stream.serve(
                daemon._pty, handle, paint, exited, reader, writer,
                on_input=self._lan_stream_input(device_id, handle),
                on_size=lambda c, r: daemon.terminal_phone_resize(
                    handle, session_id, c, r),
                codec=terminal_stream.SealedCodec(key, stream_id),
                up_frames_per_minute=(
                    terminal_stream.STREAM_MAX_UP_FRAMES_PER_MINUTE))
        finally:
            self._stream_writers.discard(writer)
            rest = [w for w in self._lan_streams.get(device_id, [])
                    if w is not writer]
            if rest:
                self._lan_streams[device_id] = rest
            else:
                self._lan_streams.pop(device_id, None)

    def _lan_stream_input(self, device_id: str, handle: str):
        """The home stream's `I` frame handler for one device: every
        keystroke is a write, so the bot's Write grant is re-read per frame
        and a refusal goes back as the stream's own `E` frame. A phone
        types under the desk's rules exactly as before."""
        def on_input(blob: bytes):
            refusal = self._bot_refusal(device_id, "write")
            if refusal:
                return False, refusal
            return self._daemon.terminal_stream_input(handle, blob)
        return on_input

    def _terminal_report_for(self, query: str, *, resize: bool,
                             viewer: str = ""):
        """One frame of a Dark Army-owned terminal's screen, on the query string so
        the loopback read and both sealed doors share one report
        (`_log_report_for`'s factoring): `session` (required), `since` (an
        int ≥ -1, the revision the caller already holds), `since_bytes` (an
        int ≥ -1, the raw-byte cursor; `-1` asks for a paint), `grid` (`0`
        to omit the rows and history — the emulator-fed phone), and
        `cols` / `rows` (each an int within the emulator's bounds): the
        panel's unconditional resize where ``resize`` is true (the loopback
        read), the away phone's own screen through `terminal_phone_resize`
        on the sealed doors. 400 in
        words on anything else. ``viewer`` is the sealed door's device id
        (`""` on loopback) and keys the away paint's remainder per phone. `available` is stated, never inferred: a session Dark Army does not
        host says so with an empty frame rather than a blank screen."""
        # `_card_sync_for`'s query discipline: a repeated key is 400 in
        # words, never the last one silently winning.
        multi = parse_qs(query or "", keep_blank_values=True)
        if any(len(v) != 1 for v in multi.values()):
            return 400, "application/json", json.dumps(
                {"error": "terminal parameters must not repeat"}).encode()
        params = {k: v[0] for k, v in multi.items()}
        session_id = str(params.get("session") or "")
        if not session_id or len(session_id) > 200:
            return 400, "application/json", json.dumps(
                {"error": "session is required"}).encode()
        since = -1
        if "since" in params:
            try:
                since = int(params["since"])
            except ValueError:
                since = -2
            if since < -1 or since > 10 ** 12:
                return 400, "application/json", json.dumps(
                    {"error": "since must be an integer revision ≥ -1"}).encode()
        cols = rows = None
        for name in ("cols", "rows"):
            if name in params:
                try:
                    value = int(params[name])
                except ValueError:
                    value = 0
                lo, hi = ((vtgrid.MIN_COLS, vtgrid.MAX_COLS) if name == "cols"
                          else (vtgrid.MIN_ROWS, vtgrid.MAX_ROWS))
                if not (lo <= value <= hi):
                    return 400, "application/json", json.dumps(
                        {"error": f"{name} must be {lo}..{hi}"}).encode()
                if name == "cols":
                    cols = value
                else:
                    rows = value
        since_bytes = None
        if "since_bytes" in params:
            try:
                since_bytes = int(params["since_bytes"])
            except ValueError:
                since_bytes = -2
            if since_bytes < -1 or since_bytes > 10 ** 15:
                return 400, "application/json", json.dumps(
                    {"error": "since_bytes must be an integer ≥ -1"}).encode()
        grid = True
        if "grid" in params:
            if params["grid"] not in ("0", "1"):
                return 400, "application/json", json.dumps(
                    {"error": "grid must be 0 or 1"}).encode()
            grid = params["grid"] == "1"
        # The sealed doors' `cols` / `rows` are the away phone's own
        # screen, routed through `terminal_phone_resize` — the stream's `S`
        # frame by another road, with the same one-owner rule — never the
        # panel's unconditional resize. Bounded above, like the panel's.
        phone_size = ((cols, rows) if not resize and cols and rows else None)
        frame = self._daemon.terminal_frame(
            session_id, since, cols if resize else None,
            rows if resize else None, resize=resize,
            since_bytes=since_bytes, grid=grid, viewer=viewer,
            phone_size=phone_size)
        frame["generated_at"] = time.time()
        return 200, "application/json", json.dumps(frame).encode()

    async def _conversation_report_for(self, query: str):
        """One page of a published session's conversation. One body, three
        doors — `_terminal_report_for`'s factoring, for its reason: the
        loopback read, the home kind and the away kind must never drift.

        Query: `session` (required, ≤ 200 chars), `since` (an int in
        ``0..10**9``, default 0), `key` (≤ 64 chars of ``[A-Za-z0-9:_-]``,
        default empty), `agent` (optional, ``conversation.AGENT_ID_RE``:
        one of the session's helpers, paged from its own journal). A repeated key is 400 in words. An unknown session
        is 404 in words; every other page is 200 with the JSON as-is.
        """
        multi = parse_qs(query or "", keep_blank_values=True)
        if any(len(v) != 1 for v in multi.values()):
            return 400, "application/json", json.dumps(
                {"error": "conversation parameters must not repeat"}).encode()
        params = {k: v[0] for k, v in multi.items()}
        session_id = str(params.get("session") or "")
        if not session_id or len(session_id) > 200:
            return 400, "application/json", json.dumps(
                {"error": "session is required"}).encode()
        since = 0
        if "since" in params:
            try:
                since = int(params["since"])
            except ValueError:
                since = -1
            if since < 0 or since > 10 ** 9:
                return 400, "application/json", json.dumps(
                    {"error": "since must be an integer in 0..1000000000"}
                ).encode()
        key = str(params.get("key") or "")
        if len(key) > 64 or not re.fullmatch(r"[A-Za-z0-9:_-]*", key):
            return 400, "application/json", json.dumps(
                {"error": "key must be at most 64 letters, digits, colons, "
                          "underscores or hyphens"}).encode()
        agent = str(params.get("agent") or "")
        if agent and not re.fullmatch(conversation.AGENT_ID_RE, agent):
            return 400, "application/json", json.dumps(
                {"error": "agent must be at most 64 letters, digits, "
                          "underscores or hyphens"}).encode()
        handler = getattr(self._daemon, "conversation_page", None)
        if handler is None:
            page = {"available": False,
                    "reason": conversation.NO_TRANSCRIPT_REASON}
        elif agent:
            page = await handler(session_id, since, key, agent=agent)
        else:
            page = await handler(session_id, since, key)
        if (isinstance(page, dict)
                and page.get("available") is False
                and page.get("reason") == conversation.UNKNOWN_SESSION_REFUSAL):
            return 404, "application/json", json.dumps(
                {"error": conversation.UNKNOWN_SESSION_REFUSAL}).encode()
        return 200, "application/json", json.dumps(
            page, allow_nan=False).encode()

    async def _prepare(self, payload: dict):
        """Ask the daemon to write a card's instructions. 409 on a refusal, in
        `_board_action`'s shape: the request was well-formed and the daemon
        declined it. The answer is deliberately not in the snapshot — a
        half-written draft belongs to the one composer that asked for it.

        **"Still writing" is a 202, not a 409.** The sealed door files every
        200 and 409 under the press's `command_token`, so a phone whose
        reply went missing on the relay can ask again with the same token
        and be handed the draft it paid for. A replay that arrives while the
        helper is still running must therefore not be *recorded* as the
        answer — a 409 there would freeze "give it a moment" into the
        ledger and every later replay would read it back. 202 is skipped by
        the ledger, and both composers already read any non-200 as a
        refusal carrying its `detail`, so the words on screen are unchanged.
        """
        result, detail = await self._daemon.prepare_card_text({
            "title": str(payload.get("title") or ""),
            "summary": str(payload.get("summary") or ""),
            "tool": str(payload.get("tool") or ""),
            "project": str(payload.get("project") or ""),
            "root": str(payload.get("root") or ""),
            "attachments": str(payload.get("attachments") or ""),
            # The one-box mode. Additive on purpose: an older client sends
            # no `idea` and gets exactly today's call and today's answer,
            # and a newer client talking to an older daemon reads these two
            # keys as empty, which both composers treat as "leave the field
            # alone" rather than as "blank it".
            "idea": str(payload.get("idea") or ""),
        })
        body = json.dumps({
            "ok": result is not None,
            "detail": detail or "",
            "prompt": (result or {}).get("prompt", ""),
            "workflow": (result or {}).get("workflow", ""),
            "title": (result or {}).get("title", ""),
            "summary": (result or {}).get("summary", ""),
            # Which of the open projects Dark Army thinks the card belongs to, or
            # "" for no opinion. Additive like `title`/`summary`: an older
            # daemon sends no key, which both composers read as "leave the
            # folder the person chose alone" rather than as "blank it".
            "suggested_root": (result or {}).get("suggested_root", ""),
            # The drafted objective, idea mode only, each possibly empty.
            # Same additive rule: an older daemon sends no key, a composer
            # applies a value only when it is non-empty *and* the box is
            # empty — a suggestion never overwrites what somebody typed.
            "beneficiary": (result or {}).get("beneficiary", ""),
            "intended_benefit": (result or {}).get("intended_benefit", ""),
            "success_criterion": (result or {}).get("success_criterion", ""),
        }).encode()
        if result is not None:
            return 200, "application/json", body
        busy = detail == daemon_board.PREPARE_BUSY_DETAIL
        return (202 if busy else 409), "application/json", body

    def _answer_question_request(self, request: _Request):
        """`(session_id, question_id, option_index)` if this is an authorised
        question answer, else None. Gated like every other write.

        `option_index` arrives as a string — the panel's `post` takes
        `[String: String]` — and is parsed defensively: a non-integer is a
        None (hence a 400 from `_route`), never a 500.
        """
        if request.path != "/api/action" or request.method != "POST":
            return None
        if not self._authorised(request):
            return None
        payload = request.json()
        if payload.get("action") != "answer_question":
            return None
        session_id = payload.get("session_id", "")
        question_id = str(payload.get("question_id") or "")
        try:
            option_index = int(str(payload.get("option_index")))
        except (TypeError, ValueError):
            return None
        if not session_id or option_index < 0:
            return None
        return session_id, question_id, option_index

    async def _answer_question(self, session_id: str, question_id: str,
                               option_index: int):
        ok, detail = await self._daemon.answer_question(
            session_id, question_id, option_index)
        body = json.dumps({"ok": ok, "detail": detail}).encode()
        return (200 if ok else 409), "application/json", body

    @staticmethod
    def _parse_option_indexes(raw):
        """The batch verb's index string as `list[list[int]]`, or None.

        Both clients post `[String: String]`, so the choices ride as text.
        Grammar: `group(,group)*` with `group = index(+index)*` — one group
        per question in dialog order, `+` joining the several picks of a
        multi-select question, so `"0+2,1"` is `[[0, 2], [1]]` and a plain
        `"1,0,2"` is `[[1], [0], [2]]`. Parsed defensively at both doors:
        `int` per part and nothing looser, every part 0-or-positive, an empty
        part (`""`, `"+"`, `"0+"`, `"+1"`, `"1,,2"`) refused, refusing (a
        None, hence a 400) on any failure rather than a 500. Never eval'd.
        The *meaning* of a group — whether that question takes several
        answers at all — is the daemon's to judge against the dialog it
        holds; this only guards the wire shape.
        """
        if not isinstance(raw, str) or not raw.strip():
            return None
        groups = []
        for segment in raw.split(","):
            group = []
            for part in segment.split("+"):
                try:
                    value = int(part.strip())
                except (TypeError, ValueError):
                    return None
                if value < 0:
                    return None
                group.append(value)
            groups.append(group)
        return groups

    def _answer_questions_request(self, request: _Request):
        """`(session_id, question_id, option_indexes)` if this is an
        authorised batch answer, else None — `_answer_question_request`'s
        sibling for the dialogs that ask more than once, gated identically.
        """
        if request.path != "/api/action" or request.method != "POST":
            return None
        if not self._authorised(request):
            return None
        payload = request.json()
        if payload.get("action") != "answer_questions":
            return None
        session_id = payload.get("session_id", "")
        question_id = str(payload.get("question_id") or "")
        option_indexes = self._parse_option_indexes(
            payload.get("option_indexes"))
        if not session_id or option_indexes is None:
            return None
        return session_id, question_id, option_indexes

    async def _answer_questions(self, session_id: str, question_id: str,
                                option_indexes: list):
        ok, detail = await self._daemon.answer_questions(
            session_id, question_id, option_indexes)
        body = json.dumps({"ok": ok, "detail": detail}).encode()
        return (200 if ok else 409), "application/json", body

    async def _reply(self, session_id: str, text: str):
        ok, detail = await self._daemon.reply_to_session(session_id, text)
        body = json.dumps({"ok": ok, "detail": detail}).encode()
        return (200 if ok else 409), "application/json", body

    async def _permission(self, request_id: str, behavior: str):
        ok, detail = await self._daemon.answer_permission(request_id, behavior)
        body = json.dumps({"ok": ok, "detail": detail}).encode()
        return (200 if ok else 409), "application/json", body

    # Ranges the panel offers. Days, or None for everything on record.
    HISTORY_RANGES = {"today": 1, "7d": 7, "30d": 30, "90d": 90, "all": None}
    #: Session ids on the `?session=` branch: non-empty, ≤128, this charset.
    #: Colon is load-bearing for Codex (`codex:{thread}`); without it every
    #: Codex card's fetch is 400 and the sheet lies about reachability.
    _SESSION_ID_CHARS = frozenset(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-:"
    )

    async def _history_report(self, request: _Request):
        """Aggregate history. Computed in SQL and summarised here — never by
        shipping every row to the browser, which stops scaling exactly when the
        history gets interesting.

        Runs in the executor: sqlite3 blocks, and this handler shares the loop
        with the daemon's own work.

        `?session=<id>` is a different question — one row, a handful of indexed
        queries — and it must never fall through to the aggregate `collect()`,
        which is a full scan and takes seconds."""
        # Validate the request before looking at the database: a malformed range
        # is a bad request whether or not history happens to be open, and
        # answering 200 for it would hide the typo.
        params = dict(
            pair.split("=", 1) for pair in request.query.split("&") if "=" in pair
        )
        if "session" in params:
            session_id = unquote(params.get("session") or "")
            if (not session_id or len(session_id) > 128
                    or any(c not in self._SESSION_ID_CHARS for c in session_id)):
                return 400, "application/json", json.dumps(
                    {"error": "malformed session id"}
                ).encode()
            store = getattr(self._daemon, "_history", None)
            if store is None:
                return 200, "application/json", json.dumps(
                    {"available": False,
                     "reason": "history database is not open"}
                ).encode()
            # Dict-membership on the loop, before the hop — same pattern as
            # `_live_limit_metrics` reading `self._daemon._agents`. Three
            # registers, because the fleet keeps a row alive by any of them:
            # the hook stream, the Codex roster (journals, no hooks) and the
            # Grok roster. Deliberately not the daemon's live-id helper, which
            # unions the snapshot cache including the `finished` bucket and
            # would caption a tombstone as still working — hence the
            # `_finished` subtraction here too.
            live = (
                session_id in getattr(self._daemon, "_session_states", {})
                or session_id in getattr(self._daemon, "_codex_records", {})
                or session_id in getattr(self._daemon, "_grok_records", {})
            ) and session_id not in getattr(self._daemon, "_finished", {})

            def fetch():
                return store.session_record(session_id)

            record = await asyncio.get_running_loop().run_in_executor(
                None, fetch)
            return 200, "application/json", json.dumps({
                "available": True,
                "generated_at": time.time(),
                "live": live,
                "session": record,
            }).encode()

        key = params.get("range", "30d")
        if key not in self.HISTORY_RANGES:
            return 400, "application/json", json.dumps(
                {"error": "unknown range", "allowed": sorted(self.HISTORY_RANGES)}
            ).encode()
        days = self.HISTORY_RANGES[key]

        # One rule for both doors. The sealed sibling `_agent_report_for`
        # raises on an over-long root and answers 400; this used to blank the
        # field and answer **machine-wide** instead, which is a refusal that
        # fails open — the caller asked about one project and got every
        # project, under the caption of the one it asked for.
        root = unquote(params.get("root", ""))
        if len(root) > 1024:
            return 400, "application/json", json.dumps(
                {"error": "a project root must be at most 1024 characters"}
            ).encode()

        store = getattr(self._daemon, "_history", None)
        if store is None:
            return 200, "application/json", json.dumps(
                {"available": False, "range": key,
                 "reason": "history database is not open"}
            ).encode()

        def collect() -> dict:
            # The role each helper name belongs to, from `crew.ROLES`
            # through the report's own one-line mapping — so the desk draws the
            # same row whether it came from here or from the joined
            # effectiveness half. Not a query: pure dict work over rows already
            # folded, inside the hop that folded them.
            agents = store.by_agent(days)
            for row in agents:
                row["role"] = agent_report.role_of(row["name"])
            return {
                "available": True,
                "range": key,
                "generated_at": time.time(),
                "totals": store.totals(days),
                "daily": store.daily_report(days or 3650),
                "by_project": store.by_project(days),
                "by_model": store.by_model(days),
                "waiting": store.waiting_summary(days),
                "limits": store.limits_report(days),
                "hourly": store.hourly_report(days),
                "context": store.context_pressure(days),
                "sessions": store.recent_sessions(days, limit=50),
                # The agent dimension: what each kind of helper cost, and the
                # heaviest individual dispatches. Inside the *same* executor
                # hop as everything else here — a hop per query would put the
                # loop between two halves of one answer.
                "by_agent": agents,
                "top_dispatches": store.top_dispatches(days),
            }

        report = await asyncio.get_running_loop().run_in_executor(None, collect)
        # The effectiveness half rides beside it. A daemon that does not carry
        # the method leaves the key absent, which both clients decode as an
        # empty report — never as a report of zero.
        handler = getattr(self._daemon, "agent_efficiency_report", None)
        if handler is not None:
            effectiveness = await handler(days, root)
            # `other_days` is the leftover-turn aggregate, served beside
            # `daily` and never written into it. Absent when the
            # effectiveness half could not be computed, so the ledger draws
            # no other line rather than subtracting cards from `daily`.
            if isinstance(effectiveness, dict):
                other = effectiveness.pop("other_days", None)
                report["effectiveness"] = effectiveness
                if other is not None:
                    report["other_days"] = other
            else:
                report["effectiveness"] = effectiveness
        return 200, "application/json", json.dumps(report).encode()

    USAGE_WINDOWS = {"session": 5 / 24, "week": 7, "day": 1}

    # Which limit bar each window is the attribution for. Given that bar's reset
    # time we can use the real window edge instead of a rolling one — see
    # `_window_start`.
    _WINDOW_BAR = {"session": "session", "week": "weekly_all"}

    def _live_limit_metrics(self) -> dict:
        """The rate-limit reading the live sessions are reporting.

        Gathering only — which of the payloads wins is `limits.pick_live`'s call,
        beside the fields it names and the bars it feeds. A session that has not
        had a statusline tick yet contributes nothing rather than a zero.

        Claude rows only, and that is not a detail: a Grok row reports its weekly
        percentage under the same `five_hour_pct` key, so an unfiltered gather
        lets it furnish the five-hour bar. See `limits.claude_payloads`."""
        return limits.pick_live(limits.claude_payloads(
            agent for group in self._agents.values() for agent in group
        ))

    def _window_start(self, key: str, days: float,
                      snapshot: dict) -> Optional[float]:
        """The instant the limit window this view is attributing actually opened.

        A limit window is fixed, not rolling: the five-hour one runs to a reset
        time and starts exactly five hours before it. Anchoring the attribution
        to that edge is the difference between "what has spent this window" and
        "what ran in the last five hours" — near the end of a window those are
        almost disjoint sets of turns.

        Falls back to rolling (None → the caller's `days`) when the bar has no
        usable reset time: no live statusline and a cache too old to trust, in
        which case a made-up edge would be worse than an honest approximation.
        A reset already in the past is one of those — that window has closed."""
        kind = self._WINDOW_BAR.get(key)
        if not kind:
            return None
        now = time.time()
        for bar in snapshot.get("bars") or []:
            if bar.get("kind") != kind:
                continue
            resets = bar.get("resets_at")
            if isinstance(resets, (int, float)) and resets > now:
                return resets - days * 86400
        return None

    async def _catch_up_report(self, query: str, device_id: str = ""):
        from urllib.parse import parse_qs
        from . import enrollment
        from .decision_store import uuid_ok
        def reply(body, status=200):
            return status, "application/json", json.dumps(body).encode()
        try:
            params = parse_qs(query, strict_parsing=True)
            allowed = {"receipt_id", "cursor", "upper_cursor", "page_cursor", "limit", "project", "since", "until"}
            if set(params) - allowed or any(len(v) != 1 for v in params.values()):
                raise ValueError()
            values = {k: v[0] for k, v in params.items()}
            if "receipt_id" in values:
                if len(values) != 1 or not uuid_ok(values["receipt_id"]):
                    raise ValueError()
            else:
                for key in ("cursor", "upper_cursor", "page_cursor", "limit"):
                    if key in values:
                        if not values[key].isdigit() or len(values[key]) > 18:
                            raise ValueError()
                        values[key] = int(values[key])
                if not 1 <= values.get("limit", 100) <= 100:
                    raise ValueError()
                for key in ("since", "until"):
                    if key in values:
                        values[key] = float(values[key])
                        if not 0 <= values[key] < 1e12:
                            raise ValueError()
                if values.get("since", 0) > values.get("until", 1e12):
                    raise ValueError()
        except (ValueError, TypeError):
            return reply({"error": "Invalid catch-up query."}, 400)
        # The verified door owns identity. Never accept a device id in a query.
        if device_id and devices.home_key(device_id) is None:
            return reply({"error": "no longer paired"}, 403)
        capture = getattr(self._daemon, "_decisions", None)
        if capture is None:
            return reply({"available": False, "items": [], "reason": "Decision history is unavailable on this Mac."})
        roots = await asyncio.get_running_loop().run_in_executor(None, enrollment.enrolled_roots)
        result = await capture.call("query", roots=roots, device=device_id, **values)
        if device_id and devices.home_key(device_id) is None:
            return reply({"error": "no longer paired"}, 403)
        current_roots = await asyncio.get_running_loop().run_in_executor(None, enrollment.enrolled_roots)
        if device_id and devices.home_key(device_id) is None:
            return reply({"error": "no longer paired"}, 403)
        if set(current_roots) != set(roots):
            return reply({"available": False, "items": [], "reason": "Project enrollment changed. Please retry."})
        return reply(result or {"available": False, "items": [], "reason": "Decision history could not be read."})

    async def _log_report(self, request: _Request):
        """`GET /api/log` on loopback: the daemon's diary (`event_log`)."""
        return self._log_report_for(request.query)

    def _bearings_report_for(self, query: str):
        """The Bearings digest, one body on three doors.

        `since` (a float ≥ 0, Recently landed strictly newer) is the only
        query key that is read; anything else is ignored as `_log_report_for`
        ignores an unknown key. 400 in words on a negative, non-numeric or
        non-finite value. `sort_keys` is here, not inside compose: the
        sections list keeps `SECTIONS` order. `parse_qs`, as
        `_conversation_report_for` reads its query, so the two sealed reads
        do not split the same string two ways.
        """
        params = parse_qs(query or "", keep_blank_values=True)
        since = None
        if "since" in params:
            try:
                since = float(params["since"][0])
            except ValueError:
                since = -1.0
            if not (since >= 0.0) or since == float("inf"):
                return 400, "application/json", json.dumps(
                    {"error": "since must be a number of seconds ≥ 0"}).encode()
        handler = getattr(self._daemon, "bearings_snapshot", None)
        if handler is None:
            body = bearings.compose(
                snapshot={}, prompts={}, notified=(), cards=(), acks=(),
                events=(), diary_available=False, since=since, now=time.time())
            body["available"] = False
        else:
            body = handler(since=since)
        return 200, "application/json", json.dumps(
            body, ensure_ascii=False, sort_keys=True).encode()

    def _log_report_for(self, query: str):
        """The diary, newest first, on the query string so all three doors
        share one report: `since` (a float ≥ 0, entries strictly newer) and
        `limit` (1..`event_log.MAX_ENTRIES`), 400 on anything else. Answered
        from memory — `EventLog.recent` copies out of a deque under its lock
        — so no executor hop. `available` is stated, not inferred: a daemon
        whose diary failed to open says so with an empty list rather than
        looking like a quiet day."""
        params = dict(
            pair.split("=", 1) for pair in (query or "").split("&") if "=" in pair
        )
        since = None
        limit = None
        if "since" in params:
            try:
                since = float(params["since"])
            except ValueError:
                since = -1.0
            if not (since >= 0.0) or since == float("inf"):
                return 400, "application/json", json.dumps(
                    {"error": "since must be a number of seconds ≥ 0"}).encode()
        if "limit" in params:
            try:
                limit = int(params["limit"])
            except ValueError:
                limit = 0
            if not (1 <= limit <= event_log.MAX_ENTRIES):
                return 400, "application/json", json.dumps(
                    {"error": f"limit must be 1..{event_log.MAX_ENTRIES}"}).encode()
        diary = getattr(self._daemon, "_event_log", None)
        if diary is None:
            body = {"available": False, "generated_at": time.time(), "events": []}
        else:
            body = {"available": True, "generated_at": time.time(),
                    "events": diary.recent(since=since, limit=limit)}
        return 200, "application/json", json.dumps(body).encode()

    async def _work_record_report_for(self, query: str):
        """What Dark Army observed one card's last run do. One body, three doors —
        `_log_report_for`'s factoring, for its reason: the loopback read, the
        home read and the away read must never drift into two answers about
        the same card.

        Two shapes on one query string. `card=<id>` returns the whole record;
        `card=<id>&file=<index>` returns one file's diff text. `available` is
        **stated** in both, so "this board is not open" and "this card has no
        record" are distinguishable from each other and from "this run changed
        nothing" — three facts a person judging an outcome has to be able to
        tell apart.

        The containment rule for the diff, in order, each failing closed:

        1. the card carries a record that has actually been written
           (`recorded_at`);
        2. `file` is an **integer index into the record's own stored list** —
           the caller never supplies a path, so there is no path to sanitise
           and no traversal to defeat;
        3. the record's `root` is re-checked against the enrolment ledger *at
           the moment of the read*, never trusted from the stored row: a
           project un-enrolled since the run must stop being readable;
        4. the resolved path is component-contained inside the real root
           (`workspace._contains`, the rule `attach_plan` and `BoardDocuments`
           already use) — belt and braces, since git produced the path;
        5. anything else is 400 or 404 **in words**. Nothing is truncated into
           legality; only the diff *text* is bounded, and that truncation is
           stated rather than inferred.
        """
        params = dict(
            pair.split("=", 1) for pair in (query or "").split("&") if "=" in pair
        )
        card_id = unquote(str(params.get("card") or ""))
        if not card_id or len(card_id) > 200:
            return 400, "application/json", json.dumps(
                {"error": "card must be a card id"}).encode()
        index = None
        if "file" in params:
            raw = unquote(str(params.get("file") or ""))
            if not raw.isdigit() or len(raw) > 6:
                return 400, "application/json", json.dumps(
                    {"error": "file must be a whole number"}).encode()
            index = int(raw)
        store = getattr(self._daemon, "_board", None)
        if store is None:
            return 200, "application/json", json.dumps(
                {"available": False, "generated_at": time.time(),
                 "record": None, "caption": work_record.CAPTION,
                 "reason": "the board is not open"}).encode()
        loop = asyncio.get_running_loop()
        record = await loop.run_in_executor(
            None, functools.partial(store.run_for, card_id))
        if index is None:
            if isinstance(record, dict) and not record.get("recorded_at"):
                # A run that has started and not ended yet. The record exists
                # in the store and says nothing; publishing it would draw a
                # finished-looking record over work still in flight.
                record = None
            if isinstance(record, dict):
                # The one sentence for the helper's work, the daemon's own
                # (`work_record.shunt_words`); `""` where nothing was
                # delegated, which a client draws nothing for.
                record["shunt_words"] = work_record.shunt_words(record)
            return 200, "application/json", json.dumps(
                {"available": True, "generated_at": time.time(),
                 "record": record if isinstance(record, dict) else None,
                 "caption": work_record.CAPTION}).encode()
        if not isinstance(record, dict) or not record.get("recorded_at"):
            return 404, "application/json", json.dumps(
                {"error": "Dark Army has no record of a run on that card"}).encode()
        row = work_record.file_path_at(record, index)
        if row is None:
            return 404, "application/json", json.dumps(
                {"error": "that file is not on Dark Army's list for this run"}
            ).encode()
        rel = str(row.get("path") or "")
        root = str(record.get("root") or "")
        enrolled = await loop.run_in_executor(
            None, enrollment.root_enrolled, root)
        if not root or not enrolled:
            return 404, "application/json", json.dumps(
                {"error": work_record.NOT_ENROLLED_REASON}).encode()
        real_root = os.path.realpath(root)
        target = os.path.realpath(os.path.join(real_root, rel))
        if not workspace._contains(real_root, target):
            return 404, "application/json", json.dumps(
                {"error": "that file is outside the card's own project"}
            ).encode()
        argv = (work_record.argv_new_file_diff(root, rel)
                if row.get("new") else
                work_record.argv_file_diff(
                    root, str(record.get("baseline") or ""), rel))
        collect = getattr(self._daemon, "_run_git", None)
        if collect is None:
            return 200, "application/json", json.dumps(
                {"available": False, "path": rel, "new": bool(row.get("new")),
                 "text": "", "truncated": False,
                 "reason": work_record.GIT_FAILED_REASON}).encode()
        ok, out, why = await collect(argv, root, truncate=True)
        # `git diff --no-index` exits 1 when the two sides differ, which for an
        # untracked file is the *expected* outcome — the whole file is new. So
        # a failed reading that still produced text is a success here, and one
        # that produced nothing is stated as a failure rather than drawn as an
        # empty file.
        if not ok and not out:
            return 200, "application/json", json.dumps(
                {"available": False, "path": rel, "new": bool(row.get("new")),
                 "text": "", "truncated": False,
                 "reason": why or work_record.GIT_FAILED_REASON}).encode()
        text, truncated = work_record.clamp_diff(out)
        return 200, "application/json", json.dumps(
            {"available": True, "path": rel, "new": bool(row.get("new")),
             "text": text, "truncated": truncated, "reason": ""}).encode()

    async def _usage_report(self, request: _Request):
        """Claude Code's /usage, for the pond: the limit bars, and what has been
        spending them.

        Three bar sources, one response: Claude's cache merged with the live
        statusline, Codex's last account windows, Grok's weekly billing. The
        attribution combines stored provider turns and local Codex journals.
        Either half can be missing without the other being useless, so each
        carries its own `available` rather than the endpoint failing as a whole."""
        return await self._usage_report_for(request.query)

    async def _usage_report_for(self, query: str):
        """`_usage_report`'s whole body, factored on the query string so the
        remote path shares one report rather than growing a second one."""
        params = dict(
            pair.split("=", 1) for pair in (query or "").split("&") if "=" in pair
        )
        key = params.get("window", "session")
        if key not in self.USAGE_WINDOWS:
            return 400, "application/json", json.dumps(
                {"error": "unknown window", "allowed": sorted(self.USAGE_WINDOWS)}
            ).encode()
        days = self.USAGE_WINDOWS[key]
        metrics = self._live_limit_metrics()
        store = getattr(self._daemon, "_history", None)

        def collect() -> dict:
            # Both legs run here: reading ~/.claude.json is a 100 KB parse and the
            # attribution is sqlite, and neither belongs on the event loop that is
            # also driving the simulator.
            # The two account-wide windows come from the statusline seconds
            # ago; the per-model one has no local source newer than Claude
            # Code's cache, which is routinely hours old and used to be drawn
            # beside them as if it were live. `merge_snapshot` freshens it and
            # nothing else — an unavailable or failed fetch returns `{}` and
            # leaves this snapshot exactly as `limits` built it.
            snapshot = limits.snapshot(metrics)
            try:
                snapshot = claude_usage.merge_snapshot(
                    snapshot, claude_usage.get_snapshot())
            except Exception:
                logger.warning("could not refresh the scoped Claude window",
                               exc_info=True)
            try:
                codex = self._daemon.codex_usage_snapshot()
            except Exception:
                logger.warning("could not read Codex usage", exc_info=True)
                codex = {"available": False, "bars": []}
            try:
                grok = grok_billing.get_snapshot()
            except Exception:
                logger.warning("could not read Grok usage", exc_info=True)
                grok = {}
            grok_bars = []
            percent = grok.get("percent") if isinstance(grok, dict) else None
            if isinstance(percent, (int, float)):
                grok_bars.append({
                    "kind": "grok_weekly",
                    "group": "weekly",
                    "provider": "grok",
                    "title": "Grok, this week",
                    "label": "7d",
                    "percent": float(percent),
                    "resets_at": grok.get("resets_at"),
                    "stale": bool(grok.get("stale")),
                })
            report = {
                "window": key,
                "generated_at": time.time(),
                "limits": {
                    **snapshot,
                    "bars": [*(snapshot.get("bars") or []),
                             *(codex.get("bars") or []),
                             *grok_bars],
                },
            }
            if store is None:
                report["attribution"] = {
                    "available": False,
                    "reason": "history database is not open",
                }
            else:
                report["attribution"] = {
                    "available": True,
                    **store.usage_attribution(
                        days, since=self._window_start(key, days, snapshot)),
                }
            now = report["generated_at"]
            since = self._window_start(key, days, snapshot) or now - days * 86400
            providers = []
            if store is not None:
                providers = store.usage_provider_groups(since, now)
                for group in providers:
                    # Grok has no Claude reset boundary: use its own rolling period.
                    if group["provider"] != "claude":
                        continue
                    group["window_label"] = (f"current {'5h' if key == 'session' else '7d'} window"
                                             if since != now - days * 86400 else
                                             f"rolling {'5h' if key == 'session' else '7d' if key == 'week' else '24h'}")
                other_groups = store.usage_provider_groups(now - days * 86400, now)
                providers = [g for g in providers if g["provider"] == "claude"]
                for group in other_groups:
                    if group["provider"] != "claude":
                        group["window_label"] = f"rolling {'5h' if key == 'session' else '7d' if key == 'week' else '24h'}"
                        providers.append(group)
            try:
                codex_group = codex_spenders.reader.snapshot(key, codex.get("bars") or [])
            except Exception:
                logger.warning("could not read Codex spenders", exc_info=True)
                start, end, label = codex_spenders.window_bounds(key, [], now)
                codex_group = {"provider": "codex", "available": False,
                               "reason": "Local Codex history could not be read.",
                               "measurement": "local_token_share", "models": [],
                               "partial": True, "window_start": start,
                               "window_end": end, "window_label": label}
            providers.insert(1 if providers and providers[0]["provider"] == "claude" else 0,
                             codex_group)
            report["attribution"]["providers"] = providers
            return report

        report = await asyncio.get_running_loop().run_in_executor(None, collect)
        return 200, "application/json", json.dumps(report).encode()

    async def _respond(self, writer, status: int, ctype: str, body: bytes) -> None:
        head = (
            f"HTTP/1.1 {status} {_STATUS_TEXT.get(status, 'OK')}\r\n"
            f"Content-Type: {ctype}\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Cache-Control: no-store\r\n"
            # A cross-origin page cannot *read* this one, which is what the token
            # and the Origin allowlist are for. Framing needs neither: the clicks
            # land on the real panel, which already holds the token, and Stop and
            # Delete are two clicks in the same place. Nothing here is ever meant
            # to be embedded, so refuse it outright.
            "X-Frame-Options: DENY\r\n"
            "Content-Security-Policy: frame-ancestors 'none'\r\n"
            "Connection: close\r\n"
            "\r\n"
        ).encode("latin-1")
        writer.write(head + body)
        await writer.drain()

    async def _serve_events(self, writer, request: Optional[_Request] = None) -> None:
        # `?sections=changed` opts this client into slim frames: any top-level
        # section that is unchanged (clocks aside) since the last frame that
        # went out arrives absent, and the client carries the last one it saw
        # forward. Opt-in by query
        # so an older panel — which never sends it — gets today's full frames
        # forever, and an older daemon — whose `_Request` has split the query
        # off since before the board existed — simply ignores it.
        params = dict(
            pair.split("=", 1)
            for pair in (request.query if request else "").split("&")
            if "=" in pair
        )
        slim = params.get("sections") == "changed"
        # `?done=review` is the second, independent opt-in: this client's board
        # section carries only the finished cards still waiting on a person,
        # and it fetches the rest once from `/api/board?column=done`. A client
        # that never sends it — every older panel — keeps today's whole Done
        # preview on every frame, so upgrading the daemon can never take a
        # screen's finished column away.
        done_review = params.get("done") == "review"
        # `?cards=delta` is the third opt-in: when only some cards moved, this
        # client's board carries just those plus `card_order` and
        # `cards_delta: true`, and it merges them into the board it holds. A
        # slim client only — a full client's frames carry whole boards by
        # definition, so a full client sending it is ignored. Attaching one
        # clears both per-card maps, so the next board any delta client is
        # sent is whole, before any delta can assume what this one holds.
        cards_delta = slim and params.get("cards") == "delta"
        queue: asyncio.Queue = asyncio.Queue(maxsize=SSE_CLIENT_QUEUE_MAX)
        self._clients[queue] = slim
        if done_review:
            self._done_review_clients.add(queue)
        if cards_delta:
            self._card_delta_clients.add(queue)
            self._last_cards_sent_full = {}
            self._last_cards_sent_review = {}
        writer.write(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: text/event-stream\r\n"
            b"Cache-Control: no-store\r\n"
            # This one builds its own head rather than going through _respond, so
            # the frame refusal has to be repeated. A stream is not a framable
            # document, but a response of this server's that lacks the header is
            # a thing someone later has to re-derive is safe.
            b"X-Frame-Options: DENY\r\n"
            b"Content-Security-Policy: frame-ancestors 'none'\r\n"
            b"Connection: keep-alive\r\n\r\n"
        )
        try:
            await writer.drain()
            # The attach frame is this client's own variant. It is the very
            # frame this opt-in exists to shrink, and it is also the one that
            # would otherwise deliver a whole Done column the client is about
            # to throw away and refetch.
            writer.write(b"data: " + json.dumps(
                self.state(done_review=done_review)).encode() + b"\n\n")
            await writer.drain()
            while True:
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_SECONDS)
                except asyncio.TimeoutError:
                    writer.write(b": keep-alive\n\n")   # comment frame, ignored by EventSource
                    await writer.drain()
                    continue
                if payload is None:
                    break
                # One line per event, and it has to stay that way: the panel
                # reads this stream with `AsyncLineSequence`, which never yields
                # the blank line that ends an SSE event, so it dispatches on the
                # `data:` line itself. `json.dumps` emits no raw newline, which
                # is what makes that safe — a pretty-printed payload would frame
                # an event across lines the panel cannot see the end of, and it
                # would stop updating in silence. Pinned by
                # `test_an_event_is_exactly_one_line`.
                writer.write(b"data: " + payload.encode() + b"\n\n")
                await writer.drain()
        except (ConnectionResetError, BrokenPipeError, OSError):
            pass
        finally:
            self._clients.pop(queue, None)
            self._done_review_clients.discard(queue)
            self._card_delta_clients.discard(queue)
            self._overflow_logged.discard(id(queue))
