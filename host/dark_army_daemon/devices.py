# host/dark_army_daemon/devices.py
"""Paired phones, and the home key that says so.

This is the first time Dark Army's data leaves loopback. The LAN listener on
``0.0.0.0:19875`` is the door; this module is the lock. A phone is paired by
redeeming a two-minute, single-use code shown as a QR on the Mac. Pairing
mints two things for the device: a **token** (its digest alone is stored; the
phone keeps it as its pairing-generation guard, and no door reads it any
more) and a 32-byte **home key**, under which every later request on that
socket is a sealed frame (``relay.seal_frame`` in the ``relay.HOME``
namespace). The home key is stored here in the clear because the Mac has to
open frames with it; ``paths._PRIVATE_FILES`` keeps the file 0600.

Copied from ``enrollment.py``: ``mint`` / ``digest``, a JSON ledger memoised on
``(mtime_ns, size)``, ``resolve`` matching digests with ``hmac.compare_digest``
and failing closed twice (empty token, empty ledger), ``snapshot`` that states
``available`` and never carries a digest or a key.

**The honest claim.** The boundary on that socket is the AEAD under a key that
never crossed the wire on any route: by QR the key rides the square the Mac
shows, and from a typed address both sides derive it from an SRP-6a exchange
over the pairing code (``srp.py``, ``pairing_srp_start`` /
``pairing_srp_finish``), so what a stranger on the Wi-Fi sees is the
transcript and never the key. A copied header buys nothing: the channel id is
derived from the key and is not a credential. This is still an
enrolment-and-revocation boundary, not authentication against a hostile local
process. Un-pairing bites on the next frame because the key is read from the
ledger per request, never cached as a verdict.

The two home counters live on the device's own ledger entry, separate from
the relay's pair in ``relay.json``: the relay channel exists only when the
away switch was on at pairing, and the two keys are two independent
sequences — one shared counter would let a stale relay refusal bounce home
frames, and a home poll every four seconds would advance a relay ``recv_ctr``
the relay never used.

``mint_device`` records a row with no pairing code. The loopback bot pair
is the only caller; a QR showing on the Mac is left showing, and the new
row is the same shape ``_spend`` writes so un-pair and an older build both
still read it.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time

from . import paths, relay, srp

logger = logging.getLogger("dark-army")

LEDGER_VERSION = 1
PAIRING_CODE_SECONDS = 120
PAIRING_FAIL_LIMIT = 5
MAX_DEVICES = 8
MAX_DEVICE_NAME_CHARS = 40

#: The typed-address pair's two bounds, per pairing code. `SRP_MAX_INFLIGHT`
#: is how many started exchanges (`A` → `(b, B)`) the pairing holds at once
#: — a fifth start evicts the oldest; `SRP_MAX_STARTS` is how many starts one
#: code takes at all (one modpow on the loop each, ~3.4 ms), after which the
#: door answers `PAKE_BUSY_REFUSAL` until a new code is minted.
SRP_MAX_INFLIGHT = 4
SRP_MAX_STARTS = 32

#: Fixed-length so ``compare_digest`` never raises on a length mismatch — a
#: wrong-length code is refused *before* the comparison, same as an empty one.
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_CODE_LEN = 8


# ── the secret ────────────────────────────────────────────────────────────────

def mint() -> str:
    """A new device token. ``token_urlsafe(32)`` — ``api_server``'s own size."""
    return secrets.token_urlsafe(32)


def digest(key: str) -> str:
    """The ledger's record of a token. SHA-256 hex, and the *only* form of a
    token that is ever written to Dark Army's own disk or published anywhere."""
    return hashlib.sha256(str(key or "").encode("utf-8")).hexdigest()


# ── the ledger ────────────────────────────────────────────────────────────────

#: ``(st_mtime_ns, st_size)`` of the ledger the cached parse belongs to, plus
#: the parse itself. Enrollment's revision-keyed shape.
_cache: tuple = (None, None)

#: The one active pairing code. Memory only — a restart forgets it, which is
#: the same as expiry. Overwritten by every ``begin``.
_pairing: dict | None = None

#: In-memory home-counter overlays, ``relay.py``'s shape: ``{device_id:
#: (ctr, last_persist_monotonic)}`` for recv, ``{device_id: ctr}`` for send
#: with the stored value a reservation.
_home_recv_mem: dict = {}
_home_send_mem: dict = {}
#: ``{home channel id: device_id}``, memoised on the ledger revision — the
#: door resolves a device from its ``X-Bob-Channel`` header on every request.
_channel_map: tuple = (None, {})


