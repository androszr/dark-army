# host/tests/test_terminal_socket.py
"""The panel's terminal as one socket (`terminal_stream.py`), and the four
seams it replaced: the per-poll grid rebuild (the emulator now parses on
read), the broker's per-chunk decode (bytes ride base64 both ways), the
ring replay on attach (`Screen.paint()` draws the screen instead), and
the desk's keys refused or awaited (raw input types straight through).

A real pty over `/bin/cat` stands in for the agent, as in
`test_terminal_stream.py`.

**And the phone door's copy of the same stream, sealed.** `SealedCodec`
wraps every frame in a `Z` frame under the device's home key with the
stream id, direction and position in the AAD; the route is a `POST` on
the phone door verified by `_home_open` before a byte is sent; a frame
that does not open closes the socket; one stream per device; a flood of
up frames closes it too.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import secrets
import signal

import pytest

from dark_army_daemon import daemon as dm
from dark_army_daemon import devices, ptyhost, relay, terminal_stream
from dark_army_daemon.vtgrid import LAG_SYNC_BYTES, Screen

from tests.test_lan_access import (  # noqa: F401  (fixtures)
    _no_fleet_snapshot, _pair_plain, ledger, machine, ports, server, token_path)
from tests.test_terminal_stream import _cat_session, _wait_for


@pytest.fixture(autouse=True)
def _reap_ptys():
    yield
    host = ptyhost.current()
    if host is not None:
        for term in host.terminals():
            try:
                os.killpg(term.pid, signal.SIGKILL)
            except OSError:
                pass


# --- the frame ----------------------------------------------------------------


def test_frames_round_trip_split_and_packed():
    a = terminal_stream.encode(terminal_stream.KIND_DATA, "héllo\x1b[H".encode())
    b = terminal_stream.encode(terminal_stream.KIND_EXIT)
    joined = a + b
    r = terminal_stream.FrameReader()
    assert r.feed(joined[:3]) == []
    assert r.feed(joined[3:7]) == []
    out = r.feed(joined[7:])
    assert [k for k, _ in out] == [b"D", b"X"]
    assert out[0][1] == "héllo\x1b[H".encode()
    assert out[1][1] == b""


def test_an_oversize_frame_is_refused_before_it_is_buffered():
    head = terminal_stream._HEAD.pack(b"D", terminal_stream.MAX_FRAME_BYTES + 1)
    with pytest.raises(ValueError):
        terminal_stream.FrameReader().feed(head)
    with pytest.raises(ValueError):
        terminal_stream.encode(b"DD", b"")


def test_size_frames_parse_or_are_ignored():
    assert terminal_stream.parse_size(b"120 40") == (120, 40)
    for bad in (b"", b"120", b"a b", b"0 40", b"120 40 1", "12é 4".encode()):
        assert terminal_stream.parse_size(bad) is None


# --- the emulator parses on read ---------------------------------------------


def test_deferred_bytes_are_parsed_by_the_next_read_in_order():
    s = Screen(20, 4)
    s.defer(b"ab")
    s.defer("─".encode()[:2])   # a box-drawing char, split
    assert s.lag == 4
    s.defer("─".encode()[2:] + b"c")
    assert s.text()[0] == "ab─c"
    assert s.lag == 0
    # Past the cap the lag is parsed at once, so memory is bounded.
    s.defer(b"x" * LAG_SYNC_BYTES)
    assert s.lag == 0


def test_every_public_read_syncs_first():
    s = Screen(10, 3)
    s.defer(b"\x1b[2;3Hq\x1b[?25l")
    assert s.cursor == (3, 1)
    s.defer(b"z")
    assert s.row_revision(1) > 0
    rows = {y: runs for y, runs in s.since(-1)[1]}
    assert rows[1][0][0].strip() == "qz"
    s.defer(b"\r\n\r\n\r\n")
    assert len(s.scrollback) == 2
    s.defer(b"w")
    assert s.snapshot()["cursor_visible"] is False
    s.defer(b"v")
    s.resize(12, 3)
    assert s.lag == 0 and s.cols == 12


def test_the_pty_host_defers_to_the_emulator_and_reads_it_whole(tmp_path):
    async def run():
        d = dm.BobDaemon(headless=True)
        handle = await _cat_session(d, tmp_path)
        term = d._pty.get(handle)
        d._pty.write(handle, "hello\r")
        assert await _wait_for(lambda: b"hello" in term.ring)
        # Nothing parsed on the loop; the read parses it.
        assert term.screen.lag > 0
        assert "hello" in "".join(d._pty.screen(handle).text())
        assert term.screen.lag == 0
        assert d.terminal_frame("s1")["available"] is True
        await d._pty.close(handle)
    asyncio.run(run())


# --- paint --------------------------------------------------------------------


def _text(rows):
    return ["".join(c[0] for c in r).rstrip() for r in rows]


def test_paint_redraws_the_screen_the_scrollback_the_cursor_and_the_modes():
    s = Screen(30, 4)
    s.feed(("l%d \x1b[32mgreen\x1b[0m \x1b[1;38;5;208mbold\x1b[0m"
            " \x1b[48;2;10;20;30mtc\x1b[0m\r\n" * 7).encode())
    s.feed(b"\x1b[?2004h\x1b[?1000h\x1b[?1h\x1b[2;5H\x1b]0;a title\x07")
    paint = s.paint()
    t = Screen(30, 4)
    t.feed(paint)
    assert t.text() == s.text()
    assert _text(t.scrollback) == _text(s.scrollback)
    assert t.cursor == s.cursor == (4, 1)
    assert t.lines[0][3:8] == s.lines[0][3:8]          # colours survive
    assert t.bracketed_paste is True
    assert {1, 1000, 2004} <= t._modes
    assert t.title == "a title"
    assert t.cursor_visible is True
    # The paint is what a viewer is fed, never the ring: it starts with a
    # reset of attributes and the cursor home, not mid-sequence.
    assert paint.startswith(b"\x1b[0m\x1b[?25l\x1b[H")


def test_paint_of_the_alternate_screen_keeps_the_primary_underneath():
    s = Screen(10, 2)
    s.feed(b"under\r\n\x1b[?1049h\x1b[Halt\x1b[?25l")
    t = Screen(10, 2)
    t.feed(s.paint())
    assert t.alt_screen is True
    assert t.text() == s.text() == ["alt", ""]
    assert t.cursor_visible is False
    t.feed(b"\x1b[?1049l")
    assert t.text()[0] == "under"


def test_snapshot_carries_modes_and_restore_takes_them_back():
    s = Screen(10, 2)
    s.feed(b"\x1b[?1004h")
    snap = s.snapshot()
    assert snap["modes"] == [1004]
    t = Screen(10, 2)
    t.restore(snap)
    assert t._modes == {1004}
    assert b"\x1b[?1004h" in t.paint()


# --- bytes stay bytes broker to daemon ----------------------------------------


def _adopted_host():
    host = ptyhost.PtyHost(persist=True)
    host._adopt({"handle": "pty-x", "pid": 0, "name": "n", "root": "/",
                 "cols": 20, "rows": 3})
    return host, host.get("pty-x")


def test_the_broker_sends_base64_and_the_daemon_counts_the_same_bytes():
    from dark_army_daemon import pty_broker
    host, term = _adopted_host()
    blob = ("─" * 30000).encode()
    sent = []

    class _Loop:
        def create_task(self, coro):
            sent.append(coro)
            coro.close()
    broker = pty_broker.Broker.__new__(pty_broker.Broker)
    broker._writer = None
    import asyncio as _a
    real = _a.get_running_loop
    _a.get_running_loop = lambda: _Loop()
    try:
        broker._on_data("pty-x", blob[:65536])
        broker._on_data("pty-x", blob[65536:])
    finally:
        _a.get_running_loop = real
    assert len(sent) == 2
    # What `_send` would have serialised, replayed into the daemon side.
    host._broker_ingest({"event": "data", "handle": "pty-x",
                         "b64": base64.b64encode(blob[:65536]).decode()})
    host._broker_ingest({"event": "data", "handle": "pty-x",
                         "b64": base64.b64encode(blob[65536:]).decode()})
    assert term.bytes_read == len(blob)
    assert bytes(term.ring) == blob[-host.ring_bytes:]
    assert "�" not in "".join(host.screen("pty-x").text())


@pytest.mark.asyncio
async def test_a_big_read_goes_out_as_ordered_frames():
    """A whole 64 KB read was ~87 KB of JSON on one line — over the default
    limit an un-upgraded daemon reads at. Sliced, in order, from one
    coroutine: one task per slice would race and the order is the screen."""
    from dark_army_daemon import pty_broker
    host, term = _adopted_host()
    blob = bytes(range(256)) * (2 * ptyhost.DATA_FRAME_BYTES // 256) + b"z" * 7
    assert len(blob) == 2 * ptyhost.DATA_FRAME_BYTES + 7
    lines = []
    broker = pty_broker.Broker.__new__(pty_broker.Broker)
    broker._writer = None

    async def _send(obj):
        lines.append(obj)
    broker._send = _send
    await broker._send_frames("pty-x", blob)
    assert len(lines) == 3
    assert [len(base64.b64decode(m["b64"])) for m in lines] == [
        ptyhost.DATA_FRAME_BYTES, ptyhost.DATA_FRAME_BYTES, 7]
    for msg in lines:
        host._broker_ingest(msg)
    assert term.bytes_read == len(blob)
    assert bytes(term.ring) == blob[-host.ring_bytes:]


def test_an_older_broker_still_running_is_understood():
    host, term = _adopted_host()
    host._broker_ingest({"event": "data", "handle": "pty-x", "data": "old text"})
    assert term.bytes_read == 8
    assert "old text" in host.screen("pty-x").text()[0]


def test_a_repeated_size_is_not_a_line_to_the_broker():
    host, term = _adopted_host()
    casts = []
    host._broker_cast = lambda op, **f: casts.append((op, f))
    assert host.resize("pty-x", 20, 3) is True
    assert casts == []
    assert host.resize("pty-x", 100, 30) is True
    assert casts == [("resize", {"handle": "pty-x", "cols": 100, "rows": 30})]
    assert host.resize("pty-x", 100, 30) is True
    assert len(casts) == 1


def test_slow_broker_ops_are_named():
    from dark_army_daemon import pty_broker
    assert pty_broker._SLOW_OPS == {"drain", "close", "close_all"}


# --- the desk types straight through -------------------------------------------


@pytest.mark.asyncio
async def test_raw_desk_input_is_typed_during_a_permission_prompt(tmp_path, monkeypatch):
    d = dm.BobDaemon(headless=True)
    handle = await _cat_session(d, tmp_path)
    term = d._pty.get(handle)
    monkeypatch.setattr(d, "_prompts_by_session", lambda: {"s1": {"request_id": "r"}})
    ok, detail = await d.terminal_input("s1", "", raw=True, data=b"1")
    assert (ok, detail) == (True, "")
    assert await _wait_for(lambda: b"1" in term.ring)
    # The line route — the older phone's and the reply box's — still refuses.
    assert await d.terminal_input("s1", "yes", from_phone=True) == (
        False, dm.TERMINAL_PROMPT_REFUSAL)
    assert await d.terminal_input("s1", "yes") == (False, dm.TERMINAL_PROMPT_REFUSAL)
    # Raw bytes from the phone take the desk's rules: the emulator is on
    # that screen too, and the dialog is answered from it.
    assert await d.terminal_input("s1", "", from_phone=True, raw=True, data=b"2") == (
        True, "")
    assert await _wait_for(lambda: b"2" in term.ring)
    await d._pty.close(handle)


@pytest.mark.asyncio
async def test_raw_input_takes_a_paste_and_never_waits_on_a_drain(tmp_path, monkeypatch):
    d = dm.BobDaemon(headless=True)
    handle = await _cat_session(d, tmp_path)
    called = []

    async def _drain(*a, **k):
        called.append(a)
        return True
    monkeypatch.setattr(d._pty, "drain", _drain)
    paste = b"x" * (dm.TERMINAL_MAX_INPUT_CHARS * 10)
    assert await d.terminal_input("s1", "", raw=True, data=paste) == (True, "")
    assert called == []
    too_big = b"x" * (dm.TERMINAL_MAX_RAW_BYTES + 1)
    assert await d.terminal_input("s1", "", raw=True, data=too_big) == (
        False, dm.TERMINAL_TOO_LONG_REFUSAL)
    assert dm.TERMINAL_MAX_RAW_BYTES > dm.TERMINAL_MAX_INPUT_CHARS
    await d._pty.close(handle)


@pytest.mark.asyncio
async def test_attach_paints_and_an_unhosted_session_is_refused_in_words(tmp_path):
    d = dm.BobDaemon(headless=True)
    assert d.terminal_attach("nobody") == (None, b"", False, dm.TERMINAL_NOT_OWNED_REFUSAL)
    handle = await _cat_session(d, tmp_path)
    term = d._pty.get(handle)
    d._pty.write(handle, "painted\r")
    assert await _wait_for(lambda: b"painted\r\n" in term.ring)
    got, paint, exited, refusal = d.terminal_attach("s1")
    assert (got, exited, refusal) == (handle, False, "")
    t = Screen(term.cols, term.rows)
    t.feed(paint)
    assert "painted" in "".join(t.text())
    await d._pty.close(handle)


# --- the socket, end to end ---------------------------------------------------


async def _open_stream(port: int, token: str, session: str):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write((f"GET /api/terminal/stream?session={session} HTTP/1.1\r\n"
                  f"Host: 127.0.0.1:{port}\r\n"
                  f"X-Bob-Token: {token}\r\n\r\n").encode())
    await writer.drain()
    head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
    status = int(head.split(b" ")[1])
    headers = {}
    for line in head.decode("latin-1").split("\r\n")[1:]:
        name, sep, value = line.partition(":")
        if sep:
            headers[name.strip().lower()] = value.strip()
    return reader, writer, status, headers


async def _next_frame(reader, frames, kind=None, timeout=5.0):
    deadline = asyncio.get_running_loop().time() + timeout
    pending: list = []
    while asyncio.get_running_loop().time() < deadline:
        for k, payload in pending:
            if kind is None or k == kind:
                return k, payload
        pending = []
        data = await asyncio.wait_for(reader.read(65536), timeout)
        if not data:
            break
        pending = frames.feed(data)
    raise AssertionError(f"no {kind!r} frame")


@pytest.mark.asyncio
async def test_the_stream_paints_types_in_order_resizes_and_reports_exit(server, tmp_path):
    srv, daemon, (loop_port, _lan) = server
    token = srv.token
    handle = await _cat_session(daemon, tmp_path)
    term = daemon._pty.get(handle)
    daemon._pty.write(handle, "before\r")
    assert await _wait_for(lambda: b"before\r\n" in term.ring)

    reader, writer, status, headers = await _open_stream(loop_port, token, "s1")
    assert status == 200
    assert headers["content-type"] == terminal_stream.CONTENT_TYPE
    frames = terminal_stream.FrameReader()
    kind, paint = await _next_frame(reader, frames, b"D")
    t = Screen(term.cols, term.rows)
    t.feed(paint)
    assert "before" in "".join(t.text())

    # Keys, one frame each, land in the order sent — including at once.
    for ch in "abcdefghij":
        writer.write(terminal_stream.encode(terminal_stream.KIND_INPUT, ch.encode()))
    writer.write(terminal_stream.encode(terminal_stream.KIND_INPUT, b"\r"))
    await writer.drain()
    assert await _wait_for(lambda: b"abcdefghij\r" in term.ring)
    echoed = bytearray()
    while b"abcdefghij" not in echoed:
        _k, payload = await _next_frame(reader, frames, b"D")
        echoed += payload

    # The size is the pane's to state.
    writer.write(terminal_stream.encode(terminal_stream.KIND_SIZE, b"90 25"))
    await writer.drain()
    assert await _wait_for(lambda: (term.cols, term.rows) == (90, 25))
    writer.write(terminal_stream.encode(terminal_stream.KIND_PING, b""))
    await writer.drain()

    # The process ending is an `X` frame, and the socket closes after it.
    os.kill(term.pid, signal.SIGKILL)
    kind, _ = await _next_frame(reader, frames, b"X")
    assert kind == b"X"
    assert await asyncio.wait_for(reader.read(1), 5) == b""
    writer.close()
    assert term.subscribers == []


@pytest.mark.asyncio
async def test_the_stream_is_token_gated_and_refuses_an_unhosted_session_in_words(server):
    srv, _daemon, (loop_port, _lan) = server
    reader, writer, status, headers = await _open_stream(loop_port, "", "nobody")
    assert status == 403
    writer.close()
    reader, writer, status, headers = await _open_stream(loop_port, srv.token, "nobody")
    assert status == 409
    body = await asyncio.wait_for(reader.readexactly(int(headers["content-length"])), 5)
    assert json.loads(body)["detail"] == dm.TERMINAL_NOT_OWNED_REFUSAL
    writer.close()
    reader, writer, status, _h = await _open_stream(loop_port, srv.token, "")
    assert status == 400
    writer.close()


@pytest.mark.asyncio
async def test_a_refused_key_comes_back_as_an_error_frame(server, tmp_path):
    srv, daemon, (loop_port, _lan) = server
    handle = await _cat_session(daemon, tmp_path)
    reader, writer, status, _h = await _open_stream(loop_port, srv.token, "s1")
    assert status == 200
    frames = terminal_stream.FrameReader()
    await _next_frame(reader, frames, b"D")
    writer.write(terminal_stream.encode(
        terminal_stream.KIND_INPUT, b"x" * (dm.TERMINAL_MAX_RAW_BYTES + 1)))
    await writer.drain()
    kind, payload = await _next_frame(reader, frames, b"E")
    assert payload.decode() == dm.TERMINAL_TOO_LONG_REFUSAL
    writer.close()
    await daemon._pty.close(handle)


@pytest.mark.asyncio
async def test_hanging_up_unsubscribes(server, tmp_path):
    srv, daemon, (loop_port, _lan) = server
    handle = await _cat_session(daemon, tmp_path)
    term = daemon._pty.get(handle)
    reader, writer, status, _h = await _open_stream(loop_port, srv.token, "s1")
    assert status == 200
    assert await _wait_for(lambda: len(term.subscribers) == 1)
    writer.close()
    assert await _wait_for(lambda: term.subscribers == [])
    await daemon._pty.close(handle)


def test_the_pane_is_a_stream_not_a_poll():
    """The Swift half: one connection per pane, fed straight into
    SwiftTerm, the wheel reported to the application, the editor's
    colours."""
    import pathlib
    src = (pathlib.Path(__file__).resolve().parents[2] / "panel" / "Sources"
           / "BobPanel" / "TerminalPane.swift").read_text()
    stream = (pathlib.Path(__file__).resolve().parents[2] / "panel" / "Sources"
              / "BobPanel" / "TerminalStream.swift").read_text()
    assert "terminalStream(session:" in src
    assert "terminalFrame(" not in src
    assert "terminalBytes(" not in src
    assert "@State private var chunk" not in src
    # The wheel is the application's: with reporting off, SwiftTerm's
    # alternate-scroll rung typed the gesture at the agent as arrow keys.
    assert "allowMouseReporting = true" in src
    assert "allowMouseReporting = false" not in src
    assert "0x1f1f1f" in src and "0xcccccc" in src
    assert "installColors(" in src
    assert "/api/terminal/stream?session=" in stream
    assert "X-Bob-Token" in stream
    assert 'contentType = "application/x-bob-terminal"' in stream
    assert terminal_stream.CONTENT_TYPE == "application/x-bob-terminal"


# --- the codec ----------------------------------------------------------------


class _PhoneCodec:
    """The phone's half of `SealedCodec`: seals up frames, opens down ones."""

    def __init__(self, key: bytes, stream_id: str) -> None:
        self.key, self.id = key, stream_id
        self.up = 0
        self.down = 0
        # Frames read off the socket and not yet opened: a read routinely
        # returns two, and a dropped one would put `down` a position behind.
        self.pending: list = []

    def seal(self, kind: bytes, payload: bytes = b"", *, n=None) -> bytes:
        n = self.up if n is None else n
        blob = relay.seal_blob(self.key, relay.DIR_PHONE_TO_MAC,
                               f"{self.id}|p2m|{n}", kind + payload, ns=relay.HOME)
        if n == self.up:
            self.up += 1
        return terminal_stream.encode(terminal_stream.KIND_SEALED, blob)

    def open(self, kind: bytes, payload: bytes):
        assert kind == terminal_stream.KIND_SEALED, kind
        plain = relay.open_blob(self.key, relay.DIR_MAC_TO_PHONE,
                                f"{self.id}|m2p|{self.down}", payload, ns=relay.HOME)
        assert plain is not None, "a down frame did not open"
        self.down += 1
        return plain[:1], plain[1:]


