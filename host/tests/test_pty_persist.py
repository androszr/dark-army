# host/tests/test_pty_persist.py
"""A Dark Army restart reconnects to live hosted terminals.

The broker process holds the master fd; a new PtyHost(persist=True) talks
to the same socket and restores the emulator grid. Close still kills the
child. Under pytest the socket lives in the temp state dir.
"""
from __future__ import annotations

import asyncio
import errno
import os
import signal
import tempfile
import time
from types import SimpleNamespace

import psutil
import pytest

from dark_army_daemon import pty_broker, ptyhost
from dark_army_daemon.paths import PTY_PID_PATH, PTY_SOCK_PATH
from dark_army_daemon.ptyhost import PtyHost

# Never the real fleet's socket: every broker this file starts is killed at
# teardown, so the path it kills has to be the temp one.
assert str(PTY_SOCK_PATH).startswith(tempfile.gettempdir()), PTY_SOCK_PATH


async def _wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return predicate()


def _broker_pid() -> int:
    try:
        return int(PTY_PID_PATH.read_text().strip())
    except (OSError, ValueError):
        return 0


def _alive(pid: int) -> bool:
    """A broker that has exited but not been reaped is gone: waiting five
    seconds twice on a zombie was every test's ten-second teardown."""
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.Error:
        return False


def _kill_broker():
    pid = _broker_pid()
    if pid:
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.kill(pid, sig)
            except OSError:
                break
            deadline = time.time() + 5.0
            while time.time() < deadline and _alive(pid):
                time.sleep(0.02)
            if not _alive(pid):
                break
    try:
        PTY_SOCK_PATH.unlink()
    except OSError:
        pass
    try:
        PTY_PID_PATH.unlink()
    except OSError:
        pass


def _brokers_on(sock: str) -> list:
    """Any process on this machine whose command line names that socket."""
    found = []
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            argv = proc.info.get("cmdline") or []
        except psutil.Error:  # pragma: no cover - a process that just went
            continue
        if any(sock == str(a) for a in argv):
            found.append(proc.info.get("pid"))
    return found


def _short_sock() -> str:
    # AF_UNIX paths are capped at 104 bytes on macOS; pytest's tmp_path is
    # longer than that.
    return os.path.join(tempfile.mkdtemp(prefix="bobpty-", dir="/tmp"), "s.sock")


@pytest.fixture
def persist_host(tmp_path):
    _kill_broker()
    h = PtyHost(persist=True, sock_path=str(PTY_SOCK_PATH),
                pid_path=str(PTY_PID_PATH), ring_bytes=4096)
    ptyhost.install(h)
    yield h
    ptyhost.install(None)
    _kill_broker()
    # A failing test used to leave a broker running for the rest of the day.
    assert _brokers_on(str(PTY_SOCK_PATH)) == []


@pytest.mark.asyncio
async def test_a_second_host_reconnects_and_sees_what_was_typed(persist_host, tmp_path):
    host = persist_host
    ok, detail, pid = await host.start(str(tmp_path), ["/bin/cat"], "cat")
    assert ok, detail
    handle = host.owns(pid)
    assert handle is not None
    assert host.write(handle, "hello\r")
    term = host.get(handle)
    assert await _wait_for(lambda: "hello" in "".join(term.screen.text()))
    await host.disconnect()
    assert psutil.pid_exists(pid)

    other = PtyHost(persist=True, sock_path=str(PTY_SOCK_PATH),
                    pid_path=str(PTY_PID_PATH), ring_bytes=4096)
    assert await other.attach()
    assert other.owns(pid) == handle
    restored = other.get(handle)
    assert restored is not None
    assert "hello" in "".join(restored.screen.text())
    assert restored.pid == pid
    await other.close(handle)
    await other.stop_broker()


@pytest.mark.asyncio
async def test_close_still_kills_the_child(persist_host, tmp_path):
    host = persist_host
    ok, _detail, pid = await host.start(str(tmp_path), ["/bin/cat"], "cat")
    assert ok
    handle = host.owns(pid)
    assert await host.close(handle)
    assert host.owns(pid) is None

    def gone():
        try:
            return psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
        except psutil.NoSuchProcess:
            return True
    assert await _wait_for(gone)
    await host.stop_broker()


