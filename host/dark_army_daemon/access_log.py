"""Every knock on the phone doors that was turned away, written down.

The LAN door (`api_server._handle_lan_client`) and the relay mailbox
(`relay_client.RelayConnector._handle_wire`) refuse a great deal — a request
in the old unsealed shape, a phone that was never paired, an envelope whose
seal does not verify, a replayed counter, a pairing attempt with the wrong
code — and until now each refusal was answered and forgotten. This is the
journal of those refusals, `event_log.EventLog`'s shape on its own file, plus
the one judgment made over it: **a burst** — `BURST_THRESHOLD` weighted
refusals from one source inside `BURST_WINDOW_SECONDS` — which raises an
alert at most once per `ALERT_COOLDOWN_SECONDS` per source and, whatever the
source, at most once per `ALERT_FLOOR_SECONDS` machine-wide: a burst inside
the floor **folds** into the open alert as a `burst_more` line, raising no
banner and no diary line, so a LAN machine cycling through free IPv6
addresses can never flood the inbox or push a real alert off the belt.

Three properties, each load-bearing:

* **Recording never changes a verdict.** Every recorder sits on the line
  before an existing refusal `return`, takes nothing from the answer and
  returns nothing. The doors refused exactly what they refused before; this
  only says so afterwards.
* **A refusal that could only have come from a keyholder never counts.**
  `ctr`, `ts` and `kind` are refusals of a frame whose seal *verified* — the
  person's own paired phone with a stale counter or a skewed clock — and
  carry `WEIGHTS` zero: logged, never a hit, so re-pairing your own phone
  cannot set the alarm off. The relay's `rate` bucket and an `oversize`
  upload from a paired phone are weight zero for the same reason: neither
  says anything about who is knocking.
* **Nothing secret can get in.** `FORBIDDEN_KEYS` widens the diary's fence
  with every name a key, a channel id, a pairing code or a sealed frame is
  stored under; the LAN recorder never passes the raw `X-Bob-Channel` value
  (it is derived from the home key), and the relay's peer is the paired
  device id — already published in the `devices` snapshot — never the
  channel id or the mailbox URL.

The detector is pure and in memory: `MAX_TRACKED_PEERS` deques of at most
`BURST_THRESHOLD` timestamps, LRU-evicted, so a flood from ten thousand
addresses costs a bounded dict and never a task or a timer.

The same file also carries the Mac's side of the phone's link timing: one
`timing` line per door per device every ten minutes (`link_timing.py`,
`BobDaemon.note_link_timing`), on its own belt (`MAX_TIMING_ENTRIES`),
never weighed and never an alert, and served only to a reader that asks
(`recent(include_timing=True)`, the `timing=1` query on the report).
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import threading
import time
from collections import OrderedDict, deque
from pathlib import Path
from typing import Optional

from . import event_log
from . import relay
from .paths import ACCESS_LOG_PATH

logger = logging.getLogger(__name__)

#: Nothing older than a month is ever returned or kept.
RETENTION_SECONDS = 30 * 86400
#: And never more lines than this, the oldest **refusal** dropped first: a
#: `burst` or `burst_ack` line is what keeps an alert open, so a flood of
#: refusals from one peer must not be able to close its own alert by pushing
#: the burst line off the end. Only a TTL or an acknowledgement closes one.
MAX_ENTRIES = 2000
#: The belt for the alert lines the cap spares: at most this many `burst` /
#: `burst_ack` lines are kept, oldest dropped first. An alert is at most one
#: per peer per hour and lives seven days, so this is never reached by
#: honest traffic; it is here so the file stays bounded whatever happens.
MAX_ALERT_ENTRIES = 200
#: How far past what is held the *file* may run before it is rewritten.
PRUNE_SLACK = 100
#: The belt for the `timing` lines — the ten-minute link-timing rollups
#: (`link_timing.py`), one per door per device. Their own belt, oldest
#: first, counted against neither `MAX_ENTRIES` nor `MAX_ALERT_ENTRIES`:
#: a rollup can never push a refusal or an alert off the end, and a flood
#: of refusals can never evict a rollup. Six hundred is over four days for
#: one phone on one door; about a day or two with two doors or two phones.
MAX_TIMING_ENTRIES = 600

#: The burst rule: this many weighted refusals from one source …
BURST_THRESHOLD = 5
#: … inside this many seconds …
BURST_WINDOW_SECONDS = 600
#: … raise one alert, and then none for this long about that source …
ALERT_COOLDOWN_SECONDS = 3600
#: … and, whatever the peer, none for this long after the last alert was
#: raised; a burst inside the floor folds into the open alert. The floor is
#: on *raising*: an acknowledgement does not reset it, and it lives in the
#: detector's memory alone, so a daemon restart lets the next burst raise.
ALERT_FLOOR_SECONDS = 600
#: How many open alerts `security_snapshot` publishes at once — the newest,
#: with a `hidden` count for the rest. `open_alerts()` itself, the loopback
#: `GET /api/access-log` and the sealed `access_log` read stay uncapped.
MAX_PUBLISHED_ALERTS = 8
#: How many `timing` lines one read returns at most (newest first), beside
#: — never inside — the ``limit`` a reader puts on the refusal and alert
#: lines: a phone reading the newest 500 rows of any kind would, after a
#: few days of rollups, see no refusal at all.
TIMING_READ_LIMIT = 100
#: How many distinct sources the daemon's fold record counts; past this the
#: figure stops growing, and `sentence()` renders a `peers` figure at or
#: over the bound as "at least" (below it, byte-identical to an exact one).
MAX_FOLDED_PEERS = 1024
#: How many sources the detector remembers at once; the least recently
#: heard from is forgotten past this.
MAX_TRACKED_PEERS = 64
#: How long a burst alert stays open without an acknowledgement.
ALERT_TTL_SECONDS = 7 * 86400
#: A peer string longer than this is clamped, never refused: an address is
#: short, and an attacker choosing the string must not choose the line size.
MAX_PEER_CHARS = 64

#: Where a knock landed. `lan` is every `/api/home` and plaintext route on
#: the Wi-Fi door; `pairing` and `upload` are their own routes there; `relay`
#: is the mailbox; `ws` is the socket relay beside it (`relay_ws.py`).
DOORS = ("lan", "pairing", "upload", "relay", "ws")

#: Why a knock was turned away. Closed: `append` refuses anything else.
REASONS = (
    "plaintext",     # a request in the old unsealed shape
    "origin",        # a browser-shaped request carrying Origin
    "unpaired",      # an X-Bob-Channel no paired phone owns
    "seal",          # relay.open_frame: the seal did not verify
    "shape",         # relay.open_frame: not a frame
    "oversize",      # relay.open_frame / the body rule: too large
    "ctr",           # relay.open_frame: a replayed counter (keyholder)
    "ts",            # relay.open_frame: a skewed clock (keyholder)
    "kind",          # a verified frame of the wrong kind (keyholder)
    "rate",          # the relay's per-device frame bucket
    "pair_code",     # a pairing attempt the ledger refused
    "pair_channel",  # a sealed pairing attempt on the wrong channel
    "not_pair",      # a sealed pairing request that was not one
    "blob",          # an upload whose sealed body did not open
    "not_found",     # a route the door does not have
    "pair_plain",    # a typed-address pair while no pairing allowed one
    "tunnel",        # a knock that landed on a VPN / tunnel address
    "busy",          # over LAN_MAX_OPEN / LAN_MAX_OPEN_PER_PEER, unread
    "pake",          # a typed-address pairing request that was not a
                     # well-formed exchange: the old plain shape, a malformed
                     # value, an unknown `A`, too many starts
)

#: How much a refusal counts towards a burst. A frame whose seal verified
#: came from a keyholder — the person's own phone — and counts for nothing.
#: `rate` is zero too: the relay's per-device bucket fires *before* the seal
#: is opened, so it is a quantity signal, not an authenticity one — the
#: person's own phone reconnecting after a gap trips it, and a stranger
#: flooding the mailbox still produces counted `seal` / `shape` refusals.
#: A fifth weight-0 case is decided in `BurstDetector.note`, not here:
#: `oversize` **with a device id** — the body rule verified the upload frame,
#: so the sender is a keyholder whose photo was merely too large.
#: `busy` is zero for `rate`'s reason: the connection cap fires on the
#: accept alone, before any seal is opened, so it is a quantity signal and
#: not an authenticity one — a keyholder phone with a stuck poll trips it,
#: and a stranger holding sockets open produces nothing a burst could read.
#: `pair_plain`, `tunnel` and `pake` stay at 1: each is a stranger's knock.
WEIGHTS = {reason: 1 for reason in REASONS}
WEIGHTS.update({"ctr": 0, "ts": 0, "kind": 0, "rate": 0, "busy": 0})

#: Every kind of line the journal holds. `burst_more` is a burst folded into
#: an open alert inside `ALERT_FLOOR_SECONDS`: append-only evidence naming
#: the alert (`alert_id`) and carrying the running `count` / `peers`
#: totals; the belt keeps the newest per alert and evicts it with its alert.
#: `timing` is one closed link-timing window (`link_timing.LinkTimer`):
#: `count` requests on `door` from `device_id` inside `window_seconds`,
#: with the per-hop figures under `hops`. It weighs nothing — it never
#: reaches `BurstDetector` — and raises nothing.
KINDS = ("refusal", "burst", "burst_ack", "burst_more", "timing")

#: Every key an entry may carry, and exactly what a line is. `peers` is the
#: distinct-source total a `burst_more` line carries; 0 on every other kind.
#: `hops` is a `timing` line's figures, a dict keyed by `HOP_KEYS`; `{}` on
#: every other kind, so the key set is one tuple whatever the kind.
PUBLISHED_KEYS = (
    "id", "ts", "kind", "door", "peer", "reason", "device_id", "text",
    "count", "window_seconds", "alert_id", "peers", "hops",
)

#: The closed set of keys a `timing` line's `hops` may carry — `n`, then
#: `p50` / `p90` / `max` for each of the seven hops. Stated literally here
#: and pinned equal to what `link_timing.LinkTimer.rollup` emits
#: (`test_link_timing.py`), so neither module can grow alone; `append`
#: refuses a key outside it.
HOP_KEYS = (
    "n",
    "dwell_p50", "dwell_p90", "dwell_max",
    "hold_p50", "hold_p90", "hold_max",
    "open_p50", "open_p90", "open_max",
    "run_p50", "run_p90", "run_max",
    "answer_p50", "answer_p90", "answer_max",
    "seal_p50", "seal_p90", "seal_max",
    "size_p50", "size_p90", "size_max",
)

#: The diary's fence plus every name a secret of the phone doors is stored
#: under. `code` is the pairing code, `chan` / `channel` the derived channel
#: id, `frame` / `wire` a sealed envelope, `digest` the ledger's stored hash.
FORBIDDEN_KEYS = event_log.FORBIDDEN_KEYS + tuple(
    k for k in ("chan", "channel", "frame", "wire", "code", "home_key",
                "relay_key", "digest", "claim")
    if k not in event_log.FORBIDDEN_KEYS)

#: The words a refusal for a missing alert is answered with.
ACCESS_ALERT_MISSING_REFUSAL = "that alert is not open"

#: The door, in the words a person reads.
DOOR_WORDS = {
    "lan": "the Wi-Fi door",
    "pairing": "the pairing door",
    "upload": "the upload door",
    "relay": "the relay mailbox",
    "ws": "the relay socket",
}

#: The door's one-word label, for a sentence that supplies its own noun
#: (`event_log.sentence("access_burst")` says "at the {door} door").
DOOR_LABELS = {
    "lan": "Wi-Fi",
    "pairing": "pairing",
    "upload": "upload",
    "relay": "relay",
    "ws": "socket",
}

#: The reason, in the words a person reads. The five the relay shares are
#: the relay's own words, so the log and the refusal agree.
REASON_WORDS = {
    "plaintext": "a request in the old unsealed shape",
    "origin": "a browser-shaped request",
    "unpaired": "a phone that is not paired here",
    "kind": "a verified envelope of the wrong kind",
    "rate": "envelopes arriving too fast",
    "pair_code": "a pairing attempt with the wrong code",
    "pair_channel": "a pairing attempt that did not match the open pairing",
    "not_pair": "a sealed request that was not a pairing request",
    "blob": "an upload that could not be opened",
    "not_found": "a request for a route that does not exist",
    "pair_plain": "a typed-address pairing while type-in pairing was off",
    "tunnel": "a request that arrived over a VPN or tunnel address",
    "busy": "too many connections open at once",
    "pake": "a typed-address pairing request in an unknown or out-of-step shape",
}
REASON_WORDS.update({k: v for k, v in relay.REFUSAL_WORDS.items()
                     if k in REASONS})


def door_word(door: str) -> str:
    return DOOR_WORDS.get(str(door or ""), "the phone door")


def door_label(door: str) -> str:
    return DOOR_LABELS.get(str(door or ""), "phone")


def _minutes(window_seconds) -> str:
    try:
        seconds = int(float(window_seconds))
    except (TypeError, ValueError):
        seconds = BURST_WINDOW_SECONDS
    minutes = max(1, (seconds + 59) // 60)
    return f"{minutes} minute" + ("s" if minutes != 1 else "")


def _peer_word(peer) -> str:
    text = " ".join(str(peer or "").split())
    return text or "an unknown source"


def sentence(kind: str, **fields) -> str:
    """The one human line about an access event, composed here and nowhere
    else; both clients draw `text` verbatim."""
    door = str(fields.get("door") or "")
    peer = _peer_word(fields.get("peer"))
    if kind == "refusal":
        reason = str(fields.get("reason") or "")
        words = REASON_WORDS.get(reason, "a request it could not accept")
        return f"{door_word(door)} refused {peer}: {words}"
    if kind == "burst":
        try:
            count = int(fields.get("count") or 0)
        except (TypeError, ValueError):
            count = 0
        peers = _peers(fields.get("peers"), default=1)
        if peers > 1:
            # Folds span past `window_seconds`, so no window clause; the
            # stored `burst` line (peers absent or 1) keeps the line below.
            # At `MAX_FOLDED_PEERS` the daemon stopped counting sources, so
            # the figure is a floor and says so.
            others = peers - 1
            return (f"{count} refused attempts from {peer} and {_at_least(peers)}"
                    f"{others} other source{'s' if others != 1 else ''} at "
                    f"{door_word(door)}")
        window = _minutes(fields.get("window_seconds"))
        return (f"{count} refused attempts from {peer} at {door_word(door)} "
                f"in {window}")
    if kind == "burst_ack":
        return f"the alert about {peer} was acknowledged"
    if kind == "burst_more":
        try:
            count = int(fields.get("count") or 0)
        except (TypeError, ValueError):
            count = 0
        peers = _peers(fields.get("peers"), default=0)
        return (f"a burst from {peer} was folded into the open alert: {count} "
                f"refused attempts from {_at_least(peers)}{peers} "
                f"source{'s' if peers != 1 else ''} so far")
    if kind == "timing":
        return _timing_sentence(door, fields.get("count"),
                                fields.get("window_seconds"),
                                fields.get("hops"))
    return ""


def _secs(value) -> str:
    """A number of seconds in the words a person reads: two figures, a
    floor under a hundredth, never scientific notation."""
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        seconds = 0.0
    if seconds < 0.0005:
        return "under 0.001 s"
    if seconds >= 99.5:
        # `.2g` would print `1e+02` from 99.5 up: whole seconds instead.
        return f"{seconds:.0f} s"
    return f"{seconds:.2g} s"


def _timing_sentence(door: str, count, window_seconds, hops) -> str:
    """One ten-minute link-timing rollup as a sentence. The relay's hops
    are pickup (`dwell`), handling (`run`) and the answer's POST
    (`answer`); the socket's are the seal's opening (`open`), handling
    and the answer down the line (`answer`); the LAN door's are the seal's
    opening (`open`), handling and the reply's sealing (`seal`) — each
    door names what it measured."""
    try:
        count = int(count or 0)
    except (TypeError, ValueError):
        count = 0
    figures = hops if isinstance(hops, dict) else {}

    def _fig(name: str) -> float:
        try:
            return max(0.0, float(figures.get(name) or 0.0))
        except (TypeError, ValueError):
            return 0.0

    head = (f"{door_label(door)}: {count} request{'s' if count != 1 else ''} "
            f"in {_minutes(window_seconds if window_seconds else BURST_WINDOW_SECONDS)}")
    if door == "relay":
        return (f"{head} — picked up in {_secs(_fig('dwell_p50'))} typical, "
                f"{_secs(_fig('dwell_p90'))} slow; handled in "
                f"{_secs(_fig('run_p50'))}; answered in "
                f"{_secs(_fig('answer_p50'))}")
    if door == "ws":
        # The socket has no mailbox: no dwell, no hold. Opening the seal,
        # handling and the sealed answer down the line are its three legs.
        return (f"{head} — opened in {_secs(_fig('open_p50'))} typical, "
                f"{_secs(_fig('open_p90'))} slow; handled in "
                f"{_secs(_fig('run_p50'))}; answered in "
                f"{_secs(_fig('answer_p50'))}")
    return (f"{head} — opened in {_secs(_fig('open_p50'))} typical, "
            f"{_secs(_fig('open_p90'))} slow; handled in "
            f"{_secs(_fig('run_p50'))}; sealed in {_secs(_fig('seal_p50'))}")


def _at_least(peers: int) -> str:
    """``"at least "`` once the distinct-source figure hit `MAX_FOLDED_PEERS`
    — the daemon counts no further, so the number is a floor — and ``""``
    below it, keeping every existing sentence byte-identical."""
    return "at least " if peers >= MAX_FOLDED_PEERS else ""


def _peers(value, *, default: int) -> int:
    try:
        peers = int(value or 0)
    except (TypeError, ValueError):
        peers = 0
    return peers if peers > 0 else default


# --- the store -----------------------------------------------------------------

def _has_forbidden(value, depth: int = 0) -> Optional[str]:
    """The first forbidden key found anywhere in ``value``, or None."""
    if depth > 8:
        return None
    if isinstance(value, dict):
        for k, v in value.items():
            if str(k) in FORBIDDEN_KEYS:
                return str(k)
            hit = _has_forbidden(v, depth + 1)
            if hit:
                return hit
    elif isinstance(value, (list, tuple)):
        for v in value:
            hit = _has_forbidden(v, depth + 1)
            if hit:
                return hit
    return None


def clamp_peer(peer) -> str:
    """One line, no control characters, at most `MAX_PEER_CHARS`."""
    text = " ".join(str(peer or "").split())
    text = "".join(ch for ch in text if ch.isprintable())
    return text[:MAX_PEER_CHARS]


class AccessLog:
    """The journal. `open()` before use; every method is thread-safe."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = Path(path) if path is not None else ACCESS_LOG_PATH
        self._lock = threading.Lock()
        self._entries: deque = deque()
        self._lines = 0
        self._closed = False

    @property
    def path(self) -> Path:
        return self._path

    # -- lifecycle --

    def open(self) -> None:
        """Load the file — tolerating a truncated last line and unknown keys —
        then prune. A file that will not parse at all is started afresh."""
        entries: list = []
        lines = 0
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                for line in fh:
                    lines += 1
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(row, dict) or "id" not in row:
                        continue
                    entries.append({k: row[k] for k in PUBLISHED_KEYS if k in row})
        except FileNotFoundError:
            pass
        except OSError:
            logger.warning("access log unreadable; starting empty", exc_info=True)
        entries.sort(key=lambda e: float(e.get("ts") or 0.0))
        with self._lock:
            self._entries = deque(entries)
            self._lines = lines
            self._closed = False
            if any(self._prune_locked()):
                self._rewrite()

    def close(self) -> None:
        with self._lock:
            self._closed = True

    # -- writes --

    def append(self, kind: str, **fields) -> Optional[dict]:
        """Record one line. Returns the entry, or None on a refusal: an
        unknown kind, door or reason, or a forbidden key at any depth."""
        if kind not in KINDS:
            logger.warning("access log refused unknown kind %r", kind)
            return None
        hit = _has_forbidden(fields)
        if hit:
            logger.warning("access log refused a %s entry carrying %r", kind, hit)
            return None
        door = str(fields.get("door") or "")
        reason = str(fields.get("reason") or "")
        alert_id = str(fields.get("alert_id") or "")
        if kind in ("refusal", "burst", "burst_more", "timing") and door not in DOORS:
            logger.warning("access log refused a %s entry on door %r", kind, door)
            return None
        hops: dict = {}
        if kind == "timing":
            given = fields.get("hops")
            if not isinstance(given, dict):
                logger.warning("access log refused a timing entry with no hops")
                return None
            for name, value in given.items():
                if str(name) not in HOP_KEYS:
                    logger.warning("access log refused a timing entry "
                                   "carrying hop %r", name)
                    return None
                try:
                    hops[str(name)] = float(value)
                except (TypeError, ValueError):
                    logger.warning("access log refused a timing entry whose "
                                   "%r is not a number", name)
                    return None
        if kind == "refusal" and reason not in REASONS:
            logger.warning("access log refused a refusal for reason %r", reason)
            return None
        if kind in ("burst_ack", "burst_more") and not alert_id:
            logger.warning("access log refused a %s naming no alert", kind)
            return None
        try:
            count = int(fields.get("count") or 0)
        except (TypeError, ValueError):
            count = 0
        try:
            window = int(fields.get("window_seconds") or 0)
        except (TypeError, ValueError):
            window = 0
        try:
            peers = int(fields.get("peers") or 0)
        except (TypeError, ValueError):
            peers = 0
        if kind != "burst_more":
            peers = 0
        peer = clamp_peer(fields.get("peer"))
        entry = {
            "id": str(fields.get("id") or "") or secrets.token_hex(6),
            "ts": float(fields.get("ts") or time.time()),
            "kind": kind,
            "door": door,
            "peer": peer,
            "reason": reason if kind == "refusal" else "",
            "device_id": clamp_peer(fields.get("device_id")),
            "text": sentence(kind, door=door, peer=peer, reason=reason,
                             count=count, window_seconds=window, peers=peers,
                             hops=hops),
            "count": count,
            "window_seconds": window,
            "alert_id": alert_id,
            "peers": peers,
            "hops": hops,
        }
        with self._lock:
            if self._closed:
                return None
            self._entries.append(entry)
            self._write_line(entry)
            aged, _capped = self._prune_locked()
            # The file is compacted once it runs `PRUNE_SLACK` lines past
            # what is held — never once per append at the cap, which with
            # the timing belt on the same file would be a full rewrite and
            # fsync per refused knock.
            if aged or self._lines >= len(self._entries) + PRUNE_SLACK:
                self._rewrite()
        return dict(entry)

    def prune(self) -> bool:
        with self._lock:
            dropped = any(self._prune_locked())
            if dropped:
                self._rewrite()
            return dropped

    # -- reads --

    def recent(self, since: Optional[float] = None,
               limit: Optional[int] = None, *,
               include_timing: bool = False,
               timing_limit: int = TIMING_READ_LIMIT) -> list:
        """Newest first; ``ts`` strictly greater than ``since`` when given;
        at most ``limit``. Copies. The retention floor applies on a read
        too: the age prune runs only on a write. `timing` rollups are
        left out unless asked for, so a reader that predates them — an
        older phone, the panel's window — never sees one; asked for, they
        ride **beside** ``limit`` under their own ``timing_limit``, so a
        few days of rollups can never crowd the refusals out of a read."""
        floor = time.time() - RETENTION_SECONDS
        with self._lock:
            rows = [r for r in self._entries
                    if float(r.get("ts") or 0.0) >= floor]
        rows.reverse()
        if since is not None:
            rows = [r for r in rows if float(r.get("ts") or 0.0) > float(since)]
        timing = [r for r in rows if r.get("kind") == "timing"]
        rows = [r for r in rows if r.get("kind") != "timing"]
        if limit is not None:
            rows = rows[:max(0, int(limit))]
        if include_timing:
            timing = timing[:max(0, int(timing_limit))]
            rows = sorted(rows + timing,
                          key=lambda r: float(r.get("ts") or 0.0), reverse=True)
        return [dict(r) for r in rows]

    def open_alerts(self, now: Optional[float] = None) -> list:
        """Burst alerts nobody has acknowledged and that have not aged out,
        oldest first — what `security_snapshot` publishes.

        This is the one place the folds are merged: each row carries the
        newest `burst_more` line's `count` and `peers` for its alert, that
        line's `ts` as `last_seen`, and a `text` recomposed from those
        totals — the stored `burst` line is never rewritten. A row with no
        fold reads `peers = 1` and `last_seen = ts`. The loopback
        `access_log` read and the snapshot both read this, so they agree.
        """
        now = time.time() if now is None else float(now)
        floor = now - ALERT_TTL_SECONDS
        with self._lock:
            acked = {str(r.get("alert_id") or "") for r in self._entries
                     if r.get("kind") == "burst_ack"}
            latest_more: dict = {}
            for r in self._entries:
                if r.get("kind") != "burst_more":
                    continue
                ref = str(r.get("alert_id") or "")
                held = latest_more.get(ref)
                if held is None or float(r.get("ts") or 0.0) >= float(held.get("ts") or 0.0):
                    latest_more[ref] = r
            rows = [dict(r) for r in self._entries
                    if r.get("kind") == "burst"
                    and float(r.get("ts") or 0.0) >= floor
                    and str(r.get("id") or "") not in acked]
        for row in rows:
            more = latest_more.get(str(row.get("id") or ""))
            if more is None:
                row["peers"] = 1
                row["last_seen"] = float(row.get("ts") or 0.0)
                continue
            row["count"] = int(more.get("count") or 0)
            row["peers"] = max(1, int(more.get("peers") or 0))
            row["last_seen"] = float(more.get("ts") or 0.0)
            row["text"] = sentence("burst", door=row.get("door"),
                                   peer=row.get("peer"), count=row["count"],
                                   window_seconds=row.get("window_seconds"),
                                   peers=row["peers"])
        return rows

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    # -- internals, lock held --

    def _prune_locked(self) -> tuple:
        floor = time.time() - RETENTION_SECONDS
        aged = False
        while self._entries and float(self._entries[0].get("ts") or 0.0) < floor:
            self._entries.popleft()
            aged = True
        capped = False
        # The timing belt runs first and counts nothing else: past
        # `MAX_TIMING_ENTRIES` the oldest rollups go, and a rollup is never
        # counted against the refusal cap or the alert belt below.
        timing_lines = sum(1 for e in self._entries if e.get("kind") == "timing")
        excess = timing_lines - MAX_TIMING_ENTRIES
        if excess > 0:
            kept: deque = deque()
            for entry in self._entries:
                if excess > 0 and entry.get("kind") == "timing":
                    excess -= 1
                    capped = True
                    continue
                kept.append(entry)
            self._entries = kept
            timing_lines = MAX_TIMING_ENTRIES
        excess = len(self._entries) - timing_lines - MAX_ENTRIES
        if excess > 0:
            # Evict the oldest refusals only; the alert lines survive the
            # cap so a burst of refusals cannot close the alert it raised.
            kept: deque = deque()
            for entry in self._entries:
                if excess > 0 and entry.get("kind") == "refusal":
                    excess -= 1
                    capped = True
                    continue
                kept.append(entry)
            self._entries = kept
        # A fold line is part of its alert: only the newest per alert is
        # kept, one naming no `burst` on the file goes, and what survives is
        # never counted against the belt — so a flood of folds can neither
        # grow the file nor evict an older open alert.
        burst_ids_now = {str(e.get("id") or "") for e in self._entries
                         if e.get("kind") == "burst"}
        newest_more: dict = {}
        for e in self._entries:
            if e.get("kind") != "burst_more":
                continue
            ref = str(e.get("alert_id") or "")
            held = newest_more.get(ref)
            if held is None or float(e.get("ts") or 0.0) >= float(held.get("ts") or 0.0):
                newest_more[ref] = e
        if newest_more:
            kept = deque()
            for e in self._entries:
                if e.get("kind") == "burst_more":
                    ref = str(e.get("alert_id") or "")
                    if ref not in burst_ids_now or newest_more.get(ref) is not e:
                        capped = True
                        continue
                kept.append(e)
            self._entries = kept
        alert_lines = sum(1 for e in self._entries
                          if e.get("kind") in ("burst", "burst_ack"))
        excess = alert_lines - MAX_ALERT_ENTRIES
        if excess > 0:
            # The belt, in two passes. First the lines nobody is waiting on:
            # a burst that was acknowledged or has aged past `ALERT_TTL_SECONDS`
            # goes together with the ack that closed it (and an ack whose
            # burst is already gone goes too), oldest first. Only when the
            # belt is still exceeded does the second pass take the oldest
            # *open* alerts — so 200 closed alerts can never push an
            # unacknowledged, in-date one off the end.
            alert_floor = time.time() - ALERT_TTL_SECONDS
            burst_ids = {str(e.get("id") or "") for e in self._entries
                         if e.get("kind") == "burst"}
            closed = {str(e.get("alert_id") or "") for e in self._entries
                      if e.get("kind") == "burst_ack"}
            closed |= {str(e.get("id") or "") for e in self._entries
                       if e.get("kind") == "burst"
                       and float(e.get("ts") or 0.0) < alert_floor}

            def _closed(entry: dict) -> bool:
                kind = entry.get("kind")
                if kind == "burst":
                    return str(entry.get("id") or "") in closed
                if kind in ("burst_ack", "burst_more"):
                    ref = str(entry.get("alert_id") or "")
                    return ref in closed or ref not in burst_ids
                return False

            # A fold line goes with its alert in either pass and spends no
            # budget of its own: it was not counted above.
            gone_bursts: set = set()
            kept = deque()
            for entry in self._entries:
                if entry.get("kind") == "burst_more":
                    if (str(entry.get("alert_id") or "") in gone_bursts
                            or (excess > 0 and _closed(entry))):
                        capped = True
                        continue
                    kept.append(entry)
                    continue
                if excess > 0 and _closed(entry):
                    excess -= 1
                    capped = True
                    if entry.get("kind") == "burst":
                        gone_bursts.add(str(entry.get("id") or ""))
                    continue
                kept.append(entry)
            self._entries = kept
            if excess > 0:
                kept = deque()
                for entry in self._entries:
                    kind = entry.get("kind")
                    if kind == "burst_more":
                        if str(entry.get("alert_id") or "") in gone_bursts:
                            capped = True
                            continue
                        kept.append(entry)
                        continue
                    if excess > 0 and kind != "refusal":
                        excess -= 1
                        capped = True
                        if kind == "burst":
                            gone_bursts.add(str(entry.get("id") or ""))
                        continue
                    kept.append(entry)
                self._entries = kept
            if gone_bursts:
                # The file is ts-sorted, so a fold never precedes its burst
                # and the walks above already took it; this is the safety
                # net for a hand-edited file, so nothing orphaned survives
                # to the next prune.
                kept = deque(e for e in self._entries
                             if not (e.get("kind") == "burst_more"
                                     and str(e.get("alert_id") or "") in gone_bursts))
                if len(kept) != len(self._entries):
                    capped = True
                    self._entries = kept
        return aged, capped

    def _write_line(self, entry: dict) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
                fh.flush()
            self._lines += 1
        except OSError:
            logger.warning("access log append failed", exc_info=True)

    def _rewrite(self) -> None:
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                for entry in self._entries:
                    fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self._path)
            self._lines = len(self._entries)
        except OSError:
            logger.warning("access log rewrite failed", exc_info=True)
            try:
                os.unlink(tmp)
            except OSError:
                pass


