# host/tests/test_card_timeline.py
"""A timeline on every card — `plans/2026-09-13-card-timeline.md`.

Four layers, each with its own seam. `card_timeline.compose` over a
hand-built facts dict (pure). `card_timeline_facts` and the `plan_attached`
boundary on a temp `board.db` with `test_board_lifecycle.py`'s clock. The
`_card_collect` seam on `ApiServer`, the two on-open readers that carry the
timeline and the sync page that must not. And source pins over the Swift:
the phone's `CardTimeline` is written exactly as the panel's, both views
draw through it, and no daemon file has heard of it.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

import pytest

from dark_army_daemon import board_lifecycle
from dark_army_daemon import card_timeline as ct
from dark_army_daemon import event_log
from dark_army_daemon.api_server import ApiServer
from dark_army_daemon.board import BoardStore
from dark_army_daemon.daemon import BobDaemon
from dark_army_daemon.event_log import EventLog
from tests.test_board_lifecycle import (  # noqa: F401
    Clock, _seeded_timeline, card, clock, observe, store,
)

ROOT = Path(__file__).resolve().parents[2]
PANEL = ROOT / "panel" / "Sources" / "BobPanel"
PHONE = ROOT / "ios" / "BobPhone"
RULE = PANEL / "CardTimeline.swift"
PANEL_VIEW = PANEL / "BoardCardSheet.swift"
PHONE_VIEW = PHONE / "CardDetailView.swift"
PHONE_MODELS = PHONE / "Models.swift"
DAEMON = ROOT / "host" / "dark_army_daemon"

#: The one fixture table `elapsed_text` and both Swift `elapsed` copies run.
ELAPSED_TABLE = (
    (0, "0s"), (59, "59s"), (60, "1m"), (3599, "59m"), (3600, "1h"),
    (7800, "2h 10m"), (86400, "1d"), (100800, "1d 4h"), (None, ""),
)


def _facts(**over):
    base = {"ledger": None, "boundaries": [], "outcome_events": [],
            "session_ids": [], "left_censored": False, "tracking_since": None}
    base.update(over)
    return base


def _card(**over):
    base = {"id": "card-1", "created_at": 1000.0, "column_name": "backlog",
            "link_state": "", "refine_state": "", "queue_state": "",
            "plan_path": "", "session_id": "", "refine_session_id": "",
            "manual_steps": "", "outcome_status": "unaccepted",
            "intended_benefit": ""}
    base.update(over)
    return base


def _kinds(timeline):
    return [s["kind"] for s in timeline["steps"]]


# --- compose, over a hand-built facts dict ---------------------------------------


@pytest.mark.parametrize("seconds,want", ELAPSED_TABLE)
def test_elapsed_text_over_the_shared_table(seconds, want):
    assert ct.elapsed_text(seconds) == want


def test_elapsed_text_refuses_a_negative_and_junk():
    assert ct.elapsed_text(-1) == ""
    assert ct.elapsed_text("soon") == ""


def test_compose_walks_the_canonical_order_with_labels_and_elapsed():
    facts = _facts(boundaries=[
        {"ts": 1010.0, "kind": "refine_started", "provenance": "refine"},
        {"ts": 1100.0, "kind": "plan_attached", "provenance": "attach"},
        {"ts": 1300.0, "kind": "queue_started", "provenance": "queue"},
        {"ts": 1310.0, "kind": "attempt_started", "provenance": "dispatch"},
        {"ts": 1310.0, "kind": "launch_started", "provenance": "dispatch"},
        {"ts": 1312.0, "kind": "session_bound", "provenance": "bind"},
        {"ts": 1400.0, "kind": "attempt_completed", "disposition": "completed"},
    ])
    card = _card(plan_path="plans/x.md", plan_approved_at=1200.0,
                 dispatched_at=1310.0, session_ended_at=1400.0)
    out = ct.compose(card, facts, [], 2000.0)
    assert out["available"] is True
    assert _kinds(out) == ["created", "refine_started", "plan_attached",
                           "plan_approved", "queued", "started", "picked_up",
                           "ended"]
    labels = [s["label"] for s in out["steps"]]
    assert labels == ["Written down", "Planning started", "Plan attached",
                      "Plan approved", "Queued", "Started",
                      "Assistant picked it up", "Assistant finished"]
    # Episode bookkeeping is not a step.
    assert "launch_started" not in labels
    gaps = [s["since_previous_seconds"] for s in out["steps"]]
    assert gaps == [None, 10.0, 90.0, 100.0, 100.0, 10.0, 2.0, 88.0]
    assert [s["elapsed"] for s in out["steps"]] == ["", "10s", "1m", "1m", "1m",
                                                     "10s", "2s", "1m"]
    assert all(s["observed"] and s["durable"] for s in out["steps"])
    assert out["truncated"] is False
    assert out["recent_only_note"] == ""


def test_an_unobserved_plan_attach_sits_at_its_rank_and_says_not_observed():
    facts = _facts(boundaries=[
        {"ts": 1300.0, "kind": "queue_started", "provenance": "queue"},
    ])
    card = _card(plan_path="plans/x.md", plan_approved_at=1200.0)
    out = ct.compose(card, facts, [], 2000.0)
    assert _kinds(out) == ["created", "plan_attached", "plan_approved", "queued"]
    attached = out["steps"][1]
    assert attached["at"] is None and attached["observed"] is False
    assert attached["gap"] == "not observed"
    assert attached["since_previous_seconds"] is None
    # The observed neighbour after it cannot measure across the hole.
    approved = out["steps"][2]
    assert approved["since_previous_seconds"] is None
    assert approved["gap"] == "not observed"
    assert approved["elapsed"] == ""
    # And the next pair measures again.
    assert out["steps"][3]["since_previous_seconds"] == 100.0


def test_before_tracking_began_on_a_left_censored_card():
    facts = _facts(left_censored=True, tracking_since=1500.0, boundaries=[
        {"ts": 1600.0, "kind": "queue_started", "provenance": "observed"},
    ])
    card = _card(plan_approved_at=1200.0, dispatched_at=1400.0)
    out = ct.compose(card, facts, [], 2000.0)
    assert _kinds(out) == ["created", "plan_approved", "started", "queued"]
    assert out["steps"][0]["gap"] == ""
    assert out["steps"][1]["gap"] == "before tracking began"
    assert out["steps"][1]["since_previous_seconds"] is None
    assert out["steps"][2]["gap"] == "before tracking began"
    # The first boundary after tracking began measures from the last stamp.
    assert out["steps"][3]["since_previous_seconds"] == 200.0
    assert out["left_censored"] is True and out["tracking_since"] == 1500.0


def test_permission_asks_fold_into_one_step_per_attempt():
    facts = _facts(session_ids=["sid-1"], boundaries=[
        {"ts": 1310.0, "kind": "attempt_started", "provenance": "dispatch"},
    ])
    rows = [
        {"kind": "permission_ask", "ts": 1330.0, "session_id": "sid-1",
         "card_id": "", "detail": {"tool": "Bash"}},
        {"kind": "permission_ask", "ts": 1320.0, "session_id": "sid-1",
         "card_id": "", "detail": {"tool": "Edit"}},
        {"kind": "permission_ask", "ts": 1340.0, "session_id": "sid-1",
         "card_id": "", "detail": {"tool": "Write"}},
        # Another session's ask is not this card's.
        {"kind": "permission_ask", "ts": 1335.0, "session_id": "sid-9",
         "card_id": "", "detail": {"tool": "Bash"}},
    ]
    out = ct.compose(_card(), facts, rows, 2000.0)
    assert _kinds(out) == ["created", "started", "permission_asks"]
    asks = out["steps"][2]
    assert asks["label"] == "3 permission asks"
    assert asks["note"].startswith("3 asks")
    assert asks["at"] == 1320.0 and asks["durable"] is False
    assert asks["since_previous_seconds"] == 10.0


def test_a_start_refusal_comes_from_the_diary_with_its_text():
    rows = [{"kind": "card_dispatch_failed", "ts": 1050.0, "session_id": "",
             "card_id": "card-1", "text": "x could not start — no window",
             "detail": {}},
            {"kind": "card_dispatch_failed", "ts": 1060.0, "session_id": "",
             "card_id": "other", "text": "not mine", "detail": {}}]
    out = ct.compose(_card(), _facts(), rows, 2000.0)
    assert _kinds(out) == ["created", "start_failed"]
    assert out["steps"][1]["note"] == "x could not start — no window"
    assert out["steps"][1]["durable"] is False


def test_moved_done_only_without_a_submission():
    plain = ct.compose(_card(column_name="done", done_at=1500.0), _facts(), [], 2000.0)
    assert _kinds(plain) == ["created", "moved_done"]
    submitted = ct.compose(
        _card(column_name="done", done_at=1500.0),
        _facts(outcome_events=[{"ts": 1500.0, "kind": "submitted", "actor": "observer"}]),
        [], 2000.0)
    assert _kinds(submitted) == ["created", "submitted"]


def test_a_stamp_stands_in_only_where_no_boundary_of_its_kind_exists():
    """`dispatched_at` is the latest attempt's; mixed with boundaries it would
    draw a restarted card's first start at its last start's time."""
    facts = _facts(boundaries=[
        {"ts": 1100.0, "kind": "attempt_started", "provenance": "dispatch"},
        {"ts": 1200.0, "kind": "attempt_completed", "disposition": "completed"},
        {"ts": 1300.0, "kind": "attempt_started", "provenance": "dispatch"},
    ])
    out = ct.compose(_card(dispatched_at=1300.0, session_ended_at=1200.0),
                     facts, [], 2000.0)
    assert [(s["kind"], s["at"]) for s in out["steps"]] == [
        ("created", 1000.0), ("started", 1100.0), ("ended", 1200.0),
        ("started", 1300.0)]
    assert out["steps"][3]["note"] == "attempt 2"
    assert out["steps"][1]["note"] == ""


def test_a_revision_request_is_one_step_not_two():
    facts = _facts(outcome_events=[
        {"ts": 1500.0, "kind": "submitted", "actor": "observer"},
        {"ts": 1600.0, "kind": "revision_requested", "actor": "user"},
        {"ts": 1600.0, "kind": "decision_revision", "actor": "user"},
        {"ts": 1600.0, "kind": "revision_note", "actor": "user"},
    ])
    out = ct.compose(_card(), facts, [], 2000.0)
    assert _kinds(out) == ["created", "submitted", "revision_requested"]


def test_truncation_drops_the_oldest_durable_first_and_says_so():
    boundaries = [{"ts": 1000.0 + i, "kind": "queue_started", "provenance": "queue"}
                  for i in range(ct.MAX_TIMELINE_STEPS + 5)]
    rows = [{"kind": "card_dispatch_failed", "ts": 1001.5, "session_id": "",
             "card_id": "card-1", "text": "refused", "detail": {}}]
    out = ct.compose(_card(), _facts(boundaries=boundaries), rows, 3000.0)
    assert out["truncated"] is True
    assert len(out["steps"]) == ct.MAX_TIMELINE_STEPS
    # The diary's (non-durable) step survived; the oldest durable ones went.
    assert "start_failed" in _kinds(out)
    assert "created" not in _kinds(out)


def test_recent_only_note_rides_only_an_old_card():
    young = ct.compose(_card(created_at=1000.0), _facts(), [], 1000.0 + 3600)
    assert young["recent_only_note"] == ""
    old = ct.compose(_card(created_at=1000.0), _facts(), [],
                     1000.0 + event_log.RETENTION_SECONDS + 1)
    assert old["recent_only_note"] == ct.RECENT_ONLY_NOTE
    assert "older than a day" in ct.RECENT_ONLY_NOTE


def test_compose_survives_no_ledger_row_at_all():
    out = ct.compose(_card(), None, None, 2000.0)
    assert out["available"] is True
    assert _kinds(out) == ["created"]
    assert out["open"] is None


@pytest.mark.parametrize("card_over,steps,kind,since", [
    (dict(refine_state="live"), [{"kind": "refine_started", "at": 1010.0}],
     "planning", 1010.0),
    (dict(refine_state="dispatching"), [], "planning", None),
    (dict(queue_state="queued", queued_at=1300.0), [], "queued", 1300.0),
    (dict(link_state="dispatching", dispatched_at=1310.0), [], "starting", 1310.0),
    (dict(link_state="live"), [{"kind": "picked_up", "at": 1312.0}], "working", 1312.0),
    (dict(column_name="in_progress", manual_steps="check it"),
     [{"kind": "manual_flagged", "at": 1500.0}], "manual_check", 1500.0),
    (dict(column_name="in_progress", manual_steps="check it"), [], "manual_check", None),
    (dict(column_name="in_progress", link_state="ended", session_ended_at=1400.0),
     [], "ended", 1400.0),
    (dict(column_name="done", done_at=1500.0, closed_by="Vex"), [], "review", 1500.0),
    # A hand drag into Done (no `closed_by`) awaits no review — the
    # acceptance rung answers where there is an objective.
    (dict(column_name="done", done_at=1500.0, reviewed_at=None,
          intended_benefit="faster"), [], "acceptance", None),
    (dict(column_name="done", done_at=1500.0, reviewed_at=1600.0,
          intended_benefit="faster"), [], "acceptance", 1600.0),
    (dict(column_name="backlog", plan_path="plans/x.md"),
     [{"kind": "plan_attached", "at": 1100.0}], "planned", 1100.0),
    (dict(column_name="backlog", plan_path="plans/x.md"),
     [{"kind": "plan_attached", "at": None}], "planned", None),
])
def test_open_state_over_its_table(card_over, steps, kind, since):
    out = ct.open_state(_card(**card_over), steps, 2000.0)
    assert out is not None
    assert out["kind"] == kind
    assert out["since"] == since
    assert out["label"] and not out["label"].endswith("since")


def test_open_state_is_none_where_nothing_waits():
    accepted = _card(column_name="done", done_at=1500.0, reviewed_at=1600.0,
                     outcome_status="accepted", intended_benefit="faster")
    assert ct.open_state(accepted, [], 2000.0) is None
    assert ct.open_state(_card(column_name="prep"), [], 2000.0) is None
    # Dragged into Done by hand, no objective: nothing to review or accept.
    dragged = _card(column_name="done", done_at=1500.0)
    assert ct.open_state(dragged, [], 2000.0) is None


def test_every_kind_has_a_rank_and_a_label_and_the_words_are_there():
    kinds = [k for k, _r, _l in ct.KINDS]
    assert len(kinds) == len(set(kinds)) == 22
    assert [r for _k, r, _l in ct.KINDS] == list(range(22))
    assert ct.NOT_OBSERVED == "not observed"
    assert ct.MAX_TIMELINE_STEPS == 200


# --- the store: facts and the plan_attached boundary --------------------------------


def _stamp_created(store, cid, when):
    """`create` stamps `created_at` off the wall clock; the fixture's clock
    starts at 1000, so the card's first moment is put on that clock too."""
    store._conn.execute("UPDATE cards SET created_at=? WHERE id=?", (when, cid))
    store._conn.commit()


