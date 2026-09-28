# host/tests/test_lan_door.py
"""The three bounds on the phone door, end to end on a real listener.

1. **The typed pair branch is gated.** A typed-address pair (`POST
   /api/pair` with no `X-Bob-Channel` — SRP-6a over the code, `tests/
   pake_pair.py`) runs only for a pairing the Pair window armed with
   `allow_typed` (`devices.begin(allow_plain=True)`); otherwise it is 403
   in `PAIR_PLAIN_REFUSAL`'s words before the body is parsed, and the
   access log holds `pair_plain` on the pairing door.
2. **A tunnel arrival is refused unread.** `_handle_lan_client` judges the
   local address the knock landed on (`sockname`) with
   `lan_hosts.arrived_on_tunnel`; a tunnel address is **closed with no
   response** and logged `tunnel` — no status at all, since every phone
   build stops its walk on any answer and a 403 un-pairs it, while a dropped
   connection is "walk on" to all of them. Every test knock lands on
   `127.0.0.1`, which the real helper admits, so the seam is the module
   function monkeypatched.
3. **Two connection caps.** Over `LAN_MAX_OPEN` in all or
   `LAN_MAX_OPEN_PER_PEER` from one address, the next accept is answered
   503 `LAN_BUSY_REFUSAL` with **nothing read**, logged as `busy` at
   weight 0, and the slot is released in the handler's `finally`.

Fixtures are `test_lan_upload.py`'s, copied not imported: free ports from
held-open sockets, a temp device ledger, the machine's addresses stubbed.
The access log is spied through a `_Recorder` standing in for
`BobDaemon.note_access_refusal`, `test_access_log_doors.py`'s shape.
"""

from __future__ import annotations

import asyncio
import http.client
import json
import socket
import urllib.error
import urllib.request

import pytest
import pytest_asyncio

from dark_army_daemon import access_log
from dark_army_daemon import api_server as api_mod
from dark_army_daemon import devices, lan_hosts, paths, relay
from dark_army_daemon.api_server import (
    LAN_BUSY_REFUSAL,
    LAN_MAX_OPEN,
    LAN_MAX_OPEN_PER_PEER,
    LAN_TUNNEL_REFUSAL,
    PAIR_PLAIN_REFUSAL,
    ApiServer,
)
from dark_army_daemon.daemon import BobDaemon
from tests.pake_pair import pair_typed, typed_exchange
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
def ports(tmp_path, monkeypatch):
    loop_port, lan_port = _free_ports(2)
    monkeypatch.setattr(api_mod, "API_TOKEN_PATH", tmp_path / "api-token")
    monkeypatch.setattr(api_mod, "ensure_state_dir", lambda: tmp_path)
    monkeypatch.setattr(api_mod, "LAN_API_PORT", lan_port)
    return loop_port, lan_port


class _Recorder:
    """Stands in for `BobDaemon.note_access_refusal`; keeps every call."""

    def __init__(self):
        self.calls: list[tuple] = []

    async def __call__(self, door, peer, reason, device_id=""):
        self.calls.append((door, peer, reason, device_id))


@pytest_asyncio.fixture
async def server(ports):
    """A started loopback + LAN listener, the access log spied."""
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
    """Let the fire-and-forget task the recorder created run."""
    for _ in range(3):
        await asyncio.sleep(0)


def _start_body() -> bytes:
    """A well-formed `pake: start` — the first request of a typed pair."""
    return json.dumps({"pake": "start", "A": "01" + "00" * 255}).encode()


async def _begin_pairing(srv, loop_port, payload: dict) -> dict:
    status, out = await _fetch(
        "/api/action", port=loop_port, data=json.dumps(payload).encode(),
        headers={"X-Bob-Token": srv.token})
    assert status == 200, out
    return json.loads(out)


# --- 1. the plain pair branch is gated --------------------------------------


@pytest.mark.asyncio
async def test_an_unarmed_pairing_refuses_a_typed_pair_in_words(server):
    srv, _daemon, recorder, _loop, lan_port = server
    devices.begin()
    status, out = await _fetch("/api/pair", port=lan_port, data=_start_body())
    assert status == 403
    assert json.loads(out)["error"] == PAIR_PLAIN_REFUSAL
    # No code was judged: the pairing is still open and un-failed.
    assert devices.pairing_open() is True
    await _settle()
    assert recorder.calls == [("pairing", "127.0.0.1", "pair_plain", "")]


