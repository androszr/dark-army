# host/tests/test_live_activity.py
"""The phone's Live Activity, Mac side: the pure subject rule
(`live_activity.py`), the daemon glue (`_push_live_activity`) and the
connector's leg (`RelayConnector.push_activity`).

Seams: snapshot dict literals for the composer, a fake connector recording
`push_activity` calls (`test_alert_delivery.py`'s pattern) for the glue, a
scripted `_http` on a real connector for the wire, and `relay.py` on the
pytest-isolated state dir for the token ledger.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest

from dark_army_daemon import alerts, cast, devices, fleet_figures, live_activity, paths, relay
from dark_army_daemon import relay_client
from dark_army_daemon import inbox_ack
from dark_army_daemon.api_server import ApiServer
from dark_army_daemon.daemon import BobDaemon

ROOT = Path(__file__).resolve().parents[2]
PUSH_JS = ROOT / "relay" / "api" / "push.js"
INBOX_SWIFT = ROOT / "ios" / "BobPhone" / "Inbox.swift"


@pytest.fixture(autouse=True)
def _no_fleet_snapshot(monkeypatch):
    monkeypatch.setattr(BobDaemon, "_schedule_agents_push", lambda self: None)


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "RELAY_PATH", tmp_path / "relay.json")
    monkeypatch.setattr(paths, "DEVICES_PATH", tmp_path / "devices.json")
    monkeypatch.setattr(paths, "STATE_DIR", tmp_path / "state")
    relay.reset()
    devices.reset()
    yield tmp_path / "relay.json"
    relay.reset()
    devices.reset()


def _row(sid, nickname="", idle=0.0, questions=None):
    row = {"session_id": sid, "nickname": nickname, "idle_seconds": idle}
    if questions is not None:
        row["questions"] = questions
    return row


# --- the pure rule: who is the subject ------------------------------------------


def test_an_empty_fleet_has_no_subject():
    assert live_activity.subject({}, {}) is None
    assert live_activity.subject({"running": [], "waiting": []}, {}) is None


def test_a_lone_waiting_row_is_attention():
    snap = {"waiting": [_row("s1", "Vex", 30)]}
    out = live_activity.subject(snap, {})
    assert out == {"session_id": "s1", "nickname": "Vex", "name": "",
                   "kind": "attention", "idle_seconds": 30.0, "quiet_since": 0.0}


def test_a_running_row_with_an_open_prompt_outranks_a_longer_attention_wait():
    snap = {"running": [_row("p1", "Cipher", 5)],
            "waiting": [_row("w1", "Vex", 600)]}
    prompts = {"p1": {"request_id": "r1", "session_id": "p1"}}
    out = live_activity.subject(snap, prompts)
    assert out["session_id"] == "p1"
    assert out["kind"] == "permission"


def test_a_question_outranks_attention():
    snap = {"waiting": [_row("w1", "Vex", 600),
                        _row("q1", "Mira", 5, questions=[{"text": "ok?"}])]}
    out = live_activity.subject(snap, {})
    assert out["session_id"] == "q1"
    assert out["kind"] == "question"


def test_a_question_admits_nothing_by_itself_the_phones_rule():
    """Admission parity, the Mac → phone direction: the phone admits a row
    by `waiting`, an open prompt or a notification card (`notifyIds`) and
    lets a question decide only the *kind*. A stale `questions` list on a
    running row that nobody lists used to push a `question` card for an
    agent the phone had never shown."""
    snap = {"running": [_row("r1", "Mira", 5, questions=[{"text": "ok?"}])]}
    assert live_activity.subject(snap, {}) is None
    # Listed by a notification card, the same row is a question.
    out = live_activity.subject(snap, {}, notified=["r1"])
    assert out["session_id"] == "r1" and out["kind"] == "question"


def test_a_notification_card_admits_a_row_the_phone_lists():
    """The phone → Mac direction: a running row with an active notification
    card is on the phone's list (`notified`), so the phone starts a card
    for it. A Mac that did not read `_active_notifications` ended that card
    at once, the phone's watcher unregistered, the next reconcile started
    again — an end/start loop on the shared push bucket."""
    snap = {"running": [_row("n1", "Vex", 12)]}
    assert live_activity.subject(snap, {}) is None
    out = live_activity.subject(snap, {}, notified=["n1"])
    assert out == {"session_id": "n1", "nickname": "Vex", "name": "",
                   "kind": "attention", "idle_seconds": 12.0, "quiet_since": 0.0}
    # An id the buckets do not carry admits nothing — rows only, both ends.
    assert live_activity.subject(snap, {}, notified=["ghost"]) is None


def test_a_card_bound_to_a_stopped_session_takes_its_entry_the_phones_one_entry_rule():
    """`PhoneInbox.oneEntryPerSubject`: a `needs_you` (or `manual_check_due`)
    card naming a session is a LOOK AT, ranked before the session's own
    STOPPED entry, and the first entry naming a session wins — so the
    session is not a subject. A prompt or a question outranks the card and
    the session stays. `refine_session_id` binds nothing here, as the
    phone's card entry carries only `session_id`."""
    # w1 must rank first on its own: two STOPPED rows sort by title, and
    # "Audit" sorts before "Cipher" (it was "Darlene" before "Elliot").
    snap = {"waiting": [_row("w1", "Audit", 600), _row("w2", "Cipher", 5)]}
    cards = [{"id": "c1", "title": "Ship it", "needs_you": True, "session_id": "w1"}]
    assert live_activity.subject(snap, {}, cards=cards)["session_id"] == "w2"
    cards = [{"id": "c1", "title": "Ship it", "manual_check_due": True, "session_id": "w1"}]
    assert live_activity.subject(snap, {}, cards=cards)["session_id"] == "w2"
    cards = [{"id": "c1", "needs_you": True, "session_id": "w1"},
             {"id": "c2", "needs_you": True, "session_id": "w2"}]
    assert live_activity.subject(snap, {}, cards=cards) is None
    # The card outranks nothing above STOPPED.
    prompts = {"w1": {"request_id": "r1", "session_id": "w1"}}
    assert live_activity.subject(snap, prompts, cards=cards)["session_id"] == "w1"
    asked = {"waiting": [_row("w1", "Audit", 600, questions=[{"text": "?"}])]}
    assert live_activity.subject(asked, {}, cards=cards)["session_id"] == "w1"
    # A card that needs nobody, or binds by refine only, drops no entry.
    for card in ({"id": "c1", "session_id": "w1"},
                 {"id": "c1", "needs_you": True, "refine_session_id": "w1"},
                 {"id": "c1", "needs_you": False, "manual_check_due": False, "session_id": "w1"}):
        assert live_activity.subject(snap, {}, cards=[card])["session_id"] == "w1"
    assert live_activity.carded_sessions(["junk", None, {"needs_you": True}]) == set()


def test_the_admission_rule_is_the_phones_line_for_line():
    """`Inbox.swift`'s admission and one-entry rule, pinned by text so a
    change on either end trips here."""
    swift = INBOX_SWIFT.read_text()
    assert "if !waiting && prompts.isEmpty && !notified { continue }" in swift
    assert "let notifyIds = Set(\n            snapshot.notifications.map(\\.sessionId)" in swift
    assert "return seen.insert(item.sessionId).inserted" in swift
    cards = swift.split("private static func cardItems(")[1]
    assert "if card.needsYou {" in cards and "} else if card.manualCheckDue {" in cards
    assert "sessionId: card.sessionId, category: nil" in cards
    src = (ROOT / "host" / "dark_army_daemon" / "live_activity.py").read_text()
    # Since 23 Sep 2026 the admission is `listed_sessions`, which the phone
    # leg of the buzz reads too: the per-row test and the set's three rungs.
    assert "if sid not in listed:" in src
    listed_src = src.split("def listed_sessions(")[1].split("\ndef ")[0]
    assert "if bucket in LIVE_BUCKETS:" in listed_src
    assert "if sid in waiting_ids or sid in prompted or sid in notified_ids}" in listed_src
    assert "return listed | (prompted - present)" in listed_src
    # The phone's side: live buckets only, a prompt with no row its own item.
    assert "guard liveBuckets.contains(category) else { continue }" in swift
    assert "for (id, prompts) in promptsBySession {\n            if seen.contains(id) { continue }" in swift
    daemon_src = (ROOT / "host" / "dark_army_daemon" / "daemon.py").read_text()
    # The Mac's `notified` is the *published* card list — `notifyIds` is
    # `snapshot.notifications`, which `_notification_snapshot()` builds and
    # the hysteresis window withholds from — never the raw dict's keys.
    assert ("notified = [str(n.get(\"session_id\") or \"\")\n"
            "                    for n in self._notification_snapshot()]") in daemon_src
    assert "notified=notified, cards=cards)" in daemon_src


# --- listed_sessions: the phone's admission set (23 Sep 2026) ---------------------


def test_listed_sessions_is_empty_for_nothing():
    assert live_activity.listed_sessions({}, {}) == set()
    assert live_activity.listed_sessions(None, None, None) == set()


def test_listed_sessions_admits_the_phones_three_rungs_and_nothing_else():
    snap = {"waiting": [_row("w1", "Vex")],
            "running": [_row("p1", "Mira"), _row("n1", "Cal"), _row("r1", "Bea")],
            "sleeping": [_row("z1", "Zed")]}
    prompts = {"p1": {"request_id": "r1"}, "gone": {"request_id": "r2"},
               "spent": None}
    listed = live_activity.listed_sessions(snap, prompts, notified=["n1", "", None])
    assert "w1" in listed                     # the waiting bucket
    assert "p1" in listed                     # a running row with a prompt
    assert "n1" in listed                     # a running row with a card
    assert "r1" not in listed and "z1" not in listed   # busy, nothing open
    # A prompt with no row: the phone draws a read-only item for it.
    assert "gone" in listed
    assert "spent" not in listed and "" not in listed
    assert listed == {"w1", "p1", "n1", "gone"}


def test_a_card_on_a_session_with_no_row_is_not_listed():
    """The phone's `sessionItems` walks rows: a card admits a row, it is no
    item by itself — unlike a prompt with no row, which is. So neither the
    set nor `subject` names it."""
    assert live_activity.listed_sessions({}, {}, notified=["ghost"]) == set()
    assert live_activity.subject({}, {}, notified=["ghost"]) is None
    assert live_activity.listed_sessions({}, {"ghost": {"request_id": "r"}}) == {"ghost"}


