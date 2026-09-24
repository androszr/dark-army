import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from dark_army_daemon import enrollment, relay
from dark_army_daemon.daemon import BobDaemon
from dark_army_daemon.decision_capture import DecisionCapture
from dark_army_daemon.decision_store import DecisionStore
from dark_army_daemon.relay_client import RelayConnector


@pytest.mark.asyncio
async def test_drain_commits_device_receipt_before_sending(tmp_path, monkeypatch):
    history = DecisionCapture(DecisionStore(tmp_path / "decisions.db"))
    await history.call("open")
    first = await history.call("observe", "q", dict(root="/a", session_id="s", question_text="Original"))
    second = await history.call("observe", "other", dict(root="/b", session_id="t", question_text="Other"))
    daemon = BobDaemon()
    daemon._decisions = history
    daemon.remote_access_enabled = True
    daemon._agents_snapshot_cache = {"waiting": [{"session_id": "s"}]}
    seen = asyncio.Event()
    async def push(did, body):
        page = await history.call("query", roots={"/a", "/b"}, device=did, receipt_id=body["receipt_id"])
        assert {item["id"] for item in page["items"]} == {first["id"], second["id"]}
        assert set(body) == {"title", "badge", "kind", "destination_version", "receipt_id"}
        seen.set()
    daemon._relay_connector = type("Connector", (), {"push_alert": staticmethod(push)})()
    monkeypatch.setattr(relay, "channel_ids", lambda: ["phone"])
    monkeypatch.setattr(relay, "push_token", lambda _: "token")
    daemon._push_phone_alerts([dict(session_id="s", nickname="Cipher", kind="question", _decision_ids=[first["id"]]),
                               dict(session_id="t", nickname="Cipher", kind="question", _decision_ids=[second["id"]])])
    await asyncio.wait_for(seen.wait(), 3)
    await history.close()


@pytest.mark.asyncio
async def test_python_sanitizer_only_forwards_valid_receipt(monkeypatch):
    monkeypatch.setattr(relay, "channel_key", lambda _: b"a" * 32)
    monkeypatch.setattr(relay, "push_token", lambda _: "a" * 64)
    monkeypatch.setattr(relay, "push_env", lambda _: "prod")
    monkeypatch.setattr(relay, "get_url", lambda: "https://relay.example")
    monkeypatch.setattr(relay, "get_push_secret", lambda: "secret")
    connector = RelayConnector(None)
    connector._http = AsyncMock(return_value=(200, b"ok"))
    receipt = "00000000-0000-4000-8000-000000000001"
    await connector.push_alert("p", dict(title="Cipher needs you", badge=1, kind="question", destination_version=1,
                                         receipt_id=receipt, question="secret", root="/private"))
    fields = json.loads(connector._http.call_args.args[2])
    assert set(fields) == {"tok", "env", "title", "badge", "kind", "destination_version", "receipt_id"}
    assert fields["receipt_id"] == receipt
    await connector.push_alert("p", dict(title="Cipher", destination_version=True, receipt_id=receipt))
    assert "receipt_id" not in json.loads(connector._http.call_args.args[2])
    if connector._executor is not None:
        connector._executor.shutdown(wait=False)


@pytest.mark.asyncio
async def test_question_hook_saved_before_snapshot_and_clear_cannot_resurrect(tmp_path, monkeypatch):
    history = DecisionCapture(DecisionStore(tmp_path / "decisions.db"))
    await history.call("open")
    monkeypatch.setattr(enrollment, "root_enrolled", lambda _: "/a")
    daemon = BobDaemon(); daemon._decisions = history
    monkeypatch.setattr(daemon, "_session_place", lambda _: ("/a", "Project"))
    monkeypatch.setattr(daemon, "_persist_sessions", lambda: None)
    question = {"text": "Choose", "id": "call", "options": ["yes", "no"]}
    daemon._track_pending_question("tool_use", "s", dict(tool_name="AskUserQuestion", question=question, questions=[question]))
    daemon._track_pending_question("tool_done", "s", {})
    # Drain serialized captures, then offer an old worker snapshot.
    await history.call("query", roots={"/a"})
    snapshot = {"waiting": [dict(session_id="s", cwd="/a", question=question)]}
    await asyncio.to_thread(history.reconcile, snapshot)
    page = await history.call("query", roots={"/a"})
    assert page["items"]
    assert "decision_episode_id" not in snapshot["waiting"][0]
    assert page["items"][-1]["status"] == "ended_unknown"
    await history.close()


