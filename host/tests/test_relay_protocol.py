# host/tests/test_relay_protocol.py
"""The sealed-envelope protocol and the relay key store.

Everything here is pure: no sockets, no daemon, a temp ``RELAY_PATH``. The
fixed vectors at the bottom are the cross-platform contract —
``RelayTransport.swift`` derives the same channel id and direction keys from
the same key bytes, and a drift on either side reads as every envelope
failing verification.
"""

from __future__ import annotations

import base64
import json
import stat
import time
import zlib

import pytest

from dark_army_daemon import paths, relay


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    path = tmp_path / "relay.json"
    monkeypatch.setattr(paths, "RELAY_PATH", path)
    monkeypatch.setattr(paths, "STATE_DIR", tmp_path)
    relay.reset()
    yield path
    relay.reset()


KEY = bytes(range(32))


# --- seal / open --------------------------------------------------------------


def test_roundtrip():
    wire = relay.seal_frame(KEY, relay.DIR_PHONE_TO_MAC, 1, "state",
                            {"q": "window=session"})
    frame, err = relay.open_frame(KEY, relay.DIR_PHONE_TO_MAC, wire, 0)
    assert err == ""
    assert frame["kind"] == "state"
    assert frame["body"] == {"q": "window=session"}
    assert frame["ctr"] == 1
    assert frame["v"] == 1
    assert frame["id"]


def test_tamper_refused():
    wire = relay.seal_frame(KEY, relay.DIR_PHONE_TO_MAC, 1, "state", {})
    raw = bytearray(base64.b64decode(wire))
    raw[-1] ^= 0x01
    bad = base64.b64encode(bytes(raw)).decode()
    frame, err = relay.open_frame(KEY, relay.DIR_PHONE_TO_MAC, bad, 0)
    assert frame is None and err == "seal"


def test_wrong_direction_key_refused():
    """A reflected frame — sealed p2m, replayed as if it were m2p — fails."""
    wire = relay.seal_frame(KEY, relay.DIR_PHONE_TO_MAC, 1, "state", {})
    frame, err = relay.open_frame(KEY, relay.DIR_MAC_TO_PHONE, wire, 0)
    assert frame is None and err == "seal"


def test_wrong_key_refused():
    wire = relay.seal_frame(KEY, relay.DIR_PHONE_TO_MAC, 1, "state", {})
    frame, err = relay.open_frame(relay.mint_key(), relay.DIR_PHONE_TO_MAC,
                                  wire, 0)
    assert frame is None and err == "seal"


def test_replayed_counter_refused():
    wire = relay.seal_frame(KEY, relay.DIR_PHONE_TO_MAC, 5, "state", {})
    frame, err = relay.open_frame(KEY, relay.DIR_PHONE_TO_MAC, wire, 5)
    assert frame is None and err == "ctr"
    frame, err = relay.open_frame(KEY, relay.DIR_PHONE_TO_MAC, wire, 9)
    assert frame is None and err == "ctr"
    frame, err = relay.open_frame(KEY, relay.DIR_PHONE_TO_MAC, wire, 4)
    assert err == "" and frame["ctr"] == 5


def test_timestamp_beyond_skew_refused():
    stale = time.time() - relay.RELAY_SKEW_SECONDS - 10
    wire = relay.seal_frame(KEY, relay.DIR_PHONE_TO_MAC, 1, "state", {},
                            ts=stale)
    frame, err = relay.open_frame(KEY, relay.DIR_PHONE_TO_MAC, wire, 0)
    assert frame is None and err == "ts"
    ahead = time.time() + relay.RELAY_SKEW_SECONDS + 10
    wire = relay.seal_frame(KEY, relay.DIR_PHONE_TO_MAC, 1, "state", {},
                            ts=ahead)
    frame, err = relay.open_frame(KEY, relay.DIR_PHONE_TO_MAC, wire, 0)
    assert frame is None and err == "ts"


def test_oversize_wire_refused():
    huge = "A" * (relay.RELAY_FRAME_MAX_BYTES + 1)
    frame, err = relay.open_frame(KEY, relay.DIR_PHONE_TO_MAC, huge, 0)
    assert frame is None and err == "oversize"


