# host/tests/test_access_log_doors.py
"""Every recording site on the phone doors, and the alert that rides them.

Seams: a real `ApiServer` on free ports over a real `BobDaemon` whose
`note_access_refusal` is replaced by a recorder (`test_lan_access`'s
fixtures, copied rather than imported — importing that module binds ports);
`_Request` built by hand with `peer`; `RelayConnector` with a stub `_api`;
and a `BobDaemon` with `_access_log` on a temp path for the raise, the
snapshot and the ack.

The standing rule pinned here: **the recorder never changes the verdict.**
Every refusal below is compared with the recorder replaced by a spy against
the same request with the real recorder — status, content type and body
(the inner dict, where the body is a freshly sealed frame).
"""

from __future__ import annotations

import asyncio
import json
import socket
import urllib.error
import urllib.request

import pytest
import pytest_asyncio

from dark_army_daemon import access_log, alerts, api_server as api_mod
from dark_army_daemon import attachments
from dark_army_daemon import devices, event_log, lan_hosts
from dark_army_daemon import paths, relay, relay_client
from dark_army_daemon import srp
from dark_army_daemon.api_server import (
    HOME_UPDATE_REFUSAL, PAIR_UPDATE_REFUSAL, ApiServer, _Request)
from dark_army_daemon.daemon import BobDaemon
from tests.pake_pair import pair_typed
from tests.free_ports import free_ports


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
def token_path(tmp_path, monkeypatch):
    path = tmp_path / "api-token"
    monkeypatch.setattr(api_mod, "API_TOKEN_PATH", path)
    monkeypatch.setattr(api_mod, "ensure_state_dir", lambda: tmp_path)
    return path


@pytest.fixture
def ports(monkeypatch):
    loop_port, lan_port = _free_ports(2)
    monkeypatch.setattr(api_mod, "LAN_API_PORT", lan_port)
    return loop_port, lan_port


class _Recorder:
    """Stands in for `BobDaemon.note_access_refusal`; keeps every call."""

    def __init__(self):
        self.calls: list[tuple] = []

    async def __call__(self, door, peer, reason, device_id=""):
        self.calls.append((door, peer, reason, device_id))


@pytest_asyncio.fixture
async def server(token_path, ports):
    loop_port, _lan_port = ports
    daemon = BobDaemon()
    srv = ApiServer(daemon, port=loop_port)
    daemon._api = srv
    recorder = _Recorder()
    daemon.note_access_refusal = recorder
    await srv.start()
    daemon.lan_access_enabled = True
    await srv.start_lan()
    try:
        yield srv, daemon, recorder, ports
    finally:
        await srv.stop()


def _fetch(path, *, port, data=None, headers=None, method=None):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=data, headers=headers or {},
        method=method or ("POST" if data is not None else "GET"),
    )
    try:
        resp = urllib.request.urlopen(req, timeout=5)
        return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


async def fetch(*args, **kwargs):
    return await asyncio.to_thread(_fetch, *args, **kwargs)


async def _settle():
    """Let the fire-and-forget task the recorder created run."""
    for _ in range(3):
        await asyncio.sleep(0)


async def _pair_plain(lan_port: int, name: str = "phone") -> dict:
    return await pair_typed(fetch, lan_port, name=name)


def _request(path="/api/home", *, headers=None, body=b"", method="POST",
             peer="10.0.0.9", preverified=None) -> _Request:
    return _Request(method, path, "", dict(headers or {}), body,
                    preverified, peer=peer)


def _open_err(key: bytes, body: bytes) -> dict:
    frame, err = relay.open_frame(key, relay.DIR_MAC_TO_PHONE, body.decode(),
                                  0, ns=relay.HOME)
    assert err == "", err
    return frame


# --- the LAN door's plaintext routes ------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("path,method", [
    ("/api/state", "GET"), ("/api/usage", "GET"), ("/api/log", "GET"),
    ("/api/terminal", "GET"), ("/api/action", "POST"),
])
async def test_each_plaintext_route_records_and_still_answers_426(server, path, method):
    srv, daemon, recorder, (_loop, lan_port) = server
    data = b"{}" if method == "POST" else None
    status, out = await fetch(path, port=lan_port, data=data)
    assert status == 426
    assert json.loads(out)["error"] == HOME_UPDATE_REFUSAL
    await _settle()
    assert recorder.calls == [("lan", "127.0.0.1", "plaintext", "")]


@pytest.mark.asyncio
async def test_an_unknown_route_records_not_found(server):
    srv, daemon, recorder, (_loop, lan_port) = server
    status, _ = await fetch("/api/events", port=lan_port)
    assert status == 404
    await _settle()
    assert recorder.calls == [("lan", "127.0.0.1", "not_found", "")]


@pytest.mark.asyncio
async def test_the_loopback_door_records_nothing(server):
    srv, daemon, recorder, (loop_port, _lan) = server
    status, _ = await fetch("/api/state", port=loop_port)
    assert status == 200
    status, _ = await fetch("/api/nothing", port=loop_port)
    await _settle()
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_a_body_over_the_cap_records_oversize_on_the_upload_door(server):
    srv, daemon, recorder, (_loop, lan_port) = server
    reader, writer = await asyncio.open_connection("127.0.0.1", lan_port)
    try:
        head = (f"POST /api/upload HTTP/1.1\r\nHost: x\r\n"
                f"Content-Length: {api_mod.MAX_BODY_BYTES + 1}\r\n\r\n")
        writer.write(head.encode())
        await writer.drain()
        # The door closes the connection with no answer, as before.
        assert await reader.read() == b""
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass
    await _settle()
    assert recorder.calls == [("upload", "127.0.0.1", "oversize", "")]


