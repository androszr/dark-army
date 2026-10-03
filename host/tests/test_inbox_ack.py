"""Inbox acknowledgement store, fingerprints, and never-kinds.

Seam: a temp file path. No live daemon.
"""
from __future__ import annotations

import hashlib
import json

from dark_army_daemon import inbox_ack as mod
from dark_army_daemon.inbox_ack import (
    ACK_KINDS,
    INBOX_ACK_KIND_REFUSAL,
    INBOX_ACK_MISSING_REFUSAL,
    INBOX_ACK_STALE_REFUSAL,
    MAX_INBOX_ACKS,
    NEVER_KINDS,
    InboxAckStore,
    card_kind_and_fp,
    fingerprint,
    question_material,
    session_kind_and_fp,
    valid_key,
)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_waiting_fingerprint_is_the_token():
    assert fingerprint("waiting", "anything") == "waiting"
    assert fingerprint("waiting", "") == "waiting"


def test_fingerprint_goldens():
    assert fingerprint("question", "tool-1") == "question:" + _sha("tool-1")
    assert fingerprint("question", "hello?") == "question:" + _sha("hello?")
    assert fingerprint("manual_check", "look") == "manual_check:" + _sha("look")
    assert fingerprint("ended_work", "") == "ended_work:" + _sha("")


def test_question_material_id_present_ignores_text():
    rows = [{"id": "a", "text": "hello"}, {"id": "b", "text": "x"}]
    assert question_material(rows) == "a|b"
    rows[0]["text"] = "changed"
    assert question_material(rows) == "a|b"


def test_question_material_empty_id_uses_text():
    assert question_material([{"id": "", "text": "hello?"}]) == "hello?"
    mixed = [{"id": "a", "text": "hello"}, {"id": "", "text": "x"}]
    assert question_material(mixed) == "hello|x"


def test_never_kinds_and_ack_kinds_partition():
    """Dismiss covers every inbox kind but the permission ask, which still
    blocks its tool call. The two retired kinds are on neither side: an old
    client's ack for them is refused as unknown."""
    assert NEVER_KINDS == frozenset({"permission"})
    assert ACK_KINDS == frozenset(
        {"question", "waiting", "ended_work", "manual_check", "start_asked",
         "review_picks"})
    assert ACK_KINDS.isdisjoint(NEVER_KINDS)
    assert "plan_ready" not in ACK_KINDS | NEVER_KINDS
    assert "awaiting_review" not in ACK_KINDS | NEVER_KINDS
    assert INBOX_ACK_KIND_REFUSAL
    assert INBOX_ACK_STALE_REFUSAL
    assert INBOX_ACK_MISSING_REFUSAL


def test_valid_key():
    assert valid_key("s:abc")
    assert valid_key("c:card-1")
    assert not valid_key("s:")
    assert not valid_key("c:")
    assert not valid_key("x:abc")
    assert not valid_key("")


def test_session_kind_permission_wins():
    kind, _fp = session_kind_and_fp(
        permission=True, questions=[{"id": "q", "text": "ok?"}], waiting=True)
    assert kind == "permission"


def test_session_kind_question_beats_waiting():
    kind, fp = session_kind_and_fp(
        permission=False, questions=[{"id": "q", "text": "ok?"}], waiting=True)
    assert kind == "question"
    assert fp == fingerprint("question", "q")


def test_card_kind_precedence():
    ended = card_kind_and_fp({
        "needs_you": True, "manual_check_due": True, "closed_by": "e"})
    assert ended[0] == "ended_work"
    manual = card_kind_and_fp({
        "manual_check_due": True, "manual_steps": "look"})
    assert manual == ("manual_check", fingerprint("manual_check", "look"))
    # A finished card awaiting review and a ready plan are not inbox
    # subjects any more: the Done column and the Backlog tab carry them.
    assert card_kind_and_fp({
        "closed_by": "cipher", "close_note": "shipped"}) is None
    assert card_kind_and_fp({
        "column_name": "backlog", "plan_path": "plans/x.md",
        "session_id": "", "refine_state": ""}) is None
    assert card_kind_and_fp({"column_name": "prep"}) is None


def test_store_save_reload_and_restart(tmp_path):
    path = tmp_path / "inbox-acks.json"
    store = InboxAckStore(path=path)
    store.load()
    assert store.records() == []
    store.ack("s:one", "question", "question:" + _sha("a"))
    store.ack("c:two", "plan_ready", fingerprint("plan_ready", "p.md"))
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["schema"] == 1
    assert [row["key"] for row in raw["acks"]] == ["s:one", "c:two"]
    again = InboxAckStore(path=path)
    again.load()
    assert again.records() == store.records()


