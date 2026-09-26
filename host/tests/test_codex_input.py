"""Shared-server mobile replies: real journals, fake server, no live writes."""

import asyncio
from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

from dark_army_daemon import codex_input as ci, codex_terminal, codex_rollouts
from tests.test_board_refine import refinement_handoff  # noqa: F401
from tests.test_codex_human_close import stopped_native, _append, _event, _reload, _row  # noqa: F401


@pytest.fixture
def shared(stopped_native, monkeypatch):
    d, _, _, _, records, _, posts = stopped_native
    r = records[0]
    rows = [json.loads(line) for line in r.path.read_text().splitlines()]
    rows[0]["payload"]["source"] = "vscode"
    r.path.write_text("\n".join(map(json.dumps, rows)) + "\n")
    r = _reload(d, records)
    d.typed_reply_enabled = True
    monkeypatch.setattr(codex_terminal, "_socket_identity", lambda _: (1, 2))
    monkeypatch.setattr(codex_terminal, "_server_identity", lambda _: (90, "/bin/codex", 1, ()))

    class Socket:
        def __init__(self):
            self.messages = asyncio.Queue()
            self.sent = []
            self.thread_change = {}
            self.turn_change = {}
            self.fail_write = False
            self.on_read = lambda: None
            self.transport = SimpleNamespace(get_extra_info=lambda _: object())

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def send(self, raw):
            msg = json.loads(raw)
            self.sent.append(msg)
            if "id" not in msg:
                return
            r = records[0]
            method = msg["method"]
            result = {}
            if method == "thread/read":
                result = {"thread": dict(id=r.thread_id, path=str(r.path), cwd=r.cwd,
                    originator="codex-tui", source="vscode", threadSource="user",
                    parentThreadId=None, canAcceptDirectInput=True,
                    status={"type": "active" if r.turn_active else "idle", "activeFlags": []})}
                result["thread"].update(self.thread_change)
                self.on_read()
            elif method == "thread/turns/list":
                result = {"data": [dict(id=r.turn_id,
                    status="inProgress" if r.turn_active else "completed")]}
                result["data"][0].update(self.turn_change)
            elif method in ("turn/start", "turn/steer"):
                if self.fail_write:
                    raise ConnectionError("reply lost after submission")
                result = {"turnId": r.turn_id} if method == "turn/steer" else {"turn": {"id": "next"}}
            await self.messages.put(json.dumps({"id": msg["id"], "result": result}))

        async def recv(self):
            return await self.messages.get()

    ws = Socket()
    monkeypatch.setattr(ci, "unix_connect", lambda *a, **k: ws)
    return d, records, ws, posts


async def observe(d, records):
    d._codex_input_proofs = await ci.observe((ci.candidate(records[0]),))


def ask(d, records, *, active):
    r = records[0]
    _append(r, _event("task_started", "turn-2", "2026-09-12T19:00:03Z"))
    _append(r, {"type": "response_item", "payload": {"type": "function_call",
        "name": "request_user_input_async", "call_id": "ask",
        "arguments": {"questions": [{"title": "Keep other projects?", "options": ["Keep", "Delete"]}]}}})
    _append(r, {"type": "response_item", "payload": {"type": "function_call_output",
        "call_id": "ask", "output": '{"accepted":true}'}})
    if not active:
        _append(r, _event("task_complete", "turn-2", "2026-09-12T19:00:04Z"))
    return _reload(d, records)