@pytest.mark.asyncio
async def test_a_terminal_with_a_big_ring_still_reconnects(persist_host, tmp_path):
    """The greeting carries every ring as base64; one over asyncio's 64 KiB
    default line limit used to raise out of `attach()` and leave the daemon
    on a half-open link where every start timed out as "broker rpc failed"."""
    await persist_host.stop_broker()
    _kill_broker()
    host = PtyHost(persist=True, sock_path=str(PTY_SOCK_PATH),
                   pid_path=str(PTY_PID_PATH), ring_bytes=256 * 1024)
    ptyhost.install(host)
    ok, detail, pid = await host.start(
        str(tmp_path),
        ["/bin/sh", "-c", "head -c 150000 /dev/zero | tr '\\0' x; exec /bin/cat"],
        "wide")
    assert ok, detail
    handle = host.owns(pid)
    term = host.get(handle)
    assert await _wait_for(lambda: term.bytes_read >= 150000, timeout=10.0)
    await host.disconnect()

    other = PtyHost(persist=True, sock_path=str(PTY_SOCK_PATH),
                    pid_path=str(PTY_PID_PATH), ring_bytes=256 * 1024)
    assert await other.attach()
    restored = other.get(handle)
    assert restored is not None
    assert len(restored.ring) > 65536
    ok2, detail2, pid2 = await other.start(str(tmp_path), ["/bin/cat"], "second")
    assert ok2, detail2
    await other.close(handle)
    await other.close(other.owns(pid2))
    await other.stop_broker()


@pytest.mark.asyncio
async def test_an_unreadable_greeting_is_not_kept_as_a_connection(monkeypatch):
    """A hello the reader refuses closes the socket and leaves nothing
    behind, so `attach()` does not answer True over a link with no
    listener."""
    monkeypatch.setattr(ptyhost, "BROKER_LINE_LIMIT", 1024)
    sock = _short_sock()

    async def serve(reader, writer):
        writer.write(b"x" * 4096)  # no newline, over the (patched) limit
        await writer.drain()
        await asyncio.sleep(0.5)
        writer.close()

    server = await asyncio.start_unix_server(serve, path=sock)
    try:
        host = PtyHost(persist=True, sock_path=sock)
        assert await host._broker_connect() == ptyhost.CONNECT_UNREADABLE
        assert host._writer is None and host._reader is None
        assert host._listen_task is None
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_a_request_answers_at_once_when_the_link_drops(tmp_path):
    sock = _short_sock()

    async def serve(reader, writer):
        writer.write(b'{"hello": true, "terminals": []}\n')
        await writer.drain()
        await reader.readline()  # the request
        writer.close()

    server = await asyncio.start_unix_server(serve, path=sock)
    try:
        host = PtyHost(persist=True, sock_path=sock)
        assert await host._broker_connect() == ptyhost.CONNECT_CONNECTED
        started = asyncio.get_running_loop().time()
        reply = await host._rpc("start", root=str(tmp_path), argv=["/bin/cat"], name="x")
        assert reply == {"ok": False, "detail": ptyhost.BROKER_LOST_DETAIL}
        assert asyncio.get_running_loop().time() - started < 2.0
        assert host._pending == {}
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_reconnect_when_the_hello_exceeds_64k(persist_host, tmp_path):
    """The live failure of 6 Sep 2026: three terminals made an 866,927-byte
    greeting, `readline()` at asyncio's 64 KiB default raised, and the new
    daemon ran detached from agents that were still going."""
    await persist_host.stop_broker()
    _kill_broker()
    host = PtyHost(persist=True, sock_path=str(PTY_SOCK_PATH),
                   pid_path=str(PTY_PID_PATH), ring_bytes=256 * 1024)
    ptyhost.install(host)
    ok, detail, pid = await host.start(
        str(tmp_path),
        ["/bin/sh", "-c", "head -c 120000 /dev/zero | tr '\\0' x; exec /bin/cat"],
        "wide")
    assert ok, detail
    handle = host.owns(pid)
    term = host.get(handle)
    assert await _wait_for(lambda: term.bytes_read >= 120000, timeout=10.0)
    assert host.bind(handle, "sess-big")
    await host.disconnect()

    other = PtyHost(persist=True, sock_path=str(PTY_SOCK_PATH),
                    pid_path=str(PTY_PID_PATH), ring_bytes=256 * 1024)
    assert await other.attach()
    restored = other.get(handle)
    assert restored is not None
    assert other.for_session("sess-big") == handle
    assert restored.bytes_read == term.bytes_read
    assert bytes(restored.ring).endswith(b"x")
    assert bytes(restored.ring).count(b"x") >= 120000
    await other.close(handle)
    await other.stop_broker()