def test_seal_refuses_a_wire_over_the_cap():
    """A frame that seals past ``RELAY_FRAME_MAX_BYTES`` comes back ``""``,
    never a wire: the other end refuses an oversize envelope unopened on
    every poll, so sending one reads as a permanently dead away path."""
    import secrets
    # Incompressible: hex of random bytes, big enough that even deflated
    # and before base64 it stays over the cap.
    huge = secrets.token_hex(relay.RELAY_FRAME_MAX_BYTES)
    wire = relay.seal_frame(KEY, relay.DIR_MAC_TO_PHONE, 1, "reply",
                            {"body": huge})
    assert wire == ""


def test_zlib_bomb_refused():
    """A sealed frame whose plaintext inflates past the cap is refused,
    not allocated."""
    deflater = zlib.compressobj(wbits=-15)
    bomb = (deflater.compress(b"\x00" * (relay.RELAY_FRAME_MAX_BYTES + 4096))
            + deflater.flush())
    import os

    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
    nonce = os.urandom(12)
    aad = (relay.AAD_PREFIX + relay.channel_id(KEY).encode() + b"|"
           + relay.DIR_PHONE_TO_MAC.encode())
    sealed = ChaCha20Poly1305(
        relay.direction_key(KEY, relay.DIR_PHONE_TO_MAC)).encrypt(
        nonce, bomb, aad)
    wire = base64.b64encode(nonce + sealed).decode()
    frame, err = relay.open_frame(KEY, relay.DIR_PHONE_TO_MAC, wire, 0)
    assert frame is None and err == "oversize"


def test_garbage_refused_in_words():
    for wire in ("", "not base64!!", base64.b64encode(b"tiny").decode()):
        frame, err = relay.open_frame(KEY, relay.DIR_PHONE_TO_MAC, wire, 0)
        assert frame is None
        assert err in relay.REFUSAL_WORDS


def test_nonces_are_random_not_counter_derived():
    a = base64.b64decode(relay.seal_frame(KEY, relay.DIR_PHONE_TO_MAC,
                                          1, "state", {}))
    b = base64.b64decode(relay.seal_frame(KEY, relay.DIR_PHONE_TO_MAC,
                                          1, "state", {}))
    assert a[:12] != b[:12]


# --- fixed vectors: the cross-platform contract -------------------------------


def test_channel_id_vector():
    assert relay.channel_id(KEY) == "a35eca31b785c5ed444835eed5e02d18"


def test_direction_key_vectors():
    assert relay.direction_key(KEY, relay.DIR_PHONE_TO_MAC).hex() == (
        "8252fa1a2e764cb9e73e604edf9b2b31a46029ffc7fb4191fd438db364dd45a4")
    assert relay.direction_key(KEY, relay.DIR_MAC_TO_PHONE).hex() == (
        "8d4d8905e63cd40eaf3661ad8e6a1a770a0016e4f860aa6d2b94040545d88e7a")


def test_info_strings_and_aad_prefix_pinned():
    assert relay.INFO_CHANNEL_ID == b"bob-relay v1 channel-id"
    assert relay.INFO_P2M == b"bob-relay v1 p2m"
    assert relay.INFO_M2P == b"bob-relay v1 m2p"
    assert relay.AAD_PREFIX == b"bob-relay v1|"


def test_compression_is_raw_deflate():
    """The one dialect Python's zlib and Apple's Compression share out of
    the box. A wrapped stream would fail to inflate on the phone."""
    assert relay._DEFLATE_WBITS == -15
    plain = b'{"v":1}'
    inflater = zlib.decompressobj(wbits=-15)
    assert inflater.decompress(relay._deflate(plain)) == plain


def test_channel_id_is_32_hex():
    chan = relay.channel_id(relay.mint_key())
    assert len(chan) == 32
    assert all(c in "0123456789abcdef" for c in chan)


# --- the home namespace: same format, its own strings, its own key -----------


