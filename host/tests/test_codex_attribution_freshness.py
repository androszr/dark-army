"""Attribution stays available when new terminals join a running daemon."""

import asyncio
import os
import pty
import select
import subprocess
import sys

import psutil
import pytest
from psutil._psposix import get_terminal_map

from dark_army_daemon import channel_server, codex_rollouts
from dark_army_daemon.daemon import BobDaemon
from tests.test_codex_rollouts import _native_holder, _root


@pytest.fixture
def late_terminal():
    """Real controlling tty born after psutil's map, owned solely by this test."""
    get_terminal_map.cache_clear()
    cached = dict(get_terminal_map())
    handles = []
    child = None
    try:
        # An idle device node can predate this test; reserve it and keep going
        # until the OS creates one that the daemon's old map cannot know.
        for _ in range(64):
            master, slave = pty.openpty()
            handles.extend((master, slave))
            if os.fstat(slave).st_rdev not in cached:
                break
        else:
            pytest.fail("could not create a terminal beyond the cached device map")
        tty = os.ttyname(slave)
        child = subprocess.Popen([
            sys.executable, "-c",
            "import fcntl,os,sys,termios; os.setsid(); "
            "fcntl.ioctl(0,termios.TIOCSCTTY,0); print('ready',flush=True); "
            "sys.stdin.read()",
        ], stdin=slave, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        assert select.select([child.stdout], [], [], 5)[0], "child did not acquire its tty"
        assert child.stdout.readline() == b"ready\n"
        process = psutil.Process(child.pid)
        assert process.terminal() is None  # the real precondition, no stub
        yield process, tty
    finally:
        if child is not None:
            child.terminate()
            child.communicate(timeout=5)
        for descriptor in handles:
            os.close(descriptor)
        get_terminal_map.cache_clear()


def test_real_process_snapshot_refreshes_terminal(late_terminal):
    process, tty = late_terminal
    observed = codex_rollouts._process_snapshot()
    found = next(item for item in observed if item.pid == process.pid)
    assert found.info["terminal"] == tty
    assert psutil.Process(process.pid).terminal() == tty


@pytest.mark.asyncio
async def test_new_pty_does_not_poison_existing_same_project_attribution(
    tmp_path, monkeypatch, late_terminal,
):
    process, tty = late_terminal
    roots, holders = [], []
    for index in range(2):
        path = tmp_path / f"root-{index}.jsonl"
        path.write_text("journal")
        roots.append(_root(f"root-{index}", cwd=str(tmp_path), path=path))
        holders.append(_native_holder(700 + index, path, f"ttys{90 + index}", cwd=str(tmp_path)))
    # Harness/journal identities are fixtures; the late terminal lookup is the
    # actual psutil method over a live child, and snapshot freshness is real.
    holders[1].info.pop("terminal")
    holders[1].terminal = process.terminal
    monkeypatch.setattr(codex_rollouts.psutil, "process_iter", lambda _attrs: iter(holders))
    daemon = BobDaemon()
    daemon._codex_records = {root.session_id: root for root in roots}
    for index in range(2):
        daemon._channels[51000 + index] = {
            "host": channel_server.HOST_CODEX, "pid": 700 + index, "cwd": str(tmp_path),
        }
    observed = codex_rollouts.resolve_navigation_proofs(codex_rollouts.project_title_roots(roots))
    assert set(observed) == {root.session_id for root in roots}
    assert observed[roots[1].session_id].tty == tty
    for index, root in enumerate(roots):
        assert await daemon._board_request_session_fresh(51000 + index) == root.session_id
    # Cwd coincidence still grants no right to borrow either holder's card.
    daemon._channels[51002] = {
        "host": channel_server.HOST_CODEX, "pid": 900, "cwd": str(tmp_path),
    }
    assert await daemon._board_request_session_fresh(51002) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("fault,reason", [
    ("timeout", "observation-timeout"),
    ("unavailable", "observation-unavailable"),
    ("registry", "registry-changed"),
    ("roster", "roster-changed"),
    ("uncertain", "holder-uncertain"),
    ("wrong-parent", "exact-parent-mismatch"),
    ("legacy", "legacy-unattributed"),
])
async def test_attribution_refusals_log_only_bounded_reasons(
    tmp_path, monkeypatch, caplog, fault, reason,
):
    from dataclasses import replace
    from tests.test_agents_poll import _navigation_daemon
    daemon, roots, _ = _navigation_daemon(tmp_path, monkeypatch)
    daemon._channels[51000] = {
        "host": channel_server.HOST_CODEX, "pid": 900, "cwd": roots[0].cwd,
        "secret": "never-log-this-secret",
    }
    async def observe(_capture, **_kwargs):
        if fault == "timeout":
            raise asyncio.TimeoutError
        if fault == "unavailable":
            raise PermissionError("never-log-this-error")
        if fault == "registry":
            daemon._channels[51000] = dict(daemon._channels[51000])
        if fault == "roster":
            daemon._codex_records[roots[0].session_id] = replace(roots[0])
        return {}, {roots[0].session_id} if fault == "uncertain" else set()
    if fault == "legacy":
        daemon._codex_navigation = {}
        monkeypatch.setattr(daemon, "_board_request_session", lambda _port: None)
    monkeypatch.setattr(daemon, "_fresh_navigation_targets", observe)
    with caplog.at_level("INFO", logger="dark-army"):
        assert await daemon._board_request_session_fresh(51000) is None
    messages = [r.getMessage() for r in caplog.records if "board attribution refused:" in r.getMessage()]
    assert messages == [f"board attribution refused: stage=request reason={reason} port=51000"]
    assert "never-log" not in caplog.text


@pytest.mark.parametrize("fault", ["registry", "roster"])
def test_downstream_guard_keeps_generations_and_logs_reason(tmp_path, monkeypatch, caplog, fault):
    from dataclasses import replace
    from tests.test_agents_poll import _navigation_daemon
    daemon, roots, _ = _navigation_daemon(tmp_path, monkeypatch)
    daemon._channels[51000] = {"host": channel_server.HOST_CODEX, "pid": 701, "cwd": roots[0].cwd}
    guard = daemon._board_author_guard(51000)
    assert guard()
    if fault == "registry":
        daemon._channels[51000] = dict(daemon._channels[51000])
    else:
        daemon._codex_records[roots[1].session_id] = replace(roots[1])
    with caplog.at_level("INFO", logger="dark-army"):
        assert not guard()
    assert f"stage=write reason={fault}-changed port=51000" in caplog.text