def test_the_sealed_codec_round_trips_and_refuses_tamper_replay_and_reorder():
    key = relay.mint_key()
    mac = terminal_stream.SealedCodec(key, "st-1")
    phone = _PhoneCodec(key, "st-1")
    # Down: the Mac seals, the phone opens, in order.
    kind, blob = mac.wrap(b"D", b"hello")
    assert kind == b"Z" and b"hello" not in blob
    assert phone.open(kind, blob) == (b"D", b"hello")
    kind, blob = mac.wrap(b"X", b"")
    assert phone.open(kind, blob) == (b"X", b"")
    # Up: the phone seals, the Mac opens.
    a = phone.seal(b"I", b"a")
    b = phone.seal(b"I", b"b")
    frames = terminal_stream.FrameReader().feed(a + b)
    assert mac.unwrap(*frames[0]) == (b"I", b"a")
    # The same wire twice: the counter moved, so the second is None.
    assert mac.unwrap(*frames[0]) is None
    # Swapped order: frame 2 sealed at position 2 cannot open at position 1
    # (a second Mac at the same position), and a flipped byte never opens.
    other = terminal_stream.SealedCodec(key, "st-1")
    other.unwrap(*frames[0])
    c = phone.seal(b"I", b"c")
    frames_c = terminal_stream.FrameReader().feed(c)
    assert other.unwrap(*frames_c[0]) is None
    assert other.unwrap(*frames[1]) == (b"I", b"b")
    tampered = bytearray(frames_c[0][1])
    tampered[-1] ^= 0x01
    assert other.unwrap(b"Z", bytes(tampered)) is None
    assert other.unwrap(*frames_c[0]) == (b"I", b"c")
    # A frame that is not sealed at all, or sealed to another stream.
    assert other.unwrap(b"I", b"plain") is None
    elsewhere = _PhoneCodec(key, "st-2").seal(b"I", b"z")
    assert other.unwrap(*terminal_stream.FrameReader().feed(elsewhere)[0]) is None


