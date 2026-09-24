# host/tests/test_pake_pairing.py
"""The typed-address pair is a PAKE: SRP-6a over the pairing code.

(a) RFC 5054 Appendix B pins the arithmetic (SHA-1, the 1024-bit group);
(b) the cross-language vector `fixtures/pake/cross-vector.json` is
reproduced byte for byte, so the file cannot drift from the Python;
(c) the pure refusals and a full in-process exchange on `PRODUCTION`;
(d)–(l) the door end to end on a real listener — the two-step exchange,
the sealed reply, the fail limit, the old shape, the shapes, the bounds,
the gate, the lifetime, the snapshot and the untouched QR route;
(m) the Swift half (`ios/BobPhone/BigUInt.swift`, `SRP.swift`) compiled
under `swiftc` and run against Python as the server — skipped without a
toolchain, never silently passed.

Fixtures are `test_lan_door.py`'s, copied not imported.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import shutil
import socket
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

import pytest
import pytest_asyncio

from dark_army_daemon import access_log
from dark_army_daemon import api_server as api_mod
from dark_army_daemon import devices, lan_hosts, paths, relay, srp
from dark_army_daemon.api_server import (
    PAIR_PLAIN_REFUSAL,
    PAIR_UPDATE_REFUSAL,
    PAKE_BUSY_REFUSAL,
    PAKE_SHAPE_REFUSAL,
    ApiServer,
)
from dark_army_daemon.daemon import BobDaemon
from tests.free_ports import free_ports
from tests.pake_pair import open_pair_reply, pair_typed, typed_exchange

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "pake" / "cross-vector.json"
BIGUINT_SWIFT = ROOT / "ios" / "BobPhone" / "BigUInt.swift"
SRP_SWIFT = ROOT / "ios" / "BobPhone" / "SRP.swift"
SRP_TESTS_SWIFT = ROOT / "ios" / "BobPhoneTests" / "SRPTests.swift"
PBXPROJ = ROOT / "ios" / "BobPhone.xcodeproj" / "project.pbxproj"

PARAMS = srp.PRODUCTION
GROUP = PARAMS.group
HEX_LEN = GROUP.byte_len * 2


# --- fixtures (test_lan_door.py's) ------------------------------------------


def _free_ports(count: int) -> list[int]:
    """From this run's and worker's block below the ephemeral range, not a
    bound-then-closed `:0` another worker's connection can take
    (`tests/free_ports.py`)."""
    return free_ports(count)


@pytest.fixture(autouse=True)
def _no_fleet_snapshot(monkeypatch):
    monkeypatch.setattr(BobDaemon, "_schedule_agents_push", lambda self: None)


@pytest.fixture(autouse=True)
def machine(monkeypatch):
    monkeypatch.setattr(lan_hosts, "live_addrs", lambda: {"en0": ["192.168.1.5"]})
    monkeypatch.setattr(lan_hosts, "default_route_addr", lambda: "192.168.1.5")
    monkeypatch.setattr(socket, "gethostname", lambda: "test-mac")


@pytest.fixture(autouse=True)
def ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "DEVICES_PATH", tmp_path / "devices.json")
    monkeypatch.setattr(paths, "RELAY_PATH", tmp_path / "relay.json")
    monkeypatch.setattr(paths, "STATE_DIR", tmp_path / "state")
    devices.reset()
    relay.reset()
    yield
    devices.reset()
    relay.reset()


@pytest.fixture
def ports(tmp_path, monkeypatch):
    loop_port, lan_port = _free_ports(2)
    monkeypatch.setattr(api_mod, "API_TOKEN_PATH", tmp_path / "api-token")
    monkeypatch.setattr(api_mod, "ensure_state_dir", lambda: tmp_path)
    monkeypatch.setattr(api_mod, "LAN_API_PORT", lan_port)
    return loop_port, lan_port


class _Recorder:
    def __init__(self):
        self.calls: list[tuple] = []

    async def __call__(self, door, peer, reason, device_id=""):
        self.calls.append((door, peer, reason, device_id))


@pytest_asyncio.fixture
async def server(ports):
    loop_port, lan_port = ports
    daemon = BobDaemon()
    srv = ApiServer(daemon, port=loop_port)
    daemon._api = srv
    recorder = _Recorder()
    daemon.note_access_refusal = recorder
    daemon.lan_access_enabled = True
    await srv.start()
    await srv.start_lan()
    try:
        yield srv, daemon, recorder, loop_port, lan_port
    finally:
        await srv.stop()


def _blocking_fetch(path, *, port, data=None, headers=None, method=None):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=data, headers=headers or {},
        method=method or ("POST" if data is not None else "GET"),
    )
    try:
        resp = urllib.request.urlopen(req, timeout=5)
        return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


async def _fetch(*args, **kwargs):
    return await asyncio.to_thread(_blocking_fetch, *args, **kwargs)


async def _settle():
    for _ in range(3):
        await asyncio.sleep(0)


async def _home_post(lan_port: int, key: bytes, kind: str, body: dict,
                     ctr: int):
    wire = relay.seal_frame(key, relay.DIR_PHONE_TO_MAC, ctr, kind, body,
                            ns=relay.HOME)
    status, out = await _fetch(
        "/api/home", port=lan_port, data=wire.encode(),
        headers={"X-Bob-Channel": relay.channel_id(key, ns=relay.HOME),
                 "Content-Type": "text/plain"})
    if status != 200:
        return status, out
    frame, err = relay.open_frame(key, relay.DIR_MAC_TO_PHONE, out.decode(),
                                  0, ns=relay.HOME)
    assert err == "", err
    return frame["body"]["status"], frame["body"].get("body", "")


def _other_code(real: str) -> str:
    other = devices._CODE_ALPHABET[:devices._CODE_LEN]
    return other if other != real else devices._CODE_ALPHABET[1:devices._CODE_LEN + 1]


async def _post_pair(lan_port: int, body: dict):
    return await _fetch("/api/pair", port=lan_port, data=json.dumps(body).encode(),
                        headers={"Content-Type": "application/json"})


# --- (a) RFC 5054 Appendix B --------------------------------------------------


_N_1024 = int(
    "EEAF0AB9ADB38DD69C33F80AFA8FC5E86072618775FF3C0B9EA2314C9C256576D674DF74"
    "96EA81D3383B4813D692C6E0E0D5D8E250B98BE48E495C1D6089DAD15DC7D7B46154D6B6"
    "CE8EF4AD69B15D4982559B297BCF1885C529F566660E57EC68EDBC3C05726CC02FD4CBF4"
    "976EAA9AFD5138FE8376435B9FC61D2FC0EB06E3", 16)
RFC = srp.Params(srp.Group(_N_1024, 2), "sha1")
RFC_I, RFC_P = "alice", "password123"
RFC_S = bytes.fromhex("BEB25379D1A8581EB5A727673A2441EE")
RFC_A_PRIV = int("60975527035CF2AD1989806F0407210BC81EDC04E2762A56AFD529DDDA2D4393", 16)
RFC_B_PRIV = int("E487CB59D31AC550471E81F00F6928E01DDA08E974A004F49E61F5D105284D20", 16)
RFC_K = "7556AA045AEF2CDD07ABAF0F665C3E818913186F"
RFC_X = "94B7555AABE9127CC58CCF4993DB6CF84D16C124"
RFC_V = (
    "7E273DE8696FFC4F4E337D05B4B375BEB0DDE1569E8FA00A9886D8129BADA1F1822223CA"
    "1A605B530E379BA4729FDC59F105B4787E5186F5C671085A1447B52A48CF1970B4FB6F84"
    "00BBF4CEBFBB168152E08AB5EA53D15C1AFF87B2B9DA6E04E058AD51CC72BFC9033B564E"
    "26480D78E955A5E29E7AB245DB2BE315E2099AFB")
RFC_A = (
    "61D5E490F6F1B79547B0704C436F523DD0E560F0C64115BB72557EC44352E8903211C046"
    "92272D8B2D1A5358A2CF1B6E0BFCF99F921530EC8E39356179EAE45E42BA92AEACED8251"
    "71E1E8B9AF6D9C03E1327F44BE087EF06530E69F66615261EEF54073CA11CF5858F0EDFD"
    "FE15EFEAB349EF5D76988A3672FAC47B0769447B")
RFC_B = (
    "BD0C61512C692C0CB6D041FA01BB152D4916A1E77AF46AE105393011BAF38964DC46A067"
    "0DD125B95A981652236F99D9B681CBF87837EC996C6DA04453728610D0C6DDB58B318885"
    "D7D82C7F8DEB75CE7BD4FBAA37089E6F9C6059F388838E7A00030B331EB76840910440B1"
    "B27AAEAEEB4012B7D7665238A8E3FB004B117B58")
RFC_U = "CE38B9593487DA98554ED47D70A7AE5F462EF019"
RFC_S_PREMASTER = (
    "B0DC82BABCF30674AE450C0287745E7990A3381F63B387AAF271A10D233861E359B48220"
    "F7C4693C9AE12B0A6F67809F0876E2D013800D6C41BB59B6D5979B5C00A172B4A2A5903A"
    "0BDCAF8A709585EB2AFAFA8F3499B200210DCC1F10EB33943CD67FC88A2F39A4BE5BEC4E"
    "C0A3212DC346D7E474B29EDE8A469FFECA686E5A")


def _hexint(text: str) -> int:
    return int(text, 16)


def test_rfc5054_appendix_b_vector():
    assert srp.k(RFC) == _hexint(RFC_K)
    x_value = srp.x(RFC, RFC_I, RFC_P, RFC_S)
    assert x_value == _hexint(RFC_X)
    v = srp.verifier(RFC, RFC_I, RFC_P, RFC_S)
    assert v == _hexint(RFC_V)
    A = srp.client_public(RFC, RFC_A_PRIV)
    assert A == _hexint(RFC_A)
    B = srp.server_public(RFC, RFC_B_PRIV, v)
    assert B == _hexint(RFC_B)
    u_value = srp.u(RFC, A, B)
    assert u_value == _hexint(RFC_U)
    assert srp.server_secret(RFC, A, RFC_B_PRIV, v, u_value) == _hexint(RFC_S_PREMASTER)
    assert srp.client_secret(RFC, B, RFC_A_PRIV, x_value, u_value) == _hexint(RFC_S_PREMASTER)


# --- (b) the cross-language vector ---------------------------------------------


CROSS_I = "7a1b2c3d4e5f6071"
CROSS_P = "K7PQ2XM9"
CROSS_S = bytes.fromhex("00112233445566778899aabbccddeeff")
CROSS_A_PRIV = int.from_bytes(bytes(range(1, 33)), "big")
CROSS_B_PRIV = int.from_bytes(bytes(range(33, 65)), "big")


def _cross_vector() -> dict:
    """The fixed exchange both ends must reproduce, on `PRODUCTION`."""
    v = srp.verifier(PARAMS, CROSS_I, CROSS_P, CROSS_S)
    A = srp.client_public(PARAMS, CROSS_A_PRIV)
    B = srp.server_public(PARAMS, CROSS_B_PRIV, v)
    u_value = srp.u(PARAMS, A, B)
    S = srp.server_secret(PARAMS, A, CROSS_B_PRIV, v, u_value)
    x_value = srp.x(PARAMS, CROSS_I, CROSS_P, CROSS_S)
    assert S == srp.client_secret(PARAMS, B, CROSS_A_PRIV, x_value, u_value)
    K = srp.session_key(PARAMS, S)
    M1 = srp.client_proof(PARAMS, CROSS_I, CROSS_S, A, B, K)
    M2 = srp.server_proof(PARAMS, A, M1, K)
    return {
        "I": CROSS_I,
        "P": CROSS_P,
        "s": CROSS_S.hex(),
        "a": srp.pad(GROUP, CROSS_A_PRIV).hex()[-64:],
        "b": srp.pad(GROUP, CROSS_B_PRIV).hex()[-64:],
        "A": srp.pad(GROUP, A).hex(),
        "B": srp.pad(GROUP, B).hex(),
        "u": u_value.to_bytes(32, "big").hex(),
        "S": srp.pad(GROUP, S).hex(),
        "K": K.hex(),
        "M1": M1.hex(),
        "M2": M2.hex(),
        "home_key": srp.home_key(K).hex(),
    }


def test_cross_vector_is_reproduced_byte_for_byte():
    vector = _cross_vector()
    on_disk = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert on_disk == vector
    assert FIXTURE.read_text(encoding="utf-8") == json.dumps(
        vector, indent=2, sort_keys=True) + "\n"


def test_the_cross_vector_is_pinned_in_the_swift_tests():
    """The phone's XCTest carries the same values as literals."""
    vector = _cross_vector()
    text = SRP_TESTS_SWIFT.read_text(encoding="utf-8")
    # The long values are split across string-literal lines and joined
    # with `+`; squash the quoting to compare.
    squashed = "".join(ch for ch in text if ch not in ' \n"+')
    for key in ("s", "a", "A", "B", "K", "M1", "M2", "home_key"):
        assert vector[key] in squashed, key
    assert f'"{CROSS_I}"' in text and f'"{CROSS_P}"' in text