@pytest.mark.asyncio
async def test_an_oversize_upload_from_a_paired_phone_records_the_device_id(server):
    """The body rule opened the header frame, so the door knows whose photo
    was too large: the refusal carries the device id, which is what makes
    it weigh nothing at the detector. The connection is still dropped with
    no answer, exactly as before."""
    srv, daemon, recorder, (_loop, lan_port) = server
    paired = await _pair_plain(lan_port)
    key, device_id = paired["key"], paired["device_id"]
    chan = relay.channel_id(key, ns=relay.HOME)
    recorder.calls.clear()
    frame = relay.seal_frame(key, relay.DIR_PHONE_TO_MAC, 1, "upload",
                             {"staging": "s1", "name": "a.png"}, ns=relay.HOME)
    reader, writer = await asyncio.open_connection("127.0.0.1", lan_port)
    try:
        head = (f"POST /api/upload HTTP/1.1\r\nHost: x\r\n"
                f"X-Bob-Channel: {chan}\r\nX-Bob-Frame: {frame}\r\n"
                f"Content-Length: {attachments.MAX_ATTACHMENT_BYTES + 2048}\r\n\r\n")
        writer.write(head.encode())
        await writer.drain()
        assert await reader.read() == b""
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass
    await _settle()
    assert recorder.calls == [("upload", "127.0.0.1", "oversize", device_id)]
    # And through the real detector, five of those alert nobody.
    d = access_log.BurstDetector()
    for door, peer, reason, dev in recorder.calls * access_log.BURST_THRESHOLD:
        assert d.note(peer, reason, 1000.0, device_id=dev) is None
    assert d.tracked() == 0


# --- _home_open, every rung -------------------------------------------------------


@pytest.mark.asyncio
async def test_home_open_records_origin_before_its_403(server):
    srv, daemon, recorder, _ = server
    out = srv._home_open(_request(headers={"origin": "http://evil"}), wire="")
    assert out[0] is None and out[2][0] == 403
    await _settle()
    assert recorder.calls == [("lan", "10.0.0.9", "origin", "")]


@pytest.mark.asyncio
async def test_home_open_records_unpaired_without_the_header_value(server, tmp_path):
    srv, daemon, recorder, _ = server
    # The real recorder this time, on a temp journal: the raw
    # `X-Bob-Channel` is derived from the home key and must never land.
    del daemon.note_access_refusal
    log = access_log.AccessLog(path=tmp_path / "access.jsonl")
    log.open()
    daemon._access_log = log
    chan = "deadbeefcafef00d" * 2
    out = srv._home_open(_request(headers={"x-bob-channel": chan}), wire="x")
    assert out[2][0] == 403
    assert json.loads(out[2][2])["error"] == "that phone is not paired"
    await _settle()
    await asyncio.get_running_loop().run_in_executor(None, lambda: None)
    rows = log.recent()
    assert len(rows) == 1
    assert rows[0]["reason"] == "unpaired"
    assert rows[0]["device_id"] == ""
    assert rows[0]["peer"] == "10.0.0.9"
    assert chan not in log.path.read_text()


@pytest.mark.asyncio
async def test_a_paired_phones_stale_counter_records_ctr_with_the_device_id(server):
    srv, daemon, recorder, (_loop, lan_port) = server
    paired = await _pair_plain(lan_port)
    key, device_id = paired["key"], paired["device_id"]
    chan = relay.channel_id(key, ns=relay.HOME)
    wire = relay.seal_frame(key, relay.DIR_PHONE_TO_MAC, 5, "state", {},
                            ns=relay.HOME)
    # Move the floor past the frame so it replays.
    devices.note_home_recv_ctr(device_id, 9, durable=True)
    recorder.calls.clear()
    with_recorder = srv._home_open(
        _request(headers={"x-bob-channel": chan}), wire=wire)
    assert with_recorder[0] is None
    status, ctype, body = with_recorder[2]
    assert (status, ctype) == (200, "text/plain")
    inner = _open_err(key, body)
    assert inner["kind"] == "err"
    assert inner["body"]["status"] == 409
    assert inner["body"]["ctr_expected"] == 10
    await _settle()
    assert recorder.calls == [("lan", "10.0.0.9", "ctr", device_id)]
    # And byte-for-byte the same verdict with the recorder spied out.
    srv._record_access = lambda *a, **k: None
    spied = srv._home_open(_request(headers={"x-bob-channel": chan}), wire=wire)
    assert (spied[2][0], spied[2][1]) == (status, ctype)
    assert _open_err(key, spied[2][2])["body"] == inner["body"]


@pytest.mark.asyncio
async def test_a_broken_seal_records_seal_with_the_device_id_and_the_same_403(server):
    srv, daemon, recorder, (_loop, lan_port) = server
    paired = await _pair_plain(lan_port)
    key, device_id = paired["key"], paired["device_id"]
    chan = relay.channel_id(key, ns=relay.HOME)
    recorder.calls.clear()
    real = srv._home_open(_request(headers={"x-bob-channel": chan}),
                          wire="not a frame at all")
    srv._record_access = lambda *a, **k: None
    spied = srv._home_open(_request(headers={"x-bob-channel": chan}),
                           wire="not a frame at all")
    assert real[2] == spied[2]
    assert real[2][0] == 403
    await _settle()
    assert len(recorder.calls) == 1
    assert recorder.calls[0][0] == "lan"
    assert recorder.calls[0][2] in ("seal", "shape")
    assert recorder.calls[0][3] == device_id


@pytest.mark.asyncio
async def test_a_wrong_kind_records_kind_and_success_records_nothing(server):
    srv, daemon, recorder, (_loop, lan_port) = server
    paired = await _pair_plain(lan_port)
    key, device_id = paired["key"], paired["device_id"]
    chan = relay.channel_id(key, ns=relay.HOME)
    recorder.calls.clear()
    wire = relay.seal_frame(key, relay.DIR_PHONE_TO_MAC, 1, "state", {},
                            ns=relay.HOME)
    out = srv._home_open(_request(path="/api/upload",
                                  headers={"x-bob-channel": chan}),
                         wire=wire, expect_kind="upload")
    assert out[0] is None and out[2][0] == 200
    await _settle()
    assert recorder.calls == [("upload", "10.0.0.9", "kind", device_id)]
    recorder.calls.clear()
    wire = relay.seal_frame(key, relay.DIR_PHONE_TO_MAC, 2, "state", {},
                            ns=relay.HOME)
    got = srv._home_open(_request(headers={"x-bob-channel": chan}), wire=wire)
    assert got[0] == device_id
    await _settle()
    assert recorder.calls == []


