"""The hook door, driven over real connections: the private Unix socket on a
short temporary path and the loopback bridge on a spare port."""

import asyncio
import json
import logging
import os
import shutil
import socket
import stat
import tempfile

import pytest
import pytest_asyncio
from dark_army_daemon import socket_server
from dark_army_daemon.socket_server import HOOK_IPC_HOST, SocketServer


def _spare_port() -> int:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind((HOOK_IPC_HOST, 0))
        return probe.getsockname()[1]
    finally:
        probe.close()


@pytest.fixture
def short_dir():
    # AF_UNIX paths are capped near 104 bytes on macOS and pytest's
    # tmp_path is longer than that, so the socket lives under /tmp.
    path = tempfile.mkdtemp(prefix="hk-", dir="/tmp")
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


class Door:
    """A started server plus everything its handler was handed."""

    def __init__(self, answer=None, path=""):
        self.port = _spare_port()
        self.path = path
        self.delivered = []
        self._answer = answer
        self.server = SocketServer(on_message=self._take, port=self.port, path=path)

    async def _take(self, message):
        self.delivered.append(message)
        return self._answer(message) if self._answer else None

    async def knock(self, payload: bytes, *, wait_reply=False):
        reader, writer = await asyncio.open_connection(HOOK_IPC_HOST, self.port)
        return await self._exchange(reader, writer, payload, wait_reply)

    async def knock_socket(self, payload: bytes, *, wait_reply=False):
        reader, writer = await asyncio.open_unix_connection(self.path)
        return await self._exchange(reader, writer, payload, wait_reply)

    @staticmethod
    async def _exchange(reader, writer, payload, wait_reply):
        writer.write(payload + b"\n")
        await writer.drain()
        reply = await reader.readline() if wait_reply else None
        writer.close()
        await writer.wait_closed()
        return reply

    async def settle(self, expected: int):
        for _ in range(100):
            if len(self.delivered) >= expected:
                return
            await asyncio.sleep(0.01)


@pytest_asyncio.fixture
async def door():
    opened = []

    async def make(answer=None, path=""):
        d = Door(answer, path)
        await d.server.start()
        opened.append(d)
        return d

    yield make
    for d in opened:
        await d.server.stop()


# ── the bridge: the loopback port, as it always was ──────────────────────────

@pytest.mark.asyncio
async def test_tcp_each_connection_hands_its_one_message_over(door):
    d = await door()
    sent = [{"event": "tool_use", "session_id": f"sess-{n}"} for n in range(4)]
    await asyncio.gather(*(d.knock(json.dumps(m).encode()) for m in sent))
    await d.settle(len(sent))
    assert sorted(m["session_id"] for m in d.delivered) == [f"sess-{n}" for n in range(4)]


@pytest.mark.asyncio
async def test_tcp_a_line_that_is_not_json_is_dropped_and_the_door_stays_open(door):
    d = await door()
    await d.knock(b"{{ this is not json")
    await d.knock(json.dumps({"event": "dismiss", "session_id": "after"}).encode())
    await d.settle(1)
    assert d.delivered == [{"event": "dismiss", "session_id": "after"}]


@pytest.mark.asyncio
async def test_tcp_a_handler_answer_comes_back_as_one_json_line(door):
    d = await door(answer=lambda m: {"echo": m["session_id"]})
    reply = await d.knock(json.dumps({"session_id": "asker"}).encode(), wait_reply=True)
    assert json.loads(reply) == {"echo": "asker"}


@pytest.mark.asyncio
async def test_tcp_no_answer_means_the_client_reads_end_of_stream(door):
    d = await door()
    reply = await d.knock(json.dumps({"session_id": "quiet"}).encode(), wait_reply=True)
    assert reply == b""
    assert d.delivered == [{"session_id": "quiet"}]


@pytest.mark.asyncio
async def test_tcp_stopping_releases_the_port(door):
    d = await door()
    await d.knock(b"{}")
    await d.server.stop()
    with pytest.raises(OSError):
        await asyncio.open_connection(HOOK_IPC_HOST, d.port)