# --- (c) pure -------------------------------------------------------------------


def test_the_production_parameters():
    assert GROUP.g == 2
    assert GROUP.N.bit_length() == 2048
    assert GROUP.byte_len == 256
    assert PARAMS.hash_name == "sha256"
    assert srp.INFO_PAKE_HOME == b"bob-home v1 pake"
    assert srp.EPHEMERAL_BYTES == 32


def test_server_secret_refuses_a_degenerate_a():
    v = srp.verifier(PARAMS, CROSS_I, CROSS_P, CROSS_S)
    for bad in (0, GROUP.N, 2 * GROUP.N):
        with pytest.raises(ValueError, match="A mod N is zero"):
            srp.server_secret(PARAMS, bad, CROSS_B_PRIV, v, 7)


def test_client_secret_refuses_a_degenerate_b_and_a_zero_u():
    x_value = srp.x(PARAMS, CROSS_I, CROSS_P, CROSS_S)
    for bad in (0, GROUP.N):
        with pytest.raises(ValueError, match="B mod N is zero"):
            srp.client_secret(PARAMS, bad, CROSS_A_PRIV, x_value, 7)
    with pytest.raises(ValueError, match="u is zero"):
        srp.client_secret(PARAMS, 12345, CROSS_A_PRIV, x_value, 0)