@pytest.mark.parametrize("bucket", ["finished", "abandoned"])
def test_a_run_that_is_over_is_never_listed(bucket):
    """A prompt or a card on a finished or abandoned row lists nothing: the
    phone marks the row seen and skips it, and a prompt on it is not an
    orphan either."""
    snap = {bucket: [_row("o1", "Vex")], "running": [_row("r1", "Bea")]}
    prompts = {"o1": {"request_id": "r1"}}
    assert live_activity.listed_sessions(snap, prompts, notified=["o1"]) == set()
    # The same session also live in another bucket is the live row's.
    snap["running"].append(_row("o1", "Vex"))
    assert live_activity.listed_sessions(snap, prompts) == {"o1"}


def test_waiters_admits_exactly_the_listed_live_rows():
    """`waiters` reads `listed_sessions`, so the buzz gate and the Live
    Activity cannot disagree about any live row."""
    snap = {"waiting": [_row("w1", "Vex"), _row("w2", "Ada", questions=[{"text": "?"}])],
            "running": [_row("p1", "Mira"), _row("n1", "Cal"), _row("r1", "Bea")],
            "sleeping": [_row("z1", "Zed"), _row("zn", "Kit")]}
    prompts = {"p1": {"request_id": "r1"}, "gone": {"request_id": "r2"}}
    notified = ["n1", "zn", "ghost"]
    listed = live_activity.listed_sessions(snap, prompts, notified)
    live = {row["session_id"] for rows in snap.values() for row in rows}
    admitted = {w["session_id"] for w in live_activity.waiters(snap, prompts, notified)}
    assert admitted == listed & live == {"w1", "w2", "p1", "n1", "zn"}


# --- shown_sessions: the listed set less the person's dismissals ---------------


def _ack(key, kind, material=""):
    return {"key": key, "kind": kind, "fp": inbox_ack.fingerprint(kind, material)}


def _three_rungs():
    snap = {"waiting": [_row("w1", "Vex")],
            "running": [_row("p1", "Mira"), _row("n1", "Cal"), _row("r1", "Bea")],
            "sleeping": [_row("z1", "Zed")]}
    prompts = {"p1": {"request_id": "r1"}, "gone": {"request_id": "r2"}}
    return snap, prompts, ["n1"]


def test_shown_sessions_with_no_acks_is_listed_sessions():
    snap, prompts, notified = _three_rungs()
    listed = live_activity.listed_sessions(snap, prompts, notified)
    assert live_activity.shown_sessions(snap, prompts, notified) == listed
    assert live_activity.shown_sessions(snap, prompts, notified, cards=[], acks=[]) == listed
    assert live_activity.shown_sessions(None, None) == set()


def test_a_dismissed_waiting_row_is_not_shown():
    snap, prompts, notified = _three_rungs()
    acks = [_ack("s:w1", "waiting")]
    shown = live_activity.shown_sessions(snap, prompts, notified, acks=acks)
    assert shown == {"p1", "n1", "gone"}


def test_a_dismissed_row_admitted_by_a_notification_card_is_not_shown():
    """A running row admitted only by its notification card draws a
    `waiting` entry on the phone too, fingerprint `waiting`."""
    snap, prompts, notified = _three_rungs()
    acks = [_ack("s:n1", "waiting")]
    shown = live_activity.shown_sessions(snap, prompts, notified, acks=acks)
    assert "n1" not in shown and "w1" in shown


def test_a_dismissed_question_is_matched_on_ids_else_texts():
    with_ids = {"waiting": [_row("q1", "Vex", questions=[
        {"id": "t1", "text": "Which?"}, {"id": "t2", "text": "And?"}])]}
    acked = [_ack("s:q1", "question", "t1|t2")]
    assert live_activity.shown_sessions(with_ids, {}, acks=acked) == set()
    no_ids = {"waiting": [_row("q1", "Vex", questions=[
        {"text": "Which?"}, {"id": "t2", "text": "And?"}])]}
    acked = [_ack("s:q1", "question", "Which?|And?")]
    assert live_activity.shown_sessions(no_ids, {}, acks=acked) == set()


def test_an_ack_on_an_old_question_does_not_hide_a_new_one():
    snap = {"waiting": [_row("q1", "Vex", questions=[{"id": "t9", "text": "New?"}])]}
    stale = [_ack("s:q1", "question", "t1")]
    assert live_activity.shown_sessions(snap, {}, acks=stale) == {"q1"}


def test_a_waiting_ack_does_not_hide_a_row_that_now_asks():
    snap = {"waiting": [_row("q1", "Vex", questions=[{"text": "Now?"}])]}
    acks = [_ack("s:q1", "waiting")]
    assert live_activity.shown_sessions(snap, {}, acks=acks) == {"q1"}


def test_a_prompted_row_is_never_hidden_by_a_waiting_ack():
    snap, prompts, notified = _three_rungs()
    acks = [_ack("s:p1", "waiting"), _ack("s:p1", "permission")]
    assert "p1" in live_activity.shown_sessions(snap, prompts, notified, acks=acks)


def test_an_orphan_prompt_is_never_hidden():
    snap, prompts, notified = _three_rungs()
    acks = [_ack("s:gone", "waiting"), _ack("s:gone", "permission")]
    assert "gone" in live_activity.shown_sessions(snap, prompts, notified, acks=acks)


def test_the_flat_question_is_fingerprinted_like_the_phone():
    row = _row("q1", "Vex", questions=[])
    row["question"] = {"text": "Flat?"}
    snap = {"waiting": [row]}
    assert live_activity.shown_sessions(
        snap, {}, acks=[_ack("s:q1", "question", "Flat?")]) == set()
    # A `waiting` ack does not match: the phone's kind is `question`.
    assert live_activity.shown_sessions(
        snap, {}, acks=[_ack("s:q1", "waiting")]) == {"q1"}


def test_a_dismissed_session_with_a_surviving_card_stays_shown():
    snap = {"waiting": [_row("w1", "Vex")]}
    card = {"id": "c1", "needs_you": True, "session_id": "w1"}
    session_ack = _ack("s:w1", "waiting")
    card_ack = _ack("c:c1", "ended_work")
    assert live_activity.shown_sessions(
        snap, {}, cards=[card], acks=[session_ack]) == {"w1"}
    assert live_activity.shown_sessions(
        snap, {}, cards=[card], acks=[session_ack, card_ack]) == set()
    # Dismissing only the card lets the session's own entry stand.
    assert live_activity.shown_sessions(
        snap, {}, cards=[card], acks=[card_ack]) == {"w1"}


def test_a_hand_check_dismissed_on_old_steps_keeps_the_session_shown():
    snap = {"waiting": [_row("w1", "Vex")]}
    card = {"id": "c2", "manual_check_due": True, "session_id": "w1",
            "manual_steps": "1. Open it."}
    acks = [_ack("s:w1", "waiting"), _ack("c:c2", "manual_check", "1. Open it.")]
    assert live_activity.shown_sessions(snap, {}, cards=[card], acks=acks) == set()
    card["manual_steps"] = "1. Open it again."
    assert live_activity.shown_sessions(snap, {}, cards=[card], acks=acks) == {"w1"}


def test_a_card_never_adds_a_session_listed_sessions_left_out():
    snap = {"running": [_row("r1", "Bea")]}
    cards = [{"id": "c1", "needs_you": True, "session_id": "r1"},
             {"id": "c2", "needs_you": True, "session_id": "ghost"}]
    acks = [_ack("s:x", "waiting")]
    assert live_activity.shown_sessions(snap, {}, cards=cards, acks=acks) == set()
    assert live_activity.shown_sessions(snap, {}, notified=["ghost"],
                                        cards=cards, acks=acks) == set()


def test_the_composer_ignores_acks_the_accepted_drift():
    """`waiters` / `subject` take no acks: the Live Activity's drift is a
    recorded decision (`docs/phone-contract.md`), and Bearings subtracts
    acks after `waiters`. The buzz gate alone reads `shown_sessions`."""
    import inspect
    assert "acks" not in inspect.signature(live_activity.waiters).parameters
    assert "acks" not in inspect.signature(live_activity.subject).parameters
    snap = {"waiting": [_row("w1", "Vex")]}
    acks = [_ack("s:w1", "waiting")]
    assert live_activity.shown_sessions(snap, {}, acks=acks) == set()
    assert [w["session_id"] for w in live_activity.waiters(snap, {})] == ["w1"]
    assert live_activity.subject(snap, {})["session_id"] == "w1"


def test_a_question_with_no_text_is_not_a_question():
    snap = {"waiting": [_row("q1", "Mira", 5, questions=[{"text": ""}])]}
    out = live_activity.subject(snap, {})
    assert out["kind"] == "attention"


def test_two_attention_rows_are_ordered_by_title_then_session_id_never_by_wait():
    """The phone's `PhoneInbox.before`: inside a kind the title decides,
    then the target key. Idle time is not a rung — the phone starts the card
    for the head of *its* list, and a Mac ranking a tie by wait flipped the
    card to the other agent on its first update."""
    snap = {"waiting": [_row("b", "Bea", 10), _row("a", "Amy", 10),
                        _row("c", "Cal", 90)]}
    assert live_activity.subject(snap, {})["session_id"] == "a"
    # Case-insensitive, as `localizedStandardCompare` is.
    snap = {"waiting": [_row("b", "bea", 10), _row("a", "Amy", 10)]}
    assert live_activity.subject(snap, {})["session_id"] == "a"
    snap = {"waiting": [_row("x", "amy", 10), _row("a", "Bea", 10)]}
    assert live_activity.subject(snap, {})["session_id"] == "x"
    # Same title: the session id.
    snap = {"waiting": [_row("b", "Amy", 900), _row("a", "Amy", 10)]}
    assert live_activity.subject(snap, {})["session_id"] == "a"
    # A nicknameless row is titled by its name, else its session id — the
    # phone's `PhoneInboxItem.title` rule.
    snap = {"waiting": [_row("b", "", 10), {"session_id": "a", "name": "Zed",
                                             "idle_seconds": 10}]}
    assert live_activity.subject(snap, {})["session_id"] == "b"


@pytest.mark.parametrize("earlier, later", [
    ("Amy", "Bea"), ("amy", "Bea"), ("Amy", "bea"),
    ("Vex2", "Vex10"), ("Cipher", "Cipher2"), ("a", "A"),
])
def test_title_key_orders_as_localized_standard_compare_does(earlier, later):
    assert live_activity.title_key(earlier) < live_activity.title_key(later)