def test_the_plain_codec_is_identity():
    codec = terminal_stream.PlainCodec()
    assert codec.wrap(b"D", b"x") == (b"D", b"x")
    assert codec.unwrap(b"I", b"y") == (b"I", b"y")
    assert terminal_stream.SEALED_CONTENT_TYPE != terminal_stream.CONTENT_TYPE
    assert terminal_stream.STREAM_MAX_UP_FRAMES_PER_MINUTE >= 600
    assert terminal_stream.STREAM_HEARTBEAT_SECONDS == 5


# --- the sealed route on the phone door ----------------------------------------


async def _post_lan_stream(lan_port: int, key: bytes, ctr: int, session: str, *,
                           kind: str = "terminal_stream", chan: str = ""):
    """POST one sealed opening frame to the phone door's stream route and
    parse the head. Returns `(reader, writer, status, headers, frame_id)`."""
    frame_id = secrets.token_hex(8)
    wire = relay.seal_frame(key, relay.DIR_PHONE_TO_MAC, ctr, kind,
                            {"session": session}, frame_id=frame_id, ns=relay.HOME)
    body = wire.encode()
    reader, writer = await asyncio.open_connection("127.0.0.1", lan_port)
    writer.write((
        "POST /api/terminal/stream HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{lan_port}\r\n"
        f"X-Bob-Channel: {chan or relay.channel_id(key, ns=relay.HOME)}\r\n"
        "Content-Type: text/plain\r\n"
        f"Content-Length: {len(body)}\r\n\r\n").encode() + body)
    await writer.drain()
    head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
    status = int(head.split(b" ")[1])
    headers = {}
    for line in head.decode("latin-1").split("\r\n")[1:]:
        name, sep, value = line.partition(":")
        if sep:
            headers[name.strip().lower()] = value.strip()
    return reader, writer, status, headers, frame_id