@pytest.mark.asyncio
async def test_the_refusal_never_counts_towards_the_fail_limit(server):
    srv, _daemon, _recorder, _loop, lan_port = server
    devices.begin()
    for _ in range(devices.PAIRING_FAIL_LIMIT + 1):
        status, _ = await _fetch("/api/pair", port=lan_port, data=_start_body())
        assert status == 403
    assert devices.pairing_open() is True
    assert devices._pairing["fails"] == 0
    # And the right code, once the window is armed, still pairs.
    devices.reset_pairing()
    paired = await pair_typed(_fetch, lan_port)
    assert paired["token"]


@pytest.mark.asyncio
async def test_an_armed_pairing_answers_a_start(server):
    srv, _daemon, recorder, _loop, lan_port = server
    code = devices.begin(allow_plain=True)
    record = await typed_exchange(_fetch, lan_port, code)
    status, out = record["start"]
    assert status == 200, out
    payload = json.loads(out)
    assert payload["pake"] == "start"
    assert payload["pairing"] and payload["salt"] and payload["B"]
    assert "home_key" not in payload and "token" not in payload
    status, out = record["finish"]
    assert status == 200, out
    assert "home_key" not in out.decode("ascii", "replace")
    await _settle()
    assert recorder.calls == []


def test_the_flag_dies_with_the_pairing():
    devices.begin(allow_plain=True)
    assert devices.pairing_allows_plain() is True
    devices.reset_pairing()
    assert devices.pairing_allows_plain() is False
    # A later, unarmed begin() replaces an armed one.
    devices.begin(allow_plain=True)
    devices.begin()
    assert devices.pairing_allows_plain() is False
    # With no pairing at all, plainly False.
    devices.reset_pairing()
    assert devices.pairing_allows_plain() is False


def test_the_flag_is_never_on_the_snapshot():
    devices.begin(allow_plain=True)
    text = json.dumps(devices.snapshot(), sort_keys=True)
    assert "plain_ok" not in text
    assert "allow" not in text
    assert devices.snapshot()["pairing_open"] is True


@pytest.mark.asyncio
async def test_a_sealed_pair_on_an_armed_pairing_still_works(server):
    """`allow_plain` widens the plain branch only; the sealed route is
    exactly what it was, so a blanket `allow_plain=True` in an older test
    changes nothing it asserts."""
    srv, _daemon, _recorder, _loop, lan_port = server
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
    assert "home_key" not in inner
    assert inner["token"]


@pytest.mark.asyncio
async def test_loopback_begin_pairing_echoes_a_bare_true_only(server):
    srv, _daemon, _recorder, loop_port, _lan = server
    armed = await _begin_pairing(srv, loop_port,
                                 {"action": "begin_pairing", "allow_typed": True})
    assert armed["allow_typed"] is True
    assert devices.pairing_allows_plain() is True
    stringy = await _begin_pairing(
        srv, loop_port, {"action": "begin_pairing", "allow_typed": "yes"})
    assert stringy["allow_typed"] is False
    assert devices.pairing_allows_plain() is False
    absent = await _begin_pairing(srv, loop_port, {"action": "begin_pairing"})
    assert absent["allow_typed"] is False
    assert devices.pairing_allows_plain() is False
    # And the code the reply carries is the one the door now takes.
    armed = await _begin_pairing(srv, loop_port,
                                 {"action": "begin_pairing", "allow_typed": True})
    assert "allow_typed" not in json.dumps(srv.state()["devices"])


def test_begin_pairing_is_loopback_only():
    assert "begin_pairing" not in ApiServer.LAN_ACTIONS
    assert "begin_pairing" not in ApiServer.REMOTE_ACTIONS
    assert set(ApiServer.REMOTE_ACTIONS) <= set(ApiServer.LAN_ACTIONS)


# --- 2. a tunnel arrival is refused unread ------------------------------------