# --- the preverified upload path -------------------------------------------------


@pytest.mark.asyncio
async def test_accept_preverified_records_unpaired_and_origin(server):
    srv, daemon, recorder, _ = server
    out = srv._home_accept_preverified(_request(path="/api/upload"))
    assert out[2][0] == 403
    await _settle()
    assert recorder.calls == [("upload", "10.0.0.9", "unpaired", "")]
    recorder.calls.clear()
    out = srv._home_accept_preverified(_request(
        path="/api/upload", headers={"origin": "http://evil"},
        preverified=("dev", b"k" * 32, {"ctr": 1})))
    assert out[2] == (403, "application/json", b'{"error":"forbidden"}')
    await _settle()
    assert recorder.calls == [("upload", "10.0.0.9", "origin", "")]
    recorder.calls.clear()
    # A key that no longer matches the ledger: un-paired mid-body.
    out = srv._home_accept_preverified(_request(
        path="/api/upload", preverified=("dev", b"k" * 32, {"ctr": 1})))
    assert out[2][0] == 403
    await _settle()
    assert recorder.calls == [("upload", "10.0.0.9", "unpaired", "")]


@pytest.mark.asyncio
async def test_an_upload_whose_blob_does_not_open_records_blob(server):
    srv, daemon, recorder, (_loop, lan_port) = server
    paired = await _pair_plain(lan_port)
    key, device_id = paired["key"], paired["device_id"]
    chan = relay.channel_id(key, ns=relay.HOME)
    recorder.calls.clear()
    frame = relay.seal_frame(key, relay.DIR_PHONE_TO_MAC, 1, "upload",
                             {"staging": "s1", "name": "a.png"}, ns=relay.HOME)
    status, out = await fetch("/api/upload", port=lan_port, data=b"garbage",
                              headers={"X-Bob-Channel": chan,
                                       "X-Bob-Frame": frame})
    assert status == 200
    await _settle()
    assert recorder.calls == [("upload", "127.0.0.1", "blob", device_id)]


# --- pairing --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plain_pairing_records_origin_and_the_old_shape(server):
    """The old plain `{code, name}` body on an armed pairing is the
    update-the-phone refusal, reason `pake`, and counts no fail."""
    srv, daemon, recorder, _ = server
    out = srv._lan_pair(_request(path="/api/pair",
                                 headers={"origin": "http://evil"}))
    assert out == (403, "application/json", b'{"error":"forbidden"}')
    await _settle()
    assert recorder.calls == [("pairing", "10.0.0.9", "origin", "")]
    recorder.calls.clear()
    devices.begin(allow_plain=True)
    body = json.dumps({"code": "000000", "name": "p"}).encode()
    real = srv._lan_pair(_request(path="/api/pair", body=body))
    assert real[0] == 403
    assert json.loads(real[2])["error"] == PAIR_UPDATE_REFUSAL
    assert devices._pairing["fails"] == 0
    await _settle()
    assert recorder.calls == [("pairing", "10.0.0.9", "pake", "")]
    srv._record_access = lambda *a, **k: None
    spied = srv._lan_pair(_request(path="/api/pair", body=body))
    assert spied == real


@pytest.mark.asyncio
async def test_typed_pairing_records_a_wrong_proof_as_pair_code(server):
    """A finish whose `M1` was computed from another code is the
    wrong-code sentence, reason `pair_code`, one fail — a wrong code's
    exact footing; the recorder spied out changes nothing."""
    srv, daemon, recorder, _ = server
    code = devices.begin(allow_plain=True)
    params = srp.PRODUCTION
    a = srp.random_ephemeral()
    A = srp.client_public(params, a)
    A_hex = srp.pad(params.group, A).hex()
    started = srv._lan_pair(_request(
        path="/api/pair", body=json.dumps({"pake": "start", "A": A_hex}).encode()))
    assert started[0] == 200, started
    reply = json.loads(started[2])
    salt = bytes.fromhex(reply["salt"])
    B = int(reply["B"], 16)
    wrong = "A" * devices._CODE_LEN if code != "A" * devices._CODE_LEN \
        else "B" * devices._CODE_LEN
    x_value = srp.x(params, reply["pairing"], wrong, salt)
    u_value = srp.u(params, A, B)
    K = srp.session_key(params, srp.client_secret(params, B, a, x_value, u_value))
    M1 = srp.client_proof(params, reply["pairing"], salt, A, B, K)
    body = json.dumps({"pake": "finish", "A": A_hex, "M1": M1.hex(),
                       "name": "p"}).encode()
    real = srv._lan_pair(_request(path="/api/pair", body=body))
    assert real[0] == 403
    assert json.loads(real[2])["error"] == "that pairing code is not valid"
    assert devices._pairing["fails"] == 1
    await _settle()
    assert recorder.calls == [("pairing", "10.0.0.9", "pair_code", "")]
    # The entry was consumed: the same finish again is out of step, and
    # the verdict is the same with the recorder spied out.
    recorder.calls.clear()
    real = srv._lan_pair(_request(path="/api/pair", body=body))
    assert real[0] == 403
    await _settle()
    assert recorder.calls == [("pairing", "10.0.0.9", "pake", "")]
    srv._record_access = lambda *a, **k: None
    spied = srv._lan_pair(_request(path="/api/pair", body=body))
    assert spied == real