@pytest.mark.asyncio
async def test_tcp_delivery_is_logged_once_per_session_and_never_the_key(door, caplog):
    """The bridge's use is visible, so its retirement can be judged — and
    the line carries the id and the event, never the key or a folder."""
    d = await door()
    caplog.set_level(logging.INFO, logger="dark-army.socket")
    first = {"event": "tool_use", "session_id": "one-bridge-session",
             "key": "secret-enrolment-key", "cwd": "/Users/somebody/project"}
    for _ in range(3):
        await d.knock(json.dumps(first).encode())
    await d.knock(json.dumps({**first, "session_id": "two-bridge-session"}).encode())
    await d.settle(4)
    lines = [r.getMessage() for r in caplog.records
             if "hook over the network port" in r.getMessage()]
    assert len(lines) == 2
    assert "one-bridge-s" in lines[0] and "tool_use" in lines[0]
    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert "secret-enrolment-key" not in joined
    assert "/Users/somebody/project" not in joined


# ── the private socket ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_socket_a_message_is_delivered(door, short_dir):
    path = os.path.join(short_dir, "hook.sock")
    d = await door(path=path)
    await d.knock_socket(json.dumps({"event": "session_start", "session_id": "s1"}).encode())
    await d.settle(1)
    assert d.delivered == [{"event": "session_start", "session_id": "s1"}]


@pytest.mark.asyncio
async def test_socket_a_handler_answer_comes_back_as_one_json_line(door, short_dir):
    path = os.path.join(short_dir, "hook.sock")
    d = await door(answer=lambda m: {"agents": []}, path=path)
    reply = await d.knock_socket(json.dumps({"event": "statusline"}).encode(),
                                 wait_reply=True)
    assert json.loads(reply) == {"agents": []}


@pytest.mark.asyncio
async def test_socket_is_mode_0600_and_its_folder_is_left_alone(door, short_dir):
    os.chmod(short_dir, 0o755)
    path = os.path.join(short_dir, "hook.sock")
    await door(path=path)
    st = os.stat(path)
    assert stat.S_ISSOCK(st.st_mode)
    assert stat.S_IMODE(st.st_mode) == 0o600
    assert stat.S_IMODE(os.stat(short_dir).st_mode) == 0o755


@pytest.mark.asyncio
async def test_socket_bind_leaves_the_process_umask_as_it_was(door, short_dir):
    before = os.umask(0o022)
    os.umask(before)
    await door(path=os.path.join(short_dir, "hook.sock"))
    after = os.umask(0o022)
    os.umask(after)
    assert after == before


@pytest.mark.asyncio
async def test_socket_a_leftover_regular_file_is_replaced(door, short_dir):
    path = os.path.join(short_dir, "hook.sock")
    with open(path, "w") as fh:
        fh.write("left behind")
    d = await door(path=path)
    assert stat.S_ISSOCK(os.stat(path).st_mode)
    await d.knock_socket(b'{"session_id": "after-a-crash"}')
    await d.settle(1)
    assert d.delivered == [{"session_id": "after-a-crash"}]


@pytest.mark.asyncio
async def test_socket_a_dead_socket_file_is_replaced(door, short_dir):
    path = os.path.join(short_dir, "hook.sock")
    dead = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    dead.bind(path)
    dead.close()          # the file stays; nobody listens behind it
    d = await door(path=path)
    await d.knock_socket(b'{"session_id": "fresh"}')
    await d.settle(1)
    assert d.delivered == [{"session_id": "fresh"}]


@pytest.mark.asyncio
async def test_socket_a_live_answerer_is_replaced_with_one_warning(door, short_dir, caplog):
    """`daemon.lock` already proves no second daemon, so whatever answers at
    the name is not one — replaced, and said."""
    path = os.path.join(short_dir, "hook.sock")
    squatter = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    squatter.bind(path)
    squatter.listen(1)
    try:
        caplog.set_level(logging.WARNING, logger="dark-army.socket")
        d = await door(path=path)
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING
                    and "answering on the hook socket" in r.getMessage()]
        assert len(warnings) == 1
        await d.knock_socket(b'{"session_id": "ours"}')
        await d.settle(1)
        assert d.delivered == [{"session_id": "ours"}]
    finally:
        squatter.close()