def test_a_full_in_process_exchange_agrees_and_a_wrong_password_does_not():
    salt = b"\x01" * 16
    v = srp.verifier(PARAMS, "id", "CODE", salt)
    a = srp.random_ephemeral()
    b, B = srp.server_start(PARAMS, v)
    A = srp.client_public(PARAMS, a)
    u_value = srp.u(PARAMS, A, B)
    K_server = srp.session_key(PARAMS, srp.server_secret(PARAMS, A, b, v, u_value))
    K_client = srp.session_key(PARAMS, srp.client_secret(
        PARAMS, B, a, srp.x(PARAMS, "id", "CODE", salt), u_value))
    assert K_server == K_client
    M1 = srp.client_proof(PARAMS, "id", salt, A, B, K_client)
    assert M1 == srp.client_proof(PARAMS, "id", salt, A, B, K_server)
    assert srp.server_proof(PARAMS, A, M1, K_server) == \
        srp.server_proof(PARAMS, A, M1, K_client)
    assert srp.home_key(K_server) == srp.home_key(K_client)
    assert len(srp.home_key(K_server)) == 32
    wrong = srp.session_key(PARAMS, srp.client_secret(
        PARAMS, B, a, srp.x(PARAMS, "id", "WRONG", salt), u_value))
    assert srp.client_proof(PARAMS, "id", salt, A, B, wrong) != M1
    # A fresh `b` every start, and never a zero `B`.
    assert srp.server_start(PARAMS, v)[0] != b
    assert srp.random_ephemeral() != 0


def test_the_home_key_is_hkdf_over_k_with_the_named_info():
    K = b"\x07" * 32
    assert srp.home_key(K) == relay._hkdf(K, b"bob-home v1 pake")
    assert srp.home_key(K) != relay._hkdf(K, b"bob-home v1 other")


# --- (d) the door, armed ---------------------------------------------------------