def _revision(path):
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def load() -> dict:
    """The ledger, memoised on its own ``(mtime_ns, size)``.

    Never raises: an unreadable or malformed ledger is an empty one, which
    fails closed (nothing is paired) rather than open.
    """
    global _cache
    path = paths.DEVICES_PATH
    rev = _revision(path)
    cached_rev, cached = _cache
    if cached is not None and rev == cached_rev:
        return cached
    data: dict = {"version": LEDGER_VERSION, "devices": []}
    if rev is not None:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                data = raw
        except (OSError, ValueError):
            logger.warning("device ledger unreadable; nothing is paired")
    entries = data.get("devices")
    if not isinstance(entries, list):
        data["devices"] = []
    else:
        data["devices"] = [d for d in entries if isinstance(d, dict)]
    _cache = (rev, data)
    return data


def invalidate() -> None:
    """Drop the memoised ledger. For tests, and for anything that just wrote."""
    global _cache
    _cache = (None, None)


def reset_pairing() -> None:
    """Drop the in-flight pairing code.

    The guess-close and ``stop_lan`` burn, and the test teardown. A restart
    does the same by forgetting ``_pairing``.
    """
    global _pairing
    _pairing = None


def pairing_open() -> bool:
    """True while a pairing code is showing and still spendable.

    False for a used code, an expired-but-not-reset dict (timeout's shape),
    and after ``reset_pairing()``.
    """
    pairing = _pairing
    if pairing is None or pairing.get("used"):
        return False
    return time.time() < float(pairing.get("expires_at") or 0)


def reset() -> None:
    """Forget everything in memory — cache, pairing code and counter
    overlays. For tests."""
    global _cache, _pairing, _home_recv_mem, _home_send_mem, _channel_map
    _cache = (None, None)
    _pairing = None
    _home_recv_mem = {}
    _home_send_mem = {}
    _channel_map = (None, {})


def save(data: dict) -> bool:
    """Write the ledger atomically at 0600. First create uses ``O_EXCL``;
    later writes tmp+replace, enrollment's shape, so a crash mid-save cannot
    truncate a live file to empty."""
    paths.ensure_state_dir()
    path = paths.DEVICES_PATH
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
            logger.warning("could not write the device ledger", exc_info=True)
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
        logger.warning("could not write the device ledger", exc_info=True)
        try:
            os.unlink(str(tmp))
        except OSError:
            pass
        return False
    invalidate()
    return True


def paired() -> list:
    """Every paired device, oldest first. Digests included — this is the
    internal view; ``snapshot()`` is what may be published."""
    return list(load().get("devices") or [])


# ── the gate ──────────────────────────────────────────────────────────────────

def resolve(token: str) -> str:
    """The paired device id whose token this is, or "".

    Fails closed twice over: an empty token returns before any comparison, and
    an empty ledger has nothing to match. Compared with ``hmac.compare_digest``.
    """
    text = str(token or "").strip()
    if not text:
        return ""
    entries = paired()
    if not entries:
        return ""
    want = digest(text)
    for entry in entries:
        stored = str(entry.get("digest") or "")
        if not stored:
            continue
        if hmac.compare_digest(stored, want):
            return str(entry.get("id") or "")
    return ""


# ── pair / unpair ─────────────────────────────────────────────────────────────

def _mint_code() -> str:
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LEN))


def begin(name_hint: str = "", *, allow_plain: bool = False) -> str:
    """Mint a new pairing code, replacing any previous unexpired one.

    One active code at a time: a second press on Pair burns the first QR.

    ``allow_plain`` arms the **typed-address** pair branch — a phone typing
    the address and code, which proves it knows the code over SRP-6a
    (``pairing_srp_start`` / ``pairing_srp_finish``) and derives the home
    key from the exchange — for this one pairing. It lives on the
    ``_pairing`` dict and nowhere else, so it dies with the code:
    ``reset_pairing()``, the fail limit, ``stop_lan``, a restart and every
    later ``begin()`` drop it. It is never persisted and never published.

    Beside the code the pairing carries its SRP half: ``pairing_id`` (the
    identity), ``srp_salt``, the ``srp_verifier`` computed from the code,
    the in-flight ``A → (b, B)`` table and its start count. All memory
    only, all dying with the code, none on the snapshot.
    """
    global _pairing
    code = _mint_code()
    pairing_id = secrets.token_hex(8)
    salt = secrets.token_bytes(16)
    _pairing = {
        "code": code,
        "expires_at": time.time() + PAIRING_CODE_SECONDS,
        "used": False,
        "fails": 0,
        "name_hint": str(name_hint or ""),
        # The home key rides the QR beside the code, so a phone that scans
        # the square already holds it and seals its very first request.
        "home_key": secrets.token_bytes(32),
        "plain_ok": bool(allow_plain),
        "pairing_id": pairing_id,
        "srp_salt": salt,
        "srp_verifier": srp.verifier(srp.PRODUCTION, pairing_id, code, salt),
        "srp_inflight": {},
        "srp_starts": 0,
    }
    return code


