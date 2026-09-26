# host/dark_army_daemon/relay.py
"""The away path's protocol and key store — pure, small, and network-free.

The phone reaches the Mac at home over ``_handle_lan_client``; away from home
it drops sealed envelopes into a dumb mailbox (``relay/api/box.js``) that the
Mac polls. This module is everything both ends must agree on: the key store on
the Mac's disk, the frame layout on the wire, and the away window that being
at home arms. Nothing here opens a socket — ``relay_client.py`` does that —
and nothing here trusts the mailbox with anything but ciphertext.

**The mailbox is not the boundary; the AEAD is.** Frames are
ChaCha20-Poly1305 over zlib-compressed JSON, keyed per device with a secret
minted on the LAN at pairing time and never sent to the relay. The AAD binds
protocol version, channel and direction, so a frame cannot be replayed back
at its sender; a per-direction monotonic counter refuses replays outright,
with a ±``RELAY_SKEW_SECONDS`` timestamp window as the backstop for counter
loss. The channel id is derived from the key but is deliberately **not** a
credential: knowing it buys ciphertext and denial of service only.

**The lease never exists at the relay.** It is a number in ``relay.json``,
armed only by a successful device resolve on the LAN socket, and checked on
the Mac's own clock per write frame. Expiry bounds what the phone may *do*,
never what it may *see* — un-pairing is the real off-switch, and ``forget``
is what makes it bite mid-window.

**How long one check-in buys is a per-device grant**, ``lease_days`` on the
channel entry, chosen by the person at this Mac from ``LEASE_DAY_CHOICES``.
``note_lan_proof`` reads it and is still the **one** site that may move an
expiry forward; ``set_lease_days`` may only clamp down, so nothing the phone
does from away can lengthen its own window.

The Swift mirror is ``ios/BobPhone/RelayTransport.swift``; the HKDF info
strings and the AAD prefix are pinned byte-for-byte by
``tests/test_phone_remote.py``, and fixed test vectors in
``tests/test_relay_protocol.py`` are the cross-platform contract.

**The home path shares the format under its own namespace and key.** A
``Namespace`` is the four byte strings a frame is derived and bound with;
``RELAY`` is today's literals, ``HOME`` is the ``bob-home`` family. Every function
here takes ``ns=RELAY`` so the existing calls and vectors are byte-identical,
and a frame sealed under one namespace can never open under the other — the
HKDF info and the AAD both differ. The home key itself lives in
``devices.json`` (``devices.py``), never here; ``seal_blob`` / ``open_blob``
are the upload body's raw AEAD, bound to the header frame that carried it.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import secrets
import time
import zlib
from typing import NamedTuple

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from . import paths

logger = logging.getLogger("dark-army")

STORE_VERSION = 1

#: One day, in seconds — the *unit* the away window is measured in, not the
#: policy. A check-in on the LAN arms ``lease_days(device)`` of these; the
#: length itself is a per-device grant chosen on the Mac.
RELAY_LEASE_SECONDS = 86400
#: The lengths a person may grant one phone, in days. Shipped in code: there
#: is no free-text length and nothing longer than a fortnight.
LEASE_DAY_CHOICES = (1, 3, 7, 14)
#: What a channel written before grants existed means, and what a fresh
#: pairing starts on — today's behaviour, exactly.
LEASE_DEFAULT_DAYS = 1
#: The timestamp window either end will accept, both ways. The counter is the
#: replay guard; this is the backstop for counter-state loss.
RELAY_SKEW_SECONDS = 300
#: A lease advance smaller than this is not written to disk — the phone polls
#: every few seconds at home, and a disk write per poll buys nothing.
RELAY_LEASE_REFRESH_SECONDS = 600
#: The largest sealed frame either end will open. Under the mailbox's own
#: 1 MB value cap, with room for base64.
RELAY_FRAME_MAX_BYTES = 900_000
#: Mac-side token buckets, applied by the connector before any decryption
#: work: frames per device per minute, and executed writes per device per
#: minute. The relay's own 240/min cap is a DoS filter, not the boundary.
RELAY_MAX_FRAMES_PER_MINUTE = 60
RELAY_MAX_WRITES_PER_MINUTE = 10
#: Raw keystrokes from the phone's terminal (`terminal_input` with `bytes`)
#: draw on their **own** bucket of this size, beside the writes bucket above
#: and never from it: a person typing into a terminal from away must not
#: spend the minute's allowance an Approve or a reply needs, and a burst of
#: answers must not strand keys already typed. The phone mirrors this number
#: (`AwayKeys.perMinute`) and batches under it; the frame bucket above still
#: bounds everything a device sends.
RELAY_MAX_KEY_WRITES_PER_MINUTE = 10


def write_bucket_kind(payload: dict) -> str:
    """Which per-device write bucket an opened action frame draws on:
    ``"keys"`` for raw terminal keystrokes, ``"writes"`` for every other
    action — the line route (`text`) included, which is a whole sentence
    and keeps its older rules. The test is the executor's own
    (``_sealed_run`` takes the raw route only for a non-empty string), so
    an empty or odd ``bytes`` beside a ``text`` line cannot charge that
    line to the keys bucket."""
    raw = payload.get("bytes")
    if (payload.get("action") == "terminal_input"
            and isinstance(raw, str) and raw):
        return "keys"
    return "writes"

#: The sentence a lapsed lease refuses a write with — verbatim on the phone,
#: so the shared prefix in ``Actions.swift`` must match its opening words.
LEASE_REFUSAL = ("away access has lapsed — check in on home Wi-Fi "
                 "to renew it")

#: Wire directions. Phone-to-Mac frames are sealed under one derived key,
#: Mac-to-phone under another, so a reflected frame fails its seal.
DIR_PHONE_TO_MAC = "p2m"
DIR_MAC_TO_PHONE = "m2p"

#: HKDF info strings and the AAD prefix — the cross-platform contract.
#: ``RelayTransport.swift`` carries the same bytes; change one and every
#: paired phone goes deaf.
INFO_CHANNEL_ID = b"bob-relay v1 channel-id"
INFO_P2M = b"bob-relay v1 p2m"
INFO_M2P = b"bob-relay v1 m2p"
AAD_PREFIX = b"bob-relay v1|"


class Namespace(NamedTuple):
    """The four byte strings one sealed path is derived and bound with. Two
    exist and they must never be merged: a home frame sealed under the
    relay's strings would open on the mailbox path, and the other way round."""
    info_channel_id: bytes
    info_p2m: bytes
    info_m2p: bytes
    aad_prefix: bytes