@pytest.mark.asyncio
async def test_the_two_steps_pair_and_the_reply_is_sealed(server):
    srv, daemon, recorder, _loop, lan_port = server
    code = devices.begin(allow_plain=True)
    record = await typed_exchange(_fetch, lan_port, code, name="typed phone")
    status, out = record["start"]
    assert status == 200, out
    started = json.loads(out)
    assert started["pake"] == "start"
    assert len(started["pairing"]) == 16 and all(c in "0123456789abcdef"
                                                  for c in started["pairing"])
    assert len(started["salt"]) == 32
    assert len(started["B"]) == HEX_LEN
    assert set(started) == {"pake", "pairing", "salt", "B"}
    status, out = record["finish"]
    assert status == 200, out
    frame, err = open_pair_reply(record)
    assert err == "", err
    assert frame["kind"] == "reply"
    assert frame["body"]["status"] == 200
    assert frame["body"]["re"] == ""
    assert frame["body"]["content_type"] == "application/json"
    inner = json.loads(frame["body"]["body"])
    assert set(inner) == {"token", "device_id", "relay_key", "relay_url",
                          "relay_ws_url", "M2"}
    assert inner["token"] and inner["device_id"]
    assert "home_key" not in inner
    expected_m2 = srp.server_proof(PARAMS, int(record["A"], 16),
                                   bytes.fromhex(record["M1"]), record["K"])
    assert inner["M2"] == expected_m2.hex()
    device_id = inner["device_id"]
    assert devices.home_key(device_id) == srp.home_key(record["K"]) == record["key"]
    assert devices.last_home_recv_ctr(device_id) == 0
    assert frame["ctr"] == 1
    assert devices.resolve(inner["token"]) == device_id
    row = next(r for r in devices.snapshot()["devices"] if r["id"] == device_id)
    assert row["name"] == "typed phone" and row["home"] is True
    # A sealed `state` frame at counter 1 under the derived key is answered.
    status, body = await _home_post(lan_port, record["key"], "state", {}, 1)
    assert status == 200
    assert "agents" in json.loads(body)
    await _settle()
    assert recorder.calls == []
    assert devices.pairing_open() is False