def test_home_namespace_vectors():
    """Fixed vectors for the home path, beside the relay's. Pinned so
    `RelayTransport.swift`'s `.home` namespace derives the same bytes."""
    assert relay.channel_id(KEY, ns=relay.HOME) == (
        "ad5bb4df5ea602409a4247a452a2e40d")
    assert relay.direction_key(KEY, relay.DIR_PHONE_TO_MAC,
                               ns=relay.HOME).hex() == (
        "17514546ea6d455082e01ba4d8a7ccb3bc556b4571f71e2d7533f9bcab34d955")
    assert relay.direction_key(KEY, relay.DIR_MAC_TO_PHONE,
                               ns=relay.HOME).hex() == (
        "07b0a62c933af6829ce3faaa8bf6df0706b2a584912d8f883b564bee5176fee5")
    # And the relay vectors did not move: the default namespace is RELAY.
    assert relay.channel_id(KEY) == relay.channel_id(KEY, ns=relay.RELAY)
    assert relay.channel_id(KEY) != relay.channel_id(KEY, ns=relay.HOME)


def test_home_namespace_strings_pinned():
    assert relay.RELAY == (relay.INFO_CHANNEL_ID, relay.INFO_P2M,
                           relay.INFO_M2P, relay.AAD_PREFIX)
    assert relay.HOME == (b"bob-home v1 channel-id", b"bob-home v1 p2m",
                          b"bob-home v1 m2p", b"bob-home v1|")


def test_a_relay_frame_never_opens_under_the_home_namespace():
    """Same key, both directions of the mistake: a mailbox frame dropped on
    the home door, and a home frame dropped in the mailbox, both fail the
    seal — the HKDF info and the AAD differ."""
    wire = relay.seal_frame(KEY, relay.DIR_PHONE_TO_MAC, 1, "state", {})
    frame, err = relay.open_frame(KEY, relay.DIR_PHONE_TO_MAC, wire, 0,
                                  ns=relay.HOME)
    assert frame is None and err == "seal"
    home = relay.seal_frame(KEY, relay.DIR_PHONE_TO_MAC, 1, "state", {},
                            ns=relay.HOME)
    frame, err = relay.open_frame(KEY, relay.DIR_PHONE_TO_MAC, home, 0)
    assert frame is None and err == "seal"
    frame, err = relay.open_frame(KEY, relay.DIR_PHONE_TO_MAC, home, 0,
                                  ns=relay.HOME)
    assert err == "" and frame["kind"] == "state"


def test_blob_roundtrip():
    raw = relay.seal_blob(KEY, relay.DIR_PHONE_TO_MAC, "frame-1", b"\x89PNG" * 40)
    assert raw[:12] != b"\x89PNG" * 3  # a nonce, not the plaintext
    assert relay.open_blob(KEY, relay.DIR_PHONE_TO_MAC, "frame-1", raw) == (
        b"\x89PNG" * 40)
    # No base64 and no deflate: nonce + ciphertext + tag, byte for byte.
    assert len(raw) == 12 + len(b"\x89PNG" * 40) + 16


def test_blob_bound_to_its_frame_id():
    """Two uploads in flight cannot swap bodies: the AAD carries the header
    frame's id, and a blob opened under another id is refused."""
    raw = relay.seal_blob(KEY, relay.DIR_PHONE_TO_MAC, "frame-1", b"photo")
    assert relay.open_blob(KEY, relay.DIR_PHONE_TO_MAC, "frame-2", raw) is None
    assert relay.open_blob(KEY, relay.DIR_PHONE_TO_MAC, "", raw) is None


def test_blob_wrong_direction_refused():
    raw = relay.seal_blob(KEY, relay.DIR_PHONE_TO_MAC, "frame-1", b"photo")
    assert relay.open_blob(KEY, relay.DIR_MAC_TO_PHONE, "frame-1", raw) is None
    assert relay.open_blob(relay.mint_key(), relay.DIR_PHONE_TO_MAC,
                           "frame-1", raw) is None
    assert relay.open_blob(KEY, relay.DIR_PHONE_TO_MAC, "frame-1",
                           raw[:-1]) is None
    assert relay.open_blob(KEY, relay.DIR_PHONE_TO_MAC, "frame-1",
                           b"tiny") is None
    # The relay namespace never opens a home blob either.
    assert relay.open_blob(KEY, relay.DIR_PHONE_TO_MAC, "frame-1", raw,
                           ns=relay.RELAY) is None


