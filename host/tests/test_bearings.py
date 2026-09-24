# host/tests/test_bearings.py
"""The Bearings digest: four fixed sections, composed once, no clock.

Seams: snapshot / prompt / card / event dict literals for `bearings.compose`,
the same `_row` shape `test_live_activity.py` uses. No daemon, no I/O.
"""

from __future__ import annotations

import inspect

from dark_army_daemon import bearings, inbox_ack
from dark_army_daemon.event_log import _has_forbidden

NOW = 1_700_000_000.0

EMPTY_TEXT = (
    "## Needs your call\n"
    "Nobody needs your call.\n"
    "\n"
    "## Recently landed\n"
    "Nothing has landed lately.\n"
    "\n"
    "## Underway\n"
    "Nothing is underway.\n"
    "\n"
    "## Coming next\n"
    "Nothing is lined up next.\n"
)

FIXTURE_TEXT = (
    "## Needs your call\n"
    "- Cipher wants to run Bash: rm -rf build\n"
    "- Mira asked: Should we ship?\n"
    "- Closed work was closed by Vex and awaits your review\n"
    "\n"
    "## Recently landed\n"
    "- Closed work was finished\n"
    "\n"
    "## Underway\n"
    "- Ledger is on Ship the digest in beta — working, large run\n"
    "- Cipher is working in alpha on unbound work\n"
    "\n"
    "## Coming next\n"
    "- Queued card in delta — Waiting for a free slot\n"
    "- Next card in delta\n"
)


def _row(sid, nickname="", idle=0.0, questions=None, **extra):
    row = {"session_id": sid, "nickname": nickname, "idle_seconds": idle}
    if questions is not None:
        row["questions"] = questions
    row.update(extra)
    return row


def _compose(**overrides):
    kwargs = dict(
        snapshot={}, prompts={}, notified=(), cards=(), acks=(),
        events=(), diary_available=True, since=None, now=NOW, skip_sessions=(),
    )
    kwargs.update(overrides)
    return bearings.compose(**kwargs)


def _fixture():
    snapshot = {
        "running": [
            _row("run-1", "Cipher", 1, card_title="unbound work",
                 project="alpha"),
            _row("run-2", "Ledger", 2, card_title="Ship the digest",
                 project="beta"),
        ],
        "waiting": [
            _row("wait-1", "Mira", 10,
                 questions=[{"text": "Should we ship?"}], project="alpha"),
        ],
    }
    prompts = {
        "run-1": {"tool_name": "Bash", "description": "rm -rf build",
                  "port": 9999, "request_id": "r-1"},
    }
    cards = [
        {"id": "c-done", "title": "Closed work", "column_name": "done",
         "closed_by": "s-1", "closed_by_name": "Vex", "reviewed_at": None,
         "project": "gamma"},
        {"id": "c-ip", "title": "Ship the digest", "column_name": "in_progress",
         "session_id": "run-2", "link_state": "live", "project": "beta",
         "run_health": {"class": "large", "attention": False}},
        {"id": "c-q", "title": "Queued card", "column_name": "backlog",
         "queue_state": "queued", "queue_reason": "Waiting for a free slot",
         "project": "delta"},
        {"id": "c-b", "title": "Next card", "column_name": "backlog",
         "project": "delta"},
        {"id": "c-p", "title": "Not next", "column_name": "prep",
         "project": "delta"},
    ]
    events = [
        {"kind": "card_done", "text": "Closed work was finished", "ts": 100.0,
         "card_id": "c-done", "session_id": "", "project": "gamma"},
        {"kind": "session_start", "text": "Cipher started", "ts": 90.0,
         "card_id": "", "session_id": "run-1", "project": "alpha"},
    ]
    return dict(snapshot=snapshot, prompts=prompts, notified=(), cards=cards,
                acks=(), events=events, diary_available=True, since=None,
                now=NOW, skip_sessions=())


def _kinds(items):
    return [item["kind"] for item in items]


def _texts(items):
    return [item["text"] for item in items]


def test_an_empty_machine_is_four_empty_sections():
    out = _compose()
    assert [section["key"] for section in out["sections"]] == [
        spec[0] for spec in bearings.SECTIONS]
    for section, spec in zip(out["sections"], bearings.SECTIONS):
        assert section["items"] == []
        assert section["title"] == spec[1]
        assert section["empty"] == spec[2]
        assert set(section) >= {"key", "title", "items", "empty"}
    assert out["text"] == EMPTY_TEXT
    assert out["available"] is True
    assert out["generated_at"] == NOW


def test_the_fixture_digest_is_byte_pinned():
    first = bearings.compose(**_fixture())
    second = bearings.compose(**_fixture())
    assert first["text"] == FIXTURE_TEXT
    assert first == second
    assert first["text"] == second["text"]
    for section in first["sections"]:
        for item in section["items"]:
            assert tuple(item) == bearings.ITEM_KEYS or set(item) == set(
                bearings.ITEM_KEYS)
            for key in bearings.ITEM_KEYS:
                assert key in item