@pytest.mark.asyncio
async def test_a_full_read_arrives_whole(persist_host, tmp_path):
    """A 64 KB read is ~87 KB of base64 JSON — one line the daemon could not
    read. Sliced on send, every byte still arrives, once, in order."""
    await persist_host.stop_broker()
    _kill_broker()
    host = PtyHost(persist=True, sock_path=str(PTY_SOCK_PATH),
                   pid_path=str(PTY_PID_PATH), ring_bytes=256 * 1024)
    ptyhost.install(host)
    ok, detail, pid = await host.start(
        str(tmp_path),
        ["/bin/sh", "-c", "head -c 65536 /dev/zero | tr '\\0' y; exec /bin/cat"],
        "burst")
    assert ok, detail
    handle = host.owns(pid)
    term = host.get(handle)
    assert await _wait_for(lambda: term.ring.count(b"y") >= 65536, timeout=10.0)
    assert term.bytes_read >= 65536
    await host.close(handle)
    await host.stop_broker()


@pytest.mark.asyncio
async def test_the_listener_logs_an_oversized_line_and_reattaches(
        monkeypatch, caplog):
    """A line over the limit mid-run is a reconnect, in the log, not a
    silent detach — and `disconnect()` ends the retrying."""
    monkeypatch.setattr(ptyhost, "BROKER_LINE_LIMIT", 4096)
    monkeypatch.setattr(ptyhost, "RECONNECT_FIRST_SECONDS", 0.05)
    sock = _short_sock()
    connections = []

    async def serve(reader, writer):
        connections.append(1)
        writer.write(b'{"hello": true, "terminals": []}\n')
        await writer.drain()
        if len(connections) == 1:
            writer.write(b"z" * 8192)  # no newline, over the patched limit
            await writer.drain()
        try:
            await reader.read()
        except (ConnectionError, OSError):
            pass
        writer.close()

    server = await asyncio.start_unix_server(serve, path=sock)
    caplog.set_level("WARNING")
    host = PtyHost(persist=True, sock_path=sock, pid_path=sock + ".pid")
    try:
        assert await host.attach()
        assert await _wait_for(lambda: len(connections) >= 2, timeout=3.0)
        assert await _wait_for(lambda: host._writer is not None, timeout=3.0)
        assert any("over" in r.message and "reconnecting" in r.message
                   for r in caplog.records)
        await host.disconnect()
        seen = len(connections)
        await asyncio.sleep(0.5)
        assert len(connections) == seen
    finally:
        await host.disconnect()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_the_fixture_leaves_no_broker_behind(persist_host, tmp_path):
    """Deliberately no `stop_broker()`: the fixture's teardown is what kills
    the broker, and it asserts nothing on this machine still names the
    socket. Four brokers from 6 Sep 2026 were this bug."""
    ok, _detail, pid = await persist_host.start(str(tmp_path), ["/bin/cat"], "cat")
    assert ok
    assert _brokers_on(str(PTY_SOCK_PATH))
    assert _broker_pid() > 0


@pytest.mark.asyncio
async def test_a_host_with_no_socket_path_never_spawns_a_broker(monkeypatch):
    """The four argument-less brokers came from an `attach()` that spawned
    on an empty path."""
    spawned = []
    host = PtyHost(persist=True, sock_path="")
    monkeypatch.setattr(host, "_spawn_broker", lambda: spawned.append(1))
    assert await host.attach() is False
    assert spawned == []