def pairing_allows_plain() -> bool:
    """Whether the active pairing was armed for a typed-address pair.

    Exactly the dict's lifetime: False once the pairing is reset or
    replaced. The typed branch checks this before it reads a byte of the
    phone's body; expiry and "already used" are still the redeem's to judge.
    """
    pairing = _pairing
    return pairing is not None and bool(pairing.get("plain_ok"))


# ── the typed-address pair: SRP-6a over the code ─────────────────────────────

def _hex_of_length(text, length: int) -> bool:
    """Exactly ``length`` lowercase hex characters — the shape every
    group element and proof arrives in. Checked before any arithmetic, and
    not secret-dependent."""
    value = str(text or "")
    return len(value) == length and all(c in "0123456789abcdef" for c in value)


def pairing_srp_start(A_hex: str) -> tuple:
    """The typed pair's first step. ``(reply, reason)``.

    ``reply`` is ``{"pairing": id, "salt": hex, "B": hex}`` on success and
    ``reason`` is then empty. Refusals, in order: no pairing or one not
    armed for typing (the door's gate is before this and never reaches it —
    the wrong-code sentence), a used code, an expired code, the start
    budget spent (``(None, "")`` — the door's ``PAKE_BUSY_REFUSAL``), and a
    malformed ``A`` (not exactly the group's length in lowercase hex, or
    ``A mod N == 0``: ``"shape"``), and an ``A`` already in flight (also
    ``"shape"``: ``A`` is public, so a neighbour replaying the phone's own
    start must not evict the phone's entry and turn its right finish into
    a counted fail; a real phone draws a fresh ``a`` per press and never
    repeats one). Each start draws a fresh ``b`` (one modpow), stores
    ``(b, B)`` under ``A``, evicting the oldest past ``SRP_MAX_INFLIGHT``,
    and steps the start count. No fail is counted: no proof was judged.
    """
    pairing = _pairing
    if pairing is None or not pairing.get("plain_ok"):
        return None, "that pairing code is not valid"
    if pairing.get("used"):
        return None, "that pairing code has already been used"
    if time.time() >= float(pairing.get("expires_at") or 0):
        return None, "that pairing code has expired"
    if int(pairing.get("srp_starts") or 0) >= SRP_MAX_STARTS:
        return None, ""
    group = srp.PRODUCTION.group
    if not _hex_of_length(A_hex, group.byte_len * 2):
        return None, "shape"
    A = int(A_hex, 16)
    if A >= group.N or A % group.N == 0:
        return None, "shape"
    inflight = pairing.setdefault("srp_inflight", {})
    if A_hex in inflight:
        return None, "shape"
    b, B = srp.server_start(srp.PRODUCTION, int(pairing["srp_verifier"]))
    inflight[A_hex] = (b, B)
    while len(inflight) > SRP_MAX_INFLIGHT:
        oldest = next(iter(inflight))
        del inflight[oldest]
    pairing["srp_starts"] = int(pairing.get("srp_starts") or 0) + 1
    return {
        "pairing": str(pairing["pairing_id"]),
        "salt": bytes(pairing["srp_salt"]).hex(),
        "B": srp.pad(group, B).hex(),
    }, ""