async def _sealed_err(reader, headers, key: bytes) -> dict:
    """The body of a sealed `err` answer, opened."""
    body = await asyncio.wait_for(reader.readexactly(int(headers["content-length"])), 5)
    frame, err = relay.open_frame(key, relay.DIR_MAC_TO_PHONE, body.decode(), 0,
                                  ns=relay.HOME)
    assert err == "", err
    assert frame["kind"] == "err"
    return frame["body"]


async def _next_sealed(reader, frames, phone: _PhoneCodec, kind=None, timeout=5.0):
    """The next down frame of `kind`, opened in order; every frame read is
    opened (the codec's position moves once per frame), so frames of
    another kind are consumed rather than dropped."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        while phone.pending:
            k, payload = phone.open(*phone.pending.pop(0))
            if kind is None or k == kind:
                return k, payload
        data = await asyncio.wait_for(reader.read(65536), timeout)
        if not data:
            break
        phone.pending.extend(frames.feed(data))
    raise AssertionError(f"no sealed {kind!r} frame")


async def _lan_ready(server):
    srv, daemon, (_loop_port, lan_port) = server
    daemon.lan_access_enabled = True
    await srv.start_lan()
    paired = await _pair_plain(lan_port)
    return srv, daemon, lan_port, paired


@pytest.mark.asyncio
async def test_the_sealed_stream_paints_types_in_order_resizes_and_reports_exit(
        server, tmp_path):
    srv, daemon, lan_port, paired = await _lan_ready(server)
    key = paired["key"]
    handle = await _cat_session(daemon, tmp_path)
    term = daemon._pty.get(handle)
    daemon._pty.write(handle, "before\r")
    assert await _wait_for(lambda: b"before\r\n" in term.ring)

    reader, writer, status, headers, frame_id = await _post_lan_stream(
        lan_port, key, 1, "s1")
    assert status == 200
    assert headers["content-type"] == terminal_stream.SEALED_CONTENT_TYPE
    phone = _PhoneCodec(key, frame_id)
    frames = terminal_stream.FrameReader()
    # Nothing after the head is plaintext: the outer kind is always `Z` and
    # the paint's words are inside the seal.
    raw = await asyncio.wait_for(reader.read(65536), 5)
    assert b"before" not in raw
    items = frames.feed(raw)
    assert items and all(k == b"Z" for k, _ in items)
    kind, paint = phone.open(*items[0])
    phone.pending.extend(items[1:])
    assert kind == b"D"
    t = Screen(term.cols, term.rows)
    t.feed(paint)
    assert "before" in "".join(t.text())
    assert srv._lan_streams[paired["device_id"]] is not None

    for ch in "abc":
        writer.write(phone.seal(b"I", ch.encode()))
    writer.write(phone.seal(b"I", b"\r"))
    await writer.drain()
    assert await _wait_for(lambda: b"abc\r" in term.ring)
    echoed = bytearray()
    while b"abc" not in echoed:
        _k, payload = await _next_sealed(reader, frames, phone, b"D")
        echoed += payload

    # The phone's size lands while the panel's pane is not showing s1.
    writer.write(phone.seal(b"S", b"90 25"))
    await writer.drain()
    assert await _wait_for(lambda: (term.cols, term.rows) == (90, 25))
    writer.write(phone.seal(b"P", b""))
    await writer.drain()

    os.kill(term.pid, signal.SIGKILL)
    kind, _ = await _next_sealed(reader, frames, phone, b"X")
    assert kind == b"X"
    assert await asyncio.wait_for(reader.read(1), 5) == b""
    writer.close()
    assert await _wait_for(lambda: paired["device_id"] not in srv._lan_streams)
    assert term.subscribers == []


@pytest.mark.asyncio
async def test_a_frame_that_does_not_open_closes_the_sealed_stream(server, tmp_path):
    srv, daemon, lan_port, paired = await _lan_ready(server)
    key = paired["key"]
    handle = await _cat_session(daemon, tmp_path)
    term = daemon._pty.get(handle)
    try:
        # A tampered tag.
        reader, writer, status, _h, frame_id = await _post_lan_stream(
            lan_port, key, 1, "s1")
        assert status == 200
        phone = _PhoneCodec(key, frame_id)
        frames = terminal_stream.FrameReader()
        await _next_sealed(reader, frames, phone, b"D")
        bad = bytearray(phone.seal(b"I", b"x"))
        bad[-1] ^= 0x01
        writer.write(bytes(bad))
        await writer.drain()
        assert await asyncio.wait_for(reader.read(65536), 5) == b""
        writer.close()
        assert await _wait_for(lambda: term.subscribers == [])
        assert b"x" not in term.ring

        # A replay: the same sealed key twice.
        reader, writer, status, _h, frame_id = await _post_lan_stream(
            lan_port, key, 2, "s1")
        assert status == 200
        phone = _PhoneCodec(key, frame_id)
        frames = terminal_stream.FrameReader()
        await _next_sealed(reader, frames, phone, b"D")
        once = phone.seal(b"I", b"y")
        writer.write(once)
        await writer.drain()
        assert await _wait_for(lambda: b"y" in term.ring)
        writer.write(once)
        await writer.drain()
        assert await _wait_for(lambda: term.subscribers == [])
        writer.close()
        assert term.ring.count(b"y") == 1

        # A plain (unsealed) frame on the sealed stream, and a frame sealed
        # to another stream's id.
        reader, writer, status, _h, frame_id = await _post_lan_stream(
            lan_port, key, 3, "s1")
        assert status == 200
        phone = _PhoneCodec(key, frame_id)
        frames = terminal_stream.FrameReader()
        await _next_sealed(reader, frames, phone, b"D")
        writer.write(_PhoneCodec(key, "elsewhere").seal(b"I", b"z"))
        await writer.drain()
        assert await asyncio.wait_for(reader.read(65536), 5) == b""
        writer.close()
        assert b"z" not in term.ring
    finally:
        await daemon._pty.close(handle)


@pytest.mark.asyncio
async def test_the_sealed_route_refuses_before_it_sends_anything(server, tmp_path):
    srv, daemon, lan_port, paired = await _lan_ready(server)
    key = paired["key"]
    # Not paired: a plain 403 and no head.
    stranger = relay.mint_key()
    reader, writer, status, headers, _id = await _post_lan_stream(
        lan_port, stranger, 1, "s1")
    assert status == 403 and headers["content-type"] == "application/json"
    body = await reader.readexactly(int(headers["content-length"]))
    assert json.loads(body)["error"] == "that phone is not paired"
    writer.close()
    # A frame of another kind on this route: a sealed err naming the kind.
    reader, writer, status, headers, _id = await _post_lan_stream(
        lan_port, key, 1, "s1", kind="state")
    assert status == 200 and headers["content-type"] == "text/plain"
    err = await _sealed_err(reader, headers, key)
    assert err["status"] == 400 and "'terminal_stream'" in err["error"]
    writer.close()
    # A session Dark Army does not host: a sealed 409 in the daemon's words.
    reader, writer, status, headers, frame_id = await _post_lan_stream(
        lan_port, key, 2, "nobody")
    assert status == 200 and headers["content-type"] == "text/plain"
    err = await _sealed_err(reader, headers, key)
    assert err == {"re": frame_id, "status": 409,
                   "error": dm.TERMINAL_NOT_OWNED_REFUSAL}
    writer.close()
    # No session at all.
    reader, writer, status, headers, _id = await _post_lan_stream(
        lan_port, key, 3, "")
    err = await _sealed_err(reader, headers, key)
    assert err["status"] == 400
    writer.close()
    # An Origin header is refused plainly, like every other phone route.
    wire = relay.seal_frame(key, relay.DIR_PHONE_TO_MAC, 4, "terminal_stream",
                            {"session": "s1"}, ns=relay.HOME).encode()
    reader, writer = await asyncio.open_connection("127.0.0.1", lan_port)
    writer.write((
        "POST /api/terminal/stream HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{lan_port}\r\nOrigin: http://evil\r\n"
        f"X-Bob-Channel: {relay.channel_id(key, ns=relay.HOME)}\r\n"
        f"Content-Length: {len(wire)}\r\n\r\n").encode() + wire)
    await writer.drain()
    head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
    assert head.split(b" ")[1] == b"403"
    writer.close()
    assert srv._lan_streams == {}


@pytest.mark.asyncio
async def test_a_second_stream_from_the_same_device_closes_the_first(server, tmp_path):
    srv, daemon, lan_port, paired = await _lan_ready(server)
    key = paired["key"]
    handle = await _cat_session(daemon, tmp_path)
    term = daemon._pty.get(handle)
    try:
        r1, w1, status, _h, id1 = await _post_lan_stream(lan_port, key, 1, "s1")
        assert status == 200
        p1 = _PhoneCodec(key, id1)
        await _next_sealed(r1, terminal_stream.FrameReader(), p1, b"D")
        assert await _wait_for(lambda: len(term.subscribers) == 1)
        r2, w2, status, _h, id2 = await _post_lan_stream(lan_port, key, 2, "s1")
        assert status == 200
        p2 = _PhoneCodec(key, id2)
        await _next_sealed(r2, terminal_stream.FrameReader(), p2, b"D")
        # The first socket is hung up on; the second is the one on file.
        assert await asyncio.wait_for(r1.read(65536), 5) == b""
        assert await _wait_for(lambda: len(term.subscribers) == 1)
        assert srv._lan_streams[paired["device_id"]] is not None
        assert ApiServer_max_streams() == 1
        w1.close()
        w2.write(p2.seal(b"I", b"k"))
        await w2.drain()
        assert await _wait_for(lambda: b"k" in term.ring)
        w2.close()
        assert await _wait_for(lambda: term.subscribers == [])
    finally:
        await daemon._pty.close(handle)


@pytest.mark.asyncio
async def test_the_eviction_reads_the_constant(server, tmp_path):
    """The bound is `MAX_LAN_STREAMS_PER_DEVICE`, not a hardcoded one: the
    eviction used to close "the previous stream" and the constant beside it
    was decoration a test could not tell from the rule."""
    srv, daemon, lan_port, paired = await _lan_ready(server)
    key = paired["key"]
    handle = await _cat_session(daemon, tmp_path)
    term = daemon._pty.get(handle)
    srv.MAX_LAN_STREAMS_PER_DEVICE = 2
    try:
        r1, w1, status, _h, id1 = await _post_lan_stream(lan_port, key, 1, "s1")
        assert status == 200
        await _next_sealed(r1, terminal_stream.FrameReader(), _PhoneCodec(key, id1), b"D")
        r2, w2, status, _h, id2 = await _post_lan_stream(lan_port, key, 2, "s1")
        assert status == 200
        await _next_sealed(r2, terminal_stream.FrameReader(), _PhoneCodec(key, id2), b"D")
        # Two is the stated bound, so both are still up.
        assert await _wait_for(
            lambda: len(srv._lan_streams[paired["device_id"]]) == 2)
        assert len(term.subscribers) == 2
        # The third evicts the oldest, and only the oldest.
        r3, w3, status, _h, id3 = await _post_lan_stream(lan_port, key, 3, "s1")
        assert status == 200
        await _next_sealed(r3, terminal_stream.FrameReader(), _PhoneCodec(key, id3), b"D")
        assert await asyncio.wait_for(r1.read(65536), 5) == b""
        assert await _wait_for(
            lambda: len(srv._lan_streams[paired["device_id"]]) == 2)
        w1.close()
        w2.close()
        w3.close()
    finally:
        srv.MAX_LAN_STREAMS_PER_DEVICE = ApiServer_max_streams()
        await daemon._pty.close(handle)


def ApiServer_max_streams() -> int:
    from dark_army_daemon.api_server import ApiServer
    return ApiServer.MAX_LAN_STREAMS_PER_DEVICE


@pytest.mark.asyncio
async def test_a_flood_of_up_frames_closes_the_sealed_stream(server, tmp_path):
    srv, daemon, lan_port, paired = await _lan_ready(server)
    key = paired["key"]
    handle = await _cat_session(daemon, tmp_path)
    term = daemon._pty.get(handle)
    try:
        reader, writer, status, _h, frame_id = await _post_lan_stream(
            lan_port, key, 1, "s1")
        assert status == 200
        phone = _PhoneCodec(key, frame_id)
        await _next_sealed(reader, terminal_stream.FrameReader(), phone, b"D")
        burst = b"".join(phone.seal(b"P", b"") for _ in range(
            terminal_stream.STREAM_MAX_UP_FRAMES_PER_MINUTE + 1))
        writer.write(burst)
        await writer.drain()
        assert await _wait_for(lambda: term.subscribers == [])
        writer.close()
    finally:
        await daemon._pty.close(handle)


@pytest.mark.asyncio
async def test_the_opening_frame_notes_the_counter_durably(server, tmp_path):
    srv, daemon, lan_port, paired = await _lan_ready(server)
    key = paired["key"]
    did = paired["device_id"]
    handle = await _cat_session(daemon, tmp_path)
    try:
        reader, writer, status, _h, frame_id = await _post_lan_stream(
            lan_port, key, 7, "s1")
        assert status == 200
        assert devices.last_home_recv_ctr(did) == 7
        # Durable: on disk, not merely in the coalescing overlay.
        assert int(devices._entry(did).get("home_recv_ctr") or 0) == 7
        writer.close()
        # And the counter is a floor: the same frame again is a sealed err
        # carrying `ctr_expected`, never a second stream.
        reader, writer, status, headers, _id = await _post_lan_stream(
            lan_port, key, 7, "s1")
        assert status == 200 and headers["content-type"] == "text/plain"
        err = await _sealed_err(reader, headers, key)
        assert err["ctr_expected"] == 8
        writer.close()
    finally:
        await daemon._pty.close(handle)


# --- the opening paint, in pieces ----------------------------------------------


def _picture_screen(screen: Screen, lines: int = 560, cols: int = 300) -> None:
    """Fill `screen` with a full-colour picture: every cell its own
    truecolor run, more lines than the scrollback keeps."""
    row = b"".join(
        b"\x1b[38;2;%d;%d;%dm\x1b[48;2;%d;%d;%dmX" % (
            i % 256, (i * 7) % 256, (i * 13) % 256,
            (i * 3) % 256, (i * 5) % 256, (i * 11) % 256)
        for i in range(cols))
    for _ in range(lines):
        screen.feed(row + b"\r\n")


async def _collect_paint(reader, frames, expected: int, timeout: float = 30.0) -> list:
    """Every `D` payload until `expected` bytes have arrived, in order — a
    read routinely returns several frames, and every one is kept."""
    got: list = []
    total = 0
    while total < expected:
        data = await asyncio.wait_for(reader.read(65536), timeout)
        assert data, "the stream closed before the paint was whole"
        for kind, payload in frames.feed(data):
            assert kind == b"D", kind
            got.append(payload)
            total += len(payload)
    return got


def test_the_paint_slice_is_named_once_and_the_sealed_budget_is_exact():
    """`PAINT_CHUNK_BYTES` is the one bound both the opening paint and the
    coalescer read; `SealedCodec.max_plain` is the frame cap less the seal's
    exact 29 bytes — and a wrap of exactly that many bytes is a frame of
    exactly `MAX_FRAME_BYTES`, the boundary asserted rather than assumed."""
    assert terminal_stream.PAINT_CHUNK_BYTES == terminal_stream.MAX_FRAME_BYTES // 2
    assert relay.BLOB_OVERHEAD_BYTES == 28
    assert terminal_stream.PlainCodec().max_plain == terminal_stream.MAX_FRAME_BYTES
    codec = terminal_stream.SealedCodec(relay.mint_key(), "s")
    assert codec.max_plain == terminal_stream.MAX_FRAME_BYTES - relay.BLOB_OVERHEAD_BYTES - 1
    assert codec.max_plain == terminal_stream.MAX_FRAME_BYTES - 29
    kind, blob = codec.wrap(b"D", b"x" * codec.max_plain)
    assert len(blob) == terminal_stream.MAX_FRAME_BYTES
    assert len(terminal_stream.encode(kind, blob)) == terminal_stream.MAX_FRAME_BYTES + 5
    with pytest.raises(ValueError):
        terminal_stream.encode(*codec.wrap(b"D", b"x" * (codec.max_plain + 1)))
    assert "PAINT_CHUNK_BYTES" in terminal_stream.__all__
    assert "STREAM_SEND_FAILED_REFUSAL" in terminal_stream.__all__
    assert terminal_stream.STREAM_SEND_FAILED_REFUSAL.startswith("Dark Army")


@pytest.mark.asyncio
async def test_a_paint_over_the_chunk_bound_arrives_as_ordered_d_frames(
        server, tmp_path, monkeypatch):
    """The fast proof: the bound driven down, the paint arrives as several
    `D` frames concatenating to `Screen.paint()` exactly."""
    srv, daemon, (loop_port, _lan) = server
    handle = await _cat_session(daemon, tmp_path)
    term = daemon._pty.get(handle)
    daemon._pty.write(handle, "\x1b[31mred\x1b[0m and \x1b[32mgreen\x1b[0m\r")
    assert await _wait_for(lambda: b"green" in term.ring)
    monkeypatch.setattr(terminal_stream, "PAINT_CHUNK_BYTES", 64)
    _h, paint, _x, _r = daemon.terminal_attach("s1")
    assert len(paint) > 64 * 3

    reader, writer, status, _headers = await _open_stream(loop_port, srv.token, "s1")
    assert status == 200
    frames = terminal_stream.FrameReader()
    pieces = await _collect_paint(reader, frames, len(paint))
    assert len(pieces) == -(-len(paint) // 64)
    assert all(len(p) <= 64 for p in pieces)
    assert b"".join(pieces) == paint
    # And the stream is still a terminal after it: keys land, echo comes back.
    writer.write(terminal_stream.encode(terminal_stream.KIND_INPUT, b"ok\r"))
    await writer.drain()
    assert await _wait_for(lambda: b"ok\r\n" in term.ring)
    echoed = bytearray()
    while b"ok" not in echoed:
        _k, payload = await _next_frame(reader, frames, b"D")
        echoed += payload
    writer.close()
    await daemon._pty.close(handle)


@pytest.mark.asyncio
async def test_a_real_picture_paints_past_the_frame_cap_and_still_arrives(
        server, tmp_path, monkeypatch):
    """A wide screen that drew a truecolor picture paints past
    `MAX_FRAME_BYTES` — the case the cap's own docstring predicted — and still
    comes back whole, in order. The cap is lowered to 64 KiB for the test:
    the mechanism (slice at `PAINT_CHUNK_BYTES`, reassemble in order) is the
    same at 4 MiB, and feeding 560 truecolor lines through pyte to reach the
    real cap was 36 s of the suite's critical path."""
    monkeypatch.setattr(terminal_stream, "MAX_FRAME_BYTES", 64 * 1024)
    monkeypatch.setattr(terminal_stream, "PAINT_CHUNK_BYTES", 32 * 1024)
    srv, daemon, (loop_port, _lan) = server
    handle = await _cat_session(daemon, tmp_path)
    term = daemon._pty.get(handle)
    term.screen.resize(60, 60)
    _picture_screen(term.screen, lines=40, cols=60)
    _h, paint, _x, _r = daemon.terminal_attach("s1")
    assert len(paint) > terminal_stream.MAX_FRAME_BYTES
    with pytest.raises(ValueError):
        terminal_stream.encode(terminal_stream.KIND_DATA, paint)

    reader, writer, status, _headers = await _open_stream(loop_port, srv.token, "s1")
    assert status == 200
    frames = terminal_stream.FrameReader()
    pieces = await _collect_paint(reader, frames, len(paint), timeout=60.0)
    assert len(pieces) >= 2
    assert all(len(p) <= terminal_stream.PAINT_CHUNK_BYTES for p in pieces)
    assert b"".join(pieces) == paint
    writer.close()
    await daemon._pty.close(handle)