@pytest.mark.asyncio
async def test_a_live_but_unreadable_broker_is_never_spawned_over(monkeypatch):
    """Spawning would unlink the live broker's socket and strand its
    terminals for good."""
    monkeypatch.setattr(ptyhost, "BROKER_LINE_LIMIT", 1024)
    monkeypatch.setattr(ptyhost, "RECONNECT_FIRST_SECONDS", 30.0)
    sock = _short_sock()

    async def serve(reader, writer):
        writer.write(b"x" * 4096)
        await writer.drain()
        await asyncio.sleep(0.5)
        writer.close()

    server = await asyncio.start_unix_server(serve, path=sock)
    spawned = []
    host = PtyHost(persist=True, sock_path=sock)
    monkeypatch.setattr(host, "_spawn_broker", lambda: spawned.append(1))
    try:
        assert await host.attach() is False
        assert spawned == []
    finally:
        await host.disconnect()
        server.close()
        await server.wait_closed()


# ── a broker nobody can reach retires itself ─────────────────────────────────


@pytest.fixture
def stranded_broker(tmp_path):
    """A `Broker` object with a real socket *file* under `tmp_path` and one
    live terminal. No child broker process and no bind: every rung of
    `_should_exit()` is decided on this object."""
    from dark_army_daemon import pty_broker

    sock = tmp_path / "s.sock"
    sock.write_text("")
    broker = pty_broker.Broker(0, str(sock), str(tmp_path / "s.pid"))
    st = os.stat(sock)
    broker._sock_ident = (st.st_dev, st.st_ino)
    broker.engine.terminals = lambda: [SimpleNamespace(exited=False)]
    yield broker, sock
    ptyhost.install(None)


def test_a_broker_on_its_own_socket_is_not_stranded(stranded_broker):
    broker, _sock = stranded_broker
    assert broker._stranded() is False
    assert broker._should_exit() is False


def test_a_stranded_broker_exits_despite_a_live_terminal(stranded_broker):
    """The bug: the live-terminal rung used to make the unreachable rung
    dead code for every broker actually doing its job."""
    broker, sock = stranded_broker
    sock.unlink()
    assert broker._stranded() is True
    assert broker._should_exit() is True


def test_a_replaced_socket_leaves_the_older_broker_stranded(stranded_broker):
    """A later broker's socket at the same path is a different inode, and
    means exactly the same thing: nobody will reach this process again."""
    broker, sock = stranded_broker
    sock.unlink()
    sock.write_text("")
    assert os.stat(sock)[1] != broker._sock_ident[1]
    assert broker._stranded() is True
    assert broker._should_exit() is True


def test_an_unrecorded_socket_identity_is_never_stranded(stranded_broker):
    """Never guess about a door we never got a reading of."""
    broker, sock = stranded_broker
    broker._sock_ident = None
    sock.unlink()
    assert broker._stranded() is False
    assert broker._should_exit() is False


def test_a_connected_daemon_is_never_stranded(stranded_broker):
    """`os.stat` proves nobody can *find* this broker, not that nobody is
    talking to it — so a live writer outranks the missing path."""
    broker, sock = stranded_broker
    sock.unlink()
    broker._writer = object()
    assert broker._stranded() is False
    assert broker._should_exit() is False


def test_a_stat_that_cannot_answer_leaves_the_broker_up(stranded_broker,
                                                        monkeypatch):
    """"Cannot tell" is not "unreachable". This rung closes live terminals,
    so only a stat that answers *there is nothing there* may fire it: a
    transient EIO, or a volume that briefly went away, must not."""
    broker, _sock = stranded_broker
    calls = []

    def _stat(path, *a, **kw):
        calls.append(path)
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(pty_broker.os, "stat", _stat)
    assert broker._stranded() is False
    assert broker._should_exit() is False
    assert broker._stranded() is False           # and again, still up
    assert calls                                 # the stat was really tried