def test_a_finished_or_abandoned_row_is_never_a_subject():
    """The phone's `liveBuckets`: a run that is over needs nobody, whatever
    its row still carries."""
    snap = {"finished": [_row("f1", "Vex", 5, questions=[{"text": "?"}])],
            "abandoned": [_row("a1", "Cipher", 5)]}
    prompts = {"f1": {"request_id": "r"}, "a1": {"request_id": "r2"}}
    assert live_activity.subject(snap, prompts) is None


def test_a_running_row_with_nothing_open_is_not_a_subject():
    snap = {"running": [_row("r1", "Vex", 5)]}
    assert live_activity.subject(snap, {}) is None


def test_a_row_with_an_empty_session_id_is_skipped():
    snap = {"waiting": [_row("", "Ghost", 500), _row("s1", "Vex", 1)]}
    assert live_activity.subject(snap, {})["session_id"] == "s1"


def test_a_row_is_read_once_across_buckets():
    """First bucket wins, the phone's `seen` set: a duplicate id in a later
    bucket never yields a second candidate."""
    snap = {"running": [_row("s1", "Vex", 5)],
            "waiting": [_row("s1", "Vex", 5)]}
    assert live_activity.subject(snap, {})["session_id"] == "s1"


def test_content_state_carries_exactly_the_six_keys():
    subject = {"session_id": "s1", "nickname": "Vex", "kind": "question",
               "idle_seconds": 90.4}
    state = live_activity.content_state(subject, "Rename the strip", 1_000_000.0)
    # Shape 1 is still exactly the six face keys. `STATE_KEYS` grew; this
    # did not.
    assert tuple(state) == live_activity.FACE_KEYS
    assert tuple(state) == live_activity.STATE_KEYS[:6]
    assert state["slug"] == cast.character_for("Vex", "s1") == "vex"
    assert state["since"] == 999_910
    assert isinstance(state["since"], int)
    assert state["work"] == "Rename the strip"
    assert state["kind"] == "question"
    assert state["session_id"] == "s1"


def test_content_state_reads_the_rows_own_stamp_before_the_clock():
    """`quiet_since` is the daemon's one stamp for the moment the row went
    quiet; the phone reads the same key, so the two ends agree to the
    second. Only a row from an older daemon (no stamp) falls back to
    ``now - idle_seconds``."""
    subject = {"session_id": "s1", "nickname": "Vex", "kind": "question",
               "idle_seconds": 90.4, "quiet_since": 999_800.0}
    state = live_activity.content_state(subject, "", 1_000_000.0)
    assert state["since"] == 999_800 and isinstance(state["since"], int)
    for bad in (0, -5, "x", float("nan"), None):
        subject["quiet_since"] = bad
        assert live_activity.content_state(subject, "", 1_000_000.0)["since"] == 999_910
    snap = {"waiting": [dict(_row("s1", "Vex", 30), quiet_since=123.0)]}
    assert live_activity.subject(snap, {})["quiet_since"] == 123.0
    assert live_activity.subject({"waiting": [_row("s1", "Vex", 30)]}, {})["quiet_since"] == 0.0


def test_the_daemon_stamps_every_row_once_from_the_snapshots_own_clock(monkeypatch):
    """`_collect_agent_stubs` writes `quiet_since` beside `idle_seconds`
    from the same `now`, whole seconds — the number both ends read."""
    d = BobDaemon()
    d._session_states["s1"] = {"state": "idle", "last_event": 1_000.0}
    monkeypatch.setattr("dark_army_daemon.daemon.time.time", lambda: 1_090.4)
    stub = next(st for st in d._collect_agent_stubs() if st["session_id"] == "s1")
    assert stub["quiet_since"] == 1_000.0
    assert stub["idle_seconds"] == pytest.approx(90.4)


def test_content_state_slug_is_the_casts_rung_for_rung():
    subject = {"session_id": "abc-123", "nickname": "Nobody", "kind": "attention",
               "idle_seconds": 0}
    state = live_activity.content_state(subject, "", 10.0)
    assert state["slug"] == cast.character_for("Nobody", "abc-123")
    assert re.match(r"^[a-z]{0,24}$", state["slug"])
    assert state["since"] == 10


def test_same_ignores_a_since_drift_under_two_seconds():
    a = {"nickname": "D", "slug": "vex", "kind": "attention", "work": "",
         "since": 100, "session_id": "s1"}
    assert live_activity.same(a, dict(a, since=101))
    assert live_activity.same(a, dict(a, since=99))
    assert not live_activity.same(a, dict(a, since=103))


@pytest.mark.parametrize("key,value", [
    ("kind", "permission"), ("nickname", "E"), ("work", "other"),
    ("session_id", "s2"), ("slug", "cipher"),
])
def test_same_notices_a_changed_field(key, value):
    a = {"nickname": "D", "slug": "vex", "kind": "attention", "work": "",
         "since": 100, "session_id": "s1"}
    assert not live_activity.same(a, dict(a, **{key: value}))


def test_same_treats_none_and_empty_alike():
    assert live_activity.same(None, None)
    assert live_activity.same(None, {})
    assert not live_activity.same(None, {"kind": "attention", "since": 0})


def test_the_ranking_agrees_with_the_phones_kind_order():
    """The parity twin: `PhoneInboxWireKind` ranks `permission = 0`, then
    `question`, with `waiting` last among the session kinds, and
    `live_activity.KINDS` is the same order under the wire's words. Inside
    a kind the phone's `before` compares the title by
    `localizedStandardCompare`, then the target key (`s:` + session id) —
    never the wait — and `live_activity.subject` sorts the same way
    (`title_key`, then `session_id`), so the phone's locally started card
    and the Mac's first update name one agent."""
    swift = INBOX_SWIFT.read_text()
    block = swift.split("enum PhoneInboxWireKind")[1].split("var name")[0]
    order = re.findall(r"case (\w+)", block)
    assert order[0] == "permission" and order[1] == "question"
    assert order.index("waiting") > order.index("question")
    assert live_activity.KINDS == ("permission", "question", "attention")
    before = swift.split("private static func before(")[1].split("\n    }\n")[0]
    rungs = [line.strip() for line in before.splitlines() if line.strip().startswith("if ") or "return a.target.key" in line]
    assert rungs[0].startswith("if a.kind != b.kind")
    assert rungs[1].startswith("if a.wire != b.wire")
    assert rungs[2].startswith("if a.title != b.title")
    assert "a.title.localizedStandardCompare(b.title)" in before
    assert rungs[-1] == "return a.target.key < b.target.key"
    assert "idleSeconds" not in before and "since" not in before
    src = (ROOT / "host" / "dark_army_daemon" / "live_activity.py").read_text()
    assert "KINDS.index(kind), title_key(nickname or name or sid), sid" in src
    # The phone's title: nickname, else name, else the session id.
    assert 'agent.nickname.isEmpty\n                ? (agent.name.isEmpty ? agent.sessionId : agent.name)' in swift


def test_the_activity_kinds_are_a_subset_of_the_buzz_kinds_at_every_end():
    assert set(relay_client.ACTIVITY_KINDS) == set(alerts.KINDS) - {"security", "finished"}
    assert relay_client.ACTIVITY_KINDS == live_activity.KINDS
    text = PUSH_JS.read_text()
    found = re.search(r'const ACTIVITY_KINDS = \[(.*?)\];', text)
    assert found, "no ACTIVITY_KINDS in push.js"
    assert tuple(re.findall(r'"(\w+)"', found.group(1))) == relay_client.ACTIVITY_KINDS
    found = re.search(r'const ACTIVITY_EVENTS = \[(.*?)\];', text)
    assert tuple(re.findall(r'"(\w+)"', found.group(1))) == relay_client.ACTIVITY_EVENTS
    # The live card's face is the closed cast list at both ends, never an
    # ASCII shape that once blanked a non-ASCII name's portrait.
    assert "SLUG_SHAPE" not in text
    assert '(slug !== "" && !FACE_SLUGS.includes(slug))' in text
    assert not hasattr(relay_client, "ACTIVITY_SLUG_SHAPE")


# --- the token ledger --------------------------------------------------------------


def test_note_activity_token_round_trips_and_an_empty_token_deletes(store):
    relay.create_channel("phone-1")
    assert relay.activity_token("phone-1") is None
    assert relay.activity_env("phone-1") == "prod"
    assert relay.note_activity_token("phone-1", "ab" * 32, "dev") is True
    assert relay.activity_token("phone-1") == "ab" * 32
    assert relay.activity_env("phone-1") == "dev"
    relay.invalidate()
    assert relay.activity_token("phone-1") == "ab" * 32
    # The push token is a separate key: neither reads the other.
    assert relay.push_token("phone-1") is None
    assert relay.note_activity_token("phone-1", "", "") is True
    entry = json.loads(store.read_text())["channels"]["phone-1"]
    for key in ("activity_token", "activity_env", "activity_updated_at"):
        assert key not in entry
    assert relay.note_activity_token("phone-9", "ab" * 32, "prod") is False
    relay.create_channel("phone-2")
    relay.note_activity_token("phone-2", "cd" * 32, "prod")
    relay.forget("phone-2")
    assert relay.activity_token("phone-2") is None


# --- the connector's leg ---------------------------------------------------------


def _connector(tmp_path):
    data = relay.load()
    data["url"] = "http://127.0.0.1:1"
    assert relay.save(data)
    assert relay.create_channel("dev-1")
    daemon = BobDaemon()
    srv = ApiServer(daemon, port=0)
    daemon._api = srv
    conn = relay_client.RelayConnector(srv)
    calls = []

    async def fake_http(method, url, data=None, headers=None):
        calls.append((method, url, json.loads(data), headers))
        return conn._answer

    conn._answer = (200, b"ok")
    conn._http = fake_http
    return conn, daemon, calls


def _state(**over):
    base = {"nickname": "Vex", "slug": "vex", "kind": "permission",
            "work": "Rename the strip", "since": 1_700_000_000,
            "session_id": "s-1"}
    base.update(over)
    return base


@pytest.mark.asyncio
async def test_push_activity_posts_only_the_allowed_keys_and_never_title(tmp_path):
    conn, _d, calls = _connector(tmp_path)
    relay.note_activity_token("dev-1", "ab" * 32, "dev")
    ok = await conn.push_activity("dev-1", {"event": "update", **_state(),
                                            "title": "Vex needs you",
                                            "badge": 3, "question": "secret?"})
    assert ok is True
    ((method, url, body, headers),) = calls
    assert method == "POST" and "/api/push?ch=" in url
    assert set(body) == {"tok", "env", "event", "nickname", "slug", "kind",
                         "work", "since", "session_id"}
    assert "title" not in body and "badge" not in body
    assert "secret" not in json.dumps(body)
    assert body["tok"] == "ab" * 32 and body["env"] == "dev"
    assert body["event"] == "update" and body["kind"] == "permission"
    assert body["since"] == 1_700_000_000.0