@pytest.mark.parametrize("local, expected", [
    ("10.8.0.3", True),
    ("192.168.1.5", False),
    ("127.0.0.1", False),
    ("169.254.7.7", False),
    ("", False),
    ("  10.8.0.3 ", True),
    ("10.9.9.9", False),   # under no interface at all
])
def test_arrived_on_tunnel_table(local, expected):
    addrs = {"en0": ["192.168.1.5"], "utun3": ["10.8.0.3"],
             "lo0": ["127.0.0.1"], "en1": ["169.254.7.7"]}
    assert lan_hosts.arrived_on_tunnel(local, addrs) is expected


def test_arrived_on_tunnel_fails_open_and_prefers_the_lan():
    # An unreadable interface list admits everything.
    assert lan_hosts.arrived_on_tunnel("10.8.0.3", {}) is False
    assert lan_hosts.arrived_on_tunnel("10.8.0.3", None) is False
    # An address both an `en` and a `utun` interface hold is the LAN's.
    both = {"en0": ["10.8.0.3"], "utun3": ["10.8.0.3"]}
    assert lan_hosts.arrived_on_tunnel("10.8.0.3", both) is False


@pytest.mark.asyncio
async def test_a_tunnel_arrival_is_refused_before_the_request_is_read(server, monkeypatch):
    srv, _daemon, recorder, _loop, lan_port = server
    seen: list[str] = []

    def judge(local, addrs):
        seen.append(local)
        return True

    monkeypatch.setattr(lan_hosts, "arrived_on_tunnel", judge)
    # A typed pair's start that *would* be answered, so the refusal is
    # plainly the arrival's and not the route's.
    devices.begin(allow_plain=True)
    # No response at all: the shipped phone stops its walk on any HTTP
    # answer and promotes the host, and a 403 would un-pair it.
    with pytest.raises((http.client.RemoteDisconnected, ConnectionResetError,
                        urllib.error.URLError)):
        await _fetch("/api/pair", port=lan_port, data=_start_body())
    # And raw: the connection is closed with not one byte written, whether
    # or not the knock sent anything.
    reader, writer = await asyncio.open_connection("127.0.0.1", lan_port)
    try:
        writer.write(b"POST /api/pair HTTP/1.1\r\nHost: x\r\nContent-Length: 0\r\n\r\n")
        await writer.drain()
        # EOF, or a reset (the request bytes were never read, so the close
        # is an RST): either way not one byte came back.
        try:
            got = await asyncio.wait_for(reader.read(), timeout=5)
        except ConnectionResetError:
            got = b""
        assert got == b""
    finally:
        writer.close()
    assert seen == ["127.0.0.1", "127.0.0.1"]
    assert LAN_TUNNEL_REFUSAL  # the words the log and the docs use
    assert devices.pairing_open() is True
    await _settle()
    assert recorder.calls == [("lan", "127.0.0.1", "tunnel", "")] * 2
    # The slot came back on both.
    assert srv._lan_open_total == 0


@pytest.mark.asyncio
async def test_a_loopback_arrival_is_admitted(server):
    srv, _daemon, recorder, _loop, lan_port = server
    status, _ = await _fetch("/api/nowhere", port=lan_port)
    assert status == 404
    await _settle()
    assert recorder.calls == [("lan", "127.0.0.1", "not_found", "")]
    devices.begin(allow_plain=True)
    status, out = await _fetch("/api/pair", port=lan_port, data=_start_body())
    assert status == 200, out


@pytest.mark.asyncio
async def test_the_week_is_sealed_only_and_the_history_page_stays_unknown(server):
    """`/api/history-week` in plaintext is the 426 every sealed read's plain
    address gives, recorded `plaintext`; `/api/history` was never a LAN route
    and stays a 404 recorded `not_found`."""
    _srv, _daemon, recorder, _loop, lan_port = server
    status, _ = await _fetch("/api/history", port=lan_port)
    assert status == 404
    status, out = await _fetch("/api/history-week", port=lan_port)
    assert status == 426
    assert json.loads(out)["error"] == api_mod.HOME_UPDATE_REFUSAL
    await _settle()
    assert recorder.calls == [("lan", "127.0.0.1", "not_found", ""),
                              ("lan", "127.0.0.1", "plaintext", "")]