def test_a_missing_socket_still_strands_the_broker(stranded_broker,
                                                   monkeypatch):
    """The narrowing above must not blunt the rung it guards."""
    broker, _sock = stranded_broker

    def _stat(path, *a, **kw):
        raise FileNotFoundError(errno.ENOENT, "No such file or directory")

    monkeypatch.setattr(pty_broker.os, "stat", _stat)
    assert broker._stranded() is True
    assert broker._should_exit() is True


def test_the_hello_names_the_broker_pid(stranded_broker):
    """The daemon learns which pid it is connected to, so its sweep can
    tell that one apart from every other."""
    import inspect
    from dark_army_daemon import pty_broker
    assert '"pid": os.getpid()' in inspect.getsource(pty_broker.Broker._client)


# ── the daemon's one-shot backstop sweep ─────────────────────────────────────


class _FakeProc:
    def __init__(self, pid, argv):
        self.info = {"pid": pid, "cmdline": list(argv),
                     "uids": SimpleNamespace(real=os.getuid())}


def _broker_argv(sock: str) -> list:
    return ["python", "-m", ptyhost.BROKER_MODULE, "--sock", sock,
            "--pidfile", sock + ".pid"]


def test_the_sweep_spares_the_connected_and_recorded_brokers(monkeypatch, tmp_path):
    sock = tmp_path / "s.sock"
    pidfile = tmp_path / "s.pid"
    pidfile.write_text("222")
    host = PtyHost(persist=True, sock_path=str(sock), pid_path=str(pidfile))
    host._broker_pid = 111
    monkeypatch.setattr(
        ptyhost.psutil, "process_iter",
        lambda attrs=None: [_FakeProc(pid, _broker_argv(str(sock)))
                            for pid in (111, 222, 333)])
    signalled = []
    monkeypatch.setattr(host, "_signal_stray", signalled.append)
    assert host._sweep_strays() == [333]
    assert signalled == [333]


def test_the_sweep_does_nothing_when_it_cannot_name_the_live_broker(
        monkeypatch, tmp_path):
    """Not knowing which one is the live one means sweeping nothing."""
    sock = tmp_path / "s.sock"
    host = PtyHost(persist=True, sock_path=str(sock),
                   pid_path=str(tmp_path / "absent.pid"))
    host._broker_pid = 0
    walked = []

    def _iter(attrs=None):
        walked.append(1)
        return []
    monkeypatch.setattr(ptyhost.psutil, "process_iter", _iter)
    signalled = []
    monkeypatch.setattr(host, "_signal_stray", signalled.append)
    assert host._sweep_strays() == []
    assert signalled == []
    assert walked == []


def test_a_pid_file_alone_never_enables_the_sweep(monkeypatch, tmp_path):
    """The hello pid is the only positive identification, so it is the only
    enabling condition. A broker built before the hello carried a pid sends
    none; a stale `pty.pid` from a previous boot naming a recycled number
    must not turn that broker into a target."""
    sock = tmp_path / "s.sock"
    pidfile = tmp_path / "s.pid"
    pidfile.write_text("222")                    # stale, names nothing live
    host = PtyHost(persist=True, sock_path=str(sock), pid_path=str(pidfile))
    host._broker_pid = 0                         # a pre-change broker's hello
    walked = []

    def _iter(attrs=None):
        walked.append(1)
        return [_FakeProc(333, _broker_argv(str(sock)))]

    monkeypatch.setattr(ptyhost.psutil, "process_iter", _iter)
    signalled = []
    monkeypatch.setattr(host, "_signal_stray", signalled.append)
    assert host._sweep_strays() == []
    assert signalled == []
    assert walked == []                          # not even the walk


