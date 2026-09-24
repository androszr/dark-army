"""Human acknowledgement of native Codex: real journals, fake OS/editor only."""
import json
from types import MappingProxyType

import pytest

from dark_army_daemon import codex_rollouts, vscode_reveal
from tests.test_board_refine import refinement_handoff  # noqa: F401 — shared fixture


def _event(kind, turn="turn-1", stamp="2026-09-12T19:00:02Z"):
    return {"timestamp": stamp, "type": "event_msg",
            "payload": {"type": kind, "turn_id": turn}}


def _append(record, row):
    with record.path.open("a") as stream:
        stream.write(json.dumps(row) + "\n")


def _reload(d, records, index=0):
    old = records[index]
    record = codex_rollouts.parse_rollout(old.path)
    record.process_seen = True
    stat = record.path.stat()
    record.revision = (stat.st_mtime_ns, stat.st_size)
    records[index] = record
    d._codex_records[record.session_id] = record
    return record


@pytest.fixture
def stopped_native(refinement_handoff, monkeypatch):
    d, store, card, plan, records, processes, posts = refinement_handoff
    record = records[0]
    rows = [json.loads(line) for line in record.path.read_text().splitlines()]
    rows[1]["timestamp"] = "2026-09-12T19:00:01Z"
    record.path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    _append(record, _event("task_complete"))
    _reload(d, records)
    d._codex_navigation = MappingProxyType(codex_rollouts.resolve_navigation_proofs(
        codex_rollouts.project_title_roots(records)))
    vscode_reveal._in_vscode_cache.clear()
    return refinement_handoff


def _row(d, record):
    stub = {"session_id": record.session_id, "pid": None, "cwd": record.cwd,
            "provider": "codex", "can_stop": False, "_category": "waiting"}
    return d._enrich_agent_stubs([stub])["waiting"][0]


@pytest.mark.asyncio
async def test_finished_native_root_can_acknowledge_without_stop_or_attachment(stopped_native, monkeypatch):
    d, store, card, plan, records, processes, posts = stopped_native
    record = records[0]
    assert not d._refinement_receipts
    row = _row(d, record)
    assert row["can_close"] is True
    assert row["can_stop"] is False and row["can_type"] is False and row["pid"] is None
    assert not d.stop_codex_session(record.session_id)[0]
    assert not (await d.close_session_terminal(record.session_id))[0]
    assert not (await d.wrap_up_session(record.session_id))[0]
    assert not posts
    monkeypatch.setattr(d, "_confirm_codex_stop", lambda *a, **k: pytest.fail("signal scheduled"))
    assert (await d.close_session_terminal(record.session_id, by_person=True))[0]
    assert posts == [{"op": "close_refinement_terminal", "pid": 701, "tty": "/dev/ttys040"}]
    assert d._closed_ids[record.session_id][1:] == (701, 100.0)
    assert record.session_id not in d._codex_records
    assert records[1].session_id in d._codex_records
    assert store.get(card["id"])["column_name"] == "prep"


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["running", "question", "helper", "permission", "pending",
    "unknown_turn", "mismatch", "missing_time", "malformed", "new_user", "new_tool", "user_event", "late_old_end",
    "old_bridge", "other_window", "no_window", "shared_window"])
async def test_ineligible_native_root_never_advertises_or_closes(stopped_native, monkeypatch, fault):
    d, store, card, plan, records, processes, posts = stopped_native
    record, sid = records[0], records[0].session_id
    if fault == "running":
        _append(record, _event("task_started", "turn-2"))
    elif fault == "question":
        _append(record, {"type": "response_item", "payload": {"type": "function_call", "name": "request_user_input",
            "call_id": "ask", "arguments": {"questions": [{"question": "Choose?", "options": []}]}}})
    elif fault == "helper":
        record.stats.agents["child"] = codex_rollouts.AgentInfo(agent_id="child", activity="running")
    elif fault == "permission":
        monkeypatch.setattr(d, "_prompts_by_session", lambda: {sid: {"id": "ask"}})
    elif fault == "pending":
        d._pending_questions[sid] = {"question": "Choose?"}
    elif fault == "unknown_turn":
        _append(record, _event("task_started", ""))
    elif fault == "mismatch":
        _append(record, _event("task_started", "turn-2"))
        _append(record, _event("task_complete", "turn-1"))
    elif fault == "missing_time":
        record.path.write_text(record.path.read_text().replace('"timestamp": "2026-09-12T19:00:02Z", ', ''))
    elif fault == "malformed":
        with record.path.open("a") as stream: stream.write("{broken\n")
    elif fault == "new_user":
        _append(record, {"type": "response_item", "payload": {"type": "message", "role": "user",
            "content": [{"type": "input_text", "text": "Continue working"}]}})
    elif fault == "new_tool":
        _append(record, {"type": "response_item", "payload": {"type": "function_call", "name": "exec_command",
            "call_id": "work", "arguments": {"cmd": "true"}}})
    elif fault in {"user_event", "late_old_end"}:
        _append(record, {"type": "event_msg", "payload": {"type": "user_message", "message": "Continue"}})
        if fault == "late_old_end": _append(record, _event("task_complete"))
    else:
        locks = [dict(lock) for lock in vscode_reveal._bob_ext_locks()]
        if fault == "old_bridge": locks[0]["extensionVersion"] = "0.1.11"
        elif fault == "other_window": locks[0]["workspaceFolders"] = ["/another-project"]
        elif fault == "no_window": locks = []
        elif fault == "shared_window": locks *= 2
        monkeypatch.setattr(vscode_reveal, "_bob_ext_locks", lambda: locks)
    if fault in {"running", "question", "unknown_turn", "mismatch", "missing_time", "malformed", "new_user", "new_tool", "user_event", "late_old_end"}:
        record = _reload(d, records)
    assert not _row(d, record)["can_close"]
    ok, detail = await d.close_session_terminal(sid, by_person=True)
    assert not ok and detail
    assert posts == [] and sid not in d._closed_ids


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", [None, {"matched": True}, {"matched": True, "closed": False},
    {"matched": "true", "closed": True}])