def test_card_timeline_facts_carries_the_boundaries_in_order(store, clock):
    c = card(store)
    _seeded_timeline(store, clock, c)
    facts = store.card_timeline_facts(c["id"])
    assert facts is not None
    kinds = [b["kind"] for b in facts["boundaries"]]
    assert kinds[:2] == ["queue_started", "attempt_started"]
    assert kinds.count("attempt_started") == 2
    assert kinds.count("session_bound") == 2
    assert kinds.count("attempt_completed") == 2
    assert [e["kind"] for e in facts["outcome_events"]] == [
        "submitted", "revision_requested", "decision_revision", "submitted",
        "submitted", "accepted"]
    assert sorted(facts["session_ids"]) == ["sid-1", "sid-2"]
    assert facts["left_censored"] is False
    assert facts["tracking_since"] == 1000.0
    # Evidence text is never selected.
    assert all("evidence" not in e for e in facts["outcome_events"])
    assert store.card_timeline_facts("nope") is None


def test_elapsed_between_neighbours(store, clock):
    """The success criterion: the 120-second fixture read as moments. Queue
    10 s, launch 0 s to bound, 40 s of work, 30 s of review, a 10 s second
    queue, 20 s of rework, 10 s to acceptance — every figure a real
    interval between two observed moments."""
    c = card(store)
    _stamp_created(store, c["id"], clock.utc)
    t0 = _seeded_timeline(store, clock, c)
    out = ct.compose(store.get(c["id"]), store.card_timeline_facts(c["id"]),
                     [], clock.utc)
    walk = [(s["kind"], s["since_previous_seconds"]) for s in out["steps"]]
    assert walk == [
        ("created", None),
        ("queued", 0.0),                # pressed the moment it was written
        ("started", 10.0),              # queue: 10
        ("picked_up", 0.0),             # launch 0 → bound
        ("ended", 40.0),
        ("submitted", 0.0),
        ("queued", 30.0),               # review: 30 (revision at 80, re-queued)
        ("revision_requested", 0.0),
        ("started", 10.0),              # second queue: 10
        ("picked_up", 0.0),
        ("ended", 20.0),                # rework's second attempt
        ("submitted", 0.0),
        ("submitted", 10.0),            # the acceptance's own submission
        ("accepted", 0.0),
    ]
    assert out["steps"][8]["note"] == "attempt 2"
    assert all(s["observed"] for s in out["steps"])
    assert out["steps"][0]["at"] == t0
    assert out["steps"][-1]["at"] == t0 + 120
    # Dragged into Done by hand and then accepted: nothing waits on it, and
    # a card with no `closed_by` never reads "Waiting for your review".
    assert out["open"] is None


