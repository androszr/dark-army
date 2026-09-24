"""Grok leader client: binary discovery, sock presence, the resident predicate.

The live ACP session is not faked here. Reachable means the leader is up
and the session carries ``resident: true`` — membership in a list is the
Grok version of ``channel: true`` on every Claude row.
"""
import asyncio
import os
import socket
import stat
import time
from pathlib import Path

import pytest

from dark_army_daemon import grok_leader


def test_initialize_params_are_what_the_leader_accepts():
    """A missing clientInfo.version is Invalid params and a 4ms disconnect."""
    params = grok_leader.initialize_params()
    assert params["protocolVersion"] == 1
    assert isinstance(params["protocolVersion"], int)
    assert params["clientInfo"]["name"] == "dark-army"
    assert params["clientInfo"]["version"]


def test_find_grok_prefers_path(monkeypatch):
    monkeypatch.setattr(grok_leader.shutil, "which", lambda _: "/somewhere/grok")
    assert grok_leader.find_grok_binary() == "/somewhere/grok"


def test_find_grok_falls_back_to_known_locations(tmp_path, monkeypatch):
    fake = tmp_path / "grok"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(grok_leader.shutil, "which", lambda _: None)
    monkeypatch.setattr(grok_leader, "GROK_CANDIDATES", (str(fake),))
    assert grok_leader.find_grok_binary() == str(fake)


def test_find_grok_ignores_non_executable(tmp_path, monkeypatch):
    plain = tmp_path / "grok"
    plain.write_text("not executable")
    monkeypatch.setattr(grok_leader.shutil, "which", lambda _: None)
    monkeypatch.setattr(grok_leader, "GROK_CANDIDATES", (str(plain),))
    assert grok_leader.find_grok_binary() is None


def test_leader_sock_missing(tmp_path):
    assert grok_leader.leader_sock_present(tmp_path / "nope") is False


def test_leader_sock_regular_file_is_not_a_socket(tmp_path):
    path = tmp_path / "leader.sock"
    path.write_text("")
    assert grok_leader.leader_sock_present(path) is False


def test_leader_sock_present_on_a_socket():
    # pytest's tmp_path is often longer than the AF_UNIX bind limit (~104).
    path = Path("/tmp") / f"bob-leader-test-{os.getpid()}.sock"
    if path.exists():
        path.unlink()
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.bind(str(path))
        assert grok_leader.leader_sock_present(path) is True
    finally:
        sock.close()
        path.unlink(missing_ok=True)


def test_resident_ids_keep_only_the_live_flag():
    """Listed without the flag, or with it false, is disk history."""
    entries = [
        {"sessionId": "live", "resident": True},
        {"sessionId": "disk", "resident": False},
        {"sessionId": "old"},
        {"resident": True},
        "garbage",
        {"session_id": "also-live", "resident": True},
    ]
    assert grok_leader.resident_ids(entries) == frozenset({"live", "also-live"})


def test_listed_without_resident_is_not_reachable():
    assert grok_leader.resident_ids([{"sessionId": "disk"}]) == frozenset()
    assert grok_leader.is_resident({"sessionId": "disk"}) is False


def test_resident_false_is_not_reachable():
    assert grok_leader.is_resident({"sessionId": "x", "resident": False}) is False


def test_resident_true_is_reachable():
    assert grok_leader.is_resident({"sessionId": "x", "resident": True}) is True


def test_sessions_from_payload_on_a_bare_list():
    payload = [{"sessionId": "a", "resident": True}, {"sessionId": "b"}]
    assert grok_leader.sessions_from_payload(payload) == payload


def test_sessions_from_payload_on_sessions_changed():
    payload = {
        "jsonrpc": "2.0",
        "method": "_x.ai/sessions/changed",
        "params": {
            "sessions": [
                {"sessionId": "live", "resident": True},
                {"sessionId": "disk", "resident": False},
            ]
        },
    }
    entries = grok_leader.sessions_from_payload(payload)
    assert grok_leader.resident_ids(entries) == frozenset({"live"})