@pytest.mark.asyncio
async def test_sealed_pairing_records_channel_seal_kind_and_code_misses(server):
    srv, daemon, recorder, _ = server
    devices.begin(allow_plain=True)
    pk = devices.pairing_home_key()
    assert pk is not None
    # Not the active pairing's channel.
    out = srv._lan_pair(_request(path="/api/pair",
                                 headers={"x-bob-channel": "0" * 32}))
    assert out[0] == 403
    await _settle()
    assert recorder.calls == [("pairing", "10.0.0.9", "pair_channel", "")]
    recorder.calls.clear()
    chan = relay.channel_id(pk, ns=relay.HOME)
    # The right channel, a body that is no frame.
    out = srv._lan_pair(_request(path="/api/pair", headers={"x-bob-channel": chan},
                                 body=b"nonsense"))
    assert out[0] == 403
    await _settle()
    assert recorder.calls[0][:3] == ("pairing", "10.0.0.9", "shape") \
        or recorder.calls[0][:3] == ("pairing", "10.0.0.9", "seal")
    recorder.calls.clear()
    # A verified frame that is not a pairing request.
    wire = relay.seal_frame(pk, relay.DIR_PHONE_TO_MAC, 1, "state", {},
                            ns=relay.HOME)
    out = srv._lan_pair(_request(path="/api/pair", headers={"x-bob-channel": chan},
                                 body=wire.encode()))
    assert out[0] == 403
    await _settle()
    assert recorder.calls == [("pairing", "10.0.0.9", "not_pair", "")]
    recorder.calls.clear()
    # A pairing request with the wrong code: a sealed err, still recorded.
    wire = relay.seal_frame(pk, relay.DIR_PHONE_TO_MAC, 2, "pair",
                            {"code": "000000", "name": "p"}, ns=relay.HOME)
    out = srv._lan_pair(_request(path="/api/pair", headers={"x-bob-channel": chan},
                                 body=wire.encode()))
    assert out[:2] == (200, "text/plain")
    await _settle()
    assert recorder.calls == [("pairing", "10.0.0.9", "pair_code", "")]


# --- the relay ----------------------------------------------------------------------


class _StubApi:
    def __init__(self):
        self.calls = []

    def _record_access(self, door, peer, reason, device_id=""):
        self.calls.append((door, peer, reason, device_id))


def test_relay_drop_records_with_the_device_id_and_still_counts():
    api = _StubApi()
    conn = relay_client.RelayConnector(api)
    conn._drop("seal", "dev1")
    assert conn.dropped == {"seal": 1}
    assert api.calls == [("relay", "dev1", "seal", "dev1")]


def test_relay_drop_survives_a_double_without_the_recorder():
    conn = relay_client.RelayConnector(object())
    conn._drop("rate", "dev1")
    assert conn.dropped == {"rate": 1}


def test_every_relay_drop_passes_the_device_id():
    from pathlib import Path
    import re
    src = Path(relay_client.__file__).read_text()
    assert re.findall(r'self\._drop\("[a-z]+"\)', src) == []
    assert src.count("self._drop(") >= 4


@pytest.mark.asyncio
async def test_a_mailbox_frame_that_does_not_open_records_seal_on_the_relay_door():
    api = _StubApi()
    conn = relay_client.RelayConnector(api)
    relay.create_channel("dev1")
    await conn._handle_wire("dev1", b"not-a-frame")
    assert api.calls and api.calls[0][0] == "relay"
    assert api.calls[0][1] == "dev1"
    assert api.calls[0][2] in ("seal", "shape")
    assert api.calls[0][3] == "dev1"


# --- the alert, the snapshot and the ack -------------------------------------------


@pytest_asyncio.fixture
async def alerting_daemon(tmp_path, monkeypatch):
    d = BobDaemon()
    log = access_log.AccessLog(path=tmp_path / "access.jsonl")
    log.open()
    d._access_log = log
    diary = event_log.EventLog(path=tmp_path / "diary.jsonl")
    diary.open()
    d._event_log = diary
    monkeypatch.setattr(d, "_wake_surfaces", _noop_async)
    banners = _Banners()
    d.add_observer(banners)
    return d, log, diary, banners


async def _noop_async(*_a, **_k):
    return None


class _Banners:
    """An `on_alerts` observer: what the menu bar's notifier would be handed."""

    def __init__(self):
        self.posted: list[dict] = []

    def on_alerts(self, alerts):
        self.posted.extend(alerts)


async def _drain_executor():
    await asyncio.get_running_loop().run_in_executor(None, lambda: None)
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_five_plaintext_knocks_raise_one_alert_and_a_sixth_raises_none(alerting_daemon):
    d, log, diary, banners = alerting_daemon
    for _ in range(access_log.BURST_THRESHOLD):
        await d.note_access_refusal("lan", "10.0.0.9", "plaintext")
    await _drain_executor()
    # The raise delivers at once (`_deliver_alerts` drains `_undelivered`
    # into the banner and the buzz), so the row is read where the notifier
    # would read it and in the `_alerts` history.
    assert d._undelivered == []
    assert len(banners.posted) == 1
    assert d._alerts[-1] == banners.posted[0]
    row = banners.posted[0]
    assert row["kind"] == "security"
    # The burst line is on the file before the banner went out, so the
    # frame the delivery wakes never carries a banner beside an empty
    # `security.alerts`; and the refusals sit before it in the log.
    assert [a["id"] for a in d.security_snapshot()["alerts"]] == \
        [row["id"].removeprefix("access:")]
    order = [r["kind"] for r in reversed(log.recent())]
    assert order == ["refusal"] * access_log.BURST_THRESHOLD + ["burst"]
    assert row["session_id"] == ""
    assert row["character"] == ""
    assert row["rule"] == "access_burst"
    assert "10.0.0.9" in row["body"]
    assert row["id"].startswith("access:")
    assert [e["kind"] for e in diary.recent()] == ["access_burst"]
    assert "10.0.0.9" in diary.recent()[0]["text"]
    kinds = [r["kind"] for r in log.recent()]
    assert kinds.count("refusal") == access_log.BURST_THRESHOLD
    assert kinds.count("burst") == 1
    # A sixth knock inside the hour: logged, no second alert.
    await d.note_access_refusal("lan", "10.0.0.9", "plaintext")
    await _drain_executor()
    assert len(banners.posted) == 1
    assert [r["kind"] for r in log.recent()].count("refusal") == 6


