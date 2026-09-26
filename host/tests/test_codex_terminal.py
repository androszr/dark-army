"""Shared-server TUI navigation uses the thread's native listener, not cwd."""

import asyncio
from dataclasses import replace
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from dark_army_daemon import codex_terminal as terminal, vscode_reveal
from dark_army_daemon.daemon import BobDaemon
from tests.test_codex_rollouts import _root


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    records = [_root(f"root-{i}", cwd=str(tmp_path), path=tmp_path / f"{i}.jsonl")
               for i in range(2)]
    for r in records:
        r.source_kind = "vscode"
    captured = terminal.roots(records)
    owners = {4100 + i: [SimpleNamespace(
        pid=700 + i, executable="/bin/codex", cwd=str(tmp_path), create_time=10.0,
        argv=("/bin/codex",), tty=f"/dev/ttys{i}", native=True,
    )] for i in range(2)}
    monkeypatch.setattr(terminal, "_listeners", lambda: owners)
    monkeypatch.setattr(terminal, "_socket_identity", lambda _: (1, 2))
    monkeypatch.setattr(terminal, "_server_identity", lambda _: (90, "/bin/codex", 1, ()))

    class Socket:
        def __init__(self):
            self.messages = asyncio.Queue()
            self.sent = []
            self.port_reads = 0
            self.error = False
            self.duplicate = False
            self.paginate = False
            self.transport = SimpleNamespace(get_extra_info=lambda _: object())

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def send(self, raw):
            d = json.loads(raw)
            self.sent.append(d)
            if "id" not in d:
                return
            result = {}
            if d["method"] == "mcpServerStatus/list":
                if self.error:
                    await self.messages.put(json.dumps({"id": d["id"], "error": {}}))
                    return
                i = next(i for i, r in enumerate(captured)
                         if r.thread_id == d["params"]["threadId"])
                self.port_reads += 1
                port = 4100 if self.duplicate else 4100 + i
                result = {"data": [{"name": "codex_tui", "runtimeStatus": "connected",
                                    "httpOrigin": f"http://127.0.0.1:{port}"}]}
                if self.paginate and "cursor" not in d["params"]:
                    result = {"data": [{"name": "other"}], "nextCursor": "next"}
            await self.messages.put(json.dumps({"id": d["id"], "result": result}))

        async def recv(self):
            return await self.messages.get()

    ws = Socket()
    monkeypatch.setattr(terminal, "unix_connect", lambda *a, **k: ws)
    return records, captured, owners, ws


@pytest.mark.asyncio
async def test_exact_endpoints_distinguish_same_project_terminals(fixture):
    _, captured, _, ws = fixture
    result = await terminal.resolve(captured, recheck=True)
    assert [result[r.session_id].pid for r in captured] == [700, 701]
    assert {r["method"] for r in ws.sent} == {
        "initialize", "initialized", "mcpServerStatus/list"}
    assert ws.port_reads == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["shared", "wrong_cwd", "missing", "error",
                                   "pid_reused", "socket_replaced", "peer_replaced"])
async def test_uncertainty_withdraws_navigation(fixture, monkeypatch, fault):
    _, captured, owners, ws = fixture
    if fault == "shared":
        ws.duplicate = True
    elif fault == "wrong_cwd":
        for values in owners.values():
            values[0].cwd = "/another/project"
    elif fault == "missing":
        owners.clear()
    elif fault == "error":
        ws.error = True
    elif fault == "pid_reused":
        calls = 0
        def listeners():
            nonlocal calls
            calls += 1
            return {port: [SimpleNamespace(**dict(vars(p[0]), create_time=float(calls)))]
                    for port, p in owners.items()}
        monkeypatch.setattr(terminal, "_listeners", listeners)
    else:
        identities = iter([1, 2])
        method = "_socket_identity" if fault == "socket_replaced" else "_server_identity"
        monkeypatch.setattr(terminal, method, lambda _: next(identities))
    assert await terminal.resolve(captured, recheck=True) == {}


@pytest.mark.asyncio
async def test_pagination_and_missing_server(fixture, monkeypatch):
    _, captured, _, ws = fixture
    ws.paginate = True
    assert len(await terminal.resolve(captured)) == 2
    monkeypatch.setattr(terminal, "_socket_identity", lambda _: (_ for _ in ()).throw(FileNotFoundError()))
    assert await terminal.resolve(captured) == {}


@pytest.mark.parametrize("origin", ["https://127.0.0.1:4", "http://localhost:4",
    "http://127.0.0.1.evil:4", "http://user@127.0.0.1:4", "http://127.0.0.1:4/path",
    "http://127.0.0.1:4?x=1", None])