@pytest.mark.asyncio
async def test_socket_another_account_is_refused_unread(door, short_dir, monkeypatch, caplog):
    monkeypatch.setattr(socket_server, "_peer_uid", lambda s: os.getuid() + 1)
    caplog.set_level(logging.WARNING, logger="dark-army.socket")
    path = os.path.join(short_dir, "hook.sock")
    d = await door(answer=lambda m: {"never": True}, path=path)
    for _ in range(2):
        reply = await d.knock_socket(b'{"session_id": "stranger"}', wait_reply=True)
        assert reply == b""
    await asyncio.sleep(0.05)
    assert d.delivered == []
    refusals = [r for r in caplog.records if "another account" in r.getMessage()]
    assert len(refusals) == 1


@pytest.mark.asyncio
async def test_socket_no_peer_reading_is_accepted(door, short_dir, monkeypatch):
    monkeypatch.setattr(socket_server, "_peer_uid", lambda s: None)
    path = os.path.join(short_dir, "hook.sock")
    d = await door(path=path)
    await d.knock_socket(b'{"session_id": "unread-peer"}')
    await d.settle(1)
    assert d.delivered == [{"session_id": "unread-peer"}]


@pytest.mark.asyncio
async def test_socket_the_real_peer_probe_reads_this_account(door, short_dir):
    """`getpeereid` over ctypes answers on this Mac, and answers us."""
    path = os.path.join(short_dir, "hook.sock")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(path)
    server.listen(1)
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        client.connect(path)
        accepted, _ = server.accept()
        try:
            assert socket_server._peer_uid(accepted) == os.getuid()
        finally:
            accepted.close()
    finally:
        client.close()
        server.close()
    assert socket_server._peer_uid(object()) is None


@pytest.mark.asyncio
async def test_socket_an_over_long_path_serves_the_bridge_alone(door, short_dir, caplog):
    path = os.path.join(short_dir, "x" * 120, "hook.sock")
    assert len(os.fsencode(path)) > socket_server.MAX_SOCK_PATH_BYTES
    caplog.set_level(logging.ERROR, logger="dark-army.socket")
    d = await door(path=path)
    assert not os.path.lexists(path)
    assert d.server.unix_path == ""
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    await d.knock(b'{"session_id": "via-the-bridge"}')
    await d.settle(1)
    assert d.delivered == [{"session_id": "via-the-bridge"}]


@pytest.mark.asyncio
async def test_socket_stop_unlinks_its_own_file(door, short_dir):
    path = os.path.join(short_dir, "hook.sock")
    d = await door(path=path)
    assert d.server.unix_path == path
    await d.server.stop()
    assert not os.path.lexists(path)
    with pytest.raises(OSError):
        await asyncio.open_unix_connection(path)


@pytest.mark.asyncio
async def test_socket_stop_leaves_a_successors_file_alone(door, short_dir):
    """A daemon shutting down late must never remove the next one's door."""
    path = os.path.join(short_dir, "hook.sock")
    d = await door(path=path)
    os.unlink(path)
    successor = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    successor.bind(path)
    try:
        await d.server.stop()
        assert stat.S_ISSOCK(os.stat(path).st_mode)
    finally:
        successor.close()


def test_socket_the_default_path_is_the_private_state_folder(monkeypatch):
    from dark_army_daemon import paths
    monkeypatch.delenv(socket_server.SOCKET_ENV, raising=False)
    assert socket_server._configured_sock() == str(paths.HOOK_SOCK_PATH)
    monkeypatch.setenv(socket_server.SOCKET_ENV, "/tmp/elsewhere.sock")
    assert socket_server._configured_sock() == "/tmp/elsewhere.sock"


# ── a door that could not open ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_socket_path_is_reported_only_once_bound(door, short_dir):
    path = os.path.join(short_dir, "hook.sock")
    d = Door(path=path)
    assert d.server.unix_path == ""            # not bound yet: nothing to tell
    await d.server.start()
    try:
        assert d.server.unix_path == path
    finally:
        await d.server.stop()
    assert d.server.unix_path == ""