# --- the detector ---------------------------------------------------------------

class BurstDetector:
    """The burst rule, pure and in memory. `note` is called on the loop for
    every refusal and answers with an alert dict or None; nothing here
    touches a file or a clock — `now` is handed in.

    Two clocks: one per peer (`ALERT_COOLDOWN_SECONDS`) and one machine-wide
    (`ALERT_FLOOR_SECONDS`, `_raised_at`). A burst that clears its peer's
    cooldown but lands inside the floor answers ``{"fold": True, ...}`` —
    fold this into the open alert — instead of the alert dict; the raise
    answer carries no `fold` key at all. The floor is on raising, knows
    nothing about acknowledgements, and is memory only: a daemon restart
    resets it and the next burst raises afresh.
    """

    def __init__(self) -> None:
        self._hits: "OrderedDict[str, deque]" = OrderedDict()
        self._alerted_at: dict = {}
        self._raised_at: Optional[float] = None

    def note(self, peer, reason: str, now: float, *,
             device_id: str = "") -> Optional[dict]:
        """Weigh one refusal; an alert dict when it completes a burst.

        ``device_id`` is the paired phone the door resolved the frame to,
        where it did: an `oversize` refusal naming one is a keyholder's
        too-large upload (the body rule opened the frame before refusing
        the body) and weighs nothing, like `ctr` / `ts` / `kind`. Every
        other reason keeps its `WEIGHTS` entry whatever rides beside it —
        an `unpaired` or `seal` refusal never carries a device id, and a
        keyword that could zero a stranger's knock would be a hole.
        """
        reason = str(reason or "")
        if WEIGHTS.get(reason, 1) <= 0:
            return None
        if reason == "oversize" and clamp_peer(device_id):
            return None
        key = clamp_peer(peer)
        hits = self._hits.get(key)
        if hits is None:
            hits = deque(maxlen=BURST_THRESHOLD)
            self._hits[key] = hits
        hits.append(float(now))
        self._hits.move_to_end(key)
        while len(self._hits) > MAX_TRACKED_PEERS:
            gone, _ = self._hits.popitem(last=False)
            self._alerted_at.pop(gone, None)
        if len(hits) < BURST_THRESHOLD:
            return None
        if float(now) - hits[0] > BURST_WINDOW_SECONDS:
            return None
        last = self._alerted_at.get(key)
        if last is not None and float(now) - last < ALERT_COOLDOWN_SECONDS:
            return None
        # The peer's own cooldown is spent whether this raises or folds: a
        # peer that folded must not fold again five knocks later.
        self._alerted_at[key] = float(now)
        hits.clear()
        if (self._raised_at is not None
                and float(now) - self._raised_at < ALERT_FLOOR_SECONDS):
            return {"fold": True, "peer": key, "count": BURST_THRESHOLD,
                    "window_seconds": BURST_WINDOW_SECONDS}
        self._raised_at = float(now)
        return {"peer": key, "count": BURST_THRESHOLD,
                "window_seconds": BURST_WINDOW_SECONDS}

    def tracked(self) -> int:
        return len(self._hits)

    def raised_at(self) -> Optional[float]:
        """When the last alert was *raised* (never a fold) — the test seam
        for the machine-wide floor."""
        return self._raised_at

    def hits(self, peer) -> int:
        """How many stamps the detector holds for ``peer`` — the test seam
        for "a weight-0 reason never inserts the peer"."""
        hits = self._hits.get(clamp_peer(peer))
        return len(hits) if hits is not None else 0