@pytest.mark.asyncio
async def test_push_activity_clamps_work_at_eighty(tmp_path):
    conn, _d, calls = _connector(tmp_path)
    relay.note_activity_token("dev-1", "ab" * 32, "prod")
    await conn.push_activity("dev-1", {"event": "update",
                                       **_state(work="x " * 200)})
    assert len(calls[0][2]["work"]) == relay_client.PUSH_WORK_CHARS


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [
    {"event": "start", **_state()},
    {"event": "", **_state()},
    {**_state()},
    {"event": "update", **_state(kind="security")},
    {"event": "update", **_state(kind="finished")},
    {"event": "update", **_state(kind="")},
])
async def test_push_activity_sends_nothing_for_an_off_list_kind_or_event(tmp_path, body):
    conn, _d, calls = _connector(tmp_path)
    relay.note_activity_token("dev-1", "ab" * 32, "prod")
    assert await conn.push_activity("dev-1", body) is False
    assert calls == []


@pytest.mark.asyncio
async def test_push_activity_drops_a_bad_session_id_but_still_sends_an_end(tmp_path):
    conn, _d, calls = _connector(tmp_path)
    relay.note_activity_token("dev-1", "ab" * 32, "prod")
    ok = await conn.push_activity("dev-1", {"event": "end",
                                            **_state(session_id="bad id!",
                                                     slug="Vex!")})
    assert ok is True
    body = calls[0][2]
    assert body["event"] == "end"
    assert "session_id" not in body
    assert body["slug"] == ""
    # An end with no last state at all is still an end.
    ok = await conn.push_activity("dev-1", {"event": "end"})
    assert ok is True
    assert "kind" not in calls[1][2]


@pytest.mark.asyncio
async def test_push_activity_reads_the_token_at_the_moment_of_use(tmp_path):
    conn, _d, calls = _connector(tmp_path)
    assert await conn.push_activity("dev-1", {"event": "update", **_state()}) is False
    assert calls == []
    relay.note_activity_token("dev-1", "ab" * 32, "prod")
    assert await conn.push_activity("dev-1", {"event": "update", **_state()}) is True
    relay.note_activity_token("dev-1", "", "")
    assert await conn.push_activity("dev-1", {"event": "update", **_state()}) is False
    assert len(calls) == 1
    # A push token alone is not an activity token.
    relay.note_push_token("dev-1", "cd" * 32, "prod")
    assert await conn.push_activity("dev-1", {"event": "update", **_state()}) is False


@pytest.mark.asyncio
async def test_push_activity_says_a_missing_route_once_and_counts_it_nowhere(tmp_path, caplog):
    conn, _d, _calls = _connector(tmp_path)
    relay.note_activity_token("dev-1", "ab" * 32, "prod")
    conn._answer = (404, b"<html>")
    with caplog.at_level("INFO"):
        assert await conn.push_activity("dev-1", {"event": "update", **_state()}) is False
        conn._answer = (503, b"unconfigured")
        assert await conn.push_activity("dev-1", {"event": "update", **_state()}) is False
    assert sum("no push route yet" in r.message for r in caplog.records) == 1
    assert conn._push_health == {}
    assert conn._health == {}