def test_attach_plan_writes_exactly_one_plan_attached_boundary(store, clock):
    c = card(store)
    got, detail = store.attach_plan(c["id"], "plans/x.md", "planner-1")
    assert got is not None, detail
    rows = list(store._conn.execute(
        "SELECT kind, provenance, episode_id FROM lifecycle_boundaries WHERE card_id=?",
        (c["id"],)))
    assert [(r["kind"], r["provenance"], r["episode_id"]) for r in rows] == [
        ("plan_attached", "attach", None)]
    # A second attach is refused and writes none.
    again, detail = store.attach_plan(c["id"], "plans/y.md", "planner-1")
    assert again is None
    assert store._conn.execute(
        "SELECT COUNT(*) FROM lifecycle_boundaries WHERE card_id=? AND kind='plan_attached'",
        (c["id"],)).fetchone()[0] == 1


def test_plan_attached_survives_prune_and_is_ignored_by_the_replays(store, clock):
    """Episode-less, so `lifecycle_prune` never selects it and the report's
    replays (`replay_close_time` / `replay_disposition`) skip it."""
    c = card(store)
    got, detail = store.attach_plan(c["id"], "plans/x.md", "planner-1")
    assert got is not None, detail
    store.lifecycle_prune(now=clock.utc + 10 * 366 * 86400)
    rows = [dict(r) for r in store._conn.execute(
        "SELECT * FROM lifecycle_boundaries WHERE card_id=?", (c["id"],))]
    assert [r["kind"] for r in rows] == ["plan_attached"]
    assert board_lifecycle.replay_close_time(rows, 1, clock.utc + 1) is None
    assert board_lifecycle.replay_disposition(rows, 1, clock.utc + 1) is None
    assert store.card_timeline_facts(c["id"])["boundaries"][-1]["kind"] == "plan_attached"
    out = ct.compose(store.get(c["id"]), store.card_timeline_facts(c["id"]), [], clock.utc)
    step = next(s for s in out["steps"] if s["kind"] == "plan_attached")
    assert step["observed"] is True
    assert out["open"] == {"kind": "planned", "label": "Planned, waiting to be started",
                           "since": step["at"]}


