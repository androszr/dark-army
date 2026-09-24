import json
import sqlite3

import pytest

from dark_army_daemon import decision_store as ds


@pytest.fixture
def store(tmp_path):
    clock = [10000000.0]
    result = ds.DecisionStore(tmp_path / "decisions.db", clock=lambda: clock[0])
    result.open()
    result.test_clock = clock
    yield result
    result.close()


def facts(text="Choose", sid="s", root="/a", source="call"):
    return dict(root=root, project="same-name", session_id=sid, source_id=source,
                kind="question", question_text=text,
                questions=[{"text": text, "options": ["yes", "no"]}])


def test_restart_continuity_and_new_generation_after_clear(store):
    first = store.observe("q", facts())
    store.close(); store.open()
    assert store.observe("q", facts())["id"] == first["id"]
    store.finish("q")
    assert store.observe("q", facts())["id"] != first["id"]


def test_multi_question_is_one_episode(store):
    data = facts()
    data["questions"].append({"text": "Second", "options": ["a", "b"]})
    item = store.observe("q", data)
    assert len(item["questions"]) == 2
    assert len(store.query(roots={"/a"})["items"]) == 1


def test_old_receipt_retains_original_and_delivery_after_replacement(store):
    first = store.observe("q", facts())
    rid = store.receipt("phone", [first["id"]])
    store.finish("q", "delivered_unconfirmed", "yes", "keystrokes delivered", episode_id=first["id"])
    store.finish("q")
    store.observe("q", facts("New"))
    store.close(); store.open()
    item = store.query(roots={"/a"}, device="phone", receipt_id=rid)["items"][0]
    assert item["question_text"] == "Choose"
    assert item["outcome"] == "yes"
    assert item["status"] == "delivered_unconfirmed"


def test_absence_never_claims_answer_and_late_delivery_cannot_touch_successor(store):
    first = store.observe("q", facts())
    second = store.observe("q", facts("New"))
    store.finish("q", "delivered_unconfirmed", "yes", "delivery", episode_id=first["id"])
    page = store.query(roots={"/a"})
    by_id = {item["id"]: item for item in page["items"]}
    assert by_id[second["id"]]["status"] == "open"
    assert by_id[first["id"]]["status"] == "delivered_unconfirmed"
    assert by_id[first["id"]]["outcome"] == "yes"


def test_receipt_immutable_members_and_device_ownership(store):
    a = store.observe("a", facts())
    b = store.observe("b", facts("Other", "b", "/b"))
    rid = store.receipt("one", [a["id"], b["id"]])
    store.observe("c", facts("unrelated", "s"))
    page = store.query(roots={"/a", "/b"}, device="one", receipt_id=rid)
    assert {item["id"] for item in page["items"]} == {a["id"], b["id"]}
    assert not store.query(roots={"/a", "/b"}, device="two", receipt_id=rid)["available"]
    scoped = store.query(roots={"/b"}, device="one", receipt_id=rid)
    assert not scoped["available"]
    assert all(item["root"] == "/b" for item in scoped["items"])


def test_receipt_does_not_guess_unknown_target(store):
    store.observe("q", facts())
    assert store.receipt("phone", ["unknown"]) is None


def test_watermark_paging_excludes_new_arrivals(store):
    for index in range(3):
        store.observe(str(index), facts(str(index)))
    first = store.query(roots={"/a"}, limit=1)
    store.observe("new", facts("new"))
    rest = store.query(roots={"/a"}, page_cursor=first["next_cursor"], upper_cursor=first["upper_cursor"])
    assert len(rest["items"]) == 2
    assert rest["upper_cursor"] == 3


def test_read_age_expiry_does_not_regress_watermark_or_hide_gap(store):
    store.observe("q", facts()); store.observe("q", facts("new"))
    store.test_clock[0] += 91 * 86400
    page = store.query(roots={"/a"}, cursor=2)
    assert page["items"] == []
    assert page["upper_cursor"] == 3
    assert page["history_gap"]


