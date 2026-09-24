# host/tests/test_ptyhost.py
"""The pty Dark Army owns: lifecycle, the ring bound, resize, close and `owns()`.

Over stub argvs (`/bin/cat`, `/bin/sh`) — no agent needed. Every test
closes what it started; a leaked child here would be exactly the orphan
the module exists to prevent.
"""

from __future__ import annotations

import asyncio
import os
import signal

import psutil
import pytest

from dark_army_daemon import ptyhost
from dark_army_daemon.ptyhost import PtyHost


async def _wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return predicate()


@pytest.fixture
def host():
    h = PtyHost(ring_bytes=4096)
    ptyhost.install(h)
    yield h
    ptyhost.install(None)
    # Belt and braces: nothing this file started may outlive it.
    for term in h.terminals():
        try:
            os.killpg(term.pid, signal.SIGKILL)
        except OSError:
            pass


@pytest.mark.asyncio
async def test_start_writes_and_reads_back_what_was_typed(host, tmp_path):
    ok, detail, pid = await host.start(str(tmp_path), ["/bin/cat"], "cat")
    assert ok, detail
    assert detail == "cat"
    assert psutil.pid_exists(pid)
    handle = host.owns(pid)
    assert handle is not None
    assert host.write(handle, "hello\r")
    term = host.get(handle)
    assert await _wait_for(lambda: b"hello" in term.ring)
    # The emulator saw the same bytes the ring did.
    assert await _wait_for(lambda: "hello" in "".join(term.screen.text()))
    assert term.cols == ptyhost.DEFAULT_COLS and term.rows == ptyhost.DEFAULT_ROWS
    assert await ptyhost.close(handle) == {
        "matched": True, "terminalName": "cat", "matchedBy": "pty"}


@pytest.mark.asyncio
async def test_the_ring_is_bounded_under_more_output_than_it_holds(host, tmp_path):
    ok, _detail, pid = await host.start(str(tmp_path), ["/bin/cat"], "cat")
    assert ok
    handle = host.owns(pid)
    term = host.get(handle)
    # Short lines with a breath between them: the tty's canonical-mode line
    # buffer, not Dark Army, drops a line longer than it holds.
    for _ in range(150):
        host.write(handle, "x" * 100 + "\r")
        await asyncio.sleep(0.005)
    assert await _wait_for(lambda: term.bytes_read > 8000)
    assert len(term.ring) <= 4096
    await host.close(handle)


@pytest.mark.asyncio
async def test_resize_changes_what_the_child_sees(host, tmp_path):
    ok, _detail, pid = await host.start(
        str(tmp_path), ["/bin/sh", "-c", "while read l; do stty size; done"], "sh")
    assert ok
    handle = host.owns(pid)
    term = host.get(handle)
    assert host.resize(handle, 100, 30)
    assert (term.cols, term.rows) == (100, 30)
    assert (term.screen.cols, term.screen.rows) == (100, 30)
    host.write(handle, "\r")
    assert await _wait_for(lambda: b"30 100" in term.ring), term.ring
    await host.close(handle)


@pytest.mark.asyncio
async def test_close_reaps_the_process_group_and_owns_is_false_after(host, tmp_path):
    ok, _detail, pid = await host.start(
        str(tmp_path), ["/bin/sh", "-c", "sleep 30; sleep 30"], "sh")
    assert ok
    handle = host.owns(pid)
    assert await _wait_for(lambda: bool(psutil.Process(pid).children()), 3.0)
    child = psutil.Process(pid).children()[0].pid
    # Descent: the child of the process Dark Army started is Dark Army's too.
    assert host.owns(child) == handle
    assert await host.close(handle, grace=1.0)
    assert host.owns(pid) is None
    assert host.owns(child) is None
    assert host.get(handle) is None

    def gone(p):
        try:
            return psutil.Process(p).status() == psutil.STATUS_ZOMBIE
        except psutil.NoSuchProcess:
            return True
    assert await _wait_for(lambda: gone(pid) and gone(child), 5.0)


@pytest.mark.asyncio
async def test_a_child_that_exits_on_its_own_is_marked_and_reaped(host, tmp_path):
    ok, _detail, pid = await host.start(str(tmp_path), ["/bin/sh", "-c", "exit 3"], "sh")
    assert ok
    handle = host.owns(pid)
    term = host.get(handle)
    assert await _wait_for(lambda: term.exited)
    assert await _wait_for(lambda: term.returncode == 3)
    # An exited terminal owns nothing: its pid may be somebody else's now.
    assert host.owns(pid) is None
    assert host.write(handle, "x") is False
    assert ptyhost.send(handle, "x") is None
    # Kept for the pane until the retention lapses, then forgotten.
    host.reap()
    assert host.get(handle) is not None
    host.reap(now=term.exited_at + ptyhost.EXITED_RETENTION_SECONDS + 1)
    assert host.get(handle) is None