# --- the store ----------------------------------------------------------------


def test_store_roundtrip(store):
    key_b64 = relay.create_channel("dev-1")
    assert key_b64
    assert store.is_file()
    assert stat.S_IMODE(store.stat().st_mode) == 0o600
    key = relay.channel_key("dev-1")
    assert key is not None
    assert base64.b64encode(key).decode() == key_b64
    relay.invalidate()
    assert relay.channel_key("dev-1") == key


def test_forget_removes_key_and_lease(store):
    relay.create_channel("dev-1")
    assert relay.channel_key("dev-1") is not None
    assert relay.lease_valid("dev-1")
    relay.forget("dev-1")
    assert relay.channel_key("dev-1") is None
    assert relay.lease_valid("dev-1") is False
    assert relay.lease_expires_at("dev-1") == 0.0
    on_disk = json.loads(store.read_text())
    assert "dev-1" not in on_disk.get("channels", {})


def test_store_tolerates_unknown_keys(store):
    relay.create_channel("dev-1")
    data = json.loads(store.read_text())
    data["future_field"] = {"anything": True}
    data["channels"]["dev-1"]["future_flag"] = 7
    store.write_text(json.dumps(data))
    relay.invalidate()
    assert relay.channel_key("dev-1") is not None
    relay.note_lan_proof("dev-1")
    kept = json.loads(store.read_text())
    assert kept["future_field"] == {"anything": True}
    assert kept["channels"]["dev-1"]["future_flag"] == 7


def test_unreadable_store_fails_closed(store):
    store.write_text("not json")
    relay.invalidate()
    assert relay.channel_key("dev-1") is None
    assert relay.get_url() == ""


def test_set_url_requires_https(store):
    ok, detail = relay.set_url("http://mailbox.example")
    assert ok is False
    assert "https://" in detail
    ok, detail = relay.set_url("https://mailbox.example/")
    assert ok is True and detail == ""
    assert relay.get_url() == "https://mailbox.example"
    ok, _ = relay.set_url("")
    assert ok is True
    assert relay.get_url() == ""


def test_push_secret_rides_the_url_setter(store):
    ok, _ = relay.set_url("https://mailbox.example", "s3same")
    assert ok is True
    assert relay.get_push_secret() == "s3same"
    # Blank or absent leaves the stored secret alone — the panel never shows
    # it back, so an untouched field must not wipe it.
    ok, _ = relay.set_url("https://mailbox.example", "")
    assert ok is True and relay.get_push_secret() == "s3same"
    ok, _ = relay.set_url("https://mailbox.example")
    assert ok is True and relay.get_push_secret() == "s3same"
    # A replacement replaces, stripped.
    ok, _ = relay.set_url("https://mailbox.example", "  fresh  ")
    assert ok is True and relay.get_push_secret() == "fresh"
    # Clearing the address clears the secret with it: a secret for no
    # mailbox is a leftover.
    ok, _ = relay.set_url("")
    assert ok is True
    assert relay.get_push_secret() == ""


def test_a_malformed_push_secret_reads_as_absent(store):
    store.write_text(json.dumps(
        {"version": 1, "url": "", "push_secret": [1], "channels": {}}))
    relay.invalidate()
    assert relay.get_push_secret() == ""


def test_recv_ctr_durable_write_persists_immediately(store):
    relay.create_channel("dev-1")
    relay.note_recv_ctr("dev-1", 7, durable=True)
    relay.reset()  # simulate a crash: memory gone, disk survives
    assert relay.last_recv_ctr("dev-1") == 7


def test_send_ctr_never_reuses_across_a_crash(store):
    relay.create_channel("dev-1")
    used = [relay.next_send_ctr("dev-1") for _ in range(3)]
    assert used == sorted(used)
    assert len(set(used)) == 3
    relay.reset()  # crash
    fresh = relay.next_send_ctr("dev-1")
    assert fresh > used[-1]