def test_session_list_of_disk_history_yields_an_empty_set():
    """No resident flag anywhere — membership is not a reachability oracle."""
    entries = grok_leader.sessions_from_payload({
        "sessions": [{"sessionId": "old-1"}, {"sessionId": "old-2"}],
    })
    assert grok_leader.list_has_resident_flag(entries) is False
    assert grok_leader.resident_ids(entries) == frozenset()


@pytest.mark.asyncio
async def test_connect_does_not_spawn_when_the_sock_is_missing(monkeypatch):
    spawned = []

    async def fake_exec(*args, **kwargs):
        spawned.append(args)
        raise AssertionError("must not spawn without a leader sock")

    monkeypatch.setattr(grok_leader, "leader_sock_present", lambda path=None: False)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    client = grok_leader.LeaderClient()
    assert await client.connect() is False
    assert spawned == []
    assert client.connected is False


@pytest.mark.asyncio
async def test_connect_is_not_retried_inside_the_backoff(monkeypatch):
    spawned = []

    async def fake_exec(*args, **kwargs):
        spawned.append(args)
        raise OSError("leader refused")

    monkeypatch.setattr(grok_leader, "leader_sock_present", lambda path=None: True)
    monkeypatch.setattr(grok_leader, "leader_sock_identity", lambda path=None: (1, 2))
    monkeypatch.setattr(grok_leader, "find_grok_binary", lambda: "/usr/bin/grok")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    client = grok_leader.LeaderClient()
    assert await client.connect() is False
    assert await client.connect() is False
    assert len(spawned) == 1
    client._fail_at = time.monotonic() - grok_leader.LEADER_CONNECT_BACKOFF_SECONDS - 1
    assert await client.connect() is False
    assert len(spawned) == 2


@pytest.mark.asyncio
async def test_connect_retries_when_the_sock_identity_changes(monkeypatch):
    spawned = []
    identities = [(1, 2), (3, 4)]

    async def fake_exec(*args, **kwargs):
        spawned.append(args)
        raise OSError("leader refused")

    monkeypatch.setattr(grok_leader, "leader_sock_present", lambda path=None: True)
    monkeypatch.setattr(
        grok_leader, "leader_sock_identity",
        lambda path=None: identities.pop(0) if identities else (3, 4),
    )
    monkeypatch.setattr(grok_leader, "find_grok_binary", lambda: "/usr/bin/grok")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    client = grok_leader.LeaderClient()
    assert await client.connect() is False
    assert await client.connect() is False
    assert len(spawned) == 2


class _FakeStdin:
    def write(self, data):
        pass

    async def drain(self):
        pass


class _FakeProc:
    def __init__(self):
        self.stdin = _FakeStdin()
        self.stdout = None
        self.returncode = 0


@pytest.mark.asyncio
async def test_prompt_timeout_fails_if_no_longer_resident(monkeypatch):
    async def fake_wait(fs, timeout=None, return_when=None):
        return set(), set(fs)

    monkeypatch.setattr(asyncio, "wait", fake_wait)
    client = grok_leader.LeaderClient()
    client.connected = True
    client._proc = _FakeProc()
    client.residents = frozenset()
    ok, detail = await client.prompt("s1", "hi")
    assert ok is False
    assert "no longer listening" in detail


@pytest.mark.asyncio
async def test_prompt_timeout_succeeds_while_still_resident(monkeypatch):
    async def fake_wait(fs, timeout=None, return_when=None):
        return set(), set(fs)

    monkeypatch.setattr(asyncio, "wait", fake_wait)
    client = grok_leader.LeaderClient()
    client.connected = True
    client._proc = _FakeProc()
    client.residents = frozenset({"s1"})
    ok, detail = await client.prompt("s1", "hi")
    assert (ok, detail) == (True, "")


@pytest.mark.asyncio
async def test_prompt_reports_failure_when_the_ack_is_cancelled(monkeypatch):
    async def fake_wait(fs, timeout=None, return_when=None):
        for fut in fs:
            if not fut.done():
                fut.cancel()
        return set(fs), set()

    monkeypatch.setattr(asyncio, "wait", fake_wait)
    client = grok_leader.LeaderClient()
    client.connected = True
    client._proc = _FakeProc()
    ok, detail = await client.prompt("s1", "hi")
    assert ok is False
    assert "no longer listening" in detail