def test_idempotent_same_triple_adds_no_row(tmp_path):
    path = tmp_path / "inbox-acks.json"
    store = InboxAckStore(path=path)
    fp = fingerprint("waiting", "")
    store.ack("s:w", "waiting", fp)
    store.ack("s:w", "waiting", fp)
    assert len(store.records()) == 1


def test_same_key_new_fp_replaces(tmp_path):
    path = tmp_path / "inbox-acks.json"
    store = InboxAckStore(path=path)
    store.ack("s:q", "question", fingerprint("question", "old"))
    store.ack("s:q", "question", fingerprint("question", "new"))
    rows = store.records()
    assert len(rows) == 1
    assert rows[0]["fp"] == fingerprint("question", "new")


def test_cap_drops_oldest(tmp_path):
    path = tmp_path / "inbox-acks.json"
    store = InboxAckStore(path=path, max_acks=3)
    for i in range(5):
        store.ack(f"s:{i}", "waiting", "waiting")
    keys = [row["key"] for row in store.records()]
    assert keys == ["s:2", "s:3", "s:4"]
    assert MAX_INBOX_ACKS == 64


def test_prune_drops_gone_keys(tmp_path):
    path = tmp_path / "inbox-acks.json"
    store = InboxAckStore(path=path)
    store.ack("s:live", "waiting", "waiting")
    store.ack("s:gone", "waiting", "waiting")
    store.ack("c:card", "plan_ready", fingerprint("plan_ready", "p.md"))
    store.prune({"s:live", "c:card"})
    assert [row["key"] for row in store.records()] == ["s:live", "c:card"]


def test_forget_session(tmp_path):
    path = tmp_path / "inbox-acks.json"
    store = InboxAckStore(path=path)
    store.ack("s:abc", "waiting", "waiting")
    store.ack("c:keep", "plan_ready", fingerprint("plan_ready", "p.md"))
    store.forget_session("abc")
    assert [row["key"] for row in store.records()] == ["c:keep"]


def test_missing_file_is_empty(tmp_path):
    store = InboxAckStore(path=tmp_path / "nope.json")
    store.load()
    assert store.records() == []


def test_corrupt_json_loads_empty(tmp_path):
    path = tmp_path / "inbox-acks.json"
    path.write_text("{not json", encoding="utf-8")
    store = InboxAckStore(path=path)
    store.load()
    assert store.records() == []


def test_extra_keys_ignored(tmp_path):
    path = tmp_path / "inbox-acks.json"
    path.write_text(json.dumps({
        "schema": 1,
        "extra": True,
        "acks": [
            {"key": "s:a", "kind": "waiting", "fp": "waiting", "token": "no"},
            {"key": "s:b"},
            "skip me",
        ],
    }), encoding="utf-8")
    store = InboxAckStore(path=path)
    store.load()
    assert store.records() == [{"key": "s:a", "kind": "waiting", "fp": "waiting"}]


def test_refusals_are_greppable():
    source = mod.__file__
    text = open(source, encoding="utf-8").read()
    assert 'INBOX_ACK_STALE_REFUSAL = "that item has changed or is already gone"' in text
    assert 'INBOX_ACK_KIND_REFUSAL = "that item cannot be acknowledged"' in text
    assert 'INBOX_ACK_MISSING_REFUSAL = "that item is not waiting on you"' in text


def _empty_agents():
    return {"running": [], "sleeping": [], "waiting": [],
            "abandoned": [], "finished": []}


def test_snapshot_before_agents_cache_does_not_wipe_session_acks(tmp_path):
    """Restart: state() before the first agents push must not empty the file."""
    from dark_army_daemon.daemon import BobDaemon

    path = tmp_path / "inbox-acks.json"
    stored = InboxAckStore(path=path)
    fp = fingerprint("question", "tool-1")
    stored.ack("s:codex1", "question", fp)

    daemon = BobDaemon(sessions_path=tmp_path / "sessions.json")
    assert "_agents_snapshot_cache" not in daemon.__dict__
    snap = daemon.inbox_snapshot()
    assert any(row["key"] == "s:codex1" for row in snap["acks"])
    disk = json.loads(path.read_text(encoding="utf-8"))
    assert any(row["key"] == "s:codex1" for row in disk["acks"])

    daemon._agents_snapshot_cache = _empty_agents()
    daemon._agents_snapshot_cache["waiting"] = [
        {"session_id": "codex1",
         "questions": [{"id": "tool-1", "text": "ok?"}]}]
    snap = daemon.inbox_snapshot()
    assert any(row["key"] == "s:codex1" for row in snap["acks"])

    daemon._agents_snapshot_cache = _empty_agents()
    daemon._session_states.pop("codex1", None)
    daemon._finished.pop("codex1", None)
    snap = daemon.inbox_snapshot()
    assert all(row["key"] != "s:codex1" for row in snap["acks"])
    disk = json.loads(path.read_text(encoding="utf-8"))
    assert all(row["key"] != "s:codex1" for row in disk["acks"])