def pairing_srp_finish(A_hex: str, M1_hex: str) -> tuple:
    """The typed pair's second step. ``(K, M2, "")`` on a proof that
    checks out, else ``(None, None, reason)``.

    Pops the in-flight entry for ``A`` — **every finish consumes its entry,
    hit or miss** — and answers ``"shape"`` when there is none (no start,
    a reset or a new code in between, a second finish of the same ``A``)
    or ``M1`` is not 64 lowercase hex. Computes ``u``, ``S``, ``K`` and the
    expected ``M1`` (two modpows), compares with ``hmac.compare_digest``;
    a mismatch counts one fail under ``PAIRING_FAIL_LIMIT`` exactly as a
    wrong code did and answers ``"code"``. Used and expired are not judged
    here: ``redeem_proven`` refuses them in the redeem's own words.
    """
    pairing = _pairing
    if pairing is None:
        return None, None, "shape"
    inflight = pairing.get("srp_inflight")
    entry = inflight.pop(str(A_hex or ""), None) if isinstance(inflight, dict) else None
    if entry is None or not _hex_of_length(M1_hex, 64):
        return None, None, "shape"
    b, B = entry
    params = srp.PRODUCTION
    A = int(A_hex, 16)
    u_value = srp.u(params, A, B)
    if u_value == 0:
        return None, None, "shape"
    S = srp.server_secret(params, A, b, int(pairing["srp_verifier"]), u_value)
    K = srp.session_key(params, S)
    expected = srp.client_proof(params, str(pairing["pairing_id"]),
                                bytes(pairing["srp_salt"]), A, B, K)
    if not hmac.compare_digest(expected, bytes.fromhex(M1_hex)):
        _note_fail(pairing)
        return None, None, "code"
    return K, srp.server_proof(params, A, expected, K), ""


def pairing_home_key():
    """The home key of the active pairing, or ``None`` when there is none.
    Handed to the panel (for the QR) and to the sealed pair branch (to open
    the phone's first frame)."""
    pairing = _pairing
    if pairing is None:
        return None
    key = pairing.get("home_key")
    return key if isinstance(key, bytes) and len(key) == 32 else None


def _note_fail(pairing: dict) -> None:
    """Count one live mismatch. At ``PAIRING_FAIL_LIMIT``, burn the window."""
    if pairing.get("used") or time.time() >= float(pairing.get("expires_at") or 0):
        return
    pairing["fails"] = int(pairing.get("fails") or 0) + 1
    if pairing["fails"] >= PAIRING_FAIL_LIMIT:
        logger.warning("pairing closed after %d failed codes", PAIRING_FAIL_LIMIT)
        reset_pairing()


def redeem(code: str, name: str, *, first_ctr: int = 0) -> tuple:
    """Spend the active pairing code. ``(token, device_id, detail)``.

    ``token`` is empty on refusal and ``detail`` then carries the reason in
    words. Marks the code used *before* returning the token, so a second
    redeem of the same code cannot race a first. Refuses expired, used and
    unknown codes, and refuses at ``MAX_DEVICES``. The name is clamped, not
    refused.

    The new entry carries the pairing's **home key** and two home counters.
    ``first_ctr`` is the counter of the sealed pair frame that redeemed the
    code (0 for a plain redeem): it becomes the device's floor, so a captured
    pair frame cannot be replayed as an accepted frame once the device
    exists.
    """
    global _pairing
    got = str(code or "").strip().upper()
    pairing = _pairing
    if pairing is None:
        return "", "", "that pairing code is not valid"
    stored = str(pairing.get("code") or "")
    if not got or len(got) != len(stored):
        _note_fail(pairing)
        return "", "", "that pairing code is not valid"
    if not hmac.compare_digest(got, stored):
        _note_fail(pairing)
        return "", "", "that pairing code is not valid"
    return _spend(pairing, name, pairing.get("home_key"), first_ctr)


def redeem_proven(name: str, *, home_key: bytes, first_ctr: int = 0) -> tuple:
    """Spend the active pairing for a phone that proved the code over SRP
    (``pairing_srp_finish``). ``redeem``'s tail with the code comparison
    skipped — the proof was the comparison — and ``home_key`` (the derived
    key, ``srp.home_key(K)``) written as the device's home key instead of
    the QR's. Same refusal order and words from "already been used" down.
    ``first_ctr`` is 0 on this route: no phone-to-Mac frame preceded the
    reply, so the phone's first sealed request is counter 1."""
    pairing = _pairing
    if pairing is None:
        return "", "", "that pairing code is not valid"
    return _spend(pairing, name, home_key, first_ctr)