def test_event_cap_gap_and_record_bounds(store, monkeypatch):
    monkeypatch.setattr(ds, "MAX_EVENTS", 2)
    monkeypatch.setattr(ds, "MAX_RECORDS", 2)
    for n in range(5):
        store.test_clock[0] += 1
        item = store.observe(str(n), facts(str(n)))
        store.receipt("phone", [item["id"]])
    assert store.query(roots={"/a"}, cursor=1)["history_gap"]
    for table in ("events", "episodes", "receipts"):
        assert store.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] <= 2


def test_clock_rollback_keeps_sequence_and_retention_monotonic(store):
    store.observe("a", facts())
    store.test_clock[0] -= 1000
    store.observe("b", facts())
    items = store.query(roots={"/a"})["items"]
    assert items[0]["cursor"] > items[1]["cursor"]
    assert items[0]["opened_at"] >= items[1]["opened_at"]


def test_future_schema_is_unavailable_and_preserves_unknown_data(tmp_path):
    path = tmp_path / "future.db"
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version=999")
        db.execute("CREATE TABLE future(value)")
    with pytest.raises(ValueError, match="newer"):
        ds.DecisionStore(path).open()
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 999
        assert db.execute("SELECT * FROM future").fetchall() == []


def test_private_files_and_coverage_failure(store):
    for suffix in ("", "-wal", "-shm"):
        assert (store.path.parent / (store.path.name + suffix)).stat().st_mode & 0o777 == 0o600
    store.failed = True
    store.observe("q", facts())
    page = store.query(roots={"/a"})
    assert page["coverage"]["storage_gap"]
    assert page["coverage"]["complete_before_observation"] is False


@pytest.mark.parametrize("value", [None, "bad", "z" * 36, {}, 3])
def test_uuid_validation_never_raises(value):
    assert not ds.uuid_ok(value)


def test_card_transitions_deduplicate_restart(store):
    card = {"id": "c", "root": "/a", "title": "Work", "updated_at": 1, "closed_by": "agent", "close_note": "Completed."}
    store.card_event(card, "card_done", {})
    store.close(); store.open()
    store.card_event(card, "card_done", {})
    items = store.query(roots={"/a"})["items"]
    assert len(items) == 1
    assert items[0]["provenance"] == "agent declaration"
    assert "accept" not in json.dumps(items)


def test_unresolved_first_globally_latest_episode_only_and_stable_pages(store):
    old = store.observe("early", facts("Early"))
    for n in range(105):
        store.observe(str(n), facts(str(n)))
        store.finish(str(n), "answered_observed", "Observed result", "test source")
    store.finish("early", "answered_observed", "Answered", "test source")
    waiting = store.observe("waiting", facts("Still waiting"))
    first = store.query(roots={"/a"}, limit=100)
    assert first["items"][0]["id"] == waiting["id"]
    assert len([i for i in first["items"] if i["id"] == old["id"]]) == 1
    assert next(i for i in first["items"] if i["id"] == old["id"])["status"] == "answered_observed"
    store.observe("new", facts("Arrived while reading"))
    rest = store.query(roots={"/a"}, upper_cursor=first["upper_cursor"], page_cursor=first["next_cursor"])
    assert len(first["items"] + rest["items"]) == 107
    assert len({i["id"] for i in first["items"] + rest["items"]}) == 107
    assert not any(i["question_text"] == "Arrived while reading" for i in rest["items"])


def test_manual_clear_keeps_plan_attachment_and_clears_manual_episode(store):
    card = dict(id="c", root="/a", title="Work", updated_at=1, manual_steps="Check it")
    store.card_event(card, "card_plan_attached", {})
    store.card_event(card, "card_manual", {})
    card.update(updated_at=2, manual_steps="")
    store.card_event(card, "card_manual_clear", {})
    page = store.query(roots={"/a"})
    assert len(page["items"]) == 2
    assert all(item["status"] != "open" for item in page["items"])
    assert {i["kind"] for i in page["items"]} == {"card_plan_attached", "card_manual_clear"}