def test_endpoint_is_only_a_loopback_port(origin):
    assert terminal._port([dict(name="codex_tui", runtimeStatus="connected", httpOrigin=origin)]) is None


@pytest.mark.asyncio
async def test_capability_and_jump_never_promote_control(fixture, monkeypatch):
    records, captured, _, _ = fixture
    daemon = BobDaemon()
    daemon._codex_records = {r.session_id: r for r in records}
    daemon._codex_terminals = await terminal.resolve(captured)
    reveal = AsyncMock(return_value=(True, "shown"))
    monkeypatch.setattr(vscode_reveal, "reveal", reveal)
    for root in captured:
        caps = daemon._session_capabilities(dict(provider="codex", session_id=root.session_id))
        assert caps == dict(can_jump=True, can_stop=False, can_hide=True, can_resume=False)
    target = daemon._codex_terminals[captured[0].session_id]
    monkeypatch.setattr(terminal, "resolve", AsyncMock(return_value={target.root.session_id: target}))
    assert await daemon.reveal_in_vscode(target.root.session_id) == (True, "shown")
    reveal.assert_awaited_once_with(700)
    reveal.reset_mock()
    monkeypatch.setattr(terminal, "resolve", AsyncMock(return_value={target.root.session_id: replace(target, created=99)}))
    assert not (await daemon.reveal_in_vscode(target.root.session_id))[0]
    reveal.assert_not_awaited()
    daemon._hidden_codex[records[0].session_id] = records[0].path, records[0].revision
    assert daemon._codex_terminal_current(records[0].session_id) is None
    records[1].parent_thread_id = "parent"
    assert daemon._codex_terminal_current(records[1].session_id) is None


@pytest.mark.asyncio
async def test_background_discovery_never_holds_snapshot_and_rejects_changed_roots(fixture, monkeypatch):
    records, captured, _, _ = fixture
    daemon = BobDaemon()
    daemon._codex_records = {r.session_id: r for r in records}
    result = await terminal.resolve(captured)
    pending = asyncio.Event()
    async def delayed(_):
        await pending.wait()
        return result
    resolve = AsyncMock(side_effect=delayed)
    monkeypatch.setattr(terminal, "resolve", resolve)
    monkeypatch.setattr(daemon, "_schedule_agents_push", lambda: None)
    await daemon._refresh_codex_terminals()
    await daemon._refresh_codex_terminals()
    assert not daemon._codex_terminal_task.done()
    daemon._codex_records.clear()
    pending.set()
    await daemon._codex_terminal_task
    assert getattr(daemon, "_codex_terminals", {}) == {}
    resolve.assert_awaited_once()


@pytest.mark.asyncio
async def test_discovery_publishes_jump_on_existing_rows_and_throttles(fixture, monkeypatch):
    records, captured, _, _ = fixture
    daemon = BobDaemon()
    daemon._codex_records = {r.session_id: r for r in records}
    result = await terminal.resolve(captured)
    resolve = AsyncMock(return_value=result)
    monkeypatch.setattr(terminal, "resolve", resolve)
    pushes = []
    monkeypatch.setattr(daemon, "_schedule_agents_push", lambda: pushes.append(True))
    await daemon._refresh_codex_terminals()
    await daemon._codex_terminal_task
    await daemon._refresh_codex_terminals()
    resolve.assert_awaited_once()
    assert pushes == [True]
    stubs = [s for s in daemon._collect_agent_stubs() if s.get("provider") == "codex"]
    assert len(stubs) == 2
    for stub in stubs:
        assert stub["can_jump"] and not stub["can_stop"]
        assert stub["pid"] is None
        assert not {"port", "tty", "executable", "created", "_codex_terminals"} & stub.keys()


@pytest.mark.asyncio
async def test_discovery_survives_activity_reordering_the_same_roots(fixture, monkeypatch):
    records, captured, _, _ = fixture
    daemon = BobDaemon()
    daemon._codex_records = {r.session_id: r for r in records}
    result = await terminal.resolve(captured)
    pending = asyncio.Event()
    async def delayed(_):
        await pending.wait()
        return result
    monkeypatch.setattr(terminal, 'resolve', delayed)
    monkeypatch.setattr(daemon, '_schedule_agents_push', lambda: None)
    await daemon._refresh_codex_terminals()
    daemon._codex_records = dict(reversed(list(daemon._codex_records.items())))
    pending.set()
    await daemon._codex_terminal_task
    assert daemon._codex_terminals == result