@pytest.mark.asyncio
async def test_a_400_from_an_old_mailbox_is_a_live_card_failure_on_its_own_record(tmp_path, caplog):
    """The live card has its own health record with its own consequence:
    an old mailbox takes the buzz and refuses the card, and one record for
    both flapped a warning pair per buzz and said "will not be buzzed"
    about a card. Neither `_health` nor `_push_health` moves."""
    conn, _d, _calls = _connector(tmp_path)
    relay.note_activity_token("dev-1", "ab" * 32, "prod")
    relay.note_push_token("dev-1", "ab" * 32, "prod")
    conn._answer = (400, b"bad request")
    with caplog.at_level("WARNING"):
        assert await conn.push_activity("dev-1", {"event": "update", **_state()}) is False
    assert conn._activity_health["dev-1"].state == "failing"
    assert conn._activity_health["dev-1"].status == 400
    assert conn._health == {}
    assert conn._push_health == {}
    lines = [r.message for r in caplog.records if r.levelname == "WARNING"]
    assert lines == ["phone live card failing (status 400) — the phone's live "
                     "card will not be updated until this clears"]
    assert not conn._push_route_missing_said
    # A buzz in between lands on its own record and logs nothing about the
    # card; the card's record stays failing, unflapped.
    conn._answer = (200, b"ok")
    assert await conn.push_alert("dev-1", {"title": "x", "badge": 1}) is True
    assert conn._push_health["dev-1"].state == "ok"
    assert conn._activity_health["dev-1"].state == "failing"
    conn._answer = (400, b"bad request")
    with caplog.at_level("WARNING"):
        assert await conn.push_activity("dev-1", {"event": "update", **_state()}) is False
    assert [r.message for r in caplog.records if r.levelname == "WARNING"] == lines
    assert conn._push_health["dev-1"].state == "ok"
    # A 401 is the secret line; a landed update recovers the record.
    conn._answer = (401, b"unauthorized")
    assert await conn.push_activity("dev-1", {"event": "update", **_state()}) is False
    conn._answer = (200, b"ok")
    assert await conn.push_activity("dev-1", {"event": "update", **_state()}) is True
    assert conn._activity_health["dev-1"].state == "ok"
    assert "phone will not be buzzed" not in " ".join(r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_the_outcome_names_why_it_did_not_land(tmp_path):
    """`push_activity_outcome`: the daemon's retry rule turns on the class.
    Refused (a 4xx, an undeployed route) is not worth sending again; dead
    (Apple's 502) is not worth sending anything to; unreachable (no answer, a 5xx) is; skipped left nothing."""
    conn, _d, calls = _connector(tmp_path)
    body = {"event": "update", **_state()}
    assert await conn.push_activity_outcome("dev-1", body) == "skipped"   # no token
    relay.note_activity_token("dev-1", "ab" * 32, "prod")
    assert await conn.push_activity_outcome("dev-1", {"event": "nope"}) == "skipped"
    assert calls == []
    for status, reply, outcome in [
        (200, b"ok", "landed"), (204, b"", "landed"),
        (400, b"bad request", "refused"), (401, b"unauthorized", "refused"),
        (404, b"<html>", "refused"), (503, b"unconfigured", "refused"),
        (502, b"apple refused", "dead"),
        (503, b"unavailable", "unreachable"), (500, b"", "unreachable"),
    ]:
        conn._answer = (status, reply)
        assert await conn.push_activity_outcome("dev-1", body) == outcome, (status, reply)

    async def boom(method, url, data=None, headers=None):
        raise OSError("no route to host")
    conn._http = boom
    assert await conn.push_activity_outcome("dev-1", body) == "unreachable"
    conn._push_bucket("dev-1")._tokens = 0.0
    assert await conn.push_activity_outcome("dev-1", body) == "skipped"
    assert set(relay_client.ACTIVITY_OUTCOMES) == {"landed", "refused", "dead",
                                                   "unreachable", "skipped"}


@pytest.mark.asyncio
async def test_the_push_bucket_is_shared_with_the_buzz(tmp_path):
    conn, _d, calls = _connector(tmp_path)
    relay.note_activity_token("dev-1", "ab" * 32, "prod")
    relay.note_push_token("dev-1", "ab" * 32, "prod")
    for i in range(relay_client.PUSH_MAX_PER_MINUTE):
        if i % 2:
            assert await conn.push_alert("dev-1", {"title": "x", "badge": 1}) is True
        else:
            assert await conn.push_activity("dev-1", {"event": "update", **_state()}) is True
    assert await conn.push_activity("dev-1", {"event": "update", **_state()}) is False
    assert len(calls) == relay_client.PUSH_MAX_PER_MINUTE


# --- the daemon glue --------------------------------------------------------------


class _Connector:
    """Records every live-card send; `answer` is the outcome it reports,
    or an awaitable it waits on first (a slow mailbox)."""

    def __init__(self, answer="landed"):
        self.pushed = []
        self.answer = answer
        self.gate = None

    async def push_activity_outcome(self, device_id, body):
        self.pushed.append((device_id, dict(body)))
        if self.gate is not None:
            await self.gate
        return self.answer

    async def push_activity(self, device_id, body):
        return await self.push_activity_outcome(device_id, body) == "landed"

    async def push_alert(self, device_id, body):
        return True


def _glued(monkeypatch, snapshot, prompts=None, cards=(), tokens=("phone-1",)):
    d = BobDaemon.__new__(BobDaemon)
    d._live_activity_last = {}
    d._live_activity_attempt = {}
    d._live_activity_inflight = {}
    d._live_activity_generation = {}
    d._live_activity_sent_at = {}
    d.phone_push_enabled = True
    d.remote_access_enabled = True
    d._relay_connector = _Connector()
    d._agents_snapshot_cache = snapshot
    d._board_state = {"cards": list(cards)}
    d._active_notifications = {}
    # `_notification_snapshot()` asks each card's session for its hysteresis
    # window off `_session_states`; a bare `__new__` daemon has none.
    d._session_states = {}
    d._permission_snapshot = lambda: list((prompts or {}).values())
    held = set(tokens)
    monkeypatch.setattr(relay, "channel_ids", lambda: {"phone-1": "c" * 32,
                                                       "phone-2": "d" * 32})
    monkeypatch.setattr(relay, "activity_token",
                        lambda did: "ab" * 32 if did in held else None)
    monkeypatch.setattr(relay, "push_token",
                        lambda did: "cd" * 32 if did == "phone-2" else None)
    return d


async def _settle():
    await asyncio.sleep(0)
    await asyncio.sleep(0)


def _push(d, snapshot=None):
    if snapshot is not None:
        d._agents_snapshot_cache = snapshot
    d._push_live_activity(d._agents_snapshot_cache)


@pytest.mark.asyncio
async def test_an_unchanged_state_sends_nothing_and_a_changed_nickname_one_update(monkeypatch):
    snap = {"waiting": [_row("s1", "Vex", 30)]}
    d = _glued(monkeypatch, snap)
    _push(d)
    await _settle()
    assert len(d._relay_connector.pushed) == 1
    did, body = d._relay_connector.pushed[0]
    assert did == "phone-1" and body["event"] == "update"
    assert set(body) == {"event", *live_activity.FACE_KEYS}
    assert body["slug"] == "vex" and body["kind"] == "attention"
    # Same picture, a tick later: nothing.
    _push(d, {"waiting": [_row("s1", "Vex", 31)]})
    await _settle()
    assert len(d._relay_connector.pushed) == 1
    # A different top waiter: one update.
    _push(d, {"waiting": [_row("s2", "Cipher", 400)]})
    await _settle()
    assert len(d._relay_connector.pushed) == 2
    assert d._relay_connector.pushed[1][1]["nickname"] == "Cipher"


@pytest.mark.asyncio
async def test_the_work_line_is_the_bound_cards_title_clamped(monkeypatch):
    snap = {"waiting": [_row("s1", "Vex", 30)]}
    card = {"id": "c1", "title": "  Rename   the " + "x" * 200,
            "session_id": "s1", "link_state": "live"}
    d = _glued(monkeypatch, snap, cards=[card])
    _push(d)
    await _settle()
    work = d._relay_connector.pushed[0][1]["work"]
    assert work.startswith("Rename the x")
    assert len(work) == relay_client.PUSH_WORK_CHARS
    assert work.endswith("…")


@pytest.mark.asyncio
async def test_an_emptied_list_sends_one_end_then_nothing(monkeypatch):
    d = _glued(monkeypatch, {"waiting": [_row("s1", "Vex", 30)]})
    _push(d)
    await _settle()
    _push(d, {"waiting": []})
    await _settle()
    assert [b["event"] for _, b in d._relay_connector.pushed] == ["update", "end"]
    end = d._relay_connector.pushed[1][1]
    assert end["nickname"] == "Vex" and end["session_id"] == "s1"
    assert d._live_activity_last["phone-1"] == "ended"
    _push(d, {"waiting": []})
    await _settle()
    assert len(d._relay_connector.pushed) == 2


@pytest.mark.asyncio
async def test_an_unreachable_end_is_retried_on_the_clock_and_a_landed_one_is_not(monkeypatch):
    """An end the mailbox could not be reached for is sent again — but no
    sooner than `LIVE_ACTIVITY_RETRY_SECONDS`, so an outage costs the shared
    bucket two tokens a minute, not one per pass."""
    from dark_army_daemon import daemon as daemon_mod
    clock = [1000.0]
    monkeypatch.setattr(daemon_mod.time, "monotonic", lambda: clock[0])
    d = _glued(monkeypatch, {"waiting": [_row("s1", "Vex", 30)]})
    _push(d)
    await _settle()
    d._relay_connector.answer = "unreachable"
    _push(d, {"waiting": []})
    await _settle()
    assert d._live_activity_last["phone-1"] != "ended"
    for _ in range(3):
        _push(d, {"waiting": []})
        await _settle()
    assert [b["event"] for _, b in d._relay_connector.pushed] == ["update", "end"]
    clock[0] += daemon_mod.LIVE_ACTIVITY_RETRY_SECONDS
    _push(d, {"waiting": []})
    await _settle()
    assert [b["event"] for _, b in d._relay_connector.pushed] == ["update", "end", "end"]
    clock[0] += daemon_mod.LIVE_ACTIVITY_RETRY_SECONDS
    d._relay_connector.answer = "landed"
    _push(d, {"waiting": []})
    await _settle()
    assert d._live_activity_last["phone-1"] == "ended"
    assert "phone-1" not in d._live_activity_attempt
    _push(d, {"waiting": []})
    await _settle()
    assert len(d._relay_connector.pushed) == 4


@pytest.mark.asyncio
async def test_a_refused_body_is_sent_once_per_distinct_state(monkeypatch):
    """An old mailbox (every POST a 400) or a wrong push secret (401):
    the same body is never sent twice. Each distinct picture costs one
    attempt; a pass with the same picture costs nothing, however many."""
    d = _glued(monkeypatch, {"waiting": [_row("s1", "Vex", 30)]})
    d._relay_connector.answer = "refused"
    for _ in range(5):
        _push(d)
        await _settle()
    assert len(d._relay_connector.pushed) == 1
    assert "phone-1" not in d._live_activity_last
    # A different picture: one more attempt, then quiet again.
    for _ in range(5):
        _push(d, {"waiting": [_row("s2", "Cipher", 400)]})
        await _settle()
    assert len(d._relay_connector.pushed) == 2
    # The list empties: one end is tried, refused, and not tried again —
    # an end does not retry a refusal, only an unreachable mailbox.
    for _ in range(5):
        _push(d, {"waiting": []})
        await _settle()
    assert [b["event"] for _, b in d._relay_connector.pushed] == ["update", "update", "end"]
    assert d._live_activity_last.get("phone-1") != "ended"
    # A fresh registration forgets the refusal: the standing state is sent.
    d.forget_live_activity("phone-1")
    _push(d, {"waiting": []})
    await _settle()
    assert len(d._relay_connector.pushed) == 4


@pytest.mark.asyncio
async def test_an_old_mailbox_never_starves_the_buzz(tmp_path, monkeypatch):
    """The reproduced storm: a mailbox answering 400 to every live-card POST
    while the buzz shares its bucket. Ten agents passes in one minute must
    cost the bucket one token, and the buzz that follows must still land."""
    conn, d, calls = _connector(tmp_path)
    relay.note_activity_token("dev-1", "ab" * 32, "prod")
    relay.note_push_token("dev-1", "ab" * 32, "prod")

    async def old_mailbox(method, url, data=None, headers=None):
        body = json.loads(data)
        calls.append(body)
        return (400, b"bad request") if "event" in body else (200, b"ok")
    conn._http = old_mailbox
    d.phone_push_enabled = True
    d.remote_access_enabled = True
    d._relay_connector = conn
    d._agents_snapshot_cache = {"waiting": [_row("s1", "Vex", 30)]}
    d._board_state = {"cards": []}
    # The rows as the daemon stamps them: the age grows, the stamp holds.
    for tick in range(10):
        _push(d, {"waiting": [dict(_row("s1", "Vex", 30 + tick),
                                   quiet_since=1_700_000_000.0)]})
        await _settle()
    assert [b["event"] for b in calls] == ["update"]
    assert conn._push_bucket("dev-1")._tokens >= relay_client.PUSH_MAX_PER_MINUTE - 1
    assert await conn.push_alert("dev-1", {"title": "Vex needs you", "badge": 1}) is True
    assert conn._push_health["dev-1"].state == "ok"
    assert conn._activity_health["dev-1"].state == "failing"


@pytest.mark.asyncio
async def test_a_slow_mailbox_is_one_send_not_one_per_pass(monkeypatch):
    """An in-flight body is not sent again while its POST is out; when it
    lands the state is remembered, and only a changed picture sends more."""
    d = _glued(monkeypatch, {"waiting": [_row("s1", "Vex", 30)]})
    gate = asyncio.get_running_loop().create_future()
    d._relay_connector.gate = gate
    for _ in range(4):
        _push(d)
        await _settle()
    assert len(d._relay_connector.pushed) == 1
    assert "phone-1" in d._live_activity_inflight
    # A changed picture during the flight is sent (it is a different body).
    _push(d, {"waiting": [_row("s2", "Cipher", 400)]})
    await _settle()
    assert len(d._relay_connector.pushed) == 2
    gate.set_result(None)
    await _settle()
    assert "phone-1" not in d._live_activity_inflight
    assert d._live_activity_last["phone-1"]["nickname"] == "Cipher"
    _push(d)
    await _settle()
    assert len(d._relay_connector.pushed) == 2


@pytest.mark.asyncio
async def test_a_raising_send_clears_the_in_flight_mark_and_counts_as_unreachable(monkeypatch):
    d = _glued(monkeypatch, {"waiting": [_row("s1", "Vex", 30)]})

    async def boom(device_id, body):
        d._relay_connector.pushed.append((device_id, dict(body)))
        raise OSError("no route")
    d._relay_connector.push_activity_outcome = boom
    _push(d)
    await _settle()
    assert "phone-1" not in d._live_activity_inflight
    assert d._live_activity_attempt["phone-1"][1] == "unreachable"
    _push(d)
    await _settle()
    assert len(d._relay_connector.pushed) == 1


@pytest.mark.asyncio
async def test_an_empty_list_with_nothing_remembered_sends_one_end(monkeypatch):
    """Nothing remembered is not "nothing up": the daemon may have restarted
    under a phone whose card is still showing, so an empty list sends one
    end until it lands — then the mark stops it, and a subject that appears
    afterwards is one update."""
    d = _glued(monkeypatch, {"waiting": []})
    _push(d)
    await _settle()
    assert [b for _, b in d._relay_connector.pushed] == [{"event": "end"}]
    assert d._live_activity_last["phone-1"] == "ended"
    _push(d)
    await _settle()
    assert len(d._relay_connector.pushed) == 1


@pytest.mark.asyncio
async def test_ended_means_no_live_token_until_the_phone_registers_again(monkeypatch):
    """A pushed end kills the activity's token. A new subject afterwards is
    not sent to it — Apple would refuse, and the refusal costs the shared
    bucket — until the phone starts a fresh activity and registers its
    token, which clears the mark."""
    d = _glued(monkeypatch, {"waiting": [_row("s1", "Vex", 30)]})
    _push(d)
    await _settle()
    _push(d, {"waiting": []})
    await _settle()
    assert d._live_activity_last["phone-1"] == "ended"
    for _ in range(3):
        _push(d, {"waiting": [_row("s2", "Cipher", 400)]})
        await _settle()
    assert [b["event"] for _, b in d._relay_connector.pushed] == ["update", "end"]
    d.forget_live_activity("phone-1")
    _push(d, {"waiting": [_row("s2", "Cipher", 400)]})
    await _settle()
    assert d._relay_connector.pushed[-1][1] == {"event": "update", **d._live_activity_last["phone-1"]}
    assert d._relay_connector.pushed[-1][1]["nickname"] == "Cipher"


@pytest.mark.asyncio
async def test_an_answer_landing_after_a_fresh_registration_is_discarded(monkeypatch):
    """The race: an `end` is in flight to token T; the phone ends locally,
    unregisters, starts a new activity and registers T2 (two
    `forget_live_activity` calls); the old `end` lands. Nothing may be
    remembered against the new activity — an "ended" mark there meant the
    new card never updated."""
    d = _glued(monkeypatch, {"waiting": [_row("s1", "Vex", 30)]})
    _push(d)
    await _settle()
    assert isinstance(d._live_activity_last["phone-1"], dict)
    gate = asyncio.get_running_loop().create_future()
    d._relay_connector.gate = gate
    _push(d, {"waiting": []})
    await _settle()
    assert d._relay_connector.pushed[-1][1]["event"] == "end"
    assert "phone-1" in d._live_activity_inflight
    d.forget_live_activity("phone-1")
    d.forget_live_activity("phone-1")
    gate.set_result(None)
    await _settle()
    assert "phone-1" not in d._live_activity_last
    assert "phone-1" not in d._live_activity_attempt
    assert "phone-1" not in d._live_activity_inflight
    # The new activity is updated on the next pass.
    d._relay_connector.gate = None
    _push(d, {"waiting": [_row("s2", "Cipher", 400)]})
    await _settle()
    assert d._relay_connector.pushed[-1][1]["event"] == "update"
    assert d._live_activity_last["phone-1"]["nickname"] == "Cipher"


@pytest.mark.asyncio
async def test_a_refusal_landing_after_a_fresh_registration_is_discarded_too(monkeypatch):
    d = _glued(monkeypatch, {"waiting": [_row("s1", "Vex", 30)]})
    d._relay_connector.answer = "refused"
    gate = asyncio.get_running_loop().create_future()
    d._relay_connector.gate = gate
    _push(d)
    await _settle()
    d.forget_live_activity("phone-1")
    gate.set_result(None)
    await _settle()
    assert "phone-1" not in d._live_activity_attempt
    # Without the registration the same refusal is remembered.
    d._relay_connector.gate = None
    _push(d)
    await _settle()
    assert d._live_activity_attempt["phone-1"][1] == "refused"


@pytest.mark.asyncio
async def test_a_dead_token_ends_the_card_until_the_phone_registers_again(monkeypatch):
    """Apple refusing the token (iOS ended the activity at eight hours) is
    an end: a changed picture sends nothing more — the fleet figures move
    every minute, and each used to cost a 502 — until a fresh registration."""
    d = _glued(monkeypatch, {"waiting": [_row("s1", "Vex", 30)]})
    d._relay_connector.answer = "dead"
    _push(d)
    await _settle()
    assert d._live_activity_last["phone-1"] == "ended"
    assert "phone-1" not in d._live_activity_attempt
    for waiting in ([_row("s2", "Cipher", 400)], [], [_row("s3", "Hex", 5)]):
        _push(d, {"waiting": waiting})
        await _settle()
    assert len(d._relay_connector.pushed) == 1
    d._relay_connector.answer = "landed"
    d.forget_live_activity("phone-1")
    _push(d, {"waiting": [_row("s3", "Hex", 5)]})
    await _settle()
    assert len(d._relay_connector.pushed) == 2
    assert d._live_activity_last["phone-1"] != "ended"


@pytest.mark.asyncio
async def test_an_older_send_landing_after_a_newer_one_is_not_the_remembered_state(monkeypatch):
    """A changed picture while a body is in flight sends the new body; the
    old answer, whenever it arrives, is discarded so the remembered state
    is never the older card."""
    d = _glued(monkeypatch, {"waiting": [_row("s1", "Vex", 30)]})
    slow = asyncio.get_running_loop().create_future()
    d._relay_connector.gate = slow
    _push(d)
    await _settle()
    d._relay_connector.gate = None
    _push(d, {"waiting": [_row("s2", "Cipher", 400)]})
    await _settle()
    assert d._live_activity_last["phone-1"]["nickname"] == "Cipher"
    slow.set_result(None)
    await _settle()
    assert d._live_activity_last["phone-1"]["nickname"] == "Cipher"
    assert [b["nickname"] for _, b in d._relay_connector.pushed] == ["Vex", "Cipher"]


@pytest.mark.asyncio
async def test_a_fresh_registration_starts_from_a_clean_slate(monkeypatch):
    d = _glued(monkeypatch, {"waiting": [_row("s1", "Vex", 30)]})
    _push(d)
    await _settle()
    _push(d, {"waiting": []})
    await _settle()
    assert d._live_activity_last["phone-1"] == "ended"
    d.forget_live_activity("phone-1")
    assert "phone-1" not in d._live_activity_last
    _push(d, {"waiting": [_row("s1", "Vex", 30)]})
    await _settle()
    assert d._relay_connector.pushed[-1][1]["event"] == "update"


@pytest.mark.asyncio
async def test_the_three_gates_and_the_token_gate(monkeypatch):
    snap = {"waiting": [_row("s1", "Vex", 30)]}
    d = _glued(monkeypatch, snap)
    d.remote_access_enabled = False
    _push(d)
    await _settle()
    assert d._relay_connector.pushed == []
    d.remote_access_enabled = True
    d.phone_push_enabled = False
    _push(d)
    await _settle()
    assert d._relay_connector.pushed == []
    d.phone_push_enabled = True
    d._relay_connector = None
    _push(d)                                  # no connector: no raise, nothing
    # No activity token at all → nothing, even with a push token (phone-2).
    d = _glued(monkeypatch, snap, tokens=())
    _push(d)
    await _settle()
    assert d._relay_connector.pushed == []


@pytest.mark.asyncio
async def test_a_prompt_wins_and_the_state_names_the_permission(monkeypatch):
    snap = {"running": [_row("p1", "Cipher", 5)],
            "waiting": [_row("w1", "Vex", 600)]}
    prompts = {"p1": {"request_id": "r1", "session_id": "p1", "tool_name": "Bash",
                      "description": "rm -rf /"}}
    d = _glued(monkeypatch, snap, prompts=prompts)
    _push(d)
    await _settle()
    ((_did, body),) = d._relay_connector.pushed
    assert body["kind"] == "permission" and body["session_id"] == "p1"
    assert "rm -rf" not in json.dumps(body)


@pytest.mark.asyncio
async def test_the_daemon_reads_its_own_notification_cards_and_board(monkeypatch):
    """The glue hands the composer `_active_notifications`' keys and the
    published board: a running row with a notification card is updated
    (never ended out from under the phone), and a stopped row bound to a
    `needs_you` card is not."""
    snap = {"running": [_row("n1", "Vex", 12)]}
    d = _glued(monkeypatch, snap)
    d._active_notifications["n1"] = {"session_id": "n1", "message": "needs you"}
    _push(d)
    await _settle()
    ((_did, body),) = d._relay_connector.pushed
    assert body["event"] == "update" and body["session_id"] == "n1"
    assert body["kind"] == "attention"
    # The card gone, the row is no subject: one end, as the phone's list empties.
    d._active_notifications.clear()
    _push(d)
    await _settle()
    assert d._relay_connector.pushed[-1][1]["event"] == "end"
    bound = _glued(monkeypatch, {"waiting": [_row("w1", "Vex", 600)]},
                   cards=[{"id": "c1", "title": "Ship it", "needs_you": True,
                           "session_id": "w1"}])
    _push(bound)
    await _settle()
    assert [b["event"] for _, b in bound._relay_connector.pushed] == ["end"]


@pytest.mark.asyncio
async def test_a_card_the_hysteresis_withholds_admits_no_subject(monkeypatch):
    """The composer reads the *published* cards (`_notification_snapshot()`,
    the phone's `notifyIds`), not `_active_notifications`' raw keys: a
    "Waiting for input" card whose session is still inside the
    `WAITING_HYSTERESIS_SECONDS` window is withheld from every surface, so
    it admits no Live Activity subject either — a stop-and-resume flicker
    inside the window must not cost an update and an end for a card the
    phone never started. The same card just outside the window is the
    subject."""
    import time as _time
    from dark_army_daemon import session_stats as ss
    window = ss.WAITING_HYSTERESIS_SECONDS
    snap = {"running": [_row("n1", "Vex", 1)]}

    inside = _glued(monkeypatch, snap)
    inside._session_states["n1"] = {
        "state": "idle", "last_event_monotonic": _time.monotonic() - 0.5}
    inside._active_notifications["n1"] = {
        "session_id": "n1", "hook": "Stop", "message": "Waiting for input"}
    assert [n["session_id"] for n in inside._notification_snapshot()] == []
    assert inside._activity_subject(snap) is None
    _push(inside)
    await _settle()
    assert [b["event"] for _, b in inside._relay_connector.pushed] == ["end"]
    assert all("n1" not in json.dumps(b) for _, b in inside._relay_connector.pushed)

    outside = _glued(monkeypatch, snap)
    outside._session_states["n1"] = {
        "state": "idle", "last_event_monotonic": _time.monotonic() - window - 1.0}
    outside._active_notifications["n1"] = {
        "session_id": "n1", "hook": "Stop", "message": "Waiting for input"}
    assert [n["session_id"] for n in outside._notification_snapshot()] == ["n1"]
    assert outside._activity_subject(snap)["session_id"] == "n1"
    _push(outside)
    await _settle()
    ((_did, body),) = outside._relay_connector.pushed
    assert body["event"] == "update" and body["session_id"] == "n1"


def test_the_glue_hands_the_composer_the_published_cards_not_the_raw_dict(monkeypatch):
    """`_activity_subject` reads `_notification_snapshot()`, never
    `_active_notifications.keys()`: a card the hysteresis window withholds
    is not a notified session for the live card either."""
    d = BobDaemon()
    d._board_state = {"cards": []}
    d._active_notifications["withheld"] = {"hook": "Notification", "message": "x"}
    d._active_notifications["shown"] = {"hook": "StopFailure", "message": "y"}
    monkeypatch.setattr(d, "_notification_snapshot",
                        lambda: [{"session_id": "shown"}])
    seen = {}

    def subject(snapshot, prompts, *, notified, cards):
        seen["notified"] = list(notified)
        return None

    monkeypatch.setattr(live_activity, "subject", subject)
    assert d._activity_subject({"waiting": []}) is None
    assert seen["notified"] == ["shown"]


@pytest.mark.asyncio
async def test_a_raising_push_leg_never_takes_the_snapshot_down(monkeypatch):
    """The call site wraps `_push_live_activity` in its own try/except: a
    push that raises still lets the pending-agents drain run after it."""
    d = BobDaemon()
    order: list = []
    monkeypatch.setattr(d, "_observers_implementing", lambda name: [object()])
    monkeypatch.setattr(d, "_agents_push_delay", lambda now: 0)
    monkeypatch.setattr(d, "_collect_agent_stubs", lambda: [])
    monkeypatch.setattr(d, "_enrich_agent_stubs",
                        lambda stubs: {"running": [], "waiting": [], "sleeping": []})
    monkeypatch.setattr(d, "_apply_grok_live_subagents", lambda snapshot: None)

    async def no_async_work():
        return None

    for name in ("_flush_auto_compacts", "_flush_session_titles",
                 "_flush_card_priorities", "_flush_work_records"):
        monkeypatch.setattr(d, name, no_async_work)
    monkeypatch.setattr(d, "_deliver_alerts", lambda: order.append("alerts"))

    def boom(snapshot):
        order.append("push")
        raise RuntimeError("relay down")

    monkeypatch.setattr(d, "_push_live_activity", boom)
    monkeypatch.setattr(d, "_drain_pending_agents_push", lambda: order.append("drain"))
    await d._push_agents_snapshot()
    assert order == ["alerts", "push", "drain"]


# --- the row says whether it is unnamed --------------------------------------------


def test_the_row_carries_the_daemons_unnamed_judgment_as_a_bool():
    """The phone's `work` line is the card's title, else the name, never the
    placeholder — `_compose_push_work`'s rule. The phone never learns the
    placeholder string (`test_card_title_on_row.py`), so the daemon
    publishes the judgment: `unnamed` is present and `True` exactly where
    the row's name is `UNNAMED_SESSION`, absent otherwise (an older phone
    reads past it; a newer phone against an older daemon decodes false)."""
    from dark_army_daemon.daemon import UNNAMED_SESSION
    sid = "00000000-0000-0000-0000-000000000000"
    d = BobDaemon()
    d._board_state = {"cards": []}
    stub = {"session_id": sid, "project": "proj", "state": "idle",
            "subagents": 0, "subagent_ids": [], "_category": "sleeping"}
    row = d._enrich_agent_stubs([dict(stub)])["sleeping"][0]
    assert row["name"] == UNNAMED_SESSION
    assert row["unnamed"] is True
    assert "card_title" not in row
    named = d._enrich_agent_stubs([dict(stub, cli_name="What I called it")])["sleeping"][0]
    assert named["name"] == "What I called it"
    assert "unnamed" not in named


# --- the snapshot: a bool, never the token ----------------------------------------


def test_the_snapshot_publishes_a_bool_never_the_token(tmp_path, monkeypatch):
    daemon = BobDaemon()
    srv = ApiServer(daemon, port=0)
    daemon._api = srv
    daemon.remote_access_enabled = True
    code = devices.begin("phone")
    token, device_id, detail = devices.redeem(code, "phone")
    assert token, detail
    assert relay.create_channel(device_id)
    row = next(r for r in daemon.devices_snapshot()["devices"] if r["id"] == device_id)
    assert row["live_activity"] is False
    apns = "0123456789abcdef" * 4
    assert relay.note_activity_token(device_id, apns, "dev")
    dump = json.dumps(daemon.devices_snapshot(), default=str)
    dump += json.dumps(srv.state(), default=str)
    dump += json.dumps(devices.snapshot(), default=str)
    assert apns not in dump
    assert "activity_token" not in dump
    assert "activity_shape" not in dump
    row = next(r for r in daemon.devices_snapshot()["devices"] if r["id"] == device_id)
    assert row["live_activity"] is True
    assert row["push"] is False


# --- shape 2: the fleet card -------------------------------------------------------


def _counts(**over):
    base = {"working": 0, "idle": 0, "attention": 0}
    base.update(over)
    return base


def _face():
    return live_activity.content_state(
        {"session_id": "s1", "nickname": "Vex", "kind": "permission",
         "idle_seconds": 0, "quiet_since": 1_700_000_000},
        "Rename the strip", 1_700_000_000)


def test_a_bool_is_not_a_count_and_does_not_turn_the_fleet_on():
    assert live_activity.is_up({"working": True}) is False
    assert live_activity.is_up({"attention": False}) is False


def test_fleet_state_drops_a_bool_cost_and_keeps_a_zero_token_figure():
    state = live_activity.fleet_state(
        {"working": 1, "attention": 0, "idle": 0},
        {"cost_usd": True, "tokens_k": 0},
        live_activity.empty_face(0))
    assert "cost_usd" not in state
    assert state["tokens_k"] == 0
    assert state["working"] == 1


def test_is_up_is_working_or_attention_and_a_missing_key_is_zero():
    assert live_activity.is_up({"working": 1}) is True
    assert live_activity.is_up({"attention": 1}) is True
    assert live_activity.is_up({"idle": 3}) is False
    assert live_activity.is_up({}) is False
    assert live_activity.is_up(None) is False


def test_fleet_state_is_the_face_the_counts_and_the_measured_figures():
    state = live_activity.fleet_state(
        {"working": 2, "attention": 1, "idle": 4},
        {"cost_usd": 1.005, "tokens_k": 45, "cost_measured": 1, "cost_rows": 2},
        _face())
    # No rate in the figures, so no rate on the card: absent is not zero.
    assert tuple(state) == live_activity.STATE_KEYS[:-2]
    assert state["needs_you"] == 1
    assert state["standing_by"] == 4
    assert state["working"] == 2
    assert state["session_id"] == "s1"
    assert state["cost_usd"] == round(1.005, 2)
    assert state["tokens_k"] == 45


def test_a_none_cost_leaves_the_key_absent_and_an_empty_face_has_no_session():
    state = live_activity.fleet_state(
        {"working": 1, "attention": 0, "idle": 0},
        {"cost_usd": None, "tokens_k": None},
        live_activity.empty_face(1_700_000_000))
    assert "cost_usd" not in state
    assert "tokens_k" not in state
    assert state["cost_usd"] != 0 if "cost_usd" in state else True
    assert state["session_id"] == ""
    assert state["kind"] == "attention"
    assert state["since"] == 0
    assert set(state) == set(live_activity.STATE_KEYS) - set(live_activity.FIGURE_KEYS)


def test_same_and_figures_only_on_two_fleet_states():
    base = live_activity.fleet_state(
        _counts(working=1), {"cost_usd": 1.0, "tokens_k": 4}, _face())
    assert live_activity.same(base, dict(base, since=base["since"] + 1)) is True
    assert live_activity.same(base, dict(base, working=2)) is False
    assert live_activity.same(base, dict(base, cost_usd=1.1)) is False
    without = dict(base)
    without.pop("cost_usd")
    assert live_activity.same(base, without) is False
    dime = dict(base, cost_usd=1.1)
    assert live_activity.figures_only_change(base, dime) is True
    assert live_activity.figures_only_change(base, dict(base, working=2)) is False
    assert live_activity.figures_only_change(base, base) is False


def test_state_publishes_fleet_figures_from_the_same_rows_and_a_still_picture_is_no_news():
    from dark_army_daemon.api_server import _news
    daemon = BobDaemon()
    srv = ApiServer(daemon, port=0)
    daemon._api = srv
    srv._agents = {"running": [{
        "session_id": "s1",
        "metrics": {"cost_usd": 1.234},
        "stats": {"total_input_tokens": 1_000, "output_tokens": 5},
    }]}
    frame = srv.state()
    # The daemon's burn meter adds the two rates (unsaid on a fresh meter);
    # the rest is `compose` over the same rows.
    figures = dict(frame["fleet_figures"])
    rates = {k: figures.pop(k) for k in ("cost_usd_hour", "tokens_k_hour")}
    assert rates == {"cost_usd_hour": None, "tokens_k_hour": None}
    assert figures == fleet_figures.compose(frame["agents"])
    assert frame["fleet_figures"]["cost_usd"] == 1.23
    assert _news(frame) == _news(srv.state())


def _as_shape(d, monkeypatch, shape, **counts):
    monkeypatch.setattr(relay, "activity_shape",
                        lambda did, _shape=shape: _shape if did == "phone-1" else 1)
    d._activity_counts = lambda: {
        "working": counts.get("working", 0),
        "idle": counts.get("idle", 0),
        "attention": counts.get("attention", 0),
        "subagents": 0,
    }


def _running(cost=None, sid="r1"):
    row = _row(sid, "Vex", 5)
    row["metrics"] = {} if cost is None else {"cost_usd": cost}
    return {"running": [row]}


@pytest.mark.asyncio
async def test_a_shape_1_phone_never_receives_a_faceless_update(monkeypatch):
    """Today's card, byte for byte: a working row with nobody waiting is
    not an update, and the waiter leaving is one end, then nothing."""
    d = _glued(monkeypatch, {"waiting": [_row("s1", "Vex", 30)]})
    _as_shape(d, monkeypatch, 1, working=1, attention=1)
    _push(d)
    await _settle()
    assert d._relay_connector.pushed[0][1]["event"] == "update"
    assert d._relay_connector.pushed[0][1]["session_id"] == "s1"
    _push(d, _running())
    await _settle()
    assert [b["event"] for _, b in d._relay_connector.pushed] == ["update", "end"]
    _push(d, _running())
    await _settle()
    assert len(d._relay_connector.pushed) == 2
    for _, body in d._relay_connector.pushed:
        assert "working" not in body and "cost_usd" not in body and "title" not in body


@pytest.mark.asyncio
async def test_a_shape_2_phone_with_nobody_waiting_gets_the_fleet_card(monkeypatch):
    d = _glued(monkeypatch, _running())
    _as_shape(d, monkeypatch, 2, working=1)
    _push(d)
    await _settle()
    ((_did, body),) = d._relay_connector.pushed
    assert body["event"] == "update"
    assert body["working"] == 1 and body["needs_you"] == 0 and body["standing_by"] == 0
    assert body["session_id"] == "" and body["kind"] == "attention" and body["since"] == 0
    assert "cost_usd" not in body and "title" not in body


@pytest.mark.asyncio
async def test_a_waiter_sits_on_the_fleet_card_and_leaves_without_an_end(monkeypatch):
    d = _glued(monkeypatch, _running())
    _as_shape(d, monkeypatch, 2, working=1)
    _push(d)
    await _settle()
    waiting = {"waiting": [_row("s1", "Vex", 30)], **_running(sid="r1")}
    # The running row and the waiter are different sessions; counts say
    # one of each.
    d._activity_counts = lambda: {"working": 1, "idle": 0, "attention": 1, "subagents": 0}
    _push(d, waiting)
    await _settle()
    face = d._relay_connector.pushed[-1][1]
    assert face["event"] == "update"
    assert face["session_id"] == "s1" and face["needs_you"] == 1
    assert face["nickname"] == "Vex"
    d._activity_counts = lambda: {"working": 1, "idle": 0, "attention": 0, "subagents": 0}
    _push(d, _running())
    await _settle()
    quiet = d._relay_connector.pushed[-1][1]
    assert quiet["event"] == "update"
    assert quiet["session_id"] == "" and quiet["needs_you"] == 0
    assert [b["event"] for _, b in d._relay_connector.pushed] == ["update", "update", "update"]


@pytest.mark.asyncio
async def test_an_empty_fleet_sends_one_end_and_then_nothing(monkeypatch):
    d = _glued(monkeypatch, _running())
    _as_shape(d, monkeypatch, 2, working=1)
    _push(d)
    await _settle()
    d._activity_counts = lambda: {"working": 0, "idle": 0, "attention": 0, "subagents": 0}
    _push(d, {})
    await _settle()
    assert d._relay_connector.pushed[-1][1]["event"] == "end"
    assert d._live_activity_last["phone-1"] == "ended"
    _push(d, {})
    await _settle()
    assert len(d._relay_connector.pushed) == 2


@pytest.mark.asyncio
async def test_a_figures_only_change_waits_and_a_count_does_not(monkeypatch):
    from dark_army_daemon import daemon as daemon_mod
    clock = [1_000.0]
    monkeypatch.setattr(daemon_mod.time, "monotonic", lambda: clock[0])
    d = _glued(monkeypatch, _running(cost=1.0))
    _as_shape(d, monkeypatch, 2, working=1)
    _push(d)
    await _settle()
    assert d._relay_connector.pushed[0][1]["cost_usd"] == 1.0
    clock[0] += 10
    _push(d, _running(cost=1.1))
    await _settle()
    assert len(d._relay_connector.pushed) == 1
    d._activity_counts = lambda: {"working": 2, "idle": 0, "attention": 0, "subagents": 0}
    _push(d, _running(cost=1.1))
    await _settle()
    assert len(d._relay_connector.pushed) == 2
    assert d._relay_connector.pushed[-1][1]["working"] == 2
    # The dime that was held goes out once the interval has passed, and
    # not before. Reset the count so only the cost moves.
    d._activity_counts = lambda: {"working": 2, "idle": 0, "attention": 0, "subagents": 0}
    clock[0] += 10
    _push(d, _running(cost=1.2))
    await _settle()
    assert len(d._relay_connector.pushed) == 2
    clock[0] = d._live_activity_sent_at["phone-1"] + daemon_mod.LIVE_ACTIVITY_FIGURES_INTERVAL_SECONDS
    _push(d, _running(cost=1.2))
    await _settle()
    assert len(d._relay_connector.pushed) == 3
    assert d._relay_connector.pushed[-1][1]["cost_usd"] == 1.2


@pytest.mark.asyncio
async def test_forget_live_activity_clears_the_figures_stamp(monkeypatch):
    d = _glued(monkeypatch, _running(cost=1.0))
    _as_shape(d, monkeypatch, 2, working=1)
    _push(d)
    await _settle()
    assert "phone-1" in d._live_activity_sent_at
    d.forget_live_activity("phone-1")
    assert "phone-1" not in d._live_activity_sent_at
    assert "phone-1" not in d._live_activity_last


@pytest.mark.asyncio
async def test_the_three_gates_still_hold_for_the_fleet_card(monkeypatch):
    d = _glued(monkeypatch, _running())
    _as_shape(d, monkeypatch, 2, working=1)
    d.remote_access_enabled = False
    _push(d)
    await _settle()
    assert d._relay_connector.pushed == []
    d.remote_access_enabled = True
    d.phone_push_enabled = False
    _push(d)
    await _settle()
    assert d._relay_connector.pushed == []
    d.phone_push_enabled = True
    d._relay_connector = None
    _push(d)
    d = _glued(monkeypatch, _running(), tokens=())
    _as_shape(d, monkeypatch, 2, working=1)
    _push(d)
    await _settle()
    assert d._relay_connector.pushed == []


@pytest.mark.asyncio
async def test_a_count_above_the_cap_is_clamped_on_the_wire(tmp_path):
    conn, _d, calls = _connector(tmp_path)
    relay.note_activity_token("dev-1", "ab" * 32, "prod", 2)
    await conn.push_activity("dev-1", {"event": "update", **_state(),
                                       "working": 2_000_000, "tokens_k": 999_999})
    posted = calls[0][2]
    assert posted["working"] == 999_999
    assert posted["tokens_k"] == 999_999


@pytest.mark.asyncio
async def test_a_fleet_body_posts_the_thirteen_keys_and_never_a_title(tmp_path):
    conn, _d, calls = _connector(tmp_path)
    relay.note_activity_token("dev-1", "ab" * 32, "dev", 2)
    body = {"event": "update", **_state(), "title": "Vex needs you",
            "working": 1, "needs_you": 0, "standing_by": 2,
            "cost_usd": 1.234, "tokens_k": 45,
            "cost_usd_hour": 4.216, "tokens_k_hour": 1100}
    assert await conn.push_activity("dev-1", body) is True
    posted = calls[0][2]
    assert set(posted) == {"tok", "env", "event", *live_activity.STATE_KEYS}
    assert "title" not in posted
    assert posted["cost_usd"] == 1.23
    assert posted["working"] == 1 and posted["tokens_k"] == 45
    assert posted["cost_usd_hour"] == 4.22 and posted["tokens_k_hour"] == 1100


@pytest.mark.asyncio
async def test_a_bad_count_is_dropped_and_a_body_without_fleet_keys_is_today(tmp_path):
    conn, _d, calls = _connector(tmp_path)
    relay.note_activity_token("dev-1", "ab" * 32, "prod")
    await conn.push_activity("dev-1", {"event": "update", **_state(),
                                       "working": -1, "needs_you": 1.5,
                                       "standing_by": 0, "tokens_k": "lots"})
    posted = calls[0][2]
    assert "working" not in posted and "needs_you" not in posted
    assert "tokens_k" not in posted
    assert posted["standing_by"] == 0
    await conn.push_activity("dev-1", {"event": "update", **_state()})
    assert set(calls[1][2]) == {"tok", "env", "event", "nickname", "slug",
                                "kind", "work", "since", "session_id"}


@pytest.mark.asyncio
async def test_a_landed_ok_on_a_fleet_body_logs_the_redeploy_once(tmp_path, caplog):
    conn, _d, _calls = _connector(tmp_path)
    relay.note_activity_token("dev-1", "ab" * 32, "prod", 2)
    fleet = {"event": "update", **_state(), "working": 1, "needs_you": 0,
             "standing_by": 0, "tokens_k": 3}
    conn._answer = (200, b"ok")
    with caplog.at_level("INFO"):
        assert await conn.push_activity_outcome("dev-1", fleet) == "landed"
        assert await conn.push_activity_outcome("dev-1", fleet) == "landed"
    assert sum("drops the fleet figures" in r.message for r in caplog.records) == 1
    caplog.clear()
    # A mailbox that answers the marker never logs, even with the
    # once-per-run flag clear — the line is for the drop, not for success.
    conn._activity_shape_said = False
    conn._answer = (200, b"ok shape=2")
    with caplog.at_level("INFO"):
        assert await conn.push_activity_outcome("dev-1", fleet) == "landed"
    assert sum("drops the fleet figures" in r.message for r in caplog.records) == 0




def test_fleet_face_falls_back_to_a_carded_waiter():
    """A waiter whose Needs-you entry is a board card (a hand-check due) is
    counted in "need you" but skipped by `subject`; the fleet card still
    draws that agent's face, as an attention face. `subject` itself — the
    face-only card's rule — is unchanged."""
    snapshot = {"waiting": [{"session_id": "s1", "nickname": "Ptys",
                             "name": "build", "idle_seconds": 30,
                             "quiet_since": 1000.0}]}
    cards = [{"session_id": "s1", "manual_check_due": True}]
    assert live_activity.subject(snapshot, {}, cards=cards) is None
    face = live_activity.fleet_face(snapshot, {}, cards=cards)
    assert face["session_id"] == "s1"
    assert face["kind"] == "attention"
    assert face["nickname"] == "Ptys"
    assert face["quiet_since"] == 1000.0
    assert live_activity.fleet_face({"waiting": []}, {}, cards=cards) is None
    # A real subject still wins.
    prompts = {"s2": {"id": "p"}}
    both = {"running": [{"session_id": "s2", "nickname": "Watch"}],
            "waiting": snapshot["waiting"]}
    assert live_activity.fleet_face(both, prompts, cards=cards)["session_id"] == "s2"


def test_phone_fleet_state_restates_the_waiting_face():
    source = (Path(__file__).resolve().parents[2]
              / "ios" / "BobPhone" / "LiveActivity.swift").read_text()
    fleet = source[source.index("static func fleetState("):
                   source.index("static func waitingFace(")]
    assert "subject(from: snapshot) ?? waitingFace(from: snapshot)" in fleet
    face = source[source.index("static func waitingFace("):
                  source.index("static func kindWord(")]
    assert "snapshot.agents.waiting.first(" in face
    assert 'kind: "attention"' in face
    assert "nickname: agent.nickname," in face


def test_fleet_state_carries_the_per_hour_rates_only_when_said():
    counts = {"working": 1, "attention": 0, "idle": 0}
    face = live_activity.empty_face(0)
    said = live_activity.fleet_state(
        counts, {"cost_usd": 87.531, "tokens_k": 23000,
                 "cost_usd_hour": 4.2, "tokens_k_hour": 1100}, face)
    assert said["cost_usd_hour"] == 4.2 and said["tokens_k_hour"] == 1100
    assert tuple(said) == live_activity.STATE_KEYS
    unsaid = live_activity.fleet_state(
        counts, {"cost_usd": 1.0, "tokens_k": 2,
                 "cost_usd_hour": None, "tokens_k_hour": None}, face)
    assert "cost_usd_hour" not in unsaid and "tokens_k_hour" not in unsaid
    # A rate moving alone is a figures-only change: it waits the interval.
    moved = dict(said, cost_usd_hour=5.0)
    assert live_activity.figures_only_change(moved, said)


@pytest.mark.asyncio
async def test_ptys_keeps_her_face_on_the_live_card(tmp_path):
    """25 Sep 2026: the live card for Ptys drew an empty tile — her name
    was spelled with a non-ASCII letter then, and the slug check blanked
    it before it left the Mac. The face is the cast list, so every name keeps its portrait and
    anything else is still dropped."""
    conn, _d, calls = _connector(tmp_path)
    relay.note_activity_token("dev-1", "ab" * 32, "dev")
    for slug, sent in (("ptys", "ptys"), ("vex", "vex"), ("stranger", ""),
                       ("Vex", ""), ("", "")):
        calls.clear()
        assert await conn.push_activity(
            "dev-1", {"event": "update", **_state(slug=slug)}) is True
        assert calls[0][2]["slug"] == sent, slug