#: The away path — today's literals, unchanged, the default everywhere.
RELAY = Namespace(INFO_CHANNEL_ID, INFO_P2M, INFO_M2P, AAD_PREFIX)
#: The home path. Same format, its own strings, its own key.
HOME = Namespace(
    b"bob-home v1 channel-id",
    b"bob-home v1 p2m",
    b"bob-home v1 m2p",
    b"bob-home v1|",
)

#: Accepted read-frame counters are persisted at most this often; accepted
#: *write* frames persist immediately, so a crash cannot reopen a window for
#: a write replay. The skew window covers the read gap.
RECV_CTR_PERSIST_SECONDS = 30
#: The Mac's send counter is persisted as a reservation this far ahead of
#: what was actually used, so a crash can skip counters but never reuse one.
SEND_CTR_RESERVE = 256

_NONCE_BYTES = 12
_TAG_BYTES = 16
_KEY_BYTES = 32

#: Raw DEFLATE (no zlib header, no adler32 trailer), because that is the one
#: dialect Python's zlib and Apple's Compression framework share out of the
#: box — ``COMPRESSION_ZLIB`` is raw DEFLATE despite its name, and a wrapped
#: stream from the default ``zlib.compress`` would fail to inflate on the
#: phone. Pinned by the roundtrip tests; do not "fix" the wbits.
_DEFLATE_WBITS = -15


def _deflate(data: bytes) -> bytes:
    deflater = zlib.compressobj(wbits=_DEFLATE_WBITS)
    return deflater.compress(data) + deflater.flush()


# ── crypto ────────────────────────────────────────────────────────────────────

def mint_key() -> bytes:
    """A new channel secret. 32 random bytes, held by exactly two devices."""
    return secrets.token_bytes(_KEY_BYTES)


def _hkdf(key: bytes, info: bytes) -> bytes:
    """HKDF-SHA256, empty salt — the RFC 5869 default, which is also what
    CryptoKit computes when handed an empty salt, so the two ends agree."""
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
                info=info).derive(key)


def channel_id(key: bytes, *, ns: Namespace = RELAY) -> str:
    """The mailbox address for this key: 16 derived bytes as 32 hex chars.

    Derived, not random, so both ends compute it from the one shared secret;
    and deliberately not a credential — it buys ciphertext, DoS and one
    held-poll window per ``PEER_REARM_SECONDS`` (`note_phone_arrived`) only.
    """
    return _hkdf(key, ns.info_channel_id)[:16].hex()


def direction_key(key: bytes, direction: str, *,
                  ns: Namespace = RELAY) -> bytes:
    """The sealing key for one direction of one channel."""
    if direction == DIR_PHONE_TO_MAC:
        return _hkdf(key, ns.info_p2m)
    if direction == DIR_MAC_TO_PHONE:
        return _hkdf(key, ns.info_m2p)
    raise ValueError(f"unknown direction: {direction!r}")


def _aad(chan: str, direction: str, *, ns: Namespace = RELAY) -> bytes:
    return (ns.aad_prefix + chan.encode("ascii") + b"|"
            + direction.encode("ascii"))


def seal_frame(key: bytes, direction: str, ctr: int, kind: str, body,
               *, ts: float | None = None, frame_id: str = "",
               ns: Namespace = RELAY) -> str:
    """One sealed envelope, ready for the mailbox.

    Wire layout is ``base64(nonce ‖ ciphertext‖tag)`` — CryptoKit's
    ``ChaChaPoly`` combined-box layout, so the Swift side seals and opens
    with its stock API. The nonce is random per frame; the counter lives
    *inside* the sealed frame, for replay ordering only, so a counter bug can
    never become nonce reuse.

    A wire over ``RELAY_FRAME_MAX_BYTES`` is refused **here**, as ``""``: the
    other end refuses an oversize envelope unopened on every poll, so sending
    one would read as a permanently dead away path rather than one failed
    answer. The caller decides what to say instead (``relay_client._answer``
    seals a small ``err`` naming the refusal).
    """
    frame = {
        "v": 1,
        "ctr": int(ctr),
        "ts": float(ts if ts is not None else time.time()),
        "id": frame_id or secrets.token_hex(8),
        "kind": str(kind),
        "body": body,
    }
    payload = _deflate(
        json.dumps(frame, separators=(",", ":")).encode("utf-8"))
    nonce = os.urandom(_NONCE_BYTES)
    sealed = ChaCha20Poly1305(direction_key(key, direction, ns=ns)).encrypt(
        nonce, payload, _aad(channel_id(key, ns=ns), direction, ns=ns))
    wire = base64.b64encode(nonce + sealed).decode("ascii")
    if len(wire) > RELAY_FRAME_MAX_BYTES:
        logger.warning("relay frame over the cap was not sealed "
                       "(%d bytes)", len(wire))
        return ""
    return wire