@pytest.mark.asyncio
async def test_a_missing_executable_refuses_in_words_and_opens_nothing(host, tmp_path):
    ok, detail, pid = await host.start(str(tmp_path), ["/nonexistent/bin/agent"], "x")
    assert ok is False and pid is None
    assert "could not start agent" in detail
    assert len(host) == 0


@pytest.mark.asyncio
async def test_a_missing_folder_and_an_empty_argv_refuse(host, tmp_path):
    ok, detail, _ = await host.start(str(tmp_path / "gone"), ["/bin/cat"], "x")
    assert ok is False and "not there" in detail
    ok, detail, _ = await host.start(str(tmp_path), [], "x")
    assert ok is False and detail == "nothing to start"


@pytest.mark.asyncio
async def test_bind_names_the_terminal_after_its_session(host, tmp_path):
    ok, _detail, pid = await host.start(str(tmp_path), ["/bin/cat"], "cat")
    assert ok
    handle = host.owns(pid)
    assert host.for_session("s1") is None
    assert host.bind(handle, "s1")
    assert host.for_session("s1") == handle
    assert host.bind("no-such-handle", "s2") is False
    await host.close(handle)
    assert host.for_session("s1") is None


@pytest.mark.asyncio
async def test_the_child_environment_names_the_terminal_and_the_port(tmp_path):
    host = PtyHost(port=19999)
    ok, _detail, pid = await host.start(
        str(tmp_path), ["/bin/sh", "-c", 'echo "$TERM $BOB_COMPANION_PORT $COLUMNS $LINES"; sleep 5'],
        "env")
    assert ok
    handle = host.owns(pid)
    term = host.get(handle)
    expected = f"{ptyhost.TERM} 19999 {ptyhost.DEFAULT_COLS} {ptyhost.DEFAULT_ROWS}".encode()
    assert await _wait_for(lambda: expected in term.ring), term.ring
    await host.close(handle, grace=0.5)


@pytest.mark.asyncio
async def test_the_child_inherits_no_session_but_keeps_the_callers_stamp(tmp_path, monkeypatch):
    """The broker keeps the environment of whichever press relaunched the
    app. A child on its pty gets none of that session's identity, and the
    origin stamp it carries is the caller's, never the inherited one."""
    from dark_army_daemon import origin
    monkeypatch.setenv("GROK_SESSION_ID", "01a094cd-f0fa")
    monkeypatch.setenv("GROK_AGENT", "1")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "somebody-else")
    monkeypatch.setenv("TERM_PROGRAM", "vscode")
    monkeypatch.setenv(origin.ENV_VAR, "card-refine|abc|bc-planner")
    host = PtyHost(port=19999)
    script = ('echo "[${GROK_SESSION_ID}|${GROK_AGENT}|${CLAUDE_CODE_SESSION_ID}'
              '|${TERM_PROGRAM}|${BOB_COMPANION_ORIGIN}]"; sleep 5')
    ok, _detail, pid = await host.start(
        str(tmp_path), ["/bin/sh", "-c", script], "env",
        env=origin.env_with(os.environ, origin.stamp("adhoc")))
    assert ok
    handle = host.owns(pid)
    term = host.get(handle)
    assert await _wait_for(lambda: b"[||||adhoc||]" in term.ring), term.ring
    await host.close(handle, grace=0.5)

    # And with no stamp handed in, the inherited one goes too.
    ok, _detail, pid = await host.start(
        str(tmp_path), ["/bin/sh", "-c", script], "env")
    assert ok
    handle = host.owns(pid)
    term = host.get(handle)
    assert await _wait_for(lambda: b"[||||]" in term.ring), term.ring
    await host.close(handle, grace=0.5)


def test_owns_answers_none_for_nonsense_and_with_no_host():
    host = PtyHost()
    assert host.owns(None) is None
    assert host.owns("x") is None
    assert host.owns(0) is None
    assert host.owns(os.getpid()) is None
    ptyhost.install(None)
    assert ptyhost.owns(os.getpid()) is None
    assert ptyhost.send("h", "x") is None