@pytest.mark.asyncio
@pytest.mark.parametrize("active", [False, True])
async def test_phone_question_publishes_reply_and_sends_addressed_input_once(shared, active):
    d, records, ws, posts = shared
    r = ask(d, records, active=active)
    await observe(d, records)
    row = d._enrich_agent_stubs([{
        "session_id": r.session_id, "pid": None, "cwd": r.cwd,
        "provider": "codex", "can_stop": False, "_category": "waiting",
        "_codex_stats": r.stats,
    }])["waiting"][0]
    assert row["channel"] and row["reply_via"] == "codex" and not row["interaction_note"]
    assert row["question"]["options"] == ["Keep", "Delete"]
    assert not row["can_type"] and not row["can_stop"] and not row["can_close"]
    assert await d.reply_to_session(r.session_id, "Keep other projects") == (True, "")
    writes = [m for m in ws.sent if m["method"] in ("turn/start", "turn/steer")]
    assert len(writes) == 1 and not posts
    assert writes[0]["method"] == ("turn/steer" if active else "turn/start")
    params = writes[0]["params"]
    assert params["threadId"] == r.thread_id
    assert params["input"] == [{"type": "text", "text": "Keep other projects", "text_elements": []}]
    if active:
        assert params["expectedTurnId"] == r.turn_id
    assert not await d.reply_to_session(r.session_id, "Again") == (True, "")
    assert not _row(d, r)["channel"]


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["wrong_thread", "wrong_path", "wrong_cwd", "child", "app",
    "unloaded", "unknown_capability", "false_capability", "approval", "picker", "wrong_turn"])
async def test_server_must_confirm_exact_loaded_root_and_turn(shared, fault):
    d, records, ws, posts = shared
    changes = {"wrong_thread": {"id": "other"}, "wrong_path": {"path": "/other"},
        "wrong_cwd": {"cwd": "/other"}, "child": {"parentThreadId": "parent"},
        "app": {"originator": "codex-app"}, "unloaded": {"status": {"type": "notLoaded"}},
        "unknown_capability": {"canAcceptDirectInput": None},
        "false_capability": {"canAcceptDirectInput": False},
        "approval": {"status": {"type": "active", "activeFlags": ["waitingOnApproval"]}},
        "picker": {"status": {"type": "active", "activeFlags": ["waitingOnUserInput"]}}}
    await observe(d, records)
    if fault == "wrong_turn":
        ws.turn_change = {"id": "other"}
    else:
        ws.thread_change = changes[fault]
    assert not (await d.reply_to_session(records[0].session_id, "Hello"))[0]
    assert not d._codex_reply_attempts
    assert not [m for m in ws.sent if m["method"].startswith("turn/")]
    await observe(d, records)
    assert not d._codex_input_proofs and not posts


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["preference", "hook_prompt", "hook_question", "collision", "expired", "peer", "socket", "journal"])
async def test_local_changes_refuse_before_send(shared, monkeypatch, fault):
    d, records, ws, posts = shared
    r = records[0]
    await observe(d, records)
    if fault == "preference": d.typed_reply_enabled = False
    elif fault == "hook_prompt": monkeypatch.setattr(d, "_prompts_by_session", lambda: {r.session_id: {}})
    elif fault == "hook_question": d._pending_questions[r.session_id] = {}
    elif fault == "collision": d._grok_records[r.session_id] = object()
    elif fault == "expired":
        p = d._codex_input_proofs[r.session_id]
        d._codex_input_proofs[r.session_id] = replace(p, observed_at=p.observed_at - 31)
    elif fault == "peer": monkeypatch.setattr(codex_terminal, "_server_identity", lambda _: (91,))
    elif fault == "socket": monkeypatch.setattr(codex_terminal, "_socket_identity", lambda _: (1, 3))
    elif fault == "journal": _append(r, _event("task_started", "turn-new"))
    if fault == "hook_question": d._pending_questions[r.session_id] = {"text": "Choose"}
    assert not (await d.reply_to_session(r.session_id, "Hello"))[0]
    assert not [m for m in ws.sent if m["method"].startswith("turn/")]
    assert not d._codex_reply_attempts and not posts


@pytest.mark.asyncio
async def test_lost_ack_is_not_retried_or_fallen_back_to_typing(shared):
    d, records, ws, posts = shared
    await observe(d, records)
    ws.fail_write = True
    sid = records[0].session_id
    ok, detail = await d.reply_to_session(sid, "Hello")
    assert not ok and "no retry" in detail
    assert not (await d.reply_to_session(sid, "Again"))[0]
    assert len([m for m in ws.sent if m["method"] == "turn/start"]) == 1
    assert not posts and sid not in d._answering