def test_a_raising_boundary_still_attaches(store, clock, monkeypatch):
    c = card(store)

    def boom(*a, **k):
        raise sqlite3.OperationalError("disk")

    monkeypatch.setattr(store, "_boundary", boom)
    got, detail = store.attach_plan(c["id"], "plans/x.md", "planner-1")
    assert got is not None, detail
    assert got["plan_path"] == "plans/x.md" and got["column_name"] == "backlog"
    assert store._conn.execute(
        "SELECT COUNT(*) FROM lifecycle_boundaries WHERE card_id=?",
        (c["id"],)).fetchone()[0] == 0
    # And a card planned before this build reads "not observed".
    out = ct.compose(store.get(c["id"]), store.card_timeline_facts(c["id"]), [], clock.utc)
    step = next(s for s in out["steps"] if s["kind"] == "plan_attached")
    assert step["observed"] is False and step["gap"] == ct.NOT_OBSERVED


# --- the read seam ----------------------------------------------------------------------


@pytest.fixture
def daemon(tmp_path):
    d = BobDaemon(sessions_path=tmp_path / "sessions.json")
    s = BoardStore(tmp_path / "board.db")
    s.connect()
    d._board = s
    log = EventLog(path=tmp_path / "log.jsonl")
    log.open()
    d._event_log = log
    try:
        yield d, s
    finally:
        log.close()
        s.close()