def test_empty_cache_still_keeps_acks_named_in_session_states(tmp_path):
    from dark_army_daemon.daemon import BobDaemon

    path = tmp_path / "inbox-acks.json"
    InboxAckStore(path=path).ack("s:hook1", "waiting", "waiting")
    daemon = BobDaemon(sessions_path=tmp_path / "sessions.json")
    daemon._agents_snapshot_cache = _empty_agents()
    daemon._session_states["hook1"] = {"state": "idle"}
    snap = daemon.inbox_snapshot()
    assert any(row["key"] == "s:hook1" for row in snap["acks"])
    daemon._session_states.pop("hook1")
    daemon._finished["hook1"] = {"stub": {}}
    snap = daemon.inbox_snapshot()
    assert any(row["key"] == "s:hook1" for row in snap["acks"])
    daemon._finished.pop("hook1")
    snap = daemon.inbox_snapshot()
    assert all(row["key"] != "s:hook1" for row in snap["acks"])


def test_agents_cache_is_assigned_before_observers():
    from pathlib import Path
    text = (Path(__file__).resolve().parents[1]
            / "dark_army_daemon" / "daemon.py").read_text()
    assign = text.index("self._agents_snapshot_cache = snapshot")
    notify = text.index('self._notify_observers("on_agents_change", snapshot)')
    assert assign < notify


def test_ack_set_builds_triples_and_skips_malformed_rows():
    assert mod.ack_set(None) == set()
    assert mod.ack_set([]) == set()
    row = {"key": "s:s1", "kind": "waiting", "fp": "waiting"}
    assert mod.ack_set([row]) == {("s:s1", "waiting", "waiting")}
    assert mod.ack_set([
        "s:s1", None, 3,
        {"kind": "waiting", "fp": "waiting"},
        {"key": "s:s1", "fp": "waiting"},
        {"key": "s:s1", "kind": "waiting"},
        {"key": "", "kind": "waiting", "fp": "waiting"},
        {"key": None, "kind": "waiting", "fp": "waiting"},
    ]) == set()
    # Duplicates collapse; the store's own records feed it unchanged.
    assert mod.ack_set([row, dict(row)]) == {("s:s1", "waiting", "waiting")}
    store = InboxAckStore()
    store.ack("c:c1", "ended_work", fingerprint("ended_work", ""))
    assert mod.ack_set(store.records()) == {
        ("c:c1", "ended_work", fingerprint("ended_work", ""))}


def test_question_list_is_the_phones_question_list():
    listed = [{"id": "t1", "text": "Which?"}]
    flat = {"text": "Flat?"}
    assert mod.question_list({"questions": listed, "question": flat}) == listed
    assert mod.question_list({"questions": [], "question": flat}) == [flat]
    assert mod.question_list({"question": flat}) == [flat]
    assert mod.question_list({"questions": [], "question": {"text": ""}}) == []
    assert mod.question_list({"question": "not a dict"}) == []
    assert mod.question_list({}) == []
    assert mod.question_list(None) == []
    assert mod.question_list("row") == []


def test_forget_waiting_drops_only_a_waiting_hide(tmp_path):
    """Waiting's fingerprint is a fixed token, so a hide kept past the next
    prompt would silence every later wait of that session (25 Sep 2026)."""
    store = InboxAckStore(path=tmp_path / "acks.json")
    store.ack("s:one", "waiting", "waiting")
    store.ack("s:two", "question", "question:abc")
    store.forget_waiting("one")
    store.forget_waiting("two")
    store.forget_waiting("")
    keys = {row["key"] for row in store.records()}
    assert keys == {"s:two"}
    again = InboxAckStore(path=tmp_path / "acks.json")
    again.load()
    assert {row["key"] for row in again.records()} == {"s:two"}, "the drop is saved"


def test_a_new_prompt_forgets_the_sessions_waiting_hide(tmp_path):
    from types import SimpleNamespace

    from dark_army_daemon.daemon import BobDaemon

    store = InboxAckStore(path=tmp_path / "acks.json")
    store.ack("s:sid", "waiting", "waiting")
    fake = SimpleNamespace(_session_states={}, _inbox_acks=store)
    BobDaemon._step_prompt(fake, "sid", 100.0)
    assert store.records() == []
    assert fake._session_states["sid"]["state"] == "thinking"