@pytest.mark.asyncio
async def test_no_secret_crosses_the_wire_during_a_typed_pairing(server):
    """The success criterion: every byte the door sent during a full typed
    pairing — both HTTP bodies, raw — carries neither the home key (base64
    or hex), the token, the relay key nor the derived key, and the stored
    `home_key_b64` is exactly the client's derived key."""
    srv, daemon, _recorder, _loop, lan_port = server
    daemon.remote_access_enabled = True
    code = devices.begin(allow_plain=True)
    record = await typed_exchange(_fetch, lan_port, code)
    wire = record["start"][1] + b"\n" + record["finish"][1]
    assert record["start"][0] == 200 and record["finish"][0] == 200
    frame, err = open_pair_reply(record)
    assert err == ""
    inner = json.loads(frame["body"]["body"])
    key = record["key"]
    text = wire.decode("ascii", "replace")
    for secret in (
        base64.b64encode(key).decode("ascii"), key.hex(), record["K"].hex(),
        inner["token"], inner["relay_key"],
        base64.b64decode(inner["relay_key"]).hex(), code,
    ):
        assert secret and secret not in text, secret
    assert b"home_key" not in wire
    assert b"token" not in wire
    assert b"relay_key" not in wire
    stored = next(e for e in devices.paired() if e["id"] == inner["device_id"])
    assert base64.b64decode(stored["home_key_b64"]) == key
    # The finish body itself is one sealed frame: base64, nothing readable.
    assert set(record["finish"][1].decode("ascii")) <= set(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=\n")


# --- (f) wrong code -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_wrong_code_counts_one_fail_and_five_close_the_pairing(server, caplog):
    caplog.set_level(logging.WARNING, logger="dark-army")
    srv, _daemon, recorder, _loop, lan_port = server
    code = devices.begin(allow_plain=True)
    wrong = _other_code(code)
    record = await typed_exchange(_fetch, lan_port, wrong, name="x")
    status, out = record["finish"]
    assert status == 403
    assert json.loads(out) == {"error": "that pairing code is not valid"}
    assert devices._pairing["fails"] == 1
    await _settle()
    assert recorder.calls == [("pairing", "127.0.0.1", "pair_code", "")]
    for _ in range(devices.PAIRING_FAIL_LIMIT - 1):
        record = await typed_exchange(_fetch, lan_port, wrong, name="x")
        assert record["finish"][0] == 403
    assert devices.pairing_open() is False
    closed = [r for r in caplog.records
              if r.getMessage() == "pairing closed after 5 failed codes"]
    assert len(closed) == 1
    assert code not in caplog.text and wrong not in caplog.text
    # Nothing to start against now: the pairing is gone, so the gate
    # speaks first, exactly as it did for a burnt code before.
    status, out = await _post_pair(lan_port, {"pake": "start", "A": "01" * 256})
    assert status == 403
    assert json.loads(out)["error"] == PAIR_PLAIN_REFUSAL


@pytest.mark.asyncio
async def test_a_right_code_after_two_wrong_ones_pairs(server):
    srv, _daemon, _recorder, _loop, lan_port = server
    code = devices.begin(allow_plain=True)
    for _ in range(2):
        record = await typed_exchange(_fetch, lan_port, _other_code(code))
        assert record["finish"][0] == 403
    assert devices._pairing["fails"] == 2
    paired = await pair_typed(_fetch, lan_port, code=code)
    assert paired["token"]
    assert devices._pairing["used"] is True


@pytest.mark.asyncio
async def test_a_wrong_identity_is_a_wrong_proof(server):
    srv, _daemon, _recorder, _loop, lan_port = server
    code = devices.begin(allow_plain=True)
    record = await typed_exchange(_fetch, lan_port, code, identity="not-the-pairing")
    assert record["finish"][0] == 403
    assert devices._pairing["fails"] == 1


# --- (g) the old shape ---------------------------------------------------------


@pytest.mark.asyncio
async def test_the_old_plain_shape_is_told_to_update_and_gets_no_key(server):
    srv, _daemon, recorder, _loop, lan_port = server
    code = devices.begin(allow_plain=True)
    for body in ({"code": code, "name": "p"}, {}, {"pake": "hello"},
                 {"pake": 7, "A": "00"}, {"code": _other_code(code)}):
        status, out = await _post_pair(lan_port, body)
        assert status == 403, body
        assert json.loads(out) == {"error": PAIR_UPDATE_REFUSAL}
        assert "home_key" not in out.decode() and "token" not in out.decode()
    assert devices._pairing["fails"] == 0
    assert devices.pairing_open() is True
    await _settle()
    assert recorder.calls == [("pairing", "127.0.0.1", "pake", "")] * 5
    # And a body that is not JSON at all.
    status, out = await _fetch("/api/pair", port=lan_port, data=b"not json")
    assert status == 403
    assert json.loads(out) == {"error": PAIR_UPDATE_REFUSAL}


# --- (h) shapes ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_malformed_a_is_out_of_step_and_counts_nothing(server):
    srv, _daemon, recorder, _loop, lan_port = server
    devices.begin(allow_plain=True)
    bad = [
        "ab" * 255,                       # too short
        "ab" * 257,                       # too long
        "AB" * 256,                       # upper-case hex
        "00" * 256,                       # A = 0
        srp.pad(GROUP, GROUP.N).hex(),    # A = N
        "zz" * 256,                       # not hex
        "",
        7,
    ]
    for A in bad:
        status, out = await _post_pair(lan_port, {"pake": "start", "A": A})
        assert status == 403, A
        assert json.loads(out) == {"error": PAKE_SHAPE_REFUSAL}, A
    assert devices._pairing["fails"] == 0
    assert devices._pairing["srp_inflight"] == {}
    assert devices._pairing["srp_starts"] == 0
    await _settle()
    assert recorder.calls == [("pairing", "127.0.0.1", "pake", "")] * len(bad)


@pytest.mark.asyncio
async def test_a_finish_without_its_start_or_twice_is_out_of_step(server):
    srv, _daemon, recorder, _loop, lan_port = server
    code = devices.begin(allow_plain=True)
    # An `A` never started.
    status, out = await _post_pair(lan_port, {
        "pake": "finish", "A": "01" * 256, "M1": "ab" * 32, "name": "p"})
    assert status == 403
    assert json.loads(out) == {"error": PAKE_SHAPE_REFUSAL}
    # A start, then a finish with `M1` out of shape: the entry is consumed.
    a = srp.random_ephemeral()
    A_hex = srp.pad(GROUP, srp.client_public(PARAMS, a)).hex()
    status, _ = await _post_pair(lan_port, {"pake": "start", "A": A_hex})
    assert status == 200
    assert A_hex in devices._pairing["srp_inflight"]
    for m1 in ("AB" * 32, "ab" * 31, "", "zz" * 32):
        status, out = await _post_pair(lan_port, {
            "pake": "finish", "A": A_hex, "M1": m1, "name": "p"})
        assert status == 403
        assert json.loads(out) == {"error": PAKE_SHAPE_REFUSAL}
    assert A_hex not in devices._pairing["srp_inflight"]
    # A right proof finished twice: the second is out of step.
    record = await typed_exchange(_fetch, lan_port, code)
    assert record["finish"][0] == 200
    status, out = await _post_pair(lan_port, {
        "pake": "finish", "A": record["A"], "M1": record["M1"], "name": "p"})
    assert status == 403
    assert json.loads(out) == {"error": PAKE_SHAPE_REFUSAL}
    assert devices._pairing["fails"] == 0
    await _settle()
    assert all(call[2] == "pake" for call in recorder.calls)
    assert len(recorder.calls) == 6


@pytest.mark.asyncio
async def test_a_replayed_start_is_out_of_step_and_the_phones_own_finish_still_pairs(server):
    # `A` is public: a LAN neighbour replaying the phone's own start must
    # not evict the phone's entry, or its right finish would read as a
    # counted wrong code and the log would blame the phone.
    srv, _daemon, recorder, _loop, lan_port = server
    code = devices.begin(allow_plain=True)
    a = srp.random_ephemeral()
    A = srp.client_public(PARAMS, a)
    A_hex = srp.pad(GROUP, A).hex()
    status, out = await _post_pair(lan_port, {"pake": "start", "A": A_hex})
    assert status == 200
    first = json.loads(out)
    held = devices._pairing["srp_inflight"][A_hex]
    status, out = await _post_pair(lan_port, {"pake": "start", "A": A_hex})
    assert status == 403
    assert json.loads(out) == {"error": PAKE_SHAPE_REFUSAL}
    assert devices._pairing["srp_inflight"][A_hex] == held
    assert devices._pairing["srp_starts"] == 1
    assert devices._pairing["fails"] == 0
    # The FIRST start's finish, with the right code, still pairs.
    salt = bytes.fromhex(first["salt"])
    B = int(first["B"], 16)
    x_value = srp.x(PARAMS, first["pairing"], code, salt)
    u_value = srp.u(PARAMS, A, B)
    K = srp.session_key(PARAMS, srp.client_secret(PARAMS, B, a, x_value, u_value))
    M1 = srp.client_proof(PARAMS, first["pairing"], salt, A, B, K)
    record = {"A": A_hex, "M1": M1.hex(), "K": K, "key": srp.home_key(K)}
    record["finish"] = await _post_pair(lan_port, {
        "pake": "finish", "A": A_hex, "M1": M1.hex(), "name": "p"})
    assert record["finish"][0] == 200
    frame, err = open_pair_reply(record)
    assert err == "" and frame["kind"] == "reply"
    inner = json.loads(frame["body"]["body"])
    assert devices.home_key(inner["device_id"]) == record["key"]
    assert devices._pairing["fails"] == 0
    assert devices.pairing_open() is False
    await _settle()
    assert recorder.calls == [("pairing", "127.0.0.1", "pake", "")]


# --- (i) bounds ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_inflight_table_keeps_the_newest_four(server):
    srv, _daemon, _recorder, _loop, lan_port = server
    devices.begin(allow_plain=True)
    assert devices.SRP_MAX_INFLIGHT == 4
    sent = []
    for _ in range(devices.SRP_MAX_INFLIGHT + 1):
        A_hex = srp.pad(GROUP, srp.client_public(PARAMS, srp.random_ephemeral())).hex()
        status, _ = await _post_pair(lan_port, {"pake": "start", "A": A_hex})
        assert status == 200
        sent.append(A_hex)
    inflight = devices._pairing["srp_inflight"]
    assert list(inflight) == sent[1:]
    assert sent[0] not in inflight
    # The evicted start's finish is out of step; the newest still pairs.
    status, out = await _post_pair(lan_port, {
        "pake": "finish", "A": sent[0], "M1": "ab" * 32, "name": "p"})
    assert status == 403 and json.loads(out)["error"] == PAKE_SHAPE_REFUSAL


@pytest.mark.asyncio
async def test_the_start_budget_answers_busy_and_leaves_the_pairing_open(server):
    srv, _daemon, recorder, _loop, lan_port = server
    code = devices.begin(allow_plain=True)
    assert devices.SRP_MAX_STARTS == 32
    devices._pairing["srp_starts"] = devices.SRP_MAX_STARTS - 1
    status, _ = await _post_pair(lan_port, {"pake": "start", "A": "01" * 256})
    assert status == 200
    status, out = await _post_pair(lan_port, {"pake": "start", "A": "02" * 256})
    assert status == 403
    assert json.loads(out) == {"error": PAKE_BUSY_REFUSAL}
    assert devices.pairing_open() is True
    assert devices._pairing["fails"] == 0
    await _settle()
    assert recorder.calls == [("pairing", "127.0.0.1", "pake", "")]
    # A new code starts the count again.
    devices.begin(allow_plain=True)
    record = await typed_exchange(_fetch, lan_port, code)
    assert record["start"][0] == 200


# --- (j) the gate is unchanged ---------------------------------------------------


@pytest.mark.asyncio
async def test_an_unarmed_pairing_is_refused_before_any_srp_runs(server, monkeypatch):
    srv, _daemon, recorder, _loop, lan_port = server
    called = []
    real = devices.pairing_srp_start

    def spy(A_hex):
        called.append(A_hex)
        return real(A_hex)

    monkeypatch.setattr(devices, "pairing_srp_start", spy)
    devices.begin()
    status, out = await _post_pair(lan_port, {"pake": "start", "A": "01" * 256})
    assert status == 403
    assert json.loads(out) == {"error": PAIR_PLAIN_REFUSAL}
    assert called == []
    assert devices.pairing_open() is True
    await _settle()
    assert recorder.calls == [("pairing", "127.0.0.1", "pair_plain", "")]
    # Armed, the same body reaches it.
    devices.begin(allow_plain=True)
    status, _ = await _post_pair(lan_port, {"pake": "start", "A": "01" * 256})
    assert status == 200
    assert called == ["01" * 256]


def test_pairing_srp_start_refuses_an_unarmed_pairing_itself():
    devices.begin()
    assert devices.pairing_srp_start("01" * 256) == (None, "that pairing code is not valid")
    devices.reset_pairing()
    assert devices.pairing_srp_start("01" * 256) == (None, "that pairing code is not valid")
    assert devices.pairing_srp_finish("01" * 256, "ab" * 32) == (None, None, "shape")


# --- (k) lifetime ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_reset_or_a_new_code_between_the_steps_is_out_of_step(server):
    """A new code between the steps forgets the start: `pake`. A reset
    leaves no pairing at all, and the unchanged gate then speaks first,
    before the body is parsed — `pair_plain`, as for any typed request
    against no armed pairing."""
    srv, _daemon, recorder, _loop, lan_port = server
    for between, words, reason in (
        (lambda: devices.begin(allow_plain=True), PAKE_SHAPE_REFUSAL, "pake"),
        (devices.reset_pairing, PAIR_PLAIN_REFUSAL, "pair_plain"),
    ):
        recorder.calls.clear()
        code = devices.begin(allow_plain=True)
        a = srp.random_ephemeral()
        A = srp.client_public(PARAMS, a)
        A_hex = srp.pad(GROUP, A).hex()
        status, out = await _post_pair(lan_port, {"pake": "start", "A": A_hex})
        assert status == 200
        reply = json.loads(out)
        between()
        salt = bytes.fromhex(reply["salt"])
        B = int(reply["B"], 16)
        u_value = srp.u(PARAMS, A, B)
        K = srp.session_key(PARAMS, srp.client_secret(
            PARAMS, B, a, srp.x(PARAMS, reply["pairing"], code, salt), u_value))
        M1 = srp.client_proof(PARAMS, reply["pairing"], salt, A, B, K)
        status, out = await _post_pair(lan_port, {
            "pake": "finish", "A": A_hex, "M1": M1.hex(), "name": "p"})
        assert status == 403
        assert json.loads(out) == {"error": words}
        await _settle()
        assert recorder.calls == [("pairing", "127.0.0.1", reason, "")]


@pytest.mark.asyncio
async def test_a_used_or_expired_code_is_refused_at_the_start_in_the_redeems_words(
        server, monkeypatch):
    srv, _daemon, recorder, _loop, lan_port = server
    code = devices.begin(allow_plain=True)
    await pair_typed(_fetch, lan_port, code=code)
    status, out = await _post_pair(lan_port, {"pake": "start", "A": "01" * 256})
    assert status == 403
    assert json.loads(out)["error"] == "that pairing code has already been used"
    now = {"t": 1_000.0}
    monkeypatch.setattr(devices.time, "time", lambda: now["t"])
    devices.begin(allow_plain=True)
    now["t"] = 1_000.0 + devices.PAIRING_CODE_SECONDS + 1
    status, out = await _post_pair(lan_port, {"pake": "start", "A": "01" * 256})
    assert status == 403
    assert json.loads(out)["error"] == "that pairing code has expired"
    await _settle()
    assert recorder.calls == [("pairing", "127.0.0.1", "pake", "")] * 2


@pytest.mark.asyncio
async def test_a_code_spent_between_the_steps_is_refused_at_the_finish(server):
    """Start, then the QR route redeems the code, then the finish: the
    proof checks out but the redeem refuses in its own words, `pair_code`."""
    srv, _daemon, recorder, _loop, lan_port = server
    code = devices.begin(allow_plain=True)
    a = srp.random_ephemeral()
    A = srp.client_public(PARAMS, a)
    A_hex = srp.pad(GROUP, A).hex()
    status, out = await _post_pair(lan_port, {"pake": "start", "A": A_hex})
    assert status == 200
    reply = json.loads(out)
    token, _did, detail = devices.redeem(code, "scanner")
    assert token, detail
    salt = bytes.fromhex(reply["salt"])
    B = int(reply["B"], 16)
    u_value = srp.u(PARAMS, A, B)
    K = srp.session_key(PARAMS, srp.client_secret(
        PARAMS, B, a, srp.x(PARAMS, reply["pairing"], code, salt), u_value))
    M1 = srp.client_proof(PARAMS, reply["pairing"], salt, A, B, K)
    status, out = await _post_pair(lan_port, {
        "pake": "finish", "A": A_hex, "M1": M1.hex(), "name": "p"})
    assert status == 403
    assert json.loads(out)["error"] == "that pairing code has already been used"
    await _settle()
    assert recorder.calls == [("pairing", "127.0.0.1", "pair_code", "")]


def test_the_snapshot_carries_none_of_the_srp_state():
    code = devices.begin(allow_plain=True)
    text = json.dumps(devices.snapshot(), sort_keys=True)
    for word in ("pairing_id", "salt", "verifier", "srp", "inflight", "fails",
                 "plain", code, devices._pairing["pairing_id"],
                 devices._pairing["srp_salt"].hex()):
        assert word not in text, word
    assert devices.snapshot()["pairing_open"] is True
    # And nothing of it is persisted: the ledger carries the derived key
    # exactly as it carries a QR key, and nothing else.
    assert "srp" not in json.dumps(devices.load())


def test_redeem_keeps_its_signature_and_shares_its_tail():
    import inspect

    assert str(inspect.signature(devices.redeem)) == \
        "(code: 'str', name: 'str', *, first_ctr: 'int' = 0) -> 'tuple'"
    redeem_src = inspect.getsource(devices.redeem)
    proven_src = inspect.getsource(devices.redeem_proven)
    assert "return _spend(" in redeem_src and "return _spend(" in proven_src
    spend_src = inspect.getsource(devices._spend)
    for words in ("that pairing code has already been used",
                  "that pairing code has expired",
                  "can pair at most"):
        assert words in spend_src
        assert words not in redeem_src
    # `redeem_proven` does what `redeem` does with a proven code.
    code = devices.begin(allow_plain=True)
    key = b"\x09" * 32
    token, device_id, detail = devices.redeem_proven("p", home_key=key)
    assert token and device_id and detail == ""
    assert devices.home_key(device_id) == key
    assert devices.last_home_recv_ctr(device_id) == 0
    token2, _did, detail = devices.redeem_proven("p", home_key=key)
    assert token2 == "" and "used" in detail
    token3, _did, detail = devices.redeem(code, "p")
    assert token3 == "" and "used" in detail
    devices.reset_pairing()
    assert devices.redeem_proven("p", home_key=key) == (
        "", "", "that pairing code is not valid")


def test_the_access_log_knows_pake():
    assert "pake" in access_log.REASONS
    assert access_log.WEIGHTS["pake"] == 1
    assert access_log.REASON_WORDS["pake"]
    assert "pair_plain" in access_log.REASONS


# --- (l) the QR route is untouched -----------------------------------------------


@pytest.mark.asyncio
async def test_a_sealed_pair_on_an_armed_pairing_still_works(server):
    srv, _daemon, recorder, _loop, lan_port = server
    code = devices.begin(allow_plain=True)
    key = devices.pairing_home_key()
    chan = relay.channel_id(key, ns=relay.HOME)
    wire = relay.seal_frame(key, relay.DIR_PHONE_TO_MAC, 1, "pair",
                            {"code": code, "name": "scanner"}, ns=relay.HOME)
    status, out = await _fetch(
        "/api/pair", port=lan_port, data=wire.encode(),
        headers={"X-Bob-Channel": chan, "Content-Type": "text/plain"})
    assert status == 200, out
    frame, err = relay.open_frame(key, relay.DIR_MAC_TO_PHONE, out.decode(), 0,
                                  ns=relay.HOME)
    assert err == "", err
    assert frame["kind"] == "reply"
    assert frame["body"]["status"] == 200
    inner = json.loads(frame["body"]["body"])
    assert set(inner) == {"token", "device_id", "relay_key", "relay_url",
                          "relay_ws_url"}
    assert "home_key" not in inner and "M2" not in inner
    assert devices.home_key(inner["device_id"]) == key
    assert devices.last_home_recv_ctr(inner["device_id"]) == 1
    await _settle()
    assert recorder.calls == []


def test_begin_pairing_still_carries_the_qr_key_and_never_the_srp_state(server):
    srv, daemon, _recorder, _loop, _lan_port = server
    daemon.lan_access_enabled = True
    reply = daemon.begin_pairing(allow_typed=True)
    assert reply["ok"] is True, reply
    assert reply["allow_typed"] is True
    assert base64.b64decode(reply["home_key"]) == devices.pairing_home_key()
    text = json.dumps(reply)
    for word in ("salt", "verifier", "srp", "pairing_id", "inflight"):
        assert word not in text, word


# --- (m) the Swift half -----------------------------------------------------------


HARNESS = r'''
import Foundation

struct In: Decodable {
    let mode: String
    let I: String
    let s: String
    let B: String
    let P: String
    let a: String
    let M2: String?
}
let input = try! JSONDecoder().decode(In.self, from: FileHandle.standardInput.readDataToEndOfFile())
var out: [String: Any] = [:]
guard var client = SRPClient(aHex: input.a) else {
    out["error"] = "bad a"
    FileHandle.standardOutput.write(try! JSONSerialization.data(withJSONObject: out))
    exit(0)
}
out["A"] = client.start()
switch client.finish(identity: input.I, saltHex: input.s, bHex: input.B, password: input.P) {
case .failure(let why):
    out["error"] = why.words
case .success(let proof):
    out["M1"] = proof.m1
    out["K"] = proof.k.map { String(format: "%02x", $0) }.joined()
    out["home_key"] = client.homeKey.map { String(format: "%02x", $0) }.joined()
    if let m2 = input.M2 {
        out["verified"] = client.verify(m2Hex: m2)
    }
}
FileHandle.standardOutput.write(try! JSONSerialization.data(withJSONObject: out, options: [.sortedKeys]))
'''


@pytest.fixture(scope="module")
def swift_client(tmp_path_factory):
    swiftc = shutil.which("swiftc")
    if not swiftc:
        pytest.skip("Swift toolchain unavailable")
    tmp = tmp_path_factory.mktemp("srp-probe")
    harness = tmp / "main.swift"
    harness.write_text(HARNESS)
    executable = tmp / "SRPProbe"
    built = subprocess.run(
        [swiftc, "-O", str(BIGUINT_SWIFT), str(SRP_SWIFT), str(harness),
         "-o", str(executable)],
        capture_output=True, text=True, timeout=300)
    assert built.returncode == 0, built.stderr

    def run(payload: dict) -> dict:
        ran = subprocess.run([str(executable)], input=json.dumps(payload),
                             capture_output=True, text=True, timeout=120)
        assert ran.returncode == 0, ran.stderr
        return json.loads(ran.stdout)

    return run


def test_the_swift_client_reproduces_the_cross_vector(swift_client):
    vector = _cross_vector()
    got = swift_client({"mode": "finish", "I": vector["I"], "s": vector["s"],
                        "B": vector["B"], "P": vector["P"], "a": vector["a"],
                        "M2": vector["M2"]})
    assert got.get("error") is None, got
    assert got["A"] == vector["A"]
    assert got["M1"] == vector["M1"]
    assert got["K"] == vector["K"]
    assert got["home_key"] == vector["home_key"]
    assert got["verified"] is True


def test_the_swift_client_agrees_with_python_as_the_server(swift_client):
    for _ in range(3):
        code = devices.begin(allow_plain=True)
        pairing = devices._pairing
        a = srp.random_ephemeral()
        a_hex = a.to_bytes(32, "big").hex()
        A = srp.client_public(PARAMS, a)
        b, B = srp.server_start(PARAMS, int(pairing["srp_verifier"]))
        got = swift_client({"mode": "finish", "I": pairing["pairing_id"],
                            "s": pairing["srp_salt"].hex(),
                            "B": srp.pad(GROUP, B).hex(), "P": code,
                            "a": a_hex})
        assert got.get("error") is None, got
        assert got["A"] == srp.pad(GROUP, A).hex()
        u_value = srp.u(PARAMS, A, B)
        S = srp.server_secret(PARAMS, A, b, int(pairing["srp_verifier"]), u_value)
        K = srp.session_key(PARAMS, S)
        M1 = srp.client_proof(PARAMS, pairing["pairing_id"], pairing["srp_salt"],
                              A, B, K)
        assert got["M1"] == M1.hex()
        assert got["K"] == K.hex()
        assert got["home_key"] == srp.home_key(K).hex()
        M2 = srp.server_proof(PARAMS, A, M1, K)
        flipped = bytes([M2[0] ^ 1]) + M2[1:]
        good = swift_client({"mode": "finish", "I": pairing["pairing_id"],
                             "s": pairing["srp_salt"].hex(),
                             "B": srp.pad(GROUP, B).hex(), "P": code,
                             "a": a_hex, "M2": M2.hex()})
        assert good["verified"] is True
        bad = swift_client({"mode": "finish", "I": pairing["pairing_id"],
                            "s": pairing["srp_salt"].hex(),
                            "B": srp.pad(GROUP, B).hex(), "P": code,
                            "a": a_hex, "M2": flipped.hex()})
        assert bad["verified"] is False


def test_the_swift_client_refuses_a_degenerate_b(swift_client):
    vector = _cross_vector()
    for B in ("00" * 256, srp.pad(GROUP, GROUP.N).hex(), "ab" * 255, "AB" * 256):
        got = swift_client({"mode": "finish", "I": vector["I"], "s": vector["s"],
                            "B": B, "P": vector["P"], "a": vector["a"]})
        assert got.get("error"), B
        assert "M1" not in got


def test_pake_swift_files_are_registered_in_the_project():
    pbx = PBXPROJ.read_text(encoding="utf-8")
    lines = pbx.splitlines()
    assert sum("BigUInt.swift" in line for line in lines) == 4
    assert sum("SRP.swift" in line for line in lines) == 4
    assert sum("SRPTests.swift" in line for line in lines) == 2
    big = BIGUINT_SWIFT.read_text(encoding="utf-8")
    srp_text = SRP_SWIFT.read_text(encoding="utf-8")
    assert [l for l in big.splitlines() if l.startswith("import ")] == ["import Foundation"]
    assert sorted(l for l in srp_text.splitlines() if l.startswith("import ")) == [
        "import CryptoKit", "import Foundation"]
    for text in (big, srp_text):
        for banned in (")!", "try!", "as!"):
            assert banned not in text, banned
    assert 'static let homeKeyInfo = Data("bob-home v1 pake".utf8)' in srp_text
    tests = SRP_TESTS_SWIFT.read_text(encoding="utf-8")
    for case in ("testRFC5054AppendixBVector", "testCrossVectorMatchesTheMac",
                 "testRefusesADegenerateB", "testRefusesAZeroU",
                 "testRejectsAWrongM2", "testHomeKeyInfoIsTheMacs"):
        assert f"func {case}()" in tests, case