def test_the_line_limit_covers_the_hello():
    """The greeting carries every terminal's whole ring plus its screen; the
    limit is that worst case, and the `start` and `reap` replies share it."""
    assert ptyhost.BROKER_LINE_LIMIT >= ptyhost.HELLO_TERMINALS_HEADROOM * (
        ptyhost.RING_BYTES * 4 // 3 + ptyhost.HELLO_SCREEN_HEADROOM_BYTES)


def test_a_data_frame_fits_an_old_daemon():
    """A downgrade still reads a new broker: base64 of one frame plus its
    JSON framing stays under asyncio's default 64 KiB line limit."""
    assert ptyhost.DATA_FRAME_BYTES * 4 // 3 + 128 < 65536


def test_nothing_here_reaches_a_file_under_paths():
    """Memory only: the ring is every secret an agent ever printed."""
    import inspect
    src = inspect.getsource(ptyhost)
    assert "from . import paths" not in src
    assert "from .paths import" not in src
    # The broker socket is a rendezvous, not a transcript. `openpty` is
    # the in-process engine; `open_unix_connection` is the persist client.
    stripped = (src.replace("openpty(", "")
                .replace("open_unix_connection(", "")
                .replace("subprocess.Popen(", ""))
    assert "open(" not in stripped


@pytest.mark.asyncio
async def test_reap_from_a_worker_thread_removes_the_reader_on_the_loop(host, tmp_path):
    """The reap used to run on the titles worker and close the master under a
    live selector key. Off-loop it now defers to the loop, and the fd comes
    off the selector — reader and writer — before it is closed."""
    ok, _detail, pid = await host.start(str(tmp_path), ["/bin/cat"], "cat")
    assert ok
    handle = host.owns(pid)
    term = host.get(handle)
    assert term.reading is True
    loop = asyncio.get_running_loop()
    assert term.master in loop._selector.get_map()
    os.killpg(pid, signal.SIGKILL)
    assert await _wait_for(lambda: term.exited)
    term.exited_at -= ptyhost.EXITED_RETENTION_SECONDS + 1
    fd = term.master
    await loop.run_in_executor(None, host.reap)
    # Deferred, so nothing happened on the worker itself…
    for _ in range(20):
        if host.get(handle) is None:
            break
        await asyncio.sleep(0.01)
    assert host.get(handle) is None
    assert term.reading is False and term.writing is False
    assert fd not in loop._selector.get_map()
    assert host.owns(pid) is None


@pytest.mark.asyncio
async def test_a_write_the_pty_does_not_take_at_once_is_queued_and_drained(tmp_path):
    """Over a pipe nobody reads, so the first write is partial: the rest is
    owed (`pending`), drained on the loop once the reader catches up, and
    `write` never reports it as the terminal being gone."""
    from dark_army_daemon.ptyhost import PtyTerminal
    from dark_army_daemon.vtgrid import Screen
    h = PtyHost(ring_bytes=4096)
    r, w = os.pipe()
    os.set_blocking(w, False)
    os.set_blocking(r, False)
    h._loop = asyncio.get_running_loop()
    term = PtyTerminal(handle="pty-pipe", name="pipe", root=str(tmp_path), pid=0,
                       master=w, proc=None, screen=Screen(80, 24), cols=80, rows=24)
    h._terms[term.handle] = term
    try:
        big = "a" * 200_000 + "\r"
        assert h.write(term.handle, big) is True
        assert h.pending(term.handle) > 0
        assert term.writing is True
        assert await h.drain(term.handle, 0.1) is False
        # Order is kept: a second line queues behind the first.
        assert h.write(term.handle, "second\r") is True
        got = bytearray()
        for _ in range(2000):
            try:
                got += os.read(r, 65536)
            except BlockingIOError:
                pass
            await asyncio.sleep(0.002)
            if h.pending(term.handle) == 0:
                break
        assert await h.drain(term.handle, 1.0) is True
        while True:
            try:
                got += os.read(r, 65536)
            except BlockingIOError:
                break
        assert got == big.encode() + b"second\r"
        assert term.writing is False
    finally:
        h._detach(term)
        os.close(r)
        os.close(w)


@pytest.mark.asyncio
async def test_bind_never_renames_a_terminal_another_session_holds(host, tmp_path):
    ok, _detail, pid = await host.start(str(tmp_path), ["/bin/cat"], "cat")
    assert ok
    handle = host.owns(pid)
    assert host.bind(handle, "s1")
    assert host.bind(handle, "s1")          # idempotent for the same session
    assert host.bind(handle, "s2") is False
    assert host.for_session("s1") == handle
    assert host.for_session("s2") is None
    assert host.get(handle).session_id == "s1"
    await host.close(handle)


@pytest.mark.asyncio
async def test_unbind_frees_the_name_so_a_successor_can_take_it(host, tmp_path):
    """`/clear` ends a session and starts a fresh id in the same process, so
    the terminal has to be re-nameable — but only once the old id is
    forgotten, never while Dark Army still holds it."""
    ok, _detail, pid = await host.start(str(tmp_path), ["/bin/cat"], "cat")
    assert ok
    handle = host.owns(pid)
    assert host.bind(handle, "old-sid")
    assert host.bind(handle, "new-sid") is False     # still held: refused
    host.unbind("old-sid")
    assert host.for_session("old-sid") is None
    assert host.get(handle).session_id == ""
    assert host.owns(pid) == handle                  # the terminal itself stays
    assert host.bind(handle, "new-sid")
    assert host.for_session("new-sid") == handle
    assert host.get(handle).session_id == "new-sid"
    await host.close(handle)


@pytest.mark.asyncio
async def test_unbind_of_an_unknown_session_is_a_no_op(host, tmp_path):
    ok, _detail, pid = await host.start(str(tmp_path), ["/bin/cat"], "cat")
    assert ok
    handle = host.owns(pid)
    assert host.bind(handle, "s1")
    host.unbind("nobody")
    host.unbind("")
    assert host.for_session("s1") == handle
    assert host.get(handle).session_id == "s1"
    await host.close(handle)


@pytest.mark.asyncio
async def test_unbind_leaves_an_exited_terminal_named(host, tmp_path):
    """A dead child gets no successor, and the ordinary exit forgets the
    session within a tick — the name is what keeps the finished row's last
    screen and its Close terminal for the retention window."""
    ok, _detail, pid = await host.start(str(tmp_path), ["/bin/cat"], "cat")
    assert ok
    handle = host.owns(pid)
    assert host.bind(handle, "s1")
    os.kill(pid, signal.SIGKILL)
    for _ in range(200):
        if host.get(handle).exited:
            break
        await asyncio.sleep(0.02)
    assert host.get(handle).exited
    host.unbind("s1")
    assert host.for_session("s1") == handle
    assert host.get(handle).session_id == "s1"
    await host.close(handle)


# ── the stray-broker classifier ──────────────────────────────────────────────
#
# Pure and table-driven: no process is listed, walked or signalled here.

_M = ptyhost.BROKER_MODULE


def _argv(*extra) -> list:
    return ["python", "-m", _M, *extra]


def test_stray_an_exact_broker_on_this_socket_is_a_stray():
    assert ptyhost.stray_broker_pids(
        [(9, _argv("--sock", "/tmp/x/pty.sock"))], "/tmp/x/pty.sock", {1}) == [9]


def test_stray_the_single_token_sock_form_is_a_stray():
    assert ptyhost.stray_broker_pids(
        [(9, _argv("--sock=/tmp/x/pty.sock"))], "/tmp/x/pty.sock", {1}) == [9]


def test_stray_a_live_pid_is_never_a_stray():
    assert ptyhost.stray_broker_pids(
        [(9, _argv("--sock", "/tmp/x/pty.sock"))], "/tmp/x/pty.sock", {9}) == []


def test_stray_a_different_socket_is_not_a_stray():
    assert ptyhost.stray_broker_pids(
        [(9, _argv("--sock", "/tmp/other.sock"))], "/tmp/x/pty.sock", {1}) == []


def test_stray_an_argument_less_broker_is_not_a_stray():
    """It derived its own path in its own process; we cannot prove which."""
    assert ptyhost.stray_broker_pids([(9, _argv())], "/tmp/x/pty.sock", {1}) == []


def test_stray_the_module_name_without_a_dash_m_is_not_a_stray():
    """`grep`, an editor and this project's own tests all name the module."""
    sock = "/tmp/x/pty.sock"
    assert ptyhost.stray_broker_pids(
        [(9, ["grep", _M, "--sock", sock])], sock, {1}) == []
    assert ptyhost.stray_broker_pids(
        [(9, ["python", "-c", "import " + _M, "--sock", sock])], sock, {1}) == []


def test_stray_a_trailing_sock_with_no_value_is_not_a_stray():
    assert ptyhost.stray_broker_pids(
        [(9, _argv("--sock"))], "/tmp/x/pty.sock", {1}) == []


def test_stray_no_socket_path_sweeps_nothing():
    assert ptyhost.stray_broker_pids(
        [(9, _argv("--sock", "/tmp/x/pty.sock"))], "", {1}) == []


def test_stray_a_symlinked_socket_path_still_matches(tmp_path):
    real = tmp_path / "real.sock"
    real.write_text("")
    link = tmp_path / "link.sock"
    link.symlink_to(real)
    assert ptyhost.stray_broker_pids(
        [(9, _argv("--sock", str(link)))], str(real), {1}) == [9]


def test_stray_the_sweep_is_bounded(monkeypatch):
    """A pathological machine cannot turn one attach into a hundred signals."""
    sock = "/tmp/x/pty.sock"
    entries = [(pid, _argv("--sock", sock)) for pid in range(100, 130)]
    found = ptyhost.stray_broker_pids(entries, sock, {1})
    assert len(found) == ptyhost.MAX_STRAY_SWEEP
    assert found == sorted(found)
    assert found[0] == 100


def test_stray_a_negative_or_non_int_pid_is_never_signalled():
    sock = "/tmp/x/pty.sock"
    assert ptyhost.stray_broker_pids(
        [(-1, _argv("--sock", sock)), ("9", _argv("--sock", sock)),
         (None, _argv("--sock", sock))], sock, {1}) == []


# ── the private hook socket ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_child_environment_names_the_terminal_and_the_socket(tmp_path, monkeypatch):
    """A host that knows the private socket tells the child that, and not
    the port: under the one address rule the port would win otherwise."""
    monkeypatch.delenv("BOB_COMPANION_PORT", raising=False)
    monkeypatch.delenv("DARK_ARMY_HOOK_SOCKET", raising=False)
    host = PtyHost(port=19999, hook_sock="/tmp/x.sock")
    ok, _detail, pid = await host.start(
        str(tmp_path), ["/bin/sh", "-c",
                        'echo "[$TERM|$DARK_ARMY_HOOK_SOCKET|$BOB_COMPANION_PORT]"; sleep 5'],
        "env")
    assert ok
    handle = host.owns(pid)
    term = host.get(handle)
    expected = f"[{ptyhost.TERM}|/tmp/x.sock|]".encode()
    assert await _wait_for(lambda: expected in term.ring), term.ring
    await host.close(handle, grace=0.5)


def test_the_spawned_broker_is_told_the_hook_socket(monkeypatch, tmp_path):
    sock = tmp_path / "s.sock"
    host = PtyHost(persist=True, port=19999, hook_sock="/tmp/h/hook.sock",
                   sock_path=str(sock), pid_path=str(tmp_path / "s.pid"))
    seen = []

    def _popen(argv, **kw):
        seen.append(list(argv))
        raise OSError("not really spawning")

    monkeypatch.setattr(ptyhost.subprocess, "Popen", _popen)
    host._spawn_broker()
    argv = seen[0]
    assert argv[argv.index("--hook-sock") + 1] == "/tmp/h/hook.sock"
    assert argv.index("--port") < argv.index("--hook-sock") < argv.index("--sock")
    assert ptyhost.stray_broker_pids([(4242, argv)], str(sock), {1}) == [4242]


def test_the_spawned_broker_without_a_socket_gets_no_flag(monkeypatch, tmp_path):
    host = PtyHost(persist=True, port=19999, sock_path=str(tmp_path / "s.sock"))
    seen = []
    monkeypatch.setattr(ptyhost.subprocess, "Popen",
                        lambda argv, **kw: seen.append(list(argv)) or (_ for _ in ()).throw(OSError()))
    host._spawn_broker()
    assert "--hook-sock" not in seen[0]


def test_stray_the_hook_socket_flag_is_not_mistaken_for_the_socket():
    """`--hook-sock` is a different flag: its value is never read as the
    broker's socket, and an argv with both still classifies on `--sock`."""
    both = _argv("--port", "19873", "--hook-sock", "/tmp/x/pty.sock",
                 "--sock", "/tmp/y/pty.sock")
    assert ptyhost.stray_broker_pids([(9, both)], "/tmp/x/pty.sock", {1}) == []
    assert ptyhost.stray_broker_pids([(9, both)], "/tmp/y/pty.sock", {1}) == [9]
    only = _argv("--hook-sock=/tmp/x/pty.sock")
    assert ptyhost.stray_broker_pids([(9, only)], "/tmp/x/pty.sock", {1}) == []