def test_the_spawned_argv_names_the_module_the_sweep_matches(monkeypatch,
                                                             tmp_path):
    """`_spawn_broker` and `stray_broker_pids` have to agree on the module
    name, or a rename blinds the sweep with every test still green."""
    sock = tmp_path / "s.sock"
    host = PtyHost(persist=True, sock_path=str(sock),
                   pid_path=str(tmp_path / "s.pid"))
    seen = []

    def _popen(argv, **kw):
        seen.append(list(argv))
        raise OSError("not really spawning")     # logged, never raised

    monkeypatch.setattr(ptyhost.subprocess, "Popen", _popen)
    host._spawn_broker()
    assert seen, "no spawn attempted"
    argv = seen[0]
    assert ptyhost.BROKER_MODULE in argv
    assert argv[argv.index(ptyhost.BROKER_MODULE) - 1] == "-m"
    # And the whole rule end to end: this argv is what the classifier reads.
    assert ptyhost.stray_broker_pids(
        [(4242, argv)], str(sock), {os.getpid()}) == [4242]


def test_the_sweep_signals_sigterm_and_never_a_process_group():
    import inspect
    src = inspect.getsource(PtyHost._signal_stray)
    assert "SIGTERM" in src
    assert "killpg" not in src and "SIGKILL" not in src


def test_the_sweep_leaves_a_pid_that_is_no_longer_that_broker(
        monkeypatch, tmp_path):
    """The scan named the pid earlier; a stray that exited in between frees
    its number for anything on the machine to inherit, so the argv is proved
    once more immediately before the signal."""
    sock = tmp_path / "s.sock"
    pidfile = tmp_path / "s.pid"
    pidfile.write_text("222")
    host = PtyHost(persist=True, sock_path=str(sock), pid_path=str(pidfile))
    killed = []
    monkeypatch.setattr(ptyhost.os, "kill",
                        lambda pid, sig: killed.append((pid, sig)))

    class _Recycled:
        def __init__(self, pid):
            self.pid = pid

        def cmdline(self):
            return ["/bin/zsh", "-l"]

    monkeypatch.setattr(ptyhost.psutil, "Process", _Recycled)
    assert host._signal_stray(333) is False
    assert killed == []

    class _StillTheBroker:
        def __init__(self, pid):
            self.pid = pid

        def cmdline(self):
            return _broker_argv(str(sock))

    monkeypatch.setattr(ptyhost.psutil, "Process", _StillTheBroker)
    assert host._signal_stray(333) is True
    assert killed == [(333, signal.SIGTERM)]


def test_the_sweep_leaves_a_pid_that_has_already_gone(monkeypatch, tmp_path):
    sock = tmp_path / "s.sock"
    host = PtyHost(persist=True, sock_path=str(sock))
    killed = []
    monkeypatch.setattr(ptyhost.os, "kill",
                        lambda pid, sig: killed.append(pid))

    def _gone(pid):
        raise psutil.NoSuchProcess(pid)
    monkeypatch.setattr(ptyhost.psutil, "Process", _gone)
    assert host._signal_stray(333) is False
    assert killed == []


def test_the_sweep_skips_a_process_whose_owner_cannot_be_read(
        monkeypatch, tmp_path):
    """psutil answers `None` where the platform refused; that is not
    ownership, and over-excluding is the safe direction for a signal."""
    sock = tmp_path / "s.sock"
    pidfile = tmp_path / "s.pid"
    pidfile.write_text("222")
    host = PtyHost(persist=True, sock_path=str(sock), pid_path=str(pidfile))
    denied = _FakeProc(333, _broker_argv(str(sock)))
    denied.info["uids"] = None
    monkeypatch.setattr(ptyhost.psutil, "process_iter",
                        lambda attrs=None: [denied])
    signalled = []
    monkeypatch.setattr(host, "_signal_stray", signalled.append)
    assert host._sweep_strays() == []
    assert signalled == []


@pytest.mark.asyncio
async def test_attach_sweeps_after_spawning_its_own_broker(
        persist_host, monkeypatch):
    """The principal case: a stranded broker no longer listens, so the first
    connect is refused, a fresh broker is spawned — and *that* attach has to
    sweep. Before this, `_swept` stayed False past the spawn and the sweep
    only ever ran after a dropped link."""
    host = persist_host
    stray = 999_999
    monkeypatch.setattr(
        ptyhost.psutil, "process_iter",
        lambda attrs=None: [_FakeProc(stray, _broker_argv(host.sock_path))])
    signalled = []
    monkeypatch.setattr(host, "_signal_stray", signalled.append)
    assert await host.attach() is True           # no broker running: spawns
    assert host._broker_pid > 0                  # the new broker named itself
    assert signalled == [stray]
    await host.stop_broker()
    # The fixture's teardown walks the real process table for a leftover
    # broker; it must not be handed this test's fake one.
    monkeypatch.undo()