def test_card_collect_carries_the_timeline_only_when_asked(daemon):
    d, s = daemon
    c, detail = s.create({"title": "t", "root": "/project", "tool": "claude"})
    assert c is not None, detail
    d._event_log.append("card_dispatch_failed", card_id=c["id"], title="t",
                        detail={"error": "no window"})
    srv = ApiServer(d, port=0)
    on_open = srv._card_collect(c["id"], with_plan=True, with_timeline=True)
    assert on_open["timeline"]["available"] is True
    assert "timeline" not in on_open["cards"][0]
    kinds = [st["kind"] for st in on_open["timeline"]["steps"]]
    assert kinds == ["created", "start_failed"]
    assert on_open["timeline"]["steps"][1]["note"].startswith("t could not start")
    # The sync shape — what the phone's background sweep reads — has none.
    sync = srv._card_collect(c["id"], with_plan=True)
    assert "timeline" not in sync
    loopback = srv._card_collect(c["id"], with_plan=False)
    assert "timeline" not in loopback
    # A card that does not exist states it.
    gone = srv._card_collect("nope", with_plan=True, with_timeline=True)
    assert gone["timeline"] == {"available": False, "reason": "no such card",
                                "steps": [], "open": None}


def test_a_failing_read_is_stated_not_raised(daemon, monkeypatch):
    d, s = daemon
    c, _ = s.create({"title": "t", "root": "/project", "tool": "claude"})

    def boom(*a, **k):
        raise sqlite3.OperationalError("locked")

    monkeypatch.setattr(s, "card_timeline_facts", boom)
    report = ApiServer(d, port=0)._card_collect(c["id"], with_plan=True,
                                                 with_timeline=True)
    assert report["available"] is True
    assert report["timeline"]["available"] is False
    assert report["timeline"]["reason"] == "the timeline could not be read"