def _spend(pairing: dict, name: str, home, first_ctr: int) -> tuple:
    """The redeem's tail, shared by ``redeem`` and ``redeem_proven``: the
    used / expired / ``MAX_DEVICES`` refusals, ``used`` marked before the
    token is minted, the ledger entry written with ``home`` as the device's
    home key. Byte-identical to what ``redeem`` did before it was factored."""
    if pairing.get("used"):
        return "", "", "that pairing code has already been used"
    if time.time() >= float(pairing.get("expires_at") or 0):
        return "", "", "that pairing code has expired"

    data = load()
    entries = list(data.get("devices") or [])
    if len(entries) >= MAX_DEVICES:
        return "", "", f"Dark Army can pair at most {MAX_DEVICES} devices"

    # Spent before the token is minted, so a crash between here and save
    # burns the code rather than leaving it redeemable twice.
    pairing["used"] = True

    label = str(name or pairing.get("name_hint") or "iPhone").strip() or "iPhone"
    if len(label) > MAX_DEVICE_NAME_CHARS:
        label = label[:MAX_DEVICE_NAME_CHARS]
    token = mint()
    device_id = secrets.token_hex(8)
    if not isinstance(home, bytes) or len(home) != 32:
        home = secrets.token_bytes(32)
    entries.append({
        "id": device_id,
        "name": label,
        "digest": digest(token),
        "paired_at": time.time(),
        "home_key_b64": base64.b64encode(home).decode("ascii"),
        "home_recv_ctr": max(0, int(first_ctr or 0)),
        "home_send_ctr": 0,
    })
    data["devices"] = entries
    if not save(data):
        return "", "", "could not record the pairing"
    _home_recv_mem.pop(device_id, None)
    _home_send_mem.pop(device_id, None)
    return token, device_id, ""


#: The name a headless pair gets when the caller did not choose one. A
#: Devices row has to be recognisable; the phone's own default is
#: ``iPhone``.
DEFAULT_BOT_NAME = "Grok"


def mint_device(name: str = "") -> tuple:
    """Record a device with no pairing code.

    ``(token, device_id, name, detail)``. ``token`` is empty on refusal
    and ``detail`` then carries the reason.

    The loopback bot pair is the only caller. There is no code to spend,
    so ``_pairing`` is not read and not written — a QR showing on the Mac
    stays showing, and redeeming it still pairs the phone. The ledger
    entry is ``_spend``'s shape, so ``unpair`` forgets it and an older
    build still reads the row. The name is clamped, not refused; a blank
    name becomes ``DEFAULT_BOT_NAME``.
    """
    data = load()
    entries = list(data.get("devices") or [])
    if len(entries) >= MAX_DEVICES:
        return "", "", "", (
            f"Dark Army can pair at most {MAX_DEVICES} devices")
    label = str(name or "").strip() or DEFAULT_BOT_NAME
    if len(label) > MAX_DEVICE_NAME_CHARS:
        label = label[:MAX_DEVICE_NAME_CHARS]
    home = secrets.token_bytes(32)
    token = mint()
    device_id = secrets.token_hex(8)
    entries.append({
        "id": device_id,
        "name": label,
        "digest": digest(token),
        "paired_at": time.time(),
        "home_key_b64": base64.b64encode(home).decode("ascii"),
        "home_recv_ctr": 0,
        "home_send_ctr": 0,
        # A phone row has no such key. ``pair_bot`` reads it so a second
        # headless mint refuses until this row is unpaired. Absent from
        # ``snapshot``: the Devices row already carries the name.
        "headless": True,
    })
    data["devices"] = entries
    if not save(data):
        # ``save`` invalidates on success. A failed write must not leave
        # the mutated cache looking paired.
        invalidate()
        return "", "", "", "could not record the pairing"
    _home_recv_mem.pop(device_id, None)
    _home_send_mem.pop(device_id, None)
    return token, device_id, label, ""


def headless_paired() -> bool:
    """Whether a headless bot row is in the ledger.

    A phone paired by QR or by a typed address has no ``headless`` key, so
    it does not close the bot door. Un-pairing the bot row is what opens
    the door again.
    """
    for entry in paired():
        if entry.get("headless") is True:
            return True
    return False


# ── the home key ──────────────────────────────────────────────────────────────

def _entry(device_id: str) -> dict | None:
    did = str(device_id or "")
    if not did:
        return None
    for entry in paired():
        if str(entry.get("id") or "") == did:
            return entry
    return None


def home_key(device_id: str):
    """The device's home key, or ``None`` — no such device, or one paired
    before sealed home access existed. Re-read from the ledger per call, so
    ``unpair`` bites on the very next frame."""
    entry = _entry(device_id)
    if entry is None:
        return None
    try:
        key = base64.b64decode(str(entry.get("home_key_b64") or ""),
                               validate=True)
    except (ValueError, TypeError):
        return None
    return key if len(key) == 32 else None