def test_sessions_from_payload_reads_upserted():
    payload = {
        "method": "_x.ai/sessions/changed",
        "params": {"upserted": [{"sessionId": "x", "resident": True}]},
    }
    assert grok_leader.sessions_from_payload(payload) == [
        {"sessionId": "x", "resident": True},
    ]


def test_sessions_changed_delta_merges_instead_of_replacing():
    current = frozenset({"a", "b"})
    payload = {
        "method": "_x.ai/sessions/changed",
        "params": {
            "upserted": [{"sessionId": "c", "resident": True}],
            "removed": ["b"],
        },
    }
    assert grok_leader.merge_sessions_changed(current, payload) == frozenset(
        {"a", "c"}
    )


def test_sessions_changed_one_entry_does_not_drop_the_rest():
    current = frozenset({"a", "b"})
    payload = {
        "method": "_x.ai/sessions/changed",
        "params": {
            "sessions": [{"sessionId": "c", "resident": True}],
        },
    }
    assert grok_leader.merge_sessions_changed(current, payload) == frozenset(
        {"a", "b", "c"}
    )


def test_sessions_changed_snapshot_drops_a_departed_client():
    """A census-shaped sessions list is the resident set, not an upsert."""
    current = frozenset({"a", "b"})
    payload = {
        "method": "_x.ai/sessions/changed",
        "params": {
            "sessions": [
                {"sessionId": "a", "resident": True},
                {"sessionId": "c", "resident": True},
            ],
        },
    }
    assert grok_leader.merge_sessions_changed(current, payload) == frozenset(
        {"a", "c"}
    )


def test_dispatch_merges_a_delta():
    client = grok_leader.LeaderClient()
    client.residents = frozenset({"a", "b"})
    client._dispatch({
        "method": "_x.ai/sessions/changed",
        "params": {
            "upserted": [{"sessionId": "c", "resident": True}],
            "removed": ["a"],
        },
    })
    assert client.residents == frozenset({"b", "c"})


@pytest.mark.asyncio
async def test_read_loop_skips_an_over_limit_line_and_keeps_reading():
    """A `session/list` reply over the stream limit raises ValueError from
    readline(); the reader used to die on it and reconnect every 30s forever.
    Now the line is skipped and the next one is still dispatched."""
    client = grok_leader.LeaderClient()

    class FakeStdout:
        def __init__(self):
            self.replies = [
                ValueError("Separator is not found, and chunk exceed the limit"),
                b'{"id": 7, "result": {"ok": true}}\n',
                b"",
            ]

        async def readline(self):
            item = self.replies.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

    class FakeProc:
        stdout = FakeStdout()
        returncode = 0

    client._proc = FakeProc()
    fut = asyncio.get_running_loop().create_future()
    client._pending[7] = fut
    await client._read_loop()
    assert fut.done() and fut.result() == ("ok", {"ok": True})


@pytest.mark.asyncio
async def test_leader_subprocess_is_spawned_with_a_wide_stream_limit(monkeypatch):
    """The 64 KiB asyncio default is smaller than a busy leader's session/list
    reply; the spawn must widen it or the reader dies on the first big line."""
    captured = {}

    async def fake_spawn(*args, **kwargs):
        captured.update(kwargs)
        raise OSError("stop here; the kwargs are what this test is about")

    monkeypatch.setattr(grok_leader, "leader_sock_present", lambda: True)
    monkeypatch.setattr(grok_leader, "leader_sock_identity", lambda: ("s", 1, 2))
    monkeypatch.setattr(grok_leader, "find_grok_binary", lambda: "/usr/local/bin/grok")
    monkeypatch.setattr(grok_leader.asyncio, "create_subprocess_exec", fake_spawn)

    client = grok_leader.LeaderClient()
    assert await client.connect() is False
    assert captured.get("limit", 0) >= 8 * 1024 * 1024