async def test_unconfirmed_human_close_never_retires_or_finishes(stopped_native, monkeypatch, reply):
    d, store, card, plan, records, processes, posts = stopped_native
    sid = records[0].session_id
    store.update(card["id"], {"column_name": "in_progress", "session_id": sid, "link_state": "live"})
    original = vscode_reveal._post_json
    async def post(*args, **kwargs):
        await original(*args, **kwargs)
        return reply
    monkeypatch.setattr(vscode_reveal, "_post_json", post)
    assert not (await d.close_session_terminal(sid, by_person=True))[0]
    assert len(posts) == 1 and sid not in d._closed_ids and sid in d._codex_records
    assert store.get(card["id"])["column_name"] == "in_progress"


@pytest.mark.asyncio
async def test_human_close_finishes_bound_card_only_after_terminal_confirmation(stopped_native):
    d, store, card, plan, records, processes, posts = stopped_native
    sid = records[0].session_id
    store.update(card["id"], {"column_name": "in_progress", "session_id": sid, "link_state": "live"})
    assert (await d.close_session_terminal(sid, by_person=True))[0]
    assert len(posts) == 1 and store.get(card["id"])["column_name"] == "done"


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["started", "completed", "missing"])
async def test_native_close_checks_retained_child_completion(stopped_native, phase):
    d, store, card, plan, records, processes, posts = stopped_native
    record, sid = records[0], records[0].session_id
    child_path = record.path.parent / "child.jsonl"
    child_rows = [
        {"type": "session_meta", "payload": {"id": "child", "cwd": record.cwd,
            "source": {"subagent": {"thread_spawn": {"parent_thread_id": record.thread_id}}}}},
        _event("task_started", "child-turn", "2026-09-12T19:00:01Z")]
    if phase == "completed": child_rows.append(_event("task_complete", "child-turn"))
    child_path.write_text("\n".join(json.dumps(row) for row in child_rows) + "\n")
    child = codex_rollouts.parse_rollout(child_path)
    d._codex_records[child.session_id] = child
    if phase == "missing": child_path.unlink()
    result = await d.close_session_terminal(sid, by_person=True)
    assert result[0] is (phase == "completed")
    assert len(posts) == (1 if phase == "completed" else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["new_turn", "new_question", "helper", "permission", "pid_reused"])
async def test_final_validation_after_transport_preparation_refuses_changes(stopped_native, monkeypatch, fault):
    d, store, card, plan, records, processes, posts = stopped_native
    record, sid = records[0], records[0].session_id
    original = vscode_reveal._post_json
    async def post(*args, **kwargs):
        if fault == "new_turn": _append(record, _event("task_started", "turn-2"))
        elif fault == "new_question": record.stats.question = {"question": "Choose?"}
        elif fault == "helper": record.spawned_paths["/root/new"] = codex_rollouts.AgentInfo(agent_id="")
        elif fault == "permission": monkeypatch.setattr(d, "_prompts_by_session", lambda: {sid: {"id": "ask"}})
        else:
            processes[0].fresh["create_time"] += 1
        return await original(*args, **kwargs)
    monkeypatch.setattr(vscode_reveal, "_post_json", post)
    assert not (await d.close_session_terminal(sid, by_person=True))[0]
    assert not posts and sid not in d._closed_ids


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["new_turn", "new_user", "malformed_tail", "new_question", "new_helper",
    "pid_reused", "tty_changed", "holder_gone", "scan_failed", "journal_replaced", "generation"])
async def test_advertised_close_rechecks_at_click(stopped_native, monkeypatch, fault):
    from copy import deepcopy
    d, store, card, plan, records, processes, posts = stopped_native
    record, sid = records[0], records[0].session_id
    assert _row(d, record)["can_close"]
    if fault == "new_turn": _append(record, _event("task_started", "turn-2"))
    elif fault == "new_user":
        _append(record, {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "New task"}]}})
    elif fault == "malformed_tail":
        with record.path.open("a") as stream: stream.write("{")
    elif fault == "new_question":
        _append(record, {"type": "response_item", "payload": {"type": "function_call", "name": "request_user_input_async", "call_id": "ask", "arguments": {"questions": [{"title": "Choose?", "options": ["A", "B"]}]}}})
    elif fault == "new_helper":
        _append(record, {"type": "response_item", "payload": {"type": "function_call", "name": "spawn_agent", "call_id": "spawn", "arguments": {"agent_type": "bc-verifier"}}})
        _append(record, {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "spawn", "output": {"task_name": "/root/helper"}}})
    elif fault == "pid_reused": processes[0].fresh["create_time"] += 1
    elif fault == "tty_changed": processes[0].fresh["terminal"] = "/dev/ttys999"
    elif fault == "holder_gone": processes.pop(0)
    elif fault == "scan_failed": monkeypatch.setattr(codex_rollouts, "_process_snapshot", lambda: None)
    elif fault == "journal_replaced":
        text = record.path.read_text(); record.path.unlink(); record.path.write_text(text)
    elif fault == "generation":
        original = vscode_reveal._post_json
        async def post(*args, **kwargs):
            d._codex_records[sid] = deepcopy(record)
            return await original(*args, **kwargs)
        monkeypatch.setattr(vscode_reveal, "_post_json", post)
    assert not (await d.close_session_terminal(sid, by_person=True))[0]
    assert posts == [] and sid not in d._closed_ids