def test_the_sync_page_stays_timeline_free(daemon):
    d, s = daemon
    c, _ = s.create({"title": "t", "root": "/project", "tool": "claude"})
    page = ApiServer(d, port=0)._cards_sync_page([c["id"]], {}, 1_000_000)
    dumped = json.dumps(page)
    assert '"timeline"' not in dumped


def test_the_marker_is_published():
    d = BobDaemon.__new__(BobDaemon)
    d.__dict__.setdefault("_observers", [])
    flags = d._pipeline_writable()
    assert flags["card_timeline_supported"] is True


# --- the two on-open readers over the wire ------------------------------------------


from tests.test_home_seal import (  # noqa: E402,F401
    server, ledger, machine, attach_dir, _no_fleet_snapshot, pair_plain, inner, fetch,
)
from dark_army_daemon import relay  # noqa: E402


@pytest.mark.asyncio
async def test_both_on_open_reads_carry_the_timeline(server, tmp_path, monkeypatch):
    api, d, lan_port = server
    s = BoardStore(tmp_path / "wire.db")
    s.connect()
    d._board = s
    try:
        c, _ = s.create({"title": "t", "root": "/project", "tool": "claude"})
        status, body = await fetch(f"/api/board?card={c['id']}", port=api._port)
        assert status == 200
        loopback = json.loads(body)
        assert loopback["timeline"]["available"] is True
        assert [st["kind"] for st in loopback["timeline"]["steps"]] == ["created"]

        phone = await pair_plain(lan_port)
        monkeypatch.setattr(relay, "lease_valid", lambda _: False)
        status, sealed = await inner(lan_port, phone["key"], "card",
                                     {"query": f"card={c['id']}"}, 1)
        assert status == 200
        report = json.loads(sealed)
        assert report["timeline"]["available"] is True
        assert report["timeline"]["steps"] == loopback["timeline"]["steps"]
    finally:
        d._board = None
        s.close()


# --- source pins over the Swift ------------------------------------------------------


def _read(path: Path) -> str:
    assert path.is_file(), f"missing file: {path}"
    text = path.read_text()
    assert text.strip(), f"empty file: {path}"
    return text


def _block(text: str, start: str) -> str:
    assert start in text, f"missing block: {start!r}"
    i = text.index(start)
    depth = 0
    for j in range(i, len(text)):
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                return text[i: j + 1]
    raise AssertionError(f"unbalanced block at {start!r}")