@pytest.mark.asyncio
async def test_double_click_sends_once(shared):
    d, records, ws, _ = shared
    await observe(d, records)
    results = await asyncio.gather(*(d.reply_to_session(records[0].session_id, "Hello") for _ in range(2)))
    assert sum(ok for ok, _ in results) == 1
    assert len([m for m in ws.sent if m["method"] == "turn/start"]) == 1


@pytest.mark.asyncio
async def test_refresh_is_read_only_and_drops_changed_roots(shared):
    d, records, ws, _ = shared
    d._refresh_codex_input()
    await d._codex_input_task
    assert d._codex_input_proofs
    assert {m["method"] for m in ws.sent} <= {"initialize", "initialized", "thread/read", "thread/turns/list"}
    ws.on_read = lambda: d._codex_records.clear()
    d._codex_input_at = 0
    d._refresh_codex_input()
    await d._codex_input_task
    assert not d._codex_input_proofs


def test_synchronous_and_unasked_active_turns_get_no_candidate(shared):
    d, records, _, _ = shared
    r = records[0]
    r.turn_active = True
    assert ci.candidate(r) is None
    r = ask(d, records, active=True)
    assert ci.candidate(r) is not None
    r.question_async = False
    assert ci.candidate(r) is None


@pytest.mark.asyncio
async def test_canceled_journal_worker_cannot_send_later(shared, monkeypatch):
    import threading
    d, records, ws, _ = shared
    await observe(d, records)
    entered, release = threading.Event(), threading.Event()
    original = codex_rollouts.parse_rollout

    def slow(path):
        entered.set()
        release.wait(2)
        return original(path)

    monkeypatch.setattr(codex_rollouts, "parse_rollout", slow)
    task = asyncio.create_task(d.reply_to_session(records[0].session_id, "Hello"))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
    await asyncio.sleep(0.05)
    assert not d._codex_reply_attempts and not d._answering
    assert not [m for m in ws.sent if m["method"].startswith("turn/")]


@pytest.mark.asyncio
async def test_permission_arriving_during_validation_prevents_write(shared, monkeypatch):
    d, records, ws, _ = shared
    await observe(d, records)
    ws.on_read = lambda: monkeypatch.setattr(d, "_prompts_by_session", lambda: {records[0].session_id: {}})
    assert not (await d.reply_to_session(records[0].session_id, "Hello"))[0]
    assert not d._codex_reply_attempts
    assert not [m for m in ws.sent if m["method"].startswith("turn/")]


@pytest.mark.asyncio
async def test_refresh_only_pushes_changed_capabilities(shared, monkeypatch):
    d, records, _, _ = shared
    pushes = []
    monkeypatch.setattr(d, "_schedule_agents_push", lambda: pushes.append(True))
    d._refresh_codex_input()
    await d._codex_input_task
    assert len(pushes) == 1
    d._codex_input_at = 0
    d._refresh_codex_input()
    await d._codex_input_task
    assert len(pushes) == 1
    d._codex_records.clear()
    d._refresh_codex_input()
    await d._codex_input_task
    assert len(pushes) == 2
    d._codex_input_at = 0
    d._refresh_codex_input()
    await d._codex_input_task
    assert len(pushes) == 2


@pytest.mark.asyncio
async def test_replies_off_never_reads_codexs_server(shared):
    d, records, ws, _ = shared
    await observe(d, records)
    d.typed_reply_enabled = False
    sent = len(ws.sent)
    d._refresh_codex_input()
    assert getattr(d, "_codex_input_task", None) is None
    assert len(ws.sent) == sent and not d._codex_input_proofs
    # Turned back on, the next refresh reads at once rather than after the interval.
    d.typed_reply_enabled = True
    d._refresh_codex_input()
    await d._codex_input_task
    assert d._codex_input_proofs


def test_replies_off_names_both_ways_to_answer(shared):
    d, records, _, _ = shared
    d.typed_reply_enabled = False
    _, note = d._codex_reply_candidate(records[0].session_id)
    assert "off in Settings" in note and "original Codex session" in note