@pytest.mark.asyncio
async def test_attach_sweeps_once(persist_host, tmp_path, monkeypatch):
    """`attach()` is called from `_broker_start`, `_rpc`, the reconnect loop
    and the daemon; the sweep is one shot per host."""
    host = persist_host
    calls = []
    monkeypatch.setattr(host, "_sweep_strays", lambda: calls.append(1) or [])
    assert await host.attach() is True          # spawns the broker, and sweeps
    await host.disconnect()
    assert await host.attach() is True          # reconnects, sweeps nothing
    assert await host.attach() is True          # the writer is already open
    assert calls == [1]
    await host.stop_broker()


@pytest.mark.asyncio
async def test_a_failing_sweep_never_stops_an_attach(persist_host, monkeypatch):
    def _boom():
        raise RuntimeError("no")
    monkeypatch.setattr(persist_host, "_sweep_strays", _boom)
    assert await persist_host.attach() is True
    await persist_host.stop_broker()


# ── the private hook socket rides the broker ─────────────────────────────────


class _Captured:
    """A writer that keeps what the broker sends."""

    def __init__(self):
        self.lines = []

    def write(self, data: bytes) -> None:
        self.lines.append(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        return None


class _Silent:
    async def readline(self) -> bytes:
        return b""


@pytest.mark.asyncio
async def test_the_hello_carries_the_hook_socket(tmp_path):
    """The daemon learns what the broker tells its terminals — the socket
    from a broker spawned with `--hook-sock`, `""` from one without."""
    import json
    for given, said in (("/tmp/x/hook.sock", "/tmp/x/hook.sock"), ("", "")):
        broker = pty_broker.Broker(0, str(tmp_path / "s.sock"),
                                   str(tmp_path / "s.pid"), given)
        try:
            writer = _Captured()
            await broker._client(_Silent(), writer)
            hello = json.loads(writer.lines[0].decode("utf-8"))
            assert hello["hello"] is True
            assert hello["hook_sock"] == said
            assert broker.engine.hook_sock == said
        finally:
            ptyhost.install(None)


def test_the_broker_takes_hook_sock_on_its_command_line(monkeypatch, tmp_path):
    seen = {}

    class _Stop(Exception):
        pass

    def _broker(port, sock, pidfile, hook_sock=""):
        seen.update(port=port, sock=sock, hook_sock=hook_sock)
        raise _Stop()

    monkeypatch.setattr(pty_broker, "Broker", _broker)
    monkeypatch.setattr(pty_broker.signal, "signal", lambda *a: None)
    with pytest.raises(_Stop):
        pty_broker.main(["--port", "4242", "--hook-sock", "/tmp/h/hook.sock",
                         "--sock", str(tmp_path / "s.sock"),
                         "--pidfile", str(tmp_path / "s.pid")])
    assert seen["hook_sock"] == "/tmp/h/hook.sock"
    assert seen["port"] == 4242


async def _hello_host(hello: bytes, hook_sock):
    """A stand-in broker that greets and then holds each connection open
    for at most two seconds, so a replaced link never pins the server."""
    sock = _short_sock()

    async def serve(reader, writer):
        writer.write(hello)
        await writer.drain()
        try:
            await asyncio.wait_for(reader.readline(), timeout=2.0)
        except (asyncio.TimeoutError, OSError):
            pass
        writer.close()

    server = await asyncio.start_unix_server(serve, path=sock)
    return server, PtyHost(persist=True, sock_path=sock, hook_sock=hook_sock)


@pytest.mark.asyncio
async def test_an_old_broker_is_named_once_at_attach(caplog):
    """A broker from before the socket keeps handing its terminals the port
    for as long as it lives — said at attach, once, never "fixed"."""
    import logging
    caplog.set_level(logging.WARNING, logger=ptyhost.logger.name)
    server, host = await _hello_host(b'{"hello": true, "pid": 0, "terminals": []}\n',
                                     "/tmp/x/hook.sock")
    try:
        for _ in range(2):
            assert await host._broker_connect() == ptyhost.CONNECT_CONNECTED
        warned = [r for r in caplog.records
                  if "predates the private hook socket" in r.getMessage()]
        assert len(warned) == 1
        assert host.broker_hook_sock == ""
    finally:
        await host.disconnect()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_a_current_broker_is_not_warned_about(caplog):
    import logging
    caplog.set_level(logging.WARNING, logger=ptyhost.logger.name)
    server, host = await _hello_host(
        b'{"hello": true, "pid": 0, "hook_sock": "/tmp/x/hook.sock", "terminals": []}\n',
        "/tmp/x/hook.sock")
    try:
        assert await host._broker_connect() == ptyhost.CONNECT_CONNECTED
        assert host.broker_hook_sock == "/tmp/x/hook.sock"
        assert not [r for r in caplog.records
                    if "predates the private hook socket" in r.getMessage()]
    finally:
        await host.disconnect()
        server.close()
        await server.wait_closed()


async def _start_through(hello: bytes) -> list:
    """The argv a stand-in broker greeting with ``hello`` is asked to run."""
    import json
    sock = _short_sock()
    seen: list = []

    async def serve(reader, writer):
        writer.write(hello)
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout=2.0)
        msg = json.loads(line)
        seen.append(msg["argv"])
        writer.write((json.dumps({"id": msg["id"], "ok": True, "pid": 1,
                                  "handle": ""}) + "\n").encode())
        await writer.drain()
        writer.close()

    server = await asyncio.start_unix_server(serve, path=sock)
    host = PtyHost(persist=True, sock_path=sock, hook_sock="/tmp/x/hook.sock")
    try:
        ok, _detail, _pid = await host.start("/tmp", ["/bin/echo", "hi"], "x")
        assert ok
    finally:
        await host.disconnect()
        server.close()
        await server.wait_closed()
    return seen[0]