def test_forward_columns_preserved_and_question_budget_strips_metadata(store):
    store.db.execute("ALTER TABLE episodes ADD COLUMN future TEXT NOT NULL DEFAULT 'kept'")
    raw = facts("界" * 7000)
    raw["questions"][0].update(secret="never store", options=["yes", {"secret": "no"}])
    item = store.observe("q", raw)
    assert len(item["question_text"]) + len(json.dumps(item["questions"], ensure_ascii=False)) <= 8002
    assert item["truncated"]
    assert "secret" not in json.dumps(item)
    assert store.db.execute("SELECT future FROM episodes").fetchone()[0] == "kept"


def test_large_group_and_page_fit_sealed_transport(store):
    from dark_army_daemon import relay
    identities = []
    for n in range(32):
        item = store.observe(str(n), facts("界\\\"" * 2600, str(n)))
        identities.append(item["id"])
    receipt = store.receipt("phone", identities)
    page = store.query(roots={"/a"}, device="phone", receipt_id=receipt)
    assert page["available"] and len(page["items"]) == 32
    assert page["next_cursor"] is None
    wire = relay.seal_frame(b"a" * 32, relay.DIR_MAC_TO_PHONE, 1, "reply",
                            {"status": 200, "body": json.dumps(page)}, ns=relay.HOME)
    opened, error = relay.open_frame(b"a" * 32, relay.DIR_MAC_TO_PHONE, wire, 0, ns=relay.HOME)
    assert not error
    assert len(json.loads(opened["body"]["body"])["items"]) == 32


@pytest.mark.parametrize("question", [
    {"text": "x" * 5000}, {"text": "Q", "options": ["x" * 500]},
    {"text": "Q", "options": ["x"] * 9}])
def test_each_structured_truncation_is_visible(store, question):
    item = store.observe("q", dict(root="/a", questions=[question]))
    assert item["truncated"]


def test_question_count_and_outcome_truncation_visible(store):
    item = store.observe("q", dict(root="/a", questions=[{"text": "Q"}] * 9))
    assert item["truncated"] and len(item["questions"]) == 8
    item = store.observe("other", facts())
    store.finish("other", "answered_observed", "x" * 2100, "result")
    saved = next(row for row in store.query(roots={"/a"})["items"] if row["id"] == item["id"])
    assert saved["truncated"] and len(saved["outcome"]) == 2000


def test_direct_replacement_preserves_delivered_evidence(store):
    first = store.observe("q", facts())
    receipt = store.receipt("phone", [first["id"]])
    store.finish("q", "delivered_unconfirmed", "Answer choices sent: yes", "keystrokes")
    store.observe("q", facts("Replacement", source="second"))
    item = store.query(roots={"/a"}, device="phone", receipt_id=receipt)["items"][0]
    assert item["status"] == "delivered_unconfirmed"
    assert item["outcome"] == "Answer choices sent: yes"
    assert not store.db.execute("SELECT active FROM episodes WHERE id=?", (first["id"],)).fetchone()[0]


def test_observed_result_matches_original_without_bootstrapping_history(store):
    first = store.observe("question:s", facts())
    store.observe("question:s", facts("Replacement", source="second"))
    store.observed_question_result("s", "call", "User selected yes")
    store.observed_question_result("s", "unknown-old-call", "old result")
    before = store.query(roots={"/a"})
    store.observed_question_result("s", "call", "User selected yes")
    store.close(); store.open()
    after = store.query(roots={"/a"})
    assert before == after
    assert len(after["items"]) == 2
    original = next(item for item in after["items"] if item["id"] == first["id"])
    assert original["status"] == "answered_observed"
    assert original["outcome"] == "User selected yes"
    assert after["items"][0]["source_id"] == "second"