def test_needs_your_call_admits_only_actionable_entries():
    snapshot = {
        "running": [_row("r1", "Cipher")],
        "finished": [_row("f1", "Ledger")],
    }
    cards = [
        {"id": "c-att", "title": "Worrying", "column_name": "in_progress",
         "session_id": "r1", "run_health": {"class": "large", "attention": True}},
        {"id": "c-plan", "title": "Ready", "column_name": "backlog",
         "plan_path": "/tmp/plan.md"},
        {"id": "c-q", "title": "Queued", "column_name": "backlog",
         "queue_state": "queued", "queue_reason": "Waiting for a free slot"},
        {"id": "c-prev", "title": "Preview", "column_name": "done",
         "done_preview": True, "reviewed_at": 1.0, "closed_by": "s-1"},
    ]
    events = [{"kind": "card_plan_attached", "text": "a plan landed", "ts": 1.0}]
    items = bearings.needs_your_call(snapshot, {}, (), cards, ())
    assert items == []
    out = _compose(snapshot=snapshot, cards=cards, events=events)
    assert out["sections"][0]["items"] == []


def test_needs_your_call_order_is_permission_question_look_stopped_review():
    snapshot = {
        "running": [_row("perm", "Zed")],
        "waiting": [
            _row("q1", "Ann", questions=[{"text": "Q?"}]),
            _row("w1", "Nyx"),
        ],
    }
    prompts = {"perm": {"tool_name": "Bash", "description": "ls"}}
    cards = [
        {"id": "c-look", "title": "Look please", "needs_you": True,
         "session_id": "other"},
        {"id": "c-check", "title": "Check please", "manual_check_due": True,
         "manual_steps": "1. Open it."},
        {"id": "c-rev", "title": "Review please", "column_name": "done",
         "closed_by": "s-1", "closed_by_name": "Vex"},
    ]
    items = bearings.needs_your_call(snapshot, prompts, (), cards, ())
    assert _kinds(items) == [
        "permission", "question", "ended_work", "manual_check", "waiting",
        "awaiting_review",
    ]


def test_one_entry_per_subject():
    cards = [{"id": "c1", "title": "Look", "needs_you": True, "session_id": "s1"}]
    stopped = {"waiting": [_row("s1", "Vex")]}
    items = bearings.needs_your_call(stopped, {}, (), cards, ())
    assert _kinds(items) == ["ended_work"]
    prompted = {"running": [_row("s1", "Vex")]}
    prompts = {"s1": {"tool_name": "Bash", "description": "ls"}}
    items = bearings.needs_your_call(prompted, prompts, (), cards, ())
    assert _kinds(items) == ["permission", "ended_work"]


def test_an_acknowledged_subject_stays_hidden():
    snap = {"waiting": [_row("s1", "Vex")]}
    waiting_fp = inbox_ack.fingerprint("waiting", "")
    acks = [{"key": "s:s1", "kind": "waiting", "fp": waiting_fp}]
    assert bearings.needs_your_call(snap, {}, (), [], acks) == []

    asked = {"waiting": [_row("s1", "Vex",
                              questions=[{"text": "New question"}])]}
    stale = [{"key": "s:s1", "kind": "question",
              "fp": inbox_ack.fingerprint("question", "Old question")}]
    items = bearings.needs_your_call(asked, {}, (), [], stale)
    assert items and items[0]["kind"] == "question"

    card = {"id": "c1", "title": "Done", "column_name": "done",
            "closed_by": "s-1", "closed_by_name": "Vex"}
    any_ack = [
        {"key": "c:c1", "kind": "ended_work",
         "fp": inbox_ack.fingerprint("ended_work", "")},
        {"key": "c:c1", "kind": "waiting", "fp": "waiting"},
    ]
    items = bearings.needs_your_call({}, {}, (), [card], any_ack)
    assert items and items[0]["kind"] == "awaiting_review"


def test_since_narrows_recently_landed_only():
    events = [
        {"kind": "card_done", "text": "old", "ts": 10.0, "card_id": "",
         "session_id": "", "project": ""},
        {"kind": "card_done", "text": "new", "ts": 50.0, "card_id": "",
         "session_id": "", "project": ""},
    ]
    snapshot = {"running": [_row("r1", "Cipher", card_title="work")]}
    cards = [
        {"id": "c-ip", "title": "Live", "column_name": "in_progress",
         "session_id": "r1", "link_state": "live"},
        {"id": "c-b", "title": "Next", "column_name": "backlog"},
    ]
    unbounded = _compose(snapshot=snapshot, cards=cards, events=events)
    filtered = [row for row in events if row["ts"] > 20]
    bounded = _compose(snapshot=snapshot, cards=cards, events=filtered, since=20)
    assert bounded["sections"][0] == unbounded["sections"][0]
    assert bounded["sections"][2] == unbounded["sections"][2]
    assert bounded["sections"][3] == unbounded["sections"][3]
    assert _texts(bounded["sections"][1]["items"]) == ["new"]
    assert _texts(unbounded["sections"][1]["items"]) == ["old", "new"]
    assert "since" not in inspect.signature(bearings.needs_your_call).parameters
    assert "since" not in inspect.signature(bearings.underway).parameters
    assert "since" not in inspect.signature(bearings.coming_next).parameters
    assert "since" in inspect.signature(bearings.recently_landed).parameters