@pytest.mark.asyncio
async def test_the_snapshot_lists_the_alert_the_moment_the_raise_returns(alerting_daemon):
    """`_raise_access_alert` awaits the burst append before `_deliver_alerts`
    and the wake: the observer the delivery calls already sees the alert in
    the snapshot, with nothing drained from the executor in between."""
    d, log, _diary, _banners = alerting_daemon
    seen: list = []

    class _Observer:
        def on_alerts(self, alerts):
            seen.append([a["id"] for a in d.security_snapshot()["alerts"]])

    d.add_observer(_Observer())
    await d._raise_access_alert("relay", {"peer": "dev1", "count": 5,
                                          "window_seconds": 600})
    assert len(seen) == 1 and len(seen[0]) == 1
    assert d.security_snapshot()["alerts"][0]["id"] == seen[0][0]
    assert [r["kind"] for r in log.recent()] == ["burst"]


@pytest.mark.asyncio
async def test_an_access_alert_leaves_the_queued_agent_alerts_to_the_snapshot(
        alerting_daemon, monkeypatch):
    """A burst raised while `_push_agents_snapshot` sits between deciding a
    card (queued in `_undelivered`) and publishing the snapshot that lists
    its session delivers its own row only. Drained here, the phone leg would
    judge the card against the stale list and write it `withheld:unlisted`
    — a real buzz lost, never re-raised."""
    from dark_army_daemon import buzz_ledger, relay
    from dark_army_daemon import daemon as daemon_module
    from tests.test_alert_delivery import _Connector, _alert, _settle

    d, _log, _diary, banners = alerting_daemon
    monkeypatch.setattr(buzz_ledger, "mac_idle_seconds", lambda: None)
    monkeypatch.setattr(daemon_module, "PUSH_GRACE_SECONDS", 0.0)
    monkeypatch.setattr(relay, "channel_ids", lambda: {"phone-1": "c" * 32})
    monkeypatch.setattr(relay, "push_token", lambda did: "ab" * 32)
    d.phone_push_enabled = True
    d.remote_access_enabled = True
    d._relay_connector = _Connector()
    outcomes = []
    monkeypatch.setattr(d, "_ledger_buzz", lambda pending, outcome, **_k:
                        outcomes.append(([a["id"] for a in pending], outcome)))
    card = dict(_alert("s3"), kind="question", nickname="Vex")
    d._agents_snapshot_cache = {"running": [{"session_id": "s3"}]}   # stale
    d._undelivered = [card]
    await d._raise_access_alert("lan", {"peer": "10.0.0.9", "count": 5,
                                        "window_seconds": 600})
    await _settle(d)
    assert [a["kind"] for a in banners.posted] == ["security"]
    assert d._undelivered == [card]            # left for the snapshot drain
    assert outcomes == [(["access:" + d._access_fold["alert_id"]], "sent:1")]
    # The snapshot lands, then its own drain: the card buzzes.
    d._agents_snapshot_cache = {"waiting": [{"session_id": "s3"}]}
    d._deliver_alerts()
    await _settle(d)
    assert outcomes[-1] == (["s3:card:1"], "sent:1")
    assert [b["kind"] for _, b in d._relay_connector.pushed] == \
        ["security", "question"]


@pytest.mark.asyncio
async def test_twenty_stale_counters_from_the_paired_phone_raise_nothing(alerting_daemon):
    d, log, diary, banners = alerting_daemon
    for _ in range(20):
        await d.note_access_refusal("lan", "10.0.0.9", "ctr", device_id="dev1")
    await _drain_executor()
    assert banners.posted == []
    assert d._alerts == []
    assert len(log.recent()) == 20
    assert all(r["device_id"] == "dev1" for r in log.recent())
    assert diary.recent() == []


@pytest.mark.asyncio
async def test_the_snapshot_lists_the_alert_until_it_is_acknowledged(alerting_daemon):
    d, log, diary, _banners = alerting_daemon
    assert d.security_snapshot() == {"available": True, "alerts": [],
                                     "hidden": 0}
    for _ in range(access_log.BURST_THRESHOLD):
        await d.note_access_refusal("pairing", "10.0.0.9", "pair_code")
    await _drain_executor()
    snap = d.security_snapshot()
    assert snap["available"] is True
    assert snap["hidden"] == 0
    assert len(snap["alerts"]) == 1
    alert = snap["alerts"][0]
    assert set(alert) == {"id", "ts", "door", "door_word", "peer", "count",
                          "window_seconds", "text", "peers", "last_seen"}
    assert alert["peers"] == 1
    assert alert["last_seen"] == alert["ts"]
    assert alert["door"] == "pairing"
    # The door word rides beside the token so no client draws `pairing`
    # / `lan` raw; it is the label the sentences use.
    assert alert["door_word"] == access_log.door_label("pairing")
    assert access_log.door_label("lan") == "Wi-Fi"
    assert alert["peer"] == "10.0.0.9"
    assert alert["count"] == access_log.BURST_THRESHOLD
    ok, detail = await d.ack_access_alert(alert["id"])
    assert (ok, detail) == (True, "")
    assert d.security_snapshot()["alerts"] == []
    ok, detail = await d.ack_access_alert(alert["id"])
    assert ok is False
    assert detail == access_log.ACCESS_ALERT_MISSING_REFUSAL
    assert [r["kind"] for r in log.recent()][0] == "burst_ack"


@pytest.mark.asyncio
async def test_ack_refuses_an_unknown_empty_or_non_string_id(alerting_daemon):
    d, _log, _diary, _banners = alerting_daemon
    for bad in ("", "   ", None, 7, "nothere"):
        ok, detail = await d.ack_access_alert(bad)
        assert ok is False
        assert detail == access_log.ACCESS_ALERT_MISSING_REFUSAL


@pytest.mark.asyncio
async def test_a_daemon_without_a_log_records_nothing_and_says_so():
    d = BobDaemon()
    assert d._access_log is None
    await d.note_access_refusal("lan", "10.0.0.9", "plaintext")
    assert d._undelivered == []
    assert d.security_snapshot() == {"available": False, "alerts": []}
    ok, detail = await d.ack_access_alert("x")
    assert ok is False and detail == access_log.ACCESS_ALERT_MISSING_REFUSAL