def test_late_delivery_and_disappearance_cannot_downgrade_observed_answer(store):
    first = store.observe("question:s", facts())
    receipt = store.receipt("phone", [first["id"]])
    store.observed_question_result("s", "call", "Observed yes")
    store.finish("question:s", "delivered_unconfirmed", "Choices sent: yes", "keystrokes", episode_id=first["id"])
    store.finish("question:s", episode_id=first["id"])
    store.observe("question:s", facts("Next", source="next"))
    item = store.query(roots={"/a"}, device="phone", receipt_id=receipt)["items"][0]
    assert item["status"] == "answered_observed"
    assert item["outcome"] == "Observed yes"
    assert item["provenance"] == "observed matching question tool result"


@pytest.mark.parametrize("roots", [{"/a"}, {"/a", "/b"}])
def test_moved_card_cannot_overlay_other_project_outcome_on_old_receipt(store, roots):
    card = dict(id="c", root="/a", title="Work", updated_at=1, manual_steps="Check A")
    store.card_event(card, "card_manual", {})
    first = store.query(roots={"/a"})["items"][0]
    receipt = store.receipt("phone", [first["id"]])
    card.update(root="/b", updated_at=2, manual_steps="PRIVATE B STEPS")
    store.card_event(card, "card_manual", {})
    page = store.query(roots=roots, device="phone", receipt_id=receipt)
    assert not page["available"] and page["items"] == []
    assert "moved" in page["reason"]
    assert "PRIVATE B" not in json.dumps(page)


def test_manual_cycles_keep_completed_record_and_old_receipt(store):
    card = dict(id="c", root="/a", title="Work", updated_at=1, manual_steps="Verify release A")
    store.card_event(card, "card_manual", {})
    first = store.query(roots={"/a"})["items"][0]
    receipt = store.receipt("phone", [first["id"]])
    card.update(updated_at=2, manual_steps="")
    store.card_event(card, "card_manual_clear", {})
    cleared = store.query(roots={"/a"}, device="phone", receipt_id=receipt)
    card.update(updated_at=3, manual_steps="Verify release B")
    store.card_event(card, "card_manual", {})
    page = store.query(roots={"/a"})
    assert len(page["items"]) == 2
    second = next(item for item in page["items"] if item["id"] != first["id"])
    assert second["status"] == "open" and second["question_text"] == "Verify release B"
    old = store.query(roots={"/a"}, device="phone", receipt_id=receipt)
    assert old["items"] == cleared["items"]
    assert old["items"][0]["status"] == "answered_observed"
    assert old["items"][0]["question_text"] == "Verify release A"
    store.close(); store.open()
    assert store.query(roots={"/a"}, device="phone", receipt_id=receipt)["items"] == old["items"]
    # Clear only the current cycle; a repeated clear creates no phantom item.
    card.update(updated_at=4, manual_steps="")
    store.card_event(card, "card_manual_clear", {})
    card.update(updated_at=5)
    store.card_event(card, "card_manual_clear", {})
    assert len(store.query(roots={"/a"})["items"]) == 2


def test_repeated_done_cycles_have_distinct_receipt_identities(store):
    card = dict(id="c", root="/a", title="Work", updated_at=1, close_note="Release A done")
    store.card_event(card, "card_done", {})
    first = store.query(roots={"/a"})["items"][0]
    receipt = store.receipt("phone", [first["id"]])
    card.update(updated_at=2, close_note="Release B done")
    store.card_event(card, "card_done", {})
    store.card_event(card, "card_done", {})
    assert len(store.query(roots={"/a"})["items"]) == 2
    old = store.query(roots={"/a"}, device="phone", receipt_id=receipt)["items"][0]
    assert old["id"] == first["id"] and old["outcome"] == "Release A done"


def test_receipt_overlay_preserves_outcome_truncation_flag(store):
    first = store.observe("question:s", facts())
    receipt = store.receipt("phone", [first["id"]])
    store.finish("question:s", "answered_observed", "Y" * 2100, "observed result")
    item = store.query(roots={"/a"}, device="phone", receipt_id=receipt)["items"][0]
    assert item["truncated"] and len(item["outcome"]) == 2000