def test_mission_controls_row_is_skipped():
    snapshot = {"running": [_row("mc", "Cipher", card_title="briefing")]}
    prompts = {"mc": {"tool_name": "Bash", "description": "curl"}}
    assert bearings.needs_your_call(
        snapshot, prompts, (), [], [], skip_sessions={"mc"}) == []
    assert bearings.underway(snapshot, [], skip_sessions={"mc"}) == []
    assert bearings.needs_your_call(snapshot, prompts, (), [], [])
    assert bearings.underway(snapshot, [])


def test_underway_keeps_board_order_and_names_unbound_sessions():
    cards = [
        {"id": "c2", "title": "Second", "column_name": "in_progress",
         "session_id": "s2", "link_state": "live", "project": "p"},
        {"id": "c1", "title": "First", "column_name": "in_progress",
         "session_id": "s1", "link_state": "live", "project": "p"},
        {"id": "c-prep", "title": "Draft", "column_name": "prep",
         "refine_session_id": "s4"},
    ]
    snapshot = {
        "running": [
            _row("s1", "Ann", project="p"),
            _row("s2", "Zed", project="p"),
            # Sorted by name after the cards: "Mira" before "Quiet".
            _row("s3", "Quiet", project="q", card_title="loose"),
            _row("s4", "Mira", project="p"),
        ],
    }
    items = bearings.underway(snapshot, cards)
    assert [item["kind"] for item in items] == ["card", "card", "refining", "session"]
    assert items[0]["card_id"] == "c2"
    assert items[1]["card_id"] == "c1"
    assert items[0]["text"] == "Zed is on Second in p — working"
    assert items[1]["text"] == "Ann is on First in p — working"
    assert items[2]["text"] == "Mira is refining Draft in p"
    assert items[2]["kind"] == "refining"
    assert items[3]["text"] == "Quiet is working in q on loose"
    assert items[3]["kind"] == "session"


def test_coming_next_is_backlog_in_board_order_with_queue_words():
    cards = [
        {"id": "p", "title": "Prep", "column_name": "prep", "project": "alpha"},
        {"id": "q", "title": "Queued", "column_name": "backlog",
         "queue_state": "queued", "queue_reason": "Waiting for a free slot",
         "project": "alpha"},
        {"id": "n", "title": "Next", "column_name": "backlog", "project": "alpha"},
        {"id": "e", "title": "Errored", "column_name": "backlog",
         "dispatch_error": "spawn failed", "project": "alpha"},
    ]
    items = bearings.coming_next(cards)
    assert _texts(items) == [
        "Queued in alpha — Waiting for a free slot",
        "Next in alpha",
        "Errored in alpha — spawn failed",
    ]
    assert _kinds(items) == ["card", "card", "card"]


def test_no_forbidden_key_rides_the_digest():
    """The prompt's `port` is in the input and must not appear in the body.

    Sections carry a field named `key` (the section id), which is also a
    `FORBIDDEN_KEYS` token meant for secrets — so the walk is over items
    and the serialized body, not the section envelopes.
    """
    import json
    out = bearings.compose(**_fixture())
    dumped = json.dumps(out)
    assert "port" not in dumped
    assert "9999" not in dumped
    for section in out["sections"]:
        assert _has_forbidden(section["items"]) is None
        assert _has_forbidden(section["title"]) is None
        assert _has_forbidden(section["empty"]) is None
    assert _has_forbidden(out["text"]) is None


def test_the_text_never_says_bob():
    assert "bob" not in bearings.compose(**_fixture())["text"].lower()


def test_a_newline_in_an_item_does_not_split_the_render():
    """A permission description that contains a newline and a section
    heading must flatten into one bullet, not mint a fifth `## ` line."""
    snapshot = {"running": [_row("s1", "Cipher", 1)]}
    prompts = {"s1": {"tool_name": "Bash",
                      "description": "echo hi\n## Recently landed"}}
    out = _compose(snapshot=snapshot, prompts=prompts)
    headings = [line for line in out["text"].splitlines() if line.startswith("## ")]
    assert headings == [
        "## Needs your call",
        "## Recently landed",
        "## Underway",
        "## Coming next",
    ]
    assert "- Cipher wants to run Bash: echo hi ## Recently landed" in out["text"]