# --- the machine-wide floor and the fold -------------------------------------------


@pytest.mark.asyncio
async def test_two_hundred_rotating_peers_raise_one_alert_and_fold_the_rest(alerting_daemon):
    """Two hundred free addresses, five plaintext knocks each, inside one
    second (well inside `ALERT_FLOOR_SECONDS`): one raise, one banner, one
    diary line, one alert row; the other 199 bursts fold. The journal keeps
    every refusal, the one `burst` line and — pinned here — exactly one
    `burst_more` line, the newest: a superseded fold leaves memory on the
    append's own prune."""
    d, log, diary, banners = alerting_daemon
    for i in range(200):
        for _ in range(access_log.BURST_THRESHOLD):
            await d.note_access_refusal("lan", f"fe80::{i:x}", "plaintext")
    await _drain_executor()
    assert len(banners.posted) == 1
    assert len(d._alerts) == 1
    assert d._undelivered == []
    assert [e["kind"] for e in diary.recent()] == ["access_burst"]
    snap = d.security_snapshot()
    assert snap["hidden"] == 0
    assert len(snap["alerts"]) == 1
    alert = snap["alerts"][0]
    assert alert["count"] == 1000
    assert alert["peers"] == 200
    assert alert["peer"] == "fe80::0"
    assert alert["text"] == ("1000 refused attempts from fe80::0 and 199 other "
                             "sources at the Wi-Fi door")
    assert alert["last_seen"] >= alert["ts"]
    kinds = [r["kind"] for r in log.recent()]
    assert kinds.count("refusal") == 1000
    assert kinds.count("burst") == 1
    assert kinds.count("burst_more") == 1
    newest_fold = [r for r in log.recent() if r["kind"] == "burst_more"][0]
    assert newest_fold["alert_id"] == alert["id"]
    assert newest_fold["peer"] == "fe80::c7"
    assert (newest_fold["count"], newest_fold["peers"]) == (1000, 200)
    # The daemon's own memory of the open alert agrees with the journal.
    assert d._access_fold["alert_id"] == alert["id"]
    assert d._access_fold["count"] == 1000
    assert len(d._access_fold["peers"]) == 200
    # The loopback / sealed `access_log` read shares the merge: uncapped and
    # the same figures.
    api = ApiServer(d)
    _status, _ctype, body = api._access_log_report_for("limit=5")
    report = json.loads(body)
    assert [a["count"] for a in report["open_alerts"]] == [1000]
    assert report["open_alerts"][0]["peers"] == 200


@pytest.mark.asyncio
async def test_a_fold_after_an_ack_stays_quiet_and_on_the_log(alerting_daemon):
    """An acknowledgement does not reset the floor: the next burst folds
    into the acknowledged alert — on the log, off the inbox."""
    d, log, diary, banners = alerting_daemon
    for _ in range(access_log.BURST_THRESHOLD):
        await d.note_access_refusal("lan", "10.0.0.9", "plaintext")
    await _drain_executor()
    alert_id = d.security_snapshot()["alerts"][0]["id"]
    assert await d.ack_access_alert(alert_id) == (True, "")
    for _ in range(access_log.BURST_THRESHOLD):
        await d.note_access_refusal("lan", "10.0.0.10", "plaintext")
    await _drain_executor()
    assert len(banners.posted) == 1
    assert d.security_snapshot()["alerts"] == []
    assert d.security_snapshot()["hidden"] == 0
    newest = log.recent()[0]
    assert newest["kind"] == "burst_more"
    assert newest["alert_id"] == alert_id
    assert newest["peer"] == "10.0.0.10"
    assert (newest["count"], newest["peers"]) == (10, 2)
    assert [e["kind"] for e in diary.recent()] == ["access_burst"]


@pytest.mark.asyncio
async def test_the_snapshot_publishes_the_newest_eight_and_counts_the_rest(alerting_daemon):
    d, log, _diary, _banners = alerting_daemon
    import time as _time
    now = _time.time()
    for i in range(12):
        log.append("burst", id=f"b{i:02d}", door="lan", peer=str(i), count=5,
                   window_seconds=600, ts=now - 100 + i)
    snap = d.security_snapshot()
    assert len(snap["alerts"]) == access_log.MAX_PUBLISHED_ALERTS == 8
    assert [a["id"] for a in snap["alerts"]] == [f"b{i:02d}" for i in range(4, 12)]
    assert snap["hidden"] == 4
    # A hidden alert still acknowledges: the ack checks the uncapped list.
    assert await d.ack_access_alert("b00") == (True, "")
    snap = d.security_snapshot()
    assert snap["hidden"] == 3
    assert [a["id"] for a in snap["alerts"]] == [f"b{i:02d}" for i in range(4, 12)]
    # The loopback / sealed read stays uncapped.
    api = ApiServer(d)
    _status, _ctype, body = api._access_log_report_for("")
    assert len(json.loads(body)["open_alerts"]) == 11


@pytest.mark.asyncio
async def test_a_fold_never_reaches_the_banner_path_or_the_diary(alerting_daemon, monkeypatch):
    d, log, diary, banners = alerting_daemon
    wakes = []

    async def _count_wake(*_a, **_k):
        wakes.append(1)

    monkeypatch.setattr(d, "_wake_surfaces", _count_wake)
    for _ in range(access_log.BURST_THRESHOLD):
        await d.note_access_refusal("relay", "dev1", "seal")
    await _drain_executor()
    assert len(wakes) == 1
    assert len(banners.posted) == 1
    for _ in range(access_log.BURST_THRESHOLD):
        await d.note_access_refusal("relay", "dev2", "seal")
    await _drain_executor()
    assert len(wakes) == 1
    assert d._undelivered == []
    assert len(d._alerts) == 1
    assert len(banners.posted) == 1
    assert [e["kind"] for e in diary.recent()] == ["access_burst"]
    assert [r["kind"] for r in log.recent()][0] == "burst_more"
    # The fold's door is the folding knock's; the alert keeps its own.
    assert log.recent()[0]["door"] == "relay"
    assert d.security_snapshot()["alerts"][0]["count"] == 10