def _code(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return "\n".join(line.split("//")[0] for line in text.splitlines())


def _squashed(text: str) -> str:
    return " ".join(_code(text).split())


#: The three wire structs live in the phone's `Models.swift` (`CardFull`
#: names them, and `test_phone_inbox.py` compiles that file with only two
#: siblings); the rule lives beside `CardSections` in the card screen.
BLOCKS = (("struct CardTimelineStep: Decodable, Identifiable {", PHONE_MODELS),
          ("struct CardTimelineOpen: Decodable, Equatable {", PHONE_MODELS),
          ("struct CardTimelineReport: Decodable {", PHONE_MODELS),
          ("enum CardTimeline {", PHONE_VIEW))


@pytest.mark.parametrize("start,phone_path", BLOCKS)
def test_the_phone_copy_is_written_exactly_as_the_panels(start, phone_path):
    assert _squashed(_block(_read(RULE), start)) == _squashed(_block(_read(phone_path), start))
    # And exactly one copy on each side.
    assert _read(RULE).count(start) == 1
    assert _read(phone_path).count(start) == 1


def test_a_doctored_copy_is_noticed():
    panel = _block(_read(RULE), "enum CardTimeline {")
    doctored = panel.replace('notObserved = "not observed"', 'notObserved = "unseen"', 1)
    assert _squashed(doctored) != _squashed(panel)


def test_the_rule_is_foundation_only_and_words_only():
    src = _read(RULE)
    assert "import SwiftUI" not in _code(src)
    assert "Color" not in _code(_block(src, "enum CardTimeline {"))
    assert 'static let notObserved = "not observed"' in src
    assert 'static let recentOnlyNote = "Permission asks older than a day are not kept."' in src
    assert src.count("static func elapsed(_ seconds: Double?) -> String") == 1
    assert "static func openLine(_ report: CardTimelineReport, now: Double) -> String?" in src
    # The phone's copy sits right after `CardSections`, inside an existing file,
    # and the structs after `CardPlan` in the models file `CardFull` lives in.
    phone = _read(PHONE_VIEW)
    assert phone.index("enum CardSections {") < phone.index("enum CardTimeline {")
    assert "struct CardTimelineReport" not in phone
    models = _read(PHONE_MODELS)
    assert models.index("struct CardPlan: Decodable {") < models.index("struct CardTimelineStep")
    assert models.index("struct CardTimelineStep") < models.index("struct CardTimelineReport")
    # The structs never reach into the rule, so the models file compiles alone.
    for start in ("struct CardTimelineStep: Decodable, Identifiable {",
                  "struct CardTimelineOpen: Decodable, Equatable {",
                  "struct CardTimelineReport: Decodable {"):
        assert "CardTimeline." not in _code(_block(models, start)), start


def test_both_views_draw_through_the_rule_once():
    for path in (PANEL_VIEW, PHONE_VIEW):
        text = _code(_read(path))
        assert text.count("CardTimeline.rows(") == 1, path.name
        assert text.count("CardTimeline.openLine(") == 1, path.name
        assert "case .timeline: timelineSection" in text, path.name
    # The phone can meet an older Mac and gates the row on the marker; the
    # panel ships with its daemon and decodes no markers (20 Sep 2026).
    assert "cardTimelineSupported" in _block(
        _code(_read(PHONE_VIEW)),
        "private func hasContent(_ s: CardSections.Section) -> Bool {")
    assert "cardTimelineSupported" not in _code(_read(PANEL_VIEW))
    panel = _code(_read(PANEL_VIEW))
    # One structured task keyed on the card and its stage, never the
    # revision; no unstructured `Task {` beside it; and no clock of its own.
    assert '.task(id: timelineFetchKey) { await loadTimeline() }' in panel
    assert 'Task { await loadTimeline() }' not in panel
    assert '"\\(live?.id ?? "")-\\(stage)"' in _block(panel, "private var timelineFetchKey: String {")
    assert "TimelineView" not in _block(panel, "private var timelineSection: some View {")
    assert "now: report.generatedAt" in panel
    phone = _code(_read(PHONE_VIEW))
    assert "now: report.generatedAt" in phone
    assert ".lineLimit(" not in phone


def test_the_daemon_has_never_heard_of_the_swift_rule():
    for path in sorted(DAEMON.rglob("*.py")):
        assert "CardTimeline" not in path.read_text(), path.name
    api = (DAEMON / "api_server.py").read_text()
    assert api.count("with_timeline=True") == 2
    for line in api.splitlines():
        if "timeline" in line:
            assert "_OMITTABLE_SECTIONS" not in line
            assert "_CLOCK_FIELDS" not in line
            assert "def state" not in line