def device_for_home_channel(chan: str) -> str:
    """The device whose home channel id this is, or ``""``.

    An empty header returns before any comparison. The map is memoised on
    the ledger's own revision, so an un-pair (a save) rebuilds it.
    """
    global _channel_map
    text = str(chan or "").strip()
    if not text:
        return ""
    data = load()
    rev = _cache[0]
    cached_rev, cached = _channel_map
    if cached_rev is None or rev != cached_rev:
        built = {}
        for entry in data.get("devices") or []:
            did = str(entry.get("id") or "")
            key = home_key(did)
            if did and key is not None:
                built[relay.channel_id(key, ns=relay.HOME)] = did
        cached = built
        _channel_map = (rev, built)
    return str(cached.get(text) or "")


def _write_entry(device_id: str, **fields) -> bool:
    data = load()
    entries = list(data.get("devices") or [])
    hit = False
    fresh = []
    for entry in entries:
        if str(entry.get("id") or "") == str(device_id or ""):
            entry = dict(entry)
            entry.update(fields)
            hit = True
        fresh.append(entry)
    if not hit:
        return False
    data["devices"] = fresh
    return save(data)


def last_home_recv_ctr(device_id: str) -> int:
    """The highest phone-to-Mac home counter accepted so far."""
    did = str(device_id or "")
    entry = _entry(did)
    stored = int(float((entry or {}).get("home_recv_ctr") or 0))
    mem = _home_recv_mem.get(did)
    return max(stored, mem[0]) if mem else stored


def note_home_recv_ctr(device_id: str, ctr: int, durable: bool = False) -> None:
    """Record an accepted phone-to-Mac home counter — ``relay.note_recv_ctr``'s
    rule: a **write** frame persists immediately, a read frame coalesces to
    one disk write per ``relay.RECV_CTR_PERSIST_SECONDS``."""
    did = str(device_id or "")
    if _entry(did) is None:
        return
    value = max(int(ctr), last_home_recv_ctr(did))
    mono = time.monotonic()
    mem = _home_recv_mem.get(did)
    _home_recv_mem[did] = (value, mem[1] if mem else 0.0)
    if not durable and mem is not None \
            and mono - mem[1] < relay.RECV_CTR_PERSIST_SECONDS:
        return
    if _write_entry(did, home_recv_ctr=value):
        _home_recv_mem[did] = (value, mono)


def next_home_send_ctr(device_id: str) -> int:
    """The next Mac-to-phone home counter, crash-safe by reservation —
    ``relay.next_send_ctr``'s shape, ``0`` for a device that is not paired or
    has no home key (nothing could be sealed to it)."""
    did = str(device_id or "")
    entry = _entry(did)
    if entry is None or home_key(did) is None:
        return 0
    reserved = int(float(entry.get("home_send_ctr") or 0))
    used = _home_send_mem.get(did, reserved)
    nxt = used + 1
    if nxt >= reserved:
        if not _write_entry(did, home_send_ctr=nxt + relay.SEND_CTR_RESERVE):
            return 0
    _home_send_mem[did] = nxt
    return nxt


def unpair(device_id: str) -> tuple:
    """Forget ``device_id``. ``(ok, message)``.

    The ledger is the authority: a device whose entry is gone has no home
    key on this Mac, so its next frame fails closed however the phone feels.
    """
    did = str(device_id or "").strip()
    if not did:
        return False, "no device"
    data = load()
    entries = list(data.get("devices") or [])
    kept = [e for e in entries if str(e.get("id") or "") != did]
    if len(kept) == len(entries):
        return False, "that device is not paired"
    data["devices"] = kept
    if not save(data):
        return False, "could not record the un-pairing"
    _home_recv_mem.pop(did, None)
    _home_send_mem.pop(did, None)
    return True, "device unpaired"


# ── what may be published ─────────────────────────────────────────────────────

def snapshot() -> dict:
    """The paired devices, as a surface may see them.

    **Digests, tokens, home keys, the pairing code and ``fails`` never appear
    here**, and a test asserts it. ``home`` per device is a bare "this phone
    has a home key". ``pairing_open`` is whether a code is showing, not what
    it is. ``available`` is stated rather than inferred from an empty list:
    an older daemon sends no section at all, and an empty list decoding as
    "no devices" would draw a Devices UI over a working panel.
    """
    rows = []
    for entry in paired():
        did = str(entry.get("id") or "")
        if not did:
            continue
        rows.append({
            "id": did,
            "name": str(entry.get("name") or "iPhone"),
            "paired_at": float(entry.get("paired_at") or 0.0),
            "home": home_key(did) is not None,
        })
    return {
        "available": True,
        "devices": rows,
        "pairing_open": pairing_open(),
    }