@pytest.mark.asyncio
async def test_concurrent_knocks_fold_into_the_one_alert(alerting_daemon):
    """`ApiServer._record_access` fires one task per knock, so a flood is
    concurrent. Every task is created before any is awaited: the first
    burst's raise sets `_access_fold` only after its executor hop, and the
    sibling bursts resuming inside that hop must wait for it under
    `_access_alert_lock` rather than find no open alert and drop their
    fold. One banner, one `burst` line, every other burst folded."""
    d, log, diary, banners = alerting_daemon
    assert isinstance(d._access_alert_lock, asyncio.Lock)
    loop = asyncio.get_running_loop()
    tasks = [
        loop.create_task(d.note_access_refusal("lan", f"fe80::{i:x}", "plaintext"))
        for i in range(20)
        for _ in range(access_log.BURST_THRESHOLD)
    ]
    await asyncio.gather(*tasks)
    await _drain_executor()
    assert len(banners.posted) == 1
    assert len(d._alerts) == 1
    assert [e["kind"] for e in diary.recent()] == ["access_burst"]
    kinds = [r["kind"] for r in log.recent()]
    assert kinds.count("refusal") == 100
    assert kinds.count("burst") == 1
    assert kinds.count("burst_more") == 1
    snap = d.security_snapshot()
    assert snap["hidden"] == 0
    assert len(snap["alerts"]) == 1
    alert = snap["alerts"][0]
    assert alert["count"] == 100
    assert alert["peers"] == 20
    assert "19 other sources" in alert["text"]
    assert d._access_fold["count"] == 100
    assert len(d._access_fold["peers"]) == 20
    assert not d._access_alert_lock.locked()


@pytest.mark.asyncio
async def test_a_fold_with_no_open_alert_is_dropped_never_raised(alerting_daemon):
    """The detector said the floor is up but the daemon holds no open alert
    (a raise skipped because the log was down at that moment): the burst
    is dropped, never promoted to a raise."""
    d, log, _diary, banners = alerting_daemon
    assert d._access_fold is None
    await d._fold_access_alert("lan", {"fold": True, "peer": "p", "count": 5,
                                       "window_seconds": 600})
    assert banners.posted == []
    assert d._alerts == []
    assert log.recent() == []


@pytest.mark.asyncio
async def test_the_folded_peer_set_is_bounded(alerting_daemon):
    d, log, _diary, _banners = alerting_daemon
    await d._raise_access_alert("lan", {"peer": "A", "count": 5,
                                        "window_seconds": 600})
    d._access_fold["peers"] = {f"p{i}" for i in range(access_log.MAX_FOLDED_PEERS)}
    await d._fold_access_alert("lan", {"fold": True, "peer": "extra", "count": 5,
                                       "window_seconds": 600})
    assert len(d._access_fold["peers"]) == access_log.MAX_FOLDED_PEERS
    assert "extra" not in d._access_fold["peers"]
    assert log.recent()[0]["peers"] == access_log.MAX_FOLDED_PEERS
    assert log.recent()[0]["count"] == 10


def test_the_push_title_for_a_security_row_names_no_agent():
    title = BobDaemon._compose_push_title([{"kind": "security", "nickname": ""}])
    assert title == "Somebody is knocking at Dark Army's door"
    assert BobDaemon._compose_push_kind([{"kind": "security"},
                                         {"kind": "permission"}]) == "security"


def test_the_kind_rides_every_table():
    assert alerts.KINDS[0] == "security"
    assert alerts.KIND_SECURITY == "security"
    assert alerts.KINDS == relay_client.PUSH_KINDS


# --- the verb, the read and the section ------------------------------------------


def test_the_ack_verb_is_on_both_phone_tuples_and_the_read_is_not():
    assert "access_alert_ack" in ApiServer.LAN_ACTIONS
    assert "access_alert_ack" in ApiServer.REMOTE_ACTIONS
    assert set(ApiServer.REMOTE_ACTIONS) <= set(ApiServer.LAN_ACTIONS)
    assert "access_log" not in ApiServer.LAN_ACTIONS
    assert "access_log" not in ApiServer.REMOTE_ACTIONS


def test_the_section_is_omittable_and_the_marker_is_published():
    assert "security" in api_mod._OMITTABLE_SECTIONS
    flags = BobDaemon()._pipeline_writable()
    assert flags["access_log_supported"] is True
    # The link-timing marker sits beside it: the phone asks for `timing=1`
    # only where the Mac states it, and decodes an absent key false.
    assert flags["link_timing_supported"] is True


@pytest.mark.asyncio
async def test_the_timing_rollups_ride_the_read_only_behind_the_opt_in(
        server, token_path, tmp_path):
    srv, daemon, recorder, (loop_port, _lan) = server
    log = access_log.AccessLog(path=tmp_path / "access.jsonl")
    log.open()
    daemon._access_log = log
    hops = {k: 0.0 for k in access_log.HOP_KEYS}
    hops["n"] = 2
    assert log.append("timing", door="relay", peer="dev1", device_id="dev1",
                      count=2, window_seconds=600, hops=hops)
    assert log.append("refusal", door="lan", peer="10.0.0.9",
                      reason="plaintext")
    token = srv.token   # desk-only: every paired phone's name
    headers = {"X-Bob-Token": token}
    status, out = await fetch("/api/access-log", port=loop_port, headers=headers)
    assert status == 200
    assert [e["kind"] for e in json.loads(out)["entries"]] == ["refusal"]
    status, out = await fetch("/api/access-log?timing=0", port=loop_port,
                              headers=headers)
    assert status == 200
    assert [e["kind"] for e in json.loads(out)["entries"]] == ["refusal"]
    status, out = await fetch("/api/access-log?timing=1&limit=5",
                              port=loop_port, headers=headers)
    assert status == 200
    entries = json.loads(out)["entries"]
    assert [e["kind"] for e in entries] == ["refusal", "timing"]
    assert set(entries[1]["hops"]) == set(access_log.HOP_KEYS)
    assert entries[0]["hops"] == {}
    for bad in ("timing=2", "timing=yes", "timing="):
        status, _ = await fetch(f"/api/access-log?{bad}", port=loop_port,
                                headers=headers)
        assert status == 400, bad
    # Days of rollups never crowd the refusal out of the phone's read: the
    # limit bounds the refusal lines, the rollups ride beside it capped.
    for i in range(access_log.MAX_TIMING_ENTRIES):
        log.append("timing", door="relay", peer="dev1", device_id="dev1",
                   count=1, window_seconds=600, hops=hops)
    status, out = await fetch("/api/access-log?timing=1&limit=500",
                              port=loop_port, headers=headers)
    assert status == 200
    kinds = [e["kind"] for e in json.loads(out)["entries"]]
    assert kinds.count("refusal") == 1
    assert kinds.count("timing") == access_log.TIMING_READ_LIMIT