@pytest.mark.asyncio
async def test_the_interface_reading_is_memoised_across_knocks(server, monkeypatch):
    srv, _daemon, _recorder, _loop, lan_port = server
    calls = []

    def live():
        calls.append(1)
        return {"en0": ["192.168.1.5"]}

    monkeypatch.setattr(lan_hosts, "live_addrs", live)
    srv._tunnel_addrs_cache = None
    await _fetch("/api/nowhere", port=lan_port)
    await _fetch("/api/nowhere", port=lan_port)
    assert len(calls) == 1
    # Past the TTL the list is read again.
    stamp, addrs = srv._tunnel_addrs_cache
    srv._tunnel_addrs_cache = (stamp - api_mod.TUNNEL_ADDRS_TTL - 1, addrs)
    await _fetch("/api/nowhere", port=lan_port)
    assert len(calls) == 2


# --- 3. the two connection caps ---------------------------------------------


def test_the_caps_are_what_the_contract_says():
    assert LAN_MAX_OPEN_PER_PEER == 6
    assert LAN_MAX_OPEN == 32
    assert api_mod._STATUS_TEXT[503] == "Service Unavailable"


def test_tunnels_are_never_offered_as_candidates():
    """The door refuses a tunnel arrival, so the pairing reply and the QR
    must not send the phone there: a phone walking its list onto one would
    be turned away (the door closes silently on one)."""
    hosts = lan_hosts.candidates(
        {"en0": ["192.168.1.5"], "utun4": ["10.8.0.3"]}, "10.8.0.3", "mac")
    assert hosts == ["192.168.1.5", "mac.local"]
    assert lan_hosts.candidates({"utun4": ["10.8.0.3"]}, "", "") == []
    assert lan_hosts.refusal({"utun4": ["10.8.0.3"]})
    assert access_log.WEIGHTS["busy"] == 0


async def _hold(lan_port: int, count: int) -> list:
    """`count` raw sockets to the door that send nothing, each accepted
    and counted before the next opens."""
    held = []
    for _ in range(count):
        reader, writer = await asyncio.open_connection("127.0.0.1", lan_port)
        held.append((reader, writer))
        # Let the accept run and the handler count it.
        for _ in range(5):
            await asyncio.sleep(0)
    return held


async def _release(held) -> None:
    for _reader, writer in held:
        writer.close()
    for _reader, writer in held:
        try:
            await writer.wait_closed()
        except (ConnectionResetError, BrokenPipeError, OSError):
            pass
    # The handlers' `finally` runs on the EOF; give them a few turns.
    for _ in range(10):
        await asyncio.sleep(0)


async def _read_head_and_body(reader) -> tuple[int, dict]:
    head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
    lines = head.decode("latin-1").split("\r\n")
    status = int(lines[0].split(" ")[1])
    length = 0
    for line in lines[1:]:
        if line.lower().startswith("content-length:"):
            length = int(line.split(":", 1)[1].strip())
    body = await asyncio.wait_for(reader.readexactly(length), timeout=5)
    return status, json.loads(body)


@pytest.mark.asyncio
async def test_the_per_peer_cap_answers_busy_without_reading_and_releases(server):
    srv, _daemon, recorder, _loop, lan_port = server
    held = await _hold(lan_port, LAN_MAX_OPEN_PER_PEER)
    assert srv._lan_open == {"127.0.0.1": LAN_MAX_OPEN_PER_PEER}
    assert srv._lan_open_total == LAN_MAX_OPEN_PER_PEER
    try:
        # The seventh sends not one byte and is answered anyway.
        reader, writer = await asyncio.open_connection("127.0.0.1", lan_port)
        try:
            status, body = await _read_head_and_body(reader)
        finally:
            writer.close()
        assert status == 503
        assert body == {"error": LAN_BUSY_REFUSAL}
        await _settle()
        assert recorder.calls == [("lan", "127.0.0.1", "busy", "")]
        # The refused knock held no slot.
        assert srv._lan_open_total == LAN_MAX_OPEN_PER_PEER
    finally:
        await _release(held)
    assert srv._lan_open == {}
    assert srv._lan_open_total == 0
    # The door answers normally again: the counter was released in `finally`.
    devices.begin(allow_plain=True)
    status, out = await _fetch("/api/pair", port=lan_port, data=_start_body())
    assert status == 200, out
    assert srv._lan_open_total == 0


