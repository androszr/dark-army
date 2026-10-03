# host/tests/test_lock_screen_actions.py
"""Allow, Deny and Acknowledge on the phone's banner — the desk's consent,
the identifiers that ride the buzz, and the two ends' shapes.

Off, the push is byte-identical to today's. On, the buzz gains an `act`
word and one or two opaque ids (never the ask's words), only when exactly
one alert is pending, and `push.js` re-checks the same shapes before it
names the notification category iOS draws the buttons for.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tests.test_relay_client import connector, mailbox  # noqa: F401 — fixtures
from dark_army_daemon import alerts as alerting
from dark_army_daemon import relay, relay_client
from dark_army_daemon.daemon import BobDaemon

ROOT = Path(__file__).resolve().parents[2]
PUSH_JS = ROOT / "relay" / "api" / "push.js"
PHONE = ROOT / "ios" / "BobPhone"


def _alert(**over) -> dict:
    base = dict(id="s1:x", session_id="s1", nickname="Vex", title="t",
                subtitle="", body="b", severity="warn", rule="attention",
                created_at=0.0, actions=[], character="", state="",
                kind="attention", request_id="")
    base.update(over)
    return base


def test_a_permission_alert_carries_its_request_id():
    """The gate stamps the prompt's id on the alert it raises, so the push
    can name it; every other rule leaves it empty."""
    src = Path(alerting.__file__).read_text()
    body = src[src.index("def _permission("):]
    body = body[:body.index("\n    def ")]
    assert "request_id=rid" in body


def test_as_dict_states_request_id_even_when_empty():
    row = alerting.Alert(id="a", session_id="s", nickname="n", title="t",
                         body="b", severity="warn", rule="attention",
                         created_at=0.0).as_dict()
    assert row["request_id"] == ""


@pytest.mark.parametrize("pending,expected", [
    ([], {}),
    ([_alert(rule="permission", kind="permission", request_id="hook-1")],
     {"act": "permission", "request_id": "hook-1", "session_id": "s1"}),
    ([_alert(rule="permission", kind="permission", request_id="")], {}),
    ([_alert(kind="question")], {"act": "acknowledge", "session_id": "s1"}),
    ([_alert(kind="attention")], {"act": "acknowledge", "session_id": "s1"}),
    ([_alert(kind="finished")], {}),
    pytest.param([_alert(kind="picks", rule="review_picks", run_id="r-1")],
     {"act": "review", "run_id": "r-1"}, id="review-bound"),
    pytest.param([_alert(kind="picks", rule="review_picks", run_id="r-1", session_id="")],
     {"act": "review", "run_id": "r-1"}, id="review-unbound"),
    pytest.param([_alert(kind="picks", rule="review_picks", run_id="")], {}, id="review-no-run"),
    pytest.param([_alert(kind="picks", rule="review_picks", run_id="r-1"), _alert(kind="question")], {}, id="review-beside-another"),
    ([_alert(kind="security", session_id="")], {}),
    ([_alert(kind="question"), _alert(kind="question", session_id="s2")], {}),
])
def test_one_alert_alone_may_be_answered_from_the_banner(pending, expected):
    assert BobDaemon._compose_push_act(pending) == expected


def test_the_switch_is_per_device_and_off_by_default(tmp_path, monkeypatch):
    assert relay.create_channel("dev-1")
    assert relay.lock_screen_actions("dev-1") is False
    assert relay.lock_screen_actions("nobody") is False
    ok, detail = relay.set_lock_screen_actions("dev-1", True)
    assert ok, detail
    assert relay.lock_screen_actions("dev-1") is True
    assert relay.set_lock_screen_actions("dev-1", "yes")[0] is False
    assert relay.set_lock_screen_actions("nobody", True)[0] is False
    ok, _ = relay.set_lock_screen_actions("dev-1", False)
    assert ok and relay.lock_screen_actions("dev-1") is False


@pytest.mark.asyncio
async def test_push_alert_joins_act_fields_only_in_shape(connector):
    conn, _srv, _key, _url = connector
    calls = []

    async def fake_http(method, url, data=None, headers=None):
        calls.append(json.loads(data))
        return 200, b"ok"

    conn._http = fake_http
    relay.note_push_token("dev-1", "ab" * 32, "dev")
    base = {"title": "Vex needs you", "badge": 1, "kind": "permission"}
    assert await conn.push_alert("dev-1", dict(base, act="permission",
                                               request_id="hook-1",
                                               session_id="s-1"))
    assert await conn.push_alert("dev-1", dict(base, act="acknowledge",
                                               session_id="s-1"))
    assert await conn.push_alert("dev-1", dict(base, act="permission",
                                               session_id="s-1"))  # no rid
    assert await conn.push_alert("dev-1", dict(base, act="reboot",
                                               session_id="s-1"))
    assert await conn.push_alert("dev-1", dict(base, act="acknowledge",
                                               session_id="bad id!"))
    assert await conn.push_alert("dev-1", dict(base))
    first, second, no_rid, bad_act, bad_sid, plain = calls
    assert {k: first[k] for k in ("act", "request_id", "session_id")} == {
        "act": "permission", "request_id": "hook-1", "session_id": "s-1"}
    assert second["act"] == "acknowledge" and "request_id" not in second
    # The act leg is refused whole on a missing rid or an unknown act, but
    # the *subject* still rides: a shaped `session_id` is the buzz's subject
    # on every device (`_compose_push_subject`, 2026-09-20), joined before and
    # independently of the act. So `no_rid` and `bad_act` carry `session_id`
    # with no `act` and no `request_id`; `bad_sid` (out of `PUSH_ID_SHAPE`)
    # and `plain` (no subject at all) carry none of the three, exactly as
    # before — the subject never leaks into a body that had nothing to say.
    for posted in (no_rid, bad_act):
        assert "act" not in posted
        assert "request_id" not in posted
        assert posted["session_id"] == "s-1"
    for posted in (bad_sid, plain):
        assert "act" not in posted
        assert "session_id" not in posted
        assert "request_id" not in posted
    assert set(plain) == {"tok", "env", "title", "badge", "kind"}


@pytest.mark.asyncio
async def test_push_alert_joins_the_review_act_with_a_run_id_and_no_session(connector):
    conn, _srv, _key, _url = connector
    calls = []

    async def fake_http(method, url, data=None, headers=None):
        calls.append(json.loads(data))
        return 200, b"ok"

    conn._http = fake_http
    relay.note_push_token("dev-1", "ab" * 32, "dev")
    base = {"title": "Vex is waiting on your picks", "badge": 1, "kind": "picks"}
    assert await conn.push_alert("dev-1", dict(base, act="review", run_id="r-1"))
    assert await conn.push_alert("dev-1", dict(base, act="review", run_id="bad id!"))
    assert await conn.push_alert("dev-1", dict(base, act="review"))
    good, bad, missing = calls
    assert good["act"] == "review" and good["run_id"] == "r-1"
    assert "session_id" not in good and "request_id" not in good
    for posted in (bad, missing):
        assert "act" not in posted and "run_id" not in posted


def test_the_review_button_rides_every_device(monkeypatch):
    """The review act writes nothing, so it is not behind the per-device
    "Answer from the lock screen" switch; every other act still is."""
    import asyncio

    daemon = BobDaemon.__new__(BobDaemon)
    daemon.remote_access_enabled = True
    daemon._agents_snapshot_cache = {"waiting": []}
    daemon._cards_snapshot_cache = []
    posted = {}

    async def push(did, body):
        posted.setdefault(len(posted), {})[did] = body

    daemon._relay_connector = type("Connector", (), {"push_alert": staticmethod(push)})()
    monkeypatch.setattr(relay, "channel_ids", lambda: ["off", "on"])
    monkeypatch.setattr(relay, "push_token", lambda _: "token")
    monkeypatch.setattr(relay, "lock_screen_actions", lambda did: did == "on")

    async def run(pending):
        posted.clear()
        assert daemon._push_phone_alerts(pending) == "sent:2"
        await asyncio.sleep(0)
        return {did: body for entry in posted.values() for did, body in entry.items()}

    picks = asyncio.run(run([_alert(kind="picks", rule="review_picks",
                                    run_id="r-1")]))
    for did in ("off", "on"):
        assert picks[did]["act"] == "review" and picks[did]["run_id"] == "r-1"
    question = asyncio.run(run([_alert(kind="question")]))
    assert "act" not in question["off"]
    assert question["on"]["act"] == "acknowledge"


def test_the_mailbox_pins_the_same_acts_and_id_shape():
    text = PUSH_JS.read_text()
    acts = re.search(r'const ACTS = \[(.*?)\];', text)
    assert acts
    assert [a.strip().strip('"') for a in acts.group(1).split(",")] \
        == list(relay_client.PUSH_ACTS)
    shape = re.search(r"const ID_SHAPE = /(.*?)/;", text)
    assert shape and shape.group(1) == relay_client.PUSH_ID_SHAPE.pattern
    assert '"bob.permission"' in text and '"bob.acknowledge"' in text
    assert '"bob.review"' in text
    assert "payload.aps.category" in text


def test_the_phone_names_the_same_categories_and_verbs():
    swift = (PHONE / "LockScreenActions.swift").read_text()
    assert 'permissionCategory = "bob.permission"' in swift
    assert 'acknowledgeCategory = "bob.acknowledge"' in swift
    assert "PhoneActions.permissionVerdict" in swift
    assert "PhoneActions.dismiss" in swift
    # Three buttons, each `.authenticationRequired` (plus the doc line).
    assert swift.count("options: [.authenticationRequired") == 3
    # Open review: a foreground button, still behind the unlock.
    assert 'reviewCategory = "bob.review"' in swift
    assert '"Open review"' in swift
    assert "options: [.foreground, .authenticationRequired]" in swift
    assert "static func opens(" in swift
    assert "RemoteAuth" not in swift
    push = (PHONE / "Push.swift").read_text()
    assert "LockScreenActions.register()" in push
    assert "LockScreenActions.press(" in push
    assert "LockScreenActions.opens(" in push
    client = (PHONE / "Client.swift").read_text()
    write = client[client.index("func lockScreenWrite("):]
    write = write[:write.index("\n    }\n")]
    assert "quietPost(" in write
    assert "receipts" not in write and "RemoteAuth" not in write


def test_the_loopback_verb_takes_a_bare_bool_only():
    from dark_army_daemon import api_server
    src = Path(api_server.__file__).read_text()
    assert '"set_lock_screen_actions"' in src[src.index("def _devices_request"):
                                             src.index("async def _devices(")]
    body = src[src.index('if action == "set_lock_screen_actions":'):]
    assert "isinstance(enabled, bool)" in body[:600]
    for name in ("LAN_ACTIONS", "REMOTE_ACTIONS"):
        block = src[src.index(f"{name} = ("):]
        block = block[:block.index("\n    )")]
        assert "set_lock_screen_actions" not in block


@pytest.mark.asyncio
async def test_review_act_never_writes_a_request_id(connector):
    conn, _srv, _key, _url = connector
    calls = []

    async def fake_http(method, url, data=None, headers=None):
        calls.append(json.loads(data))
        return 200, b"ok"

    conn._http = fake_http
    relay.note_push_token("dev-1", "ab" * 32, "dev")
    assert await conn.push_alert("dev-1", {"title": "t", "badge": 1, "kind": "picks",
                                           "act": "review", "run_id": "r-1",
                                           "request_id": "hook-1"})
    assert calls[0]["act"] == "review" and "request_id" not in calls[0]


def test_review_act_is_not_behind_the_switch_in_the_loop():
    src = Path(BobDaemon.__module__.replace(".", "/") + ".py")
    text = (ROOT / "host" / src).read_text()
    assert 'act.get("act") == "review"' in text


def test_review_run_id_is_a_closed_field_on_the_alert():
    row = alerting.Alert(id="a", session_id="", nickname="n", title="t", body="b",
                         severity="warn", rule="review_picks", created_at=0.0,
                         run_id="r-1").as_dict()
    assert row["run_id"] == "r-1"