@pytest.mark.asyncio
async def test_new_hook_absent_from_old_snapshot_stays_open(tmp_path, monkeypatch):
    history = DecisionCapture(DecisionStore(tmp_path / "decisions.db"))
    await history.call("open")
    history.question_hook("s", dict(root="/a", session_id="s", question_text="New"))
    await asyncio.to_thread(history.reconcile, {"waiting": []})
    assert (await history.call("query", roots={"/a"}))["items"][0]["status"] == "open"
    history.question_clear("s", forget=True)
    assert (await history.call("query", roots={"/a"}))["items"][0]["status"] == "ended_unknown"
    await history.close()


@pytest.mark.asyncio
async def test_actual_permission_and_linked_card_alerts_freeze_exact_targets(tmp_path, monkeypatch):
    from dark_army_daemon.alerts import AlertPolicy
    monkeypatch.setattr(enrollment, "root_enrolled", lambda root: "/a")
    history = DecisionCapture(DecisionStore(tmp_path / "decisions.db"))
    await history.call("open")
    prompt = dict(session_id="s", request_id="request-one", tool_name="Bash", description="Run tests")
    history.permission(prompt, "permission_ask", "/a/sub", "Project", {})
    snapshot = {"waiting": [dict(session_id="s", nickname="Cipher", project="Project")]}
    raised = [a.as_dict() for a in AlertPolicy().evaluate(snapshot, {}, 100, prompts={"s": prompt})]
    assert len(raised) == 1 and raised[0]["rule"] == "permission"
    await asyncio.to_thread(history.freeze_alert_targets, raised, snapshot, [], {"s": prompt})
    receipt = await history.call("receipt", "phone", raised[0]["_decision_ids"])
    page = await history.call("query", roots={"/a"}, device="phone", receipt_id=receipt)
    assert page["items"][0]["source_id"] == "request-one"
    card = dict(id="card-one", root="/a", session_id="s", title="Work", updated_at=1)
    await history.call("card_event", card, "card_done", {})
    alert = dict(session_id="s", rule="card", kind="finished")
    await asyncio.to_thread(history.freeze_alert_targets, [alert], snapshot, [card], {})
    receipt = await history.call("receipt", "phone", alert["_decision_ids"])
    page = await history.call("query", roots={"/a"}, device="phone", receipt_id=receipt)
    assert page["items"][0]["card_id"] == "card-one"
    await history.close()


@pytest.mark.asyncio
async def test_failed_open_recovers_and_marks_gap_without_raising(tmp_path, monkeypatch):
    store = DecisionStore(tmp_path / "decisions.db")
    history = DecisionCapture(store)
    real_open = store.open
    monkeypatch.setattr(store, "open", lambda: (_ for _ in ()).throw(OSError("disk unavailable")))
    assert await history.call("open") is None
    monkeypatch.setattr(store, "open", real_open)
    item = await history.call("observe", "q", dict(root="/a", question_text="Recovered"))
    assert item
    assert (await history.call("query", roots={"/a"}))["coverage"]["storage_gap"]
    await history.close()


@pytest.mark.asyncio
async def test_real_exclusive_lock_open_failure_recovers(tmp_path, monkeypatch):
    import sqlite3
    from dark_army_daemon import decision_store
    monkeypatch.setattr(decision_store, "BUSY_TIMEOUT_MS", 50)
    path = tmp_path / "locked.db"
    blocker = sqlite3.connect(path)
    blocker.execute("BEGIN EXCLUSIVE")
    history = DecisionCapture(DecisionStore(path))
    try:
        assert await history.call("open") is None
        assert history.store.db is None
        blocker.rollback()
        item = await history.call("observe", "q", dict(root="/a", question_text="Recovered"))
        assert item
        assert (await history.call("query", roots={"/a"}))["coverage"]["storage_gap"]
    finally:
        blocker.close()
        await history.close()


@pytest.mark.asyncio
async def test_marker_request_and_late_reply_keep_original_episode(tmp_path, monkeypatch):
    history = DecisionCapture(DecisionStore(tmp_path / "decisions.db"))
    await history.call("open")
    monkeypatch.setattr(enrollment, "root_enrolled", lambda _: "/a")
    daemon = BobDaemon(); daemon._decisions = history
    monkeypatch.setattr(daemon, "_session_provider", lambda _: "claude")
    monkeypatch.setattr(daemon, "_channel_for_session", lambda _: {"port": 1})
    history.question_clear("s")
    snapshot = {"waiting": [dict(session_id="s", cwd="/a", last_summary="Plan ready. Proceed?", reply_options=["Yes", "No"]),
                             dict(session_id="quiet", cwd="/a", last_text="An ordinary unmarked status report.")]}
    await asyncio.to_thread(history.reconcile, snapshot)
    original = snapshot["waiting"][0]["decision_episode_id"]
    assert "decision_episode_id" not in snapshot["waiting"][1]
    async def deliver(*_):
        # A current `bob-actions` offer: a summary alone opens no episode
        # since 22 Sep 2026 (S1's mirror, `alerts.offered_reply`).
        newer = {"waiting": [dict(session_id="s", cwd="/a", last_summary="A different request",
                                  reply_options=["Ship it"])]}
        await asyncio.to_thread(history.reconcile, newer)
        return True
    monkeypatch.setattr(daemon, "push_channel_event", deliver)
    assert await daemon.reply_to_session("s", "Yes") == (True, "")
    page = await history.call("query", roots={"/a"})
    old = next(item for item in page["items"] if item["id"] == original)
    assert old["outcome"] == "Yes" and old["status"] == "delivered_unconfirmed"
    assert any(item["id"] != original and item["status"] == "open" for item in page["items"])
    await history.close()