def test_busy_never_raises_a_burst():
    detector = access_log.BurstDetector()
    for _ in range(access_log.BURST_THRESHOLD * 2):
        assert detector.note("10.0.0.9", "busy", 1000.0) is None
    assert detector.tracked() == 0
    # The strangers' reasons still count.
    for reason in ("pair_plain", "tunnel", "pake"):
        d = access_log.BurstDetector()
        alert = None
        for _ in range(access_log.BURST_THRESHOLD):
            alert = d.note("10.0.0.9", reason, 1000.0)
        assert alert is not None, reason


@pytest.mark.asyncio
async def test_the_global_cap_is_read_through_the_module(server, monkeypatch):
    srv, _daemon, recorder, _loop, lan_port = server
    monkeypatch.setattr(api_mod, "LAN_MAX_OPEN", 2)
    monkeypatch.setattr(api_mod, "LAN_MAX_OPEN_PER_PEER", 99)
    held = await _hold(lan_port, 2)
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", lan_port)
        try:
            status, body = await _read_head_and_body(reader)
        finally:
            writer.close()
        assert status == 503
        assert body["error"] == LAN_BUSY_REFUSAL
        await _settle()
        assert recorder.calls == [("lan", "127.0.0.1", "busy", "")]
    finally:
        await _release(held)
    assert srv._lan_open_total == 0
    status, _ = await _fetch("/api/nowhere", port=lan_port)
    assert status == 404


@pytest.mark.asyncio
async def test_every_ordinary_knock_releases_its_slot(server):
    srv, _daemon, _recorder, _loop, lan_port = server
    for _ in range(LAN_MAX_OPEN_PER_PEER + 2):
        status, _ = await _fetch("/api/nowhere", port=lan_port)
        assert status == 404
    assert srv._lan_open == {}
    assert srv._lan_open_total == 0


# --- keepalive: a phone that walks away gives its slot back ------------------


def test_arm_keepalive_sets_the_options_on_a_real_tcp_socket():
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    client = socket.create_connection(listener.getsockname())
    accepted, _ = listener.accept()
    try:
        assert accepted.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE) == 0

        class _Writer:
            def get_extra_info(self, name, default=None):
                return accepted if name == "socket" else default

        ApiServer._arm_keepalive(_Writer())
        # macOS reports the flag bit, not 1: non-zero is on.
        assert accepted.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE) != 0
        assert accepted.getsockopt(
            socket.IPPROTO_TCP, socket.TCP_KEEPALIVE) == api_mod.LAN_KEEPALIVE_IDLE_SECONDS
        assert accepted.getsockopt(
            socket.IPPROTO_TCP, socket.TCP_KEEPINTVL) == api_mod.LAN_KEEPALIVE_INTERVAL_SECONDS
        assert accepted.getsockopt(
            socket.IPPROTO_TCP, socket.TCP_KEEPCNT) == api_mod.LAN_KEEPALIVE_COUNT
    finally:
        accepted.close()
        client.close()
        listener.close()


def test_arm_keepalive_never_raises_without_a_socket():
    class _Bare:
        def get_extra_info(self, name, default=None):
            return default

    ApiServer._arm_keepalive(_Bare())

    class _Refusing:
        def setsockopt(self, *_):
            raise OSError("no")

    class _Writer:
        def get_extra_info(self, name, default=None):
            return _Refusing() if name == "socket" else default

    ApiServer._arm_keepalive(_Writer())


@pytest.mark.asyncio
async def test_keepalive_is_armed_on_the_lan_door_only(server, monkeypatch):
    srv, _daemon, _recorder, loop_port, lan_port = server
    armed: list[str] = []

    def spy(writer):
        armed.append(str((writer.get_extra_info("peername") or ("",))[0]))

    monkeypatch.setattr(ApiServer, "_arm_keepalive", staticmethod(spy))
    status, _ = await _fetch("/api/state", port=loop_port)
    assert status == 200
    assert armed == []
    status, _ = await _fetch("/api/nowhere", port=lan_port)
    assert status == 404
    assert armed == ["127.0.0.1"]


def test_the_four_reasons_are_recorded_before_their_returns():
    """One recorder per new refusal, on the line before its return;
    `pake` has several refusal sites and is counted at least once."""
    import inspect

    src = inspect.getsource(ApiServer._handle_lan_client) + inspect.getsource(
        ApiServer._lan_pair)
    for reason in ("pair_plain", "tunnel", "busy"):
        assert src.count(f'"{reason}")') == 1, reason
    assert src.count('"pake")') >= 1