@pytest.mark.parametrize("kind,detail,words", [
    ("card_dispatched", {}, "implementation terminal opened"),
    ("card_dispatched", {"phase": "refinement"}, "refinement terminal opened"),
    ("card_plan_attached", {}, "plan was attached"),
    ("card_done", {"closed_by": "user"}, "declared Done"),
])
def test_proven_card_recovery_closes_failure_and_preserves_receipt(store, kind, detail, words):
    card = dict(id="c", root="/a", title="Work", updated_at=1)
    store.card_event(card, "card_dispatch_failed", {"error": "No editor window"})
    first = store.query(roots={"/a"})["items"][0]
    receipt = store.receipt("phone", [first["id"]])
    store.reconcile_missing(set(), include_permissions=True)
    assert store.query(roots={"/a"})["items"][0]["status"] == "open"
    card.update(updated_at=2)
    store.card_event(card, kind, detail)
    recovered = store.query(roots={"/a"}, device="phone", receipt_id=receipt)["items"][0]
    assert recovered["id"] == first["id"] and recovered["status"] == "superseded"
    assert recovered["question_text"] == "No editor window"
    assert words in recovered["outcome"] and "No editor window" in recovered["outcome"]
    assert not any(i["status"] == "open" for i in store.query(roots={"/a"})["items"])
    store.close(); store.open()
    store.card_event(card, kind, detail)
    assert store.query(roots={"/a"}, device="phone", receipt_id=receipt)["items"][0] == recovered


def test_failure_cycles_and_stale_success_replay_keep_old_receipt_identity(store):
    card = dict(id="c", root="/a", title="Work", updated_at=1)
    store.card_event(card, "card_dispatch_failed", {"error": "First failure"})
    first = store.query(roots={"/a"})["items"][0]
    receipt = store.receipt("phone", [first["id"]])
    store.card_event(dict(card, updated_at=2), "card_dispatch_failed", {"error": "Second failure"})
    assert len(store.query(roots={"/a"})["items"]) == 1
    recovered_card = dict(card, updated_at=3)
    store.card_event(recovered_card, "card_dispatched", {})
    recovered = store.query(roots={"/a"}, device="phone", receipt_id=receipt)["items"][0]
    assert recovered["question_text"] == "First failure"
    assert "Second failure" in recovered["outcome"]
    store.card_event(dict(card, updated_at=4), "card_done", {})
    store.card_event(dict(card, updated_at=5), "card_dispatch_failed", {"error": "Next run failed"})
    store.card_event(recovered_card, "card_dispatched", {})
    page = store.query(roots={"/a"})
    assert len(page["items"]) == 3
    current = page["items"][0]
    assert current["id"] != first["id"] and current["status"] == "open"
    assert current["question_text"] == "Next run failed"
    assert store.query(roots={"/a"}, device="phone", receipt_id=receipt)["items"][0] == recovered


def test_legacy_failure_receipt_still_shows_error_after_recovery(store):
    card = dict(id="c", root="/a", title="Work", updated_at=1)
    store.card_event(card, "card_dispatch_failed", {"error": "Legacy failure"})
    # Model the persisted shape from the pre-fix writer: no question_text.
    row = store.db.execute("SELECT id,data FROM episodes").fetchone()
    item = json.loads(row["data"])
    item["question_text"] = ""
    with store.db:
        store.db.execute("UPDATE episodes SET data=?", (json.dumps(item),))
        store.db.execute("UPDATE events SET data=?", (json.dumps(item),))
    receipt = store.receipt("phone", [row["id"]])
    store.card_event(dict(card, updated_at=2), "card_dispatched", {})
    recovered = store.query(roots={"/a"}, device="phone", receipt_id=receipt)["items"][0]
    assert recovered["status"] == "superseded" and "Legacy failure" in recovered["outcome"]