def test_transcript_question_result_is_exact_bounded_and_not_published():
    from dark_army_daemon.session_stats import _Accum, _fold_line, stats_to_dict
    acc = _Accum()
    def fold(kind, content):
        _fold_line(json.dumps({"type": kind, "message": {"role": kind, "model": "claude-sonnet-4-5", "content": content}}), acc)
    fold("assistant", [{"type": "tool_use", "name": "AskUserQuestion", "id": "call",
                        "input": {"questions": [{"question": "Choose", "options": [{"label": "Yes"}]}]}}])
    fold("user", [{"type": "tool_result", "tool_use_id": "other", "content": "unrelated"}])
    assert not acc.stats.question_results
    fold("user", [{"type": "tool_result", "tool_use_id": "call", "content": "Y" * 2100}])
    assert acc.stats.question_results == [{"source_id": "call", "outcome": "Y" * 2000, "truncated": True}]
    assert not acc.stats.question and "question_results" not in stats_to_dict(acc.stats)
    fold("assistant", [{"type": "tool_use", "name": "AskUserQuestion", "id": "cancel",
                        "input": {"questions": [{"question": "Again"}]}}])
    fold("user", [{"type": "tool_result", "tool_use_id": "cancel", "is_error": True, "content": "cancelled"}])
    assert len(acc.stats.question_results) == 1


@pytest.mark.parametrize("output,expected", [
    ({"answers": {"choice": {"answers": ["Yes"]}}}, "choice: Yes"),
    ({"answers": {"choice": {"answers": ["Yes"]}}, "error": "cancelled"}, None),
    ({}, None)])
def test_codex_explicit_answer_result_is_bounded_and_exact(tmp_path, output, expected):
    from tests.test_codex_rollouts import _parity_rollout, _parity_call, row
    record = _parity_rollout(tmp_path, [
        _parity_call("request_user_input", arguments={"questions": [{"question": "Continue?"}]}),
        row("response_item", {"type": "function_call_output", "call_id": "unrelated", "output": json.dumps(output)}),
        row("response_item", {"type": "function_call_output", "call_id": "ask", "output": json.dumps(output)})])
    assert not record.stats.question
    if expected:
        assert record.stats.question_results == [{"source_id": "ask", "outcome": expected, "truncated": False}]
    else:
        assert record.stats.question_results == []


@pytest.mark.asyncio
async def test_a_single_alert_drain_names_its_subject_beside_the_receipt(tmp_path, monkeypatch):
    """One pending alert about a session bound to a card: the body carries
    `session_id` and `card_id` beside `destination_version` / `receipt_id`,
    on every device — the collapsed body above stays exactly its five keys."""
    history = DecisionCapture(DecisionStore(tmp_path / "decisions.db"))
    await history.call("open")
    item = await history.call("observe", "q", dict(root="/a", session_id="s", question_text="Original"))
    daemon = BobDaemon()
    daemon._decisions = history
    daemon.remote_access_enabled = True
    daemon._agents_snapshot_cache = {"waiting": [{"session_id": "s"}]}
    daemon._board_state = {"cards": [dict(id="card-one", title="Work", session_id="s", link_state="live")]}
    seen = asyncio.Event()
    async def push(did, body):
        # `work` (the card's title) and `need` (a question's line) ride as
        # they did before; the subject is the two ids beside the receipt.
        assert set(body) == {"title", "badge", "kind", "work", "need", "session_id",
                             "card_id", "destination_version", "receipt_id"}
        assert body["session_id"] == "s" and body["card_id"] == "card-one"
        assert "act" not in body and "request_id" not in body
        seen.set()
    daemon._relay_connector = type("Connector", (), {"push_alert": staticmethod(push)})()
    monkeypatch.setattr(relay, "channel_ids", lambda: ["phone"])
    monkeypatch.setattr(relay, "push_token", lambda _: "token")
    monkeypatch.setattr(relay, "lock_screen_actions", lambda _: False)
    daemon._push_phone_alerts([dict(session_id="s", nickname="Cipher", kind="question", _decision_ids=[item["id"]])])
    await asyncio.wait_for(seen.wait(), 3)
    await history.close()