@pytest.mark.asyncio
async def test_an_old_broker_is_handed_a_login_path_in_the_argv(monkeypatch):
    """A broker from before `adopt_login_path` gives its children launchd's
    bare PATH and cannot be told otherwise through `env`, so the agent is
    started through `/usr/bin/env PATH=…` (25 Sep 2026: `node` missing)."""
    from dark_army_daemon import subprocess_env
    monkeypatch.setattr(subprocess_env, "login_path_entries",
                        lambda *a: ["/opt/homebrew/bin"])
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    argv = await _start_through(b'{"hello": true, "pid": 0, "terminals": []}\n')
    assert argv == ["/usr/bin/env", "PATH=/usr/bin:/bin:/opt/homebrew/bin",
                    "/bin/echo", "hi"]


@pytest.mark.asyncio
async def test_a_current_broker_is_handed_the_argv_unchanged():
    # The key's presence is the mark, an empty value included.
    argv = await _start_through(
        b'{"hello": true, "pid": 0, "hook_sock": "", "terminals": []}\n')
    assert argv == ["/bin/echo", "hi"]


def test_the_login_path_argv_runs_with_that_path_and_keeps_its_pid():
    """What the old broker does with the argv: exec it under a bare PATH.
    `env` sets PATH and execs in place, so the child sees the login PATH
    and the pid the broker reports is the program's own."""
    import subprocess
    host = PtyHost(persist=True, sock_path=_short_sock())
    argv = host._login_path_argv(
        ["/bin/sh", "-c", 'echo "$$ $PATH"; ps -o comm= -p $$'])
    proc = subprocess.Popen(argv, env={"PATH": "/usr/bin:/bin"},
                            stdout=subprocess.PIPE, text=True)
    out, _ = proc.communicate(timeout=10)
    first, comm = out.strip().splitlines()
    pid, path = first.split(" ", 1)
    assert int(pid) == proc.pid
    assert "sh" in comm
    from dark_army_daemon import subprocess_env
    for entry in subprocess_env.login_path_entries():
        assert entry in path.split(":")