@pytest.mark.asyncio
async def test_the_sealed_paint_stays_under_the_frame_cap_in_order(
        server, tmp_path, monkeypatch):
    """The phone door: every `Z` frame's outer length is under the cap, the
    opened payloads concatenate to the paint, and the positions are
    consecutive from 0 — the codec's own counter, one per slice."""
    srv, daemon, lan_port, paired = await _lan_ready(server)
    key = paired["key"]
    handle = await _cat_session(daemon, tmp_path)
    term = daemon._pty.get(handle)
    daemon._pty.write(handle, "\x1b[34mblue\x1b[0m lines\r")
    assert await _wait_for(lambda: b"lines" in term.ring)
    monkeypatch.setattr(terminal_stream, "PAINT_CHUNK_BYTES", 50)
    _h, paint, _x, _r = daemon.terminal_attach("s1")
    assert len(paint) > 150

    reader, writer, status, headers, frame_id = await _post_lan_stream(
        lan_port, key, 1, "s1")
    assert status == 200
    phone = _PhoneCodec(key, frame_id)
    frames = terminal_stream.FrameReader()
    pieces: list = []
    outer: list = []
    total = 0
    while total < len(paint):
        data = await asyncio.wait_for(reader.read(65536), 5)
        assert data
        for k, blob in frames.feed(data):
            assert k == b"Z"
            assert len(blob) <= terminal_stream.MAX_FRAME_BYTES
            outer.append(len(blob))
            kind, payload = phone.open(k, blob)
            assert kind == b"D"
            pieces.append(payload)
            total += len(payload)
    assert b"".join(pieces) == paint
    assert len(pieces) == -(-len(paint) // 50)
    assert phone.down == len(pieces)
    # Each sealed frame is its slice plus exactly the 29 bytes the seal adds.
    assert all(o == len(p) + relay.BLOB_OVERHEAD_BYTES + 1
               for o, p in zip(outer, pieces))
    writer.close()
    await daemon._pty.close(handle)


@pytest.mark.asyncio
async def test_an_unexpected_send_failure_is_one_e_frame_and_a_log_line(
        server, tmp_path, monkeypatch, caplog):
    """A paint that cannot be sent for a reason that is not the peer hanging
    up: one `E` frame in `STREAM_SEND_FAILED_REFUSAL`'s words, one log
    record naming the handle — never a socket that closes in silence."""
    srv, daemon, (loop_port, _lan) = server
    handle = await _cat_session(daemon, tmp_path)
    real_encode = terminal_stream.encode
    calls = {"n": 0}

    def failing_encode(kind, payload=b""):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ValueError("frame too large")
        return real_encode(kind, payload)

    monkeypatch.setattr(terminal_stream, "encode", failing_encode)
    caplog.set_level(logging.ERROR, logger="dark-army.terminal_stream")
    reader, writer, status, _headers = await _open_stream(loop_port, srv.token, "s1")
    assert status == 200
    frames = terminal_stream.FrameReader()
    kind, payload = await _next_frame(reader, frames, None)
    assert kind == b"E"
    assert payload.decode() == terminal_stream.STREAM_SEND_FAILED_REFUSAL
    # The stream ends behind it: nothing else was sent.
    assert await asyncio.wait_for(reader.read(1), 5) == b""
    records = [r for r in caplog.records if r.name == "dark-army.terminal_stream"]
    assert len(records) == 1
    assert handle in records[0].getMessage()
    assert "could not send" in records[0].getMessage()
    assert records[0].exc_info is not None
    writer.close()
    await daemon._pty.close(handle)


@pytest.mark.asyncio
async def test_a_hang_up_is_still_silent(server, tmp_path, caplog):
    """The ordinary disconnect — the pane closing — writes no line: a line
    there would fire on every pane close."""
    srv, daemon, (loop_port, _lan) = server
    handle = await _cat_session(daemon, tmp_path)
    term = daemon._pty.get(handle)
    caplog.set_level(logging.DEBUG, logger="dark-army.terminal_stream")
    reader, writer, status, _h = await _open_stream(loop_port, srv.token, "s1")
    assert status == 200
    assert await _wait_for(lambda: len(term.subscribers) == 1)
    writer.close()
    assert await _wait_for(lambda: term.subscribers == [])
    assert [r for r in caplog.records if r.name == "dark-army.terminal_stream"] == []
    await daemon._pty.close(handle)