@pytest.mark.asyncio
async def test_the_loopback_read_is_token_gated(server, token_path):
    srv, daemon, recorder, (loop_port, _lan) = server
    status, _ = await fetch("/api/access-log", port=loop_port)
    assert status == 403
    token = srv.token   # desk-only: every paired phone's name
    status, out = await fetch("/api/access-log?limit=5", port=loop_port,
                              headers={"X-Bob-Token": token})
    assert status == 200
    body = json.loads(out)
    assert set(body) == {"available", "generated_at", "entries", "open_alerts"}
    status, out = await fetch("/api/access-log?limit=0", port=loop_port,
                              headers={"X-Bob-Token": token})
    assert status == 400
    status, out = await fetch("/api/access-log?since=-1", port=loop_port,
                              headers={"X-Bob-Token": token})
    assert status == 400


@pytest.mark.asyncio
async def test_the_sealed_read_and_the_sealed_ack(server, tmp_path):
    srv, daemon, recorder, (_loop, lan_port) = server
    del daemon.note_access_refusal
    log = access_log.AccessLog(path=tmp_path / "access.jsonl")
    log.open()
    daemon._access_log = log
    monkey_wake = daemon._wake_surfaces
    daemon._wake_surfaces = _noop_async
    try:
        log.append("burst", id="b1", door="lan", peer="10.0.0.9", count=5,
                   window_seconds=600)
        paired = await _pair_plain(lan_port)
        key = paired["key"]
        chan = relay.channel_id(key, ns=relay.HOME)

        async def home(kind, body, ctr):
            wire = relay.seal_frame(key, relay.DIR_PHONE_TO_MAC, ctr, kind, body,
                                    ns=relay.HOME)
            status, out = await fetch("/api/home", port=lan_port,
                                      data=wire.encode(),
                                      headers={"X-Bob-Channel": chan,
                                               "Content-Type": "text/plain"})
            assert status == 200, out
            frame, err = relay.open_frame(key, relay.DIR_MAC_TO_PHONE,
                                          out.decode(), 0, ns=relay.HOME)
            assert err == "", err
            return int(frame["body"]["status"]), json.loads(frame["body"]["body"])

        status, body = await home("access_log", {"query": "limit=10"}, 1)
        assert status == 200
        assert body["available"] is True
        assert [a["id"] for a in body["open_alerts"]] == ["b1"]
        status, body = await home("state", {}, 2)
        assert status == 200
        assert body["security"]["alerts"][0]["id"] == "b1"
        status, body = await home("action", {"action": "access_alert_ack",
                                             "id": "b1"}, 3)
        assert status == 200, body
        assert body["ok"] is True
        assert daemon.security_snapshot()["alerts"] == []
        status, body = await home("action", {"action": "access_alert_ack",
                                             "id": "b1"}, 4)
        assert status == 409
        assert body["detail"] == access_log.ACCESS_ALERT_MISSING_REFUSAL
        status, body = await home("action", {"action": "access_alert_ack",
                                             "id": 5}, 5)
        assert status == 400
    finally:
        daemon._wake_surfaces = monkey_wake


@pytest.mark.asyncio
async def test_the_loopback_ack_is_awaited_and_gated(server, token_path, tmp_path):
    srv, daemon, recorder, (loop_port, _lan) = server
    log = access_log.AccessLog(path=tmp_path / "access.jsonl")
    log.open()
    daemon._access_log = log
    daemon._wake_surfaces = _noop_async
    log.append("burst", id="b2", door="relay", peer="dev1", count=5,
               window_seconds=600)
    body = json.dumps({"action": "access_alert_ack", "id": "b2"}).encode()
    status, _ = await fetch("/api/action", port=loop_port, data=body)
    assert status in (403, 404)
    assert daemon.security_snapshot()["alerts"]
    token = srv.token   # desk-only: every paired phone's name
    status, out = await fetch("/api/action", port=loop_port, data=body,
                              headers={"X-Bob-Token": token,
                                       "Content-Type": "application/json"})
    assert status == 200, out
    assert daemon.security_snapshot()["alerts"] == []
    status, out = await fetch("/api/action", port=loop_port, data=body,
                              headers={"X-Bob-Token": token,
                                       "Content-Type": "application/json"})
    assert status == 409
    assert json.loads(out)["detail"] == access_log.ACCESS_ALERT_MISSING_REFUSAL


@pytest.mark.asyncio
async def test_the_section_rides_state_and_a_slim_stream_may_omit_it(server):
    """`state()` carries `security` beside `inbox`; the omission from a slim
    frame is pinned once for every member of `_OMITTABLE_SECTIONS` by
    `test_api_server.test_a_slim_stream_omits_every_unchanged_section`."""
    srv, daemon, recorder, _ = server
    state = srv.state()
    assert state["security"] == {"available": False, "alerts": []}
    assert api_mod._OMITTABLE_SECTIONS.index("security") \
        > api_mod._OMITTABLE_SECTIONS.index("inbox")