#: ``open_frame`` refusal codes, and the words each is worth. ``ctr`` is the
#: one the connector answers (with ``ctr_expected``, sealed to the phone);
#: everything else is dropped and counted.
REFUSAL_WORDS = {
    "oversize": "an envelope was too large",
    "seal": "an envelope failed verification",
    "shape": "an envelope was not a frame",
    "ctr": "an envelope replayed an old counter",
    "ts": "an envelope's timestamp was too far from now",
}


def open_frame(key: bytes, direction: str, wire: str, last_ctr: int,
               *, now: float | None = None, ns: Namespace = RELAY):
    """``(frame, refusal)`` — the frame dict and ``""``, or ``None`` and a
    code from ``REFUSAL_WORDS``.

    Refuses, in order: oversize, a broken seal, a frame that is not a frame,
    a counter at or below the last accepted one, and a timestamp outside
    ±``RELAY_SKEW_SECONDS``. The seal is checked before the counter so a
    forger learns nothing about counter state from the refusal.
    """
    text = str(wire or "")
    if len(text) > RELAY_FRAME_MAX_BYTES:
        return None, "oversize"
    try:
        raw = base64.b64decode(text, validate=True)
    except (ValueError, TypeError):
        return None, "seal"
    if len(raw) < _NONCE_BYTES + _TAG_BYTES:
        return None, "seal"
    nonce, sealed = raw[:_NONCE_BYTES], raw[_NONCE_BYTES:]
    try:
        payload = ChaCha20Poly1305(
            direction_key(key, direction, ns=ns)).decrypt(
            nonce, sealed, _aad(channel_id(key, ns=ns), direction, ns=ns))
    except InvalidTag:
        return None, "seal"
    # Bounded inflate: a compressed body that expands past the frame cap is
    # refused mid-stream rather than allocated.
    inflater = zlib.decompressobj(wbits=_DEFLATE_WBITS)
    try:
        plain = inflater.decompress(payload, RELAY_FRAME_MAX_BYTES)
    except zlib.error:
        return None, "shape"
    if inflater.unconsumed_tail:
        return None, "oversize"
    try:
        frame = json.loads(plain.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None, "shape"
    if not isinstance(frame, dict) or frame.get("v") != 1:
        return None, "shape"
    try:
        ctr = int(frame.get("ctr"))
        ts = float(frame.get("ts"))
    except (TypeError, ValueError):
        return None, "shape"
    if ctr <= int(last_ctr):
        return None, "ctr"
    moment = time.time() if now is None else float(now)
    if abs(ts - moment) > RELAY_SKEW_SECONDS:
        return None, "ts"
    return frame, ""


def _blob_aad(key: bytes, direction: str, frame_id: str,
              ns: Namespace) -> bytes:
    return (ns.aad_prefix + channel_id(key, ns=ns).encode("ascii") + b"|"
            + direction.encode("ascii") + b"|blob|"
            + str(frame_id or "").encode("utf-8"))


#: Exactly what `seal_blob` adds to its input: the nonce in front and the
#: Poly1305 tag behind, and nothing else — ChaCha20 ciphertext is
#: plaintext-length, and this route has no deflate and no base64. A caller
#: sizing a payload to fit a bounded frame (`terminal_stream.SealedCodec`)
#: reads this rather than a private name or a guess.
BLOB_OVERHEAD_BYTES = _NONCE_BYTES + _TAG_BYTES


def seal_blob(key: bytes, direction: str, frame_id: str, data: bytes,
              *, ns: Namespace = HOME) -> bytes:
    """A raw sealed body — ``nonce ‖ ciphertext ‖ tag``, no base64 and no
    deflate — for the one route whose payload is a file rather than JSON.

    The AAD carries the **frame id** of the header frame that describes the
    upload, so a blob is bound to exactly one request: two uploads in flight
    cannot have their bodies swapped, and a captured body cannot be replayed
    under another header. The seal alone does not give this; the AAD does.
    """
    nonce = os.urandom(_NONCE_BYTES)
    sealed = ChaCha20Poly1305(direction_key(key, direction, ns=ns)).encrypt(
        nonce, bytes(data or b""), _blob_aad(key, direction, frame_id, ns))
    return nonce + sealed


def open_blob(key: bytes, direction: str, frame_id: str, raw: bytes,
              *, ns: Namespace = HOME):
    """The plaintext of ``seal_blob``'s output, or ``None`` on any refusal —
    wrong key, wrong direction, wrong frame id, or bytes that are not a
    blob at all. There is no counter here: the header frame carried it."""
    raw = bytes(raw or b"")
    if len(raw) < _NONCE_BYTES + _TAG_BYTES:
        return None
    nonce, sealed = raw[:_NONCE_BYTES], raw[_NONCE_BYTES:]
    try:
        return ChaCha20Poly1305(direction_key(key, direction, ns=ns)).decrypt(
            nonce, sealed, _blob_aad(key, direction, frame_id, ns))
    except InvalidTag:
        return None


# ── the store ─────────────────────────────────────────────────────────────────
#
# `relay.json`: {"version": 1, "url": "", "push_secret": "",
#     "channels": {device_id: {
#     "key_b64": …, "created_at": …, "lease_expires_at": …,
#     "lease_days": …, "send_ctr": …, "recv_ctr": …, "last_frame_at": …}}}
# Devices.py's shape throughout: memoised on (mtime_ns, size), atomic 0600
# writes, unknown keys carried through untouched (the sessions.json rule).

_cache: tuple = (None, None)

#: In-memory counter overlays, so read-frame accepts and answer sends do not
#: cost a disk write each. ``{device_id: (ctr, last_persist_monotonic)}`` for
#: recv; ``{device_id: ctr}`` for send, with the stored value a reservation.
_recv_mem: dict = {}
_send_mem: dict = {}
#: When each channel last *carried* a phone-to-Mac frame, wall clock. The
#: stored value rides ``note_recv_ctr``'s existing persistence, which
#: coalesces read frames; this overlay keeps the live answer fresh between
#: those writes so the connector's idle/active decision is never a persist
#: window behind the truth. ``{device_id: epoch_seconds}``.
_frame_mem: dict = {}
#: When the socket relay last told the Mac the phone arrived on its line
#: (`peer:1`, `relay_ws.py`), wall clock, memory only. The phone opens its
#: socket the moment it wakes, so this is the earliest word the Mac gets
#: that a person picked the phone up — before any frame is sent. The
#: mailbox connector arms its held polls on it for ``PEER_ARM_SECONDS`` and
#: is woken out of an idle sleep by `_peer_waiters`, so a request that falls
#: through to the mailbox is not left behind a thirty-second idle gap.
_peer_mem: dict = {}
#: One event per device the mailbox connector's idle sleep waits on.
_peer_waiters: dict = {}
#: When the socket relay last said the phone left its line (`peer:0`),
#: wall clock, memory only. A departure ends the arm at once: a phone that
#: opened its line at home and closed it on the home answer costs the
#: mailbox seconds of held polling, not ``PEER_ARM_SECONDS``.
_peer_left_mem: dict = {}
#: When the phone last proved itself on the socket lane — a verified frame
#: (`relay_ws._handle_wire`) — wall clock, memory only. A socket frame
#: deliberately stamps no `last_frame_at`, so this is that lane's proof for
#: `note_phone_arrived`'s budget. A home request is deliberately **not** a
#: proof here: a phone open at home proves itself every few seconds, and a
#: stranger reconnecting as the phone would then re-arm on every one
#: (security review, 25 Sep 2026). Only the away lanes reset the budget.
_proof_mem: dict = {}
#: `peer:1` is the relay's word, not a proof: anybody holding the channel
#: id can connect as the phone and make the relay say it. So a `peer:1`
#: arms the held polls at most once per this many seconds, unless the
#: phone proved itself away — a verified mailbox or socket frame — since the last
#: arm. A real phone proves itself on every away wake it uses; a stranger
#: reconnecting in a loop buys one ``PEER_ARM_SECONDS`` window per half
#: hour (≈ 5% of the time held), not a mailbox held for ever.
PEER_REARM_SECONDS = 1800.0


def note_phone_proof(device_id: str) -> None:
    """The socket lane verified a frame from this device."""
    did = str(device_id or "")
    if did:
        _proof_mem[did] = time.time()


def note_phone_left(device_id: str) -> None:
    """The socket relay said the phone left its line: the arm ends."""
    did = str(device_id or "")
    if did:
        _peer_left_mem[did] = time.time()


def note_phone_arrived(device_id: str) -> bool:
    """The socket relay said the phone is on the line: stamp it and wake
    the mailbox connector's idle sleep for that device — within the
    `PEER_REARM_SECONDS` budget. Returns whether it armed."""
    did = str(device_id or "")
    if not did:
        return False
    now = time.time()
    last = float(_peer_mem.get(did, 0.0))
    proved = max(last_frame_at(did), float(_proof_mem.get(did, 0.0)))
    if last and now - last < PEER_REARM_SECONDS and proved <= last:
        return False
    _peer_mem[did] = now
    waiter = _peer_waiters.get(did)
    if waiter is not None:
        waiter.set()
    return True


def last_phone_arrived_at(device_id: str) -> float:
    """`note_phone_arrived`'s stamp, epoch seconds, while the phone is still
    on its line; ``0.0`` if never, or once a `peer:0` came after it."""
    did = str(device_id or "")
    arrived = float(_peer_mem.get(did, 0.0))
    if float(_peer_left_mem.get(did, 0.0)) >= arrived:
        return 0.0
    return arrived


def phone_arrival_waiter(device_id: str) -> "asyncio.Event":
    """The event `note_phone_arrived` sets for one device, made on first
    use — on the daemon loop, the only caller."""
    did = str(device_id or "")
    waiter = _peer_waiters.get(did)
    if waiter is None:
        waiter = asyncio.Event()
        _peer_waiters[did] = waiter
    return waiter


def _revision(path):
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def load() -> dict:
    """The store, memoised on its own ``(mtime_ns, size)``. Never raises: an
    unreadable store is an empty one, which fails closed (no channels)."""
    global _cache
    path = paths.RELAY_PATH
    rev = _revision(path)
    cached_rev, cached = _cache
    if cached is not None and rev == cached_rev:
        return cached
    data: dict = {"version": STORE_VERSION, "url": "", "channels": {}}
    if rev is not None:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                data = raw
        except (OSError, ValueError):
            logger.warning("relay store unreadable; no channels exist")
    if not isinstance(data.get("channels"), dict):
        data["channels"] = {}
    if not isinstance(data.get("url"), str):
        data["url"] = ""
    if not isinstance(data.get("push_secret"), str):
        data["push_secret"] = ""
    # The socket relay's address (`relay_ws.py`), a new key beside `url`:
    # an older build never reads it and `save` carries it through.
    if not isinstance(data.get("ws_url"), str):
        data["ws_url"] = ""
    _cache = (rev, data)
    return data


def invalidate() -> None:
    """Drop the memoised store. For tests, and for anything that just wrote."""
    global _cache
    _cache = (None, None)


def reset() -> None:
    """Forget everything in memory — cache and counter overlays. For tests."""
    global _cache, _recv_mem, _send_mem, _frame_mem
    _cache = (None, None)
    _recv_mem = {}
    _send_mem = {}
    _frame_mem = {}


def save(data: dict) -> bool:
    """Write the store atomically at 0600 — ``devices.save``'s shape."""
    paths.ensure_state_dir()
    path = paths.RELAY_PATH
    payload = json.dumps(data, indent=2).encode("utf-8")
    if not path.exists():
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            try:
                os.write(fd, payload)
            finally:
                os.close(fd)
            invalidate()
            return True
        except FileExistsError:
            pass
        except OSError:
            logger.warning("could not write the relay store", exc_info=True)
            return False
    tmp = path.with_name(path.name + ".tmp")
    try:
        fd = os.open(str(tmp), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        try:
            os.write(fd, payload)
        finally:
            os.close(fd)
        os.replace(str(tmp), str(path))
    except OSError:
        logger.warning("could not write the relay store", exc_info=True)
        try:
            os.unlink(str(tmp))
        except OSError:
            pass
        return False
    invalidate()
    return True


def _channel(device_id: str) -> dict | None:
    entry = load().get("channels", {}).get(str(device_id or ""))
    return entry if isinstance(entry, dict) else None


def create_channel(device_id: str) -> str:
    """Mint a channel for one device and return the key, base64.

    Called from the pair handler, which runs on the LAN socket — pairing *is*
    the at-home proof, so the lease starts armed. The returned key crosses
    the LAN once, inside the pair response, and is never written anywhere but
    ``relay.json`` and the phone's Keychain.
    """
    did = str(device_id or "").strip()
    if not did:
        return ""
    key = mint_key()
    data = load()
    channels = dict(data.get("channels") or {})
    channels[did] = {
        "key_b64": base64.b64encode(key).decode("ascii"),
        "created_at": time.time(),
        "lease_expires_at": time.time() + RELAY_LEASE_SECONDS,
        # A fresh pairing starts on the default grant, so today's behaviour
        # is byte-identical and the published field is there from the first
        # frame. The grant dies with `forget`, exactly like the key.
        "lease_days": LEASE_DEFAULT_DAYS,
        "send_ctr": 0,
        "recv_ctr": 0,
        # Pairing is a strong signal the phone is about to be used, so a
        # fresh channel starts inside the connector's active window rather
        # than paying an idle wake-up on its very first away request.
        "last_frame_at": time.time(),
    }
    data["channels"] = channels
    if not save(data):
        return ""
    _recv_mem.pop(did, None)
    _send_mem.pop(did, None)
    _frame_mem.pop(did, None)
    return base64.b64encode(key).decode("ascii")


def channel_key(device_id: str) -> bytes | None:
    """The channel secret, or None. Re-read from the store per call — the
    ``devices.resolve`` discipline, so ``forget`` bites on the next frame."""
    entry = _channel(device_id)
    if entry is None:
        return None
    try:
        key = base64.b64decode(str(entry.get("key_b64") or ""), validate=True)
    except (ValueError, TypeError):
        return None
    return key if len(key) == _KEY_BYTES else None


def channel_ids() -> dict:
    """``{device_id: channel_id}`` for every stored channel with a usable key."""
    out = {}
    for did in list(load().get("channels") or {}):
        key = channel_key(did)
        if key is not None:
            out[did] = channel_id(key)
    return out


def forget(device_id: str) -> None:
    """Delete one channel — key, lease and counters. The away kill switch."""
    did = str(device_id or "")
    data = load()
    channels = dict(data.get("channels") or {})
    if did not in channels:
        return
    del channels[did]
    data["channels"] = channels
    save(data)
    _recv_mem.pop(did, None)
    _send_mem.pop(did, None)
    _frame_mem.pop(did, None)


def note_push_token(device_id: str, token: str, env: str) -> bool:
    """Record (or clear) one phone's APNs token on its channel entry.

    The token rides the existing channel entry — the sessions.json rule, so
    an older build carries the keys through untouched — and therefore dies
    whole with ``forget``: un-pairing is the push kill switch too. An empty
    token deletes the three keys (the phone's own unregister). ``False``
    when this device has no channel, which is a phone paired before the away
    path existed (or while ``remote_access`` was off): no channel means no
    mailbox to route a buzz through, so there is nothing to record.
    """
    did = str(device_id or "")
    if _channel(did) is None:
        return False
    data = load()
    channels = dict(data.get("channels") or {})
    entry = dict(channels.get(did) or {})
    if not entry:
        return False
    if not token:
        entry.pop("push_token", None)
        entry.pop("push_env", None)
        entry.pop("push_updated_at", None)
    else:
        entry["push_token"] = str(token)
        entry["push_env"] = str(env or "prod")
        entry["push_updated_at"] = time.time()
    channels[did] = entry
    data["channels"] = channels
    return save(data)


def push_token(device_id: str) -> str | None:
    """The stored APNs token, or None. Re-read from the store per call — the
    ``channel_key`` discipline, so ``forget`` bites on the very next buzz."""
    entry = _channel(device_id)
    if entry is None:
        return None
    token = str(entry.get("push_token") or "")
    return token or None


def push_env(device_id: str) -> str:
    """``"prod"`` or ``"dev"`` — which APNs host this token is valid against.
    Defaults to prod, matching a TestFlight build's token."""
    entry = _channel(device_id)
    if entry is None:
        return "prod"
    env = str(entry.get("push_env") or "")
    return env if env in ("prod", "dev") else "prod"


def note_activity_token(device_id: str, token: str, env: str, shape: int = 1) -> bool:
    """Record (or clear) one phone's Live Activity **update** token.

    `note_push_token`'s twin, on the same channel entry and under the same
    discipline: the keys ride the entry an older build carries through
    untouched, die whole with ``forget``, and an empty token deletes all
    three plus ``activity_shape`` (the phone's own unregister, sent when
    its activity ends). The token is per *activity*, not per phone —
    ActivityKit mints a fresh one for every activity the app starts — so a
    registration always overwrites. ``shape`` is which card the phone
    draws (1 face-only, 2 fleet); an older caller omits it and stores 1.
    ``False`` when this device has no channel: no mailbox, nothing to route
    an update through.
    """
    did = str(device_id or "")
    if _channel(did) is None:
        return False
    data = load()
    channels = dict(data.get("channels") or {})
    entry = dict(channels.get(did) or {})
    if not entry:
        return False
    if not token:
        entry.pop("activity_token", None)
        entry.pop("activity_env", None)
        entry.pop("activity_updated_at", None)
        entry.pop("activity_shape", None)
    else:
        entry["activity_token"] = str(token)
        entry["activity_env"] = str(env or "prod")
        entry["activity_updated_at"] = time.time()
        entry["activity_shape"] = int(shape)
    channels[did] = entry
    data["channels"] = channels
    return save(data)


def activity_token(device_id: str) -> str | None:
    """The stored Live Activity update token, or None. Re-read from the
    store per call — `push_token`'s rule, so an unregister or a `forget`
    bites on the very next update."""
    entry = _channel(device_id)
    if entry is None:
        return None
    token = str(entry.get("activity_token") or "")
    return token or None


def activity_env(device_id: str) -> str:
    """``"prod"`` or ``"dev"`` — which APNs host `activity_token`'s token is
    valid against; `push_env`'s rule, defaulting to prod."""
    entry = _channel(device_id)
    if entry is None:
        return "prod"
    env = str(entry.get("activity_env") or "")
    return env if env in ("prod", "dev") else "prod"


def activity_shape(device_id: str) -> int:
    """Which card this phone draws: 1 face-only, 2 the fleet.

    The stored ``activity_shape`` when it is 1 or 2; otherwise 1. An entry
    written by an older build carries no key and reads 1, so a phone that
    never said which card it draws keeps receiving the face-only card.
    Re-read from the store per call, `activity_token`'s discipline. Never
    published on a snapshot.
    """
    entry = _channel(device_id)
    if entry is None:
        return 1
    raw = entry.get("activity_shape", 1)
    if isinstance(raw, bool) or not isinstance(raw, int) or raw not in (1, 2):
        return 1
    return raw


def lease_days(device_id: str) -> int:
    """How many days one check-in at home buys this phone.

    ``0`` means two different things that behave the same way — no channel
    at all, or a grant the person ended — and both arm nothing. An entry
    written before grants existed carries no key and reads as
    ``LEASE_DEFAULT_DAYS``: upgrading must never silently revoke a paired
    phone's away access. Re-read from the store per call, the
    ``channel_key`` discipline.
    """
    entry = _channel(device_id)
    if entry is None:
        return 0
    raw = entry.get("lease_days")
    if isinstance(raw, bool) or not isinstance(raw, int):
        return LEASE_DEFAULT_DAYS
    if raw == 0 or raw in LEASE_DAY_CHOICES:
        return raw
    return LEASE_DEFAULT_DAYS


def lock_screen_actions(device_id: str) -> bool:
    """Whether this phone may answer a buzz from its lock screen — the
    per-device opt-in that lets a push carry the prompt's id. Default off;
    an entry written before the switch existed reads as off. Re-read from
    the store per call, the ``channel_key`` discipline."""
    entry = _channel(device_id)
    if entry is None:
        return False
    return entry.get("lock_screen_actions") is True


def set_lock_screen_actions(device_id: str, enabled: bool) -> tuple:
    """Let this phone answer a permission or acknowledge a waiting agent
    from its lock screen, or stop. ``(ok, detail)``.

    A consent switch at the desk, on purpose: with it on, the buzz carries
    the prompt's id and the session's id — two opaque identifiers, still no
    words — and the phone's Allow / Deny / Acknowledge buttons post the
    same verbs the app posts, behind iOS's own unlock on the action, the
    pairing, the lease and the receipt token. Off, the push is byte-identical
    to today's and the buttons open the app.
    """
    did = str(device_id or "")
    if _channel(did) is None:
        return False, "that phone is not paired"
    if not isinstance(enabled, bool):
        return False, "that is not a switch position"
    data = load()
    channels = dict(data.get("channels") or {})
    entry = dict(channels.get(did) or {})
    if not entry:
        return False, "that phone is not paired"
    entry["lock_screen_actions"] = enabled
    channels[did] = entry
    data["channels"] = channels
    if not save(data):
        return False, "could not record the switch"
    return True, ""


def set_lease_days(device_id: str, days: int) -> tuple:
    """Grant this phone a length, or end its away access. ``(ok, detail)``.

    **It may only clamp the running window down, never out.** Shortening (or
    ending) bites at once; lengthening is applied by the next check-in at
    home, which is ``note_lan_proof``'s job and nobody else's. Written that
    way deliberately: a setter that stamped ``now + days`` would be a second
    arming site, and one reachable by a verb.
    """
    did = str(device_id or "")
    if _channel(did) is None:
        return False, "that phone is not paired"
    if isinstance(days, bool) or not isinstance(days, int):
        return False, "that is not a length Dark Army offers"
    if days != 0 and days not in LEASE_DAY_CHOICES:
        return False, "that is not a length Dark Army offers"
    data = load()
    channels = dict(data.get("channels") or {})
    entry = dict(channels.get(did) or {})
    if not entry:
        return False, "that phone is not paired"
    entry["lease_days"] = days
    have = float(entry.get("lease_expires_at") or 0.0)
    if days == 0:
        entry["lease_expires_at"] = 0.0
    else:
        entry["lease_expires_at"] = min(
            have, time.time() + days * RELAY_LEASE_SECONDS)
    channels[did] = entry
    data["channels"] = channels
    if not save(data):
        return False, "could not record the away window"
    return True, ""


def note_lan_proof(device_id: str) -> None:
    """A device was just seen at home — re-arm its away window.

    **The one site that may move an expiry forward.** The length is the
    device's own grant; at ``0`` the person ended away access for this phone
    and coming home must not reopen it.

    Persisted only when the advance is worth more than
    ``RELAY_LEASE_REFRESH_SECONDS``, so the phone's 4-second home poll does
    not cost a disk write each. That guard compares an *advance*, so it is
    length-agnostic — and it is why shortening lives in ``set_lease_days``
    and never here. No channel, no lease: a device paired before the away
    path existed simply has nothing to arm.
    """
    entry = _channel(device_id)
    if entry is None:
        return
    days = lease_days(device_id)
    if days <= 0:
        return
    want = time.time() + days * RELAY_LEASE_SECONDS
    have = float(entry.get("lease_expires_at") or 0.0)
    if want - have <= RELAY_LEASE_REFRESH_SECONDS:
        return
    data = load()
    channels = dict(data.get("channels") or {})
    updated = dict(channels.get(str(device_id or "")) or {})
    updated["lease_expires_at"] = want
    channels[str(device_id or "")] = updated
    data["channels"] = channels
    save(data)


def lease_valid(device_id: str, now: float | None = None) -> bool:
    """Whether this device may still *act* from away. Reads may continue
    regardless — expiry bounds doing, not seeing."""
    return lease_expires_at(device_id) > (time.time() if now is None
                                          else float(now))


def lease_expires_at(device_id: str) -> float:
    entry = _channel(device_id)
    if entry is None:
        return 0.0
    return float(entry.get("lease_expires_at") or 0.0)


def set_url(url: str, push_secret=None) -> tuple:
    """Record the mailbox address. ``(ok, detail)``, refusal in words.

    ``https://`` or nothing: the envelopes are sealed either way, but plain
    HTTP would hand every network on the path the channel ids and the traffic
    pattern for free. An empty address clears the setting.

    ``push_secret`` is the mailbox's ``PUSH_SECRET``, stored beside the
    address because they name the same deployment. ``None`` or blank leaves
    the stored secret alone — the panel cannot pre-fill a field it is never
    shown, so an untouched field must not wipe the secret — and clearing the
    address clears the secret with it: a secret for no mailbox is a leftover.
    """
    text = str(url or "").strip().rstrip("/")
    if text and not text.startswith("https://"):
        return False, "the relay address must start with https://"
    data = load()
    data["url"] = text
    secret = str(push_secret or "").strip()
    if not text:
        data["push_secret"] = ""
    elif secret:
        data["push_secret"] = secret
    if not save(data):
        return False, "could not record the relay address"
    return True, ""


def get_url() -> str:
    return str(load().get("url") or "")


def get_push_secret() -> str:
    """The mailbox's push password. For the connector's push POST alone —
    never a snapshot, never a log line."""
    return str(load().get("push_secret") or "")


#: The socket address refusal, in words — the panel's sheet shows it verbatim.
WS_URL_REFUSAL = "the socket address must start with wss://"


def set_ws_url(url: str) -> tuple:
    """Record the socket relay's address (`relay-ws/`). ``(ok, detail)``,
    refusal in words.

    ``wss://`` or nothing, `set_url`'s rule for the same reason: the frames
    are sealed either way, but a plain socket would hand every network on
    the path the channel id and the traffic pattern for free. An empty
    address clears the setting; nothing else in the store moves — same
    key, same counters, same channels (`docs/relay-socket-contract.md`).
    """
    text = str(url or "").strip().rstrip("/")
    # The connector appends `/ws` itself, so an address pasted with the
    # endpoint already on it (`wss://host/ws`, `wss://host/ws/`) is stored
    # without it — the Mac would otherwise dial `/ws/ws` (a relay 400) while
    # the phone's `RelaySocket.url` tolerates either. Only a path segment is
    # stripped, never a host that happens to be named `ws`.
    if text.count("/") > 2 and text.endswith("/ws"):
        text = text[:-3].rstrip("/")
    if text and not text.startswith("wss://"):
        return False, WS_URL_REFUSAL
    data = load()
    data["ws_url"] = text
    if not save(data):
        return False, "could not record the socket address"
    return True, ""


def get_ws_url() -> str:
    return str(load().get("ws_url") or "")


# ── counters ──────────────────────────────────────────────────────────────────

def last_recv_ctr(device_id: str) -> int:
    """The highest phone-to-Mac counter accepted so far."""
    did = str(device_id or "")
    entry = _channel(did)
    stored = int(float((entry or {}).get("recv_ctr") or 0))
    mem = _recv_mem.get(did)
    return max(stored, mem[0]) if mem else stored


def note_recv_ctr(device_id: str, ctr: int, durable: bool = False, *,
                  liveness: bool = True) -> None:
    """Record an accepted phone-to-Mac counter.

    ``durable`` is set for every accepted **write** frame — persisted
    immediately, so a crash cannot reopen a window for a write replay. Read
    frames coalesce to one write per ``RECV_CTR_PERSIST_SECONDS``; the
    timestamp window backstops the gap.

    ``liveness`` is the mailbox connector's default: an accepted frame also
    stamps `last_frame_at`, the restart-safe leg of its idle/active pacing.
    The socket lane (`relay_ws.py`) passes ``False``: same counter, so a
    frame replayed across the two paths is still refused, but the mailbox
    keeps its own pacing and idles down while the phone lives on the socket
    — the Upstash saving the socket is meant to show.
    """
    did = str(device_id or "")
    current = last_recv_ctr(did)
    value = max(int(ctr), current)
    mono = time.monotonic()
    carried = time.time()
    mem = _recv_mem.get(did)
    _recv_mem[did] = (value, mem[1] if mem else 0.0)
    # The liveness overlay is stamped on *every* accepted mailbox frame, even
    # the ones whose counter write coalesces away: "when did away last work?"
    # must not lag the truth by a persist window.
    if liveness:
        _frame_mem[did] = carried
    if not durable and mem is not None \
            and mono - mem[1] < RECV_CTR_PERSIST_SECONDS:
        return
    data = load()
    channels = dict(data.get("channels") or {})
    entry = dict(channels.get(did) or {})
    if not entry:
        return
    entry["recv_ctr"] = value
    if liveness:
        entry["last_frame_at"] = carried
    channels[did] = entry
    data["channels"] = channels
    if save(data):
        _recv_mem[did] = (value, mono)


def last_frame_at(device_id: str) -> float:
    """When this channel last carried a verified phone-to-Mac frame, epoch
    seconds; ``0.0`` if it never has.

    Restart-safe by construction — the stored value survives a daemon restart
    and the overlay covers the coalescing window — which is why it is the
    restart-safe leg of the connector's idle/active pacing. The other leg is
    the connector's in-memory push-ok stamp, forgotten on restart and on a
    ``remote_access`` toggle by decision: a buzz is stale in seconds, and the
    phone's first frame re-arms the window through this value.
    """
    did = str(device_id or "")
    entry = _channel(did)
    stored = float((entry or {}).get("last_frame_at") or 0.0)
    return max(stored, float(_frame_mem.get(did, 0.0)))


def next_send_ctr(device_id: str) -> int:
    """The next Mac-to-phone counter, crash-safe by reservation.

    The stored ``send_ctr`` is a ceiling on what may ever have been used, not
    the last value used: when the in-memory counter reaches it, another
    ``SEND_CTR_RESERVE`` block is persisted first. A crash therefore skips
    counters — harmless, the phone checks ``>`` — and can never reuse one.
    """
    did = str(device_id or "")
    entry = _channel(did)
    if entry is None:
        return 0
    reserved = int(float(entry.get("send_ctr") or 0))
    used = _send_mem.get(did, reserved)
    nxt = used + 1
    if nxt >= reserved:
        data = load()
        channels = dict(data.get("channels") or {})
        fresh = dict(channels.get(did) or {})
        if not fresh:
            return 0
        fresh["send_ctr"] = nxt + SEND_CTR_RESERVE
        channels[did] = fresh
        data["channels"] = channels
        if not save(data):
            return 0
    _send_mem[did] = nxt
    return nxt