@pytest.mark.asyncio
async def test_socket_a_failed_bind_reports_no_path_and_the_bridge_serves(door, short_dir, caplog):
    """A short path that still cannot be bound (its folder is a file) is one
    ERROR; the socket is never reported, so no terminal is told it."""
    blocker = os.path.join(short_dir, "notadir")
    with open(blocker, "w") as fh:
        fh.write("")
    caplog.set_level(logging.ERROR, logger="dark-army.socket")
    d = await door(path=os.path.join(blocker, "hook.sock"))
    assert d.server.unix_path == ""
    assert [r for r in caplog.records if r.levelno == logging.ERROR]
    await d.knock(b'{"session_id": "bridge-after-bind-failure"}')
    await d.settle(1)
    assert d.delivered == [{"session_id": "bridge-after-bind-failure"}]


@pytest.mark.asyncio
async def test_socket_a_failed_bind_hands_terminals_the_port(short_dir, tmp_path):
    """The daemon's hand-over after `start()`: a door with no socket leaves
    the pty host on the port, so a child still reports somewhere."""
    from types import SimpleNamespace
    from dark_army_daemon import ptyhost
    from dark_army_daemon.daemon import BobDaemon
    blocker = os.path.join(short_dir, "notadir")
    with open(blocker, "w") as fh:
        fh.write("")
    server = SocketServer(on_message=None, port=_spare_port(),
                          path=os.path.join(blocker, "hook.sock"))
    await server.start()
    try:
        host = ptyhost.PtyHost(port=19999)
        BobDaemon._hand_terminals_the_hook_door(SimpleNamespace(_pty=host, _socket=server))
        assert host.hook_sock == ""
        ok, _detail, pid = await host.start(
            str(tmp_path), ["/bin/sh", "-c",
                            'echo "[$DARK_ARMY_HOOK_SOCKET|$BOB_COMPANION_PORT]"; sleep 5'],
            "env")
        assert ok
        term = host.get(host.owns(pid))
        for _ in range(250):
            if b"[|19999]" in term.ring:
                break
            await asyncio.sleep(0.02)
        assert b"[|19999]" in term.ring, term.ring
        await host.close(host.owns(pid), grace=0.5)
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_socket_a_bound_door_hands_terminals_the_socket(short_dir):
    from types import SimpleNamespace
    from dark_army_daemon import ptyhost
    from dark_army_daemon.daemon import BobDaemon
    path = os.path.join(short_dir, "hook.sock")
    server = SocketServer(on_message=None, port=_spare_port(), path=path)
    await server.start()
    try:
        host = ptyhost.PtyHost(port=19999)
        BobDaemon._hand_terminals_the_hook_door(SimpleNamespace(_pty=host, _socket=server))
        assert host.hook_sock == path
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_socket_a_taken_bridge_port_leaves_the_socket_serving(door, short_dir, caplog):
    """Another account holding 127.0.0.1:19873 at start must not take the
    daemon down: one ERROR, and the private socket serves alone."""
    squatter = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    squatter.bind((HOOK_IPC_HOST, 0))
    squatter.listen(1)
    port = squatter.getsockname()[1]
    path = os.path.join(short_dir, "hook.sock")
    caplog.set_level(logging.ERROR, logger="dark-army.socket")
    delivered = []

    async def take(message):
        delivered.append(message)

    server = SocketServer(on_message=take, port=port, path=path)
    try:
        await server.start()
        errors = [r for r in caplog.records if "port taken" in r.getMessage()]
        assert len(errors) == 1
        assert server.unix_path == path
        reader, writer = await asyncio.open_unix_connection(path)
        writer.write(b'{"session_id": "socket-alone"}\n')
        await writer.drain()
        writer.close()
        await writer.wait_closed()
        for _ in range(100):
            if delivered:
                break
            await asyncio.sleep(0.01)
        assert delivered == [{"session_id": "socket-alone"}]
    finally:
        await server.stop()
        squatter.close()
    assert not os.path.lexists(path)


@pytest.mark.asyncio
async def test_socket_both_doors_failing_still_raises(short_dir):
    blocker = os.path.join(short_dir, "notadir")
    with open(blocker, "w") as fh:
        fh.write("")
    squatter = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    squatter.bind((HOOK_IPC_HOST, 0))
    squatter.listen(1)
    try:
        server = SocketServer(on_message=None, port=squatter.getsockname()[1],
                              path=os.path.join(blocker, "hook.sock"))
        with pytest.raises(OSError):
            await server.start()
        await server.stop()
    finally:
        squatter.close()
