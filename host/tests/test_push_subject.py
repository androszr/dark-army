# host/tests/test_push_subject.py
"""A tapped notification opens from the picture the phone already holds.

The Mac's half: a single-alert buzz names its subject — the opaque session
id and, where the session is bound to a card, the card id — on every device,
joined only in `PUSH_ID_SHAPE`; a collapsed buzz and a machine alert carry
neither. The phone's half is structural: the held-first resolver is pure,
the model's `open()` never fetches, and the log is a protected file scoped
to the pairing. Plan: `plans/2026-09-20-offline-notification-cache.md`.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tests.test_relay_client import connector, mailbox  # noqa: F401 — fixtures
from dark_army_daemon import relay
from dark_army_daemon.daemon import BobDaemon, _ACTIVE_CARD_LINKS

ROOT = Path(__file__).resolve().parents[2]
PUSH_JS = ROOT / "relay" / "api" / "push.js"
PHONE = ROOT / "ios" / "BobPhone"
WIDGET = ROOT / "ios" / "BobPhoneWidget"
SHARED = ROOT / "ios" / "Shared"
TRANSPORT = ROOT / "docs" / "transport-contract.md"
PHONE_CONTRACT = ROOT / "docs" / "phone-contract.md"


def _alert(**over) -> dict:
    base = dict(id="s1:x", session_id="s1", nickname="Vex", title="t",
                subtitle="", body="b", severity="warn", rule="attention",
                created_at=0.0, actions=[], character="", state="",
                kind="attention", request_id="")
    base.update(over)
    return base


def _daemon(cards: list) -> BobDaemon:
    daemon = BobDaemon()
    daemon._board_state = {"cards": cards}
    return daemon


def _code(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return "\n".join(line.split("//")[0] for line in text.splitlines())


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
                return text[i:j + 1]
    raise AssertionError(f"unbalanced block at {start!r}")


# --- (1) the subject the daemon composes ------------------------------------------


def test_one_alert_bound_to_a_card_names_session_and_card():
    daemon = _daemon([dict(id="card-9", title="Work", session_id="s1", link_state="live")])
    assert daemon._compose_push_subject([_alert()]) == {"session_id": "s1", "card_id": "card-9"}


def test_one_alert_with_no_card_names_the_session_alone():
    daemon = _daemon([dict(id="card-9", title="Work", session_id="other", link_state="live")])
    assert daemon._compose_push_subject([_alert()]) == {"session_id": "s1"}


def test_a_refined_card_binds_its_planning_session_too():
    daemon = _daemon([dict(id="card-9", title="Work", refine_session_id="s1", refine_state="live")])
    assert daemon._compose_push_subject([_alert()]) == {"session_id": "s1", "card_id": "card-9"}


@pytest.mark.parametrize("pending", [
    [],
    [_alert(), _alert(session_id="s2")],
    [_alert(kind="security", session_id="")],
    [_alert(session_id="")],
    [None],
    ["not a dict"],
])
def test_a_collapsed_buzz_a_machine_alert_and_an_unknown_shape_carry_nothing(pending):
    daemon = _daemon([dict(id="card-9", title="Work", session_id="s1", link_state="live")])
    assert daemon._compose_push_subject(pending) == {}


def test_the_subject_joins_every_device_before_the_act_leg(monkeypatch):
    """`_push_phone_alerts` puts the subject on the body itself, so a phone
    with the lock-screen switch off gets it too, and the act leg's
    `session_id` is the same value — byte-identical act fields."""
    daemon = _daemon([dict(id="card-9", title="Work", session_id="s1", link_state="live")])
    daemon.remote_access_enabled = True
    daemon._agents_snapshot_cache = {"waiting": []}
    posted = {}

    async def push(did, body):
        posted[did] = body

    daemon._relay_connector = type("Connector", (), {"push_alert": staticmethod(push)})()
    monkeypatch.setattr(relay, "channel_ids", lambda: ["off", "on"])
    monkeypatch.setattr(relay, "push_token", lambda _: "token")
    monkeypatch.setattr(relay, "lock_screen_actions", lambda did: did == "on")
    import asyncio

    async def run():
        assert daemon._push_phone_alerts([_alert(kind="question")]) == "sent:2"
        await asyncio.sleep(0)
    asyncio.run(run())
    assert posted["off"]["session_id"] == "s1" and posted["off"]["card_id"] == "card-9"
    assert "act" not in posted["off"]
    assert posted["on"]["session_id"] == "s1" and posted["on"]["card_id"] == "card-9"
    assert posted["on"]["act"] == "acknowledge"


# --- (2) the refactor pin: `_card_titles_by_session` answers as before --------------


def _titles_before(cards: list) -> dict:
    """The two-pass walk exactly as it stood before `_card_field_by_session`
    was factored out (verbatim, 2026-09-20), so a drift in either pass or the
    first-wins rule fails here rather than on somebody's push."""
    out: dict = {}
    for active_only in (True, False):
        for card in cards:
            if not isinstance(card, dict):
                continue
            title = " ".join(str(card.get("title") or "").split())
            if not title:
                continue
            for sid_key, state_key in (
                ("session_id", "link_state"),
                ("refine_session_id", "refine_state"),
            ):
                if active_only and card.get(state_key) not in _ACTIVE_CARD_LINKS:
                    continue
                sid = str(card.get(sid_key) or "")
                if sid and sid not in out:
                    out[sid] = title
    return out


FIXTURE = [
    dict(id="stale", title="  Old   work ", session_id="s1", link_state="ended"),
    dict(id="untitled", title="", session_id="s1", link_state="live"),
    dict(id="active", title="Live work", session_id="s1", link_state="live"),
    dict(id="refine", title="Planned", refine_session_id="s2", refine_state="live"),
    dict(id="second-stale", title="Older", session_id="s3", link_state="ended"),
    dict(id="first-stale", title="Oldest", session_id="s3", link_state=""),
    "junk",
    dict(id="no-sid", title="Nobody", session_id="", link_state="live"),
]


def test_card_titles_by_session_answers_exactly_as_before():
    daemon = _daemon(FIXTURE)
    expected = _titles_before(FIXTURE)
    assert expected == {"s1": "Live work", "s2": "Planned", "s3": "Older"}
    assert daemon._card_titles_by_session() == expected
    # The id walk skips a card with no *id*, not one with no title: the
    # untitled active card is the first active link naming s1, so it wins
    # there, exactly as the first-wins rule reads per field.
    assert daemon._card_ids_by_session() == {"s1": "untitled", "s2": "refine", "s3": "second-stale"}


def test_the_untitled_card_is_skipped_for_the_title_and_an_idless_one_for_the_id():
    cards = [dict(id="", title="Nameless id", session_id="s1", link_state="live"),
             dict(id="has-id", title="", session_id="s1", link_state="live")]
    daemon = _daemon(cards)
    assert daemon._card_titles_by_session() == {"s1": "Nameless id"}
    assert daemon._card_ids_by_session() == {"s1": "has-id"}


def test_an_empty_or_missing_board_answers_nothing():
    assert _daemon([])._card_titles_by_session() == {}
    daemon = BobDaemon()
    daemon._board_state = None
    assert daemon._card_titles_by_session() == {}
    assert daemon._card_ids_by_session() == {}


# --- (3) the wire: `push_alert` joins the subject only in shape -------------------


@pytest.mark.asyncio
async def test_push_alert_joins_session_and_card_only_in_shape(connector):  # noqa: F811
    conn, _srv, _key, _url = connector
    calls = []

    async def fake_http(method, url, data=None, headers=None):
        calls.append(json.loads(data))
        return 200, b"ok"

    conn._http = fake_http
    relay.note_push_token("dev-1", "ab" * 32, "dev")
    base = {"title": "Vex needs you", "badge": 1, "kind": "question"}
    assert await conn.push_alert("dev-1", dict(base, session_id="s-1", card_id="card-9"))
    assert await conn.push_alert("dev-1", dict(base, session_id="s-1", card_id="bad id!"))
    assert await conn.push_alert("dev-1", dict(base, session_id="bad id!", card_id="card-9"))
    assert await conn.push_alert("dev-1", dict(base, session_id="", card_id=""))
    assert await conn.push_alert("dev-1", dict(base))
    both, bad_card, bad_sid, empty, neither = calls
    assert both["session_id"] == "s-1" and both["card_id"] == "card-9"
    assert bad_card["session_id"] == "s-1" and "card_id" not in bad_card
    assert bad_sid["card_id"] == "card-9" and "session_id" not in bad_sid
    for posted in (empty, neither):
        assert "session_id" not in posted and "card_id" not in posted
    assert set(neither) == {"tok", "env", "title", "badge", "kind"}
    # The act leg rides on the shaped subject: no shaped sid, no act.
    for posted in calls:
        assert "act" not in posted


# --- (4) the mailbox re-checks the same shapes -------------------------------------


def test_the_mailbox_refuses_a_bad_subject_inside_the_alert_leg():
    text = PUSH_JS.read_text()
    assert text.count('"bad subject"') == 1
    assert "body.card_id" in text
    alert_leg = text[text.index("} else {"):text.index("let status;")]
    assert "body.card_id" in alert_leg and "body.session_id" in alert_leg
    assert '"bad subject"' in alert_leg
    # The subject picks no category: that line stays inside the act block.
    subject = alert_leg[alert_leg.index("body.card_id !== undefined"):alert_leg.index("if (body.act !== undefined)")]
    assert "category" not in subject


# --- (5) the docs no longer say only version/receipt metadata rides --------------


def test_the_transport_contract_names_the_subject():
    text = TRANSPORT.read_text()
    assert "APNs carries only version/receipt metadata" not in text
    assert text.count("A buzz names its subject") == 1
    assert "NotificationLogStore" in PHONE_CONTRACT.read_text()


# --- (6) the phone's structure: held first, pure resolver, protected log ------------


def test_push_swift_banks_at_the_four_sources_and_reads_no_kind():
    text = PHONE.joinpath("Push.swift").read_text()
    assert '"kind"' not in text
    assert text.count("bank(") >= 4
    for anchor in ("willPresent", "didReceive", "func clearBadge()", "func clearDeliveredIfQuiet("):
        block = text.split(anchor, 1)[1].split("\n    }", 1)[0]
        assert "bank(" in block, anchor
    present = text.split("willPresent", 1)[1].split("\n    }", 1)[0]
    # The pin's anchor: the block still ends in `return []`, with the bank above it.
    assert present.rstrip().endswith("return []")
    assert present.index("bank(") < present.index("return []")
    # The tray is read before it is emptied, at both clearing sites.
    for anchor in ("func clearBadge()", "func clearDeliveredIfQuiet("):
        block = text.split(anchor, 1)[1].split("\n    }", 1)[0]
        assert block.index("deliveredNotifications()") < block.index("removeAllDeliveredNotifications()"), anchor
    assert "func takeBanked()" in text
    assert "NotificationLogStore.maxEntries" in text


def test_the_held_resolver_is_pure_and_the_models_open_never_fetches():
    resolver = _code(PHONE.joinpath("HeldDestination.swift").read_text())
    body = _block(resolver, "static func resolve(")
    for word in ("await", "client", "fetch", "URLSession"):
        assert word not in body, word
    assert "snapshot.board.cards" in body
    assert "PhoneInbox.uniqueAgent(" in body
    model = _code(PHONE.joinpath("NotificationDestinationModel.swift").read_text())
    open_body = _block(model, "func open()")
    for word in ("refresh", "fetch"):
        assert word not in open_body, word
    assert "log.entry(for: route.receiptId)" in open_body
    assert "HeldDestination.resolve(" in open_body
    resolve_body = _block(model, "func resolve()")
    assert "await refresh()" in resolve_body
    assert resolve_body.index("isLive()") < resolve_body.index("await fetch(")


def test_the_view_opens_held_first_and_draws_the_offline_sentence_only_without_a_held_view():
    text = PHONE.joinpath("CatchUpView.swift").read_text()
    view = _block(_code(text), "struct NotificationDestinationView")
    assert view.index("model.open()") < view.index("model.resolve()")
    task = _block(view, ".task {")
    assert task.index("model.open()") < task.index("resolvedNotification(")
    sentence = "Offline. This notification will open after Dark Army reconnects."
    assert text.count(sentence) == 1
    line = next(l for l in text.splitlines() if sentence in l)
    assert "model.held == nil" in line
    # The held view marks nothing read: consume and remember stay on the live branch.
    # The held branch yields to any page, available or not: an unavailable
    # page's reason is the Mac's own answer and must reach the screen.
    held_branch = view[view.index("else if page == nil, let held = model.held"):view.index(".task {")]
    for word in ("router.consume(", "rememberNotification(", "snapshot ="):
        assert word not in held_branch, word
    assert "HeldPictureBanner(" in held_branch and "HeldDestinationView(held: held" in held_branch
    # Something to press when a live Mac answered nothing this once.
    assert 'DecryptButton("Retry")' in held_branch
    # The seen list's banner follows the fleet's rule for its tail.
    assert "HeldPictureBanner(asOf: asOf, reaching: client.status != .unreachable)" in text
    # Catch up lists the buzzes this phone saw when the Mac is out of reach.
    assert "NOTIFICATIONS SEEN ON THIS PHONE" in text
    assert "client.notificationLog.entries" in text
    assert "HeldDestination.resolve(entry: entry, snapshot: client.snapshot)" in text


def test_the_client_scopes_the_log_to_the_pairing_and_restore_never_reads_it():
    client = _code(PHONE.joinpath("Client.swift").read_text())
    assert client.count("notificationLog.adopt(record.token)") == 1
    assert client.count("notificationLog.forget()") == 1
    forget = _block(client, "private func forgetPairing()")
    assert "notificationLog.forget()" in forget
    restore = _block(client, "private func restoreHeldPicture(")
    assert "notificationLog" not in restore
    assert client.count("snapshot = decoded") == 1 and client.count("snapshot = held") == 1
    assert "notificationLog.enrich(receiptId:" in client
    app = _code(PHONE.joinpath("BobPhoneApp.swift").read_text())
    gate = app[app.index(".onChange(of: lock.unlocked)"):app.index(".onChange(of: pairing.record)")]
    assert "client.notificationLog.load()" in gate
    assert gate.index("client.heldPicture.load()") < gate.index("client.notificationLog.load()")
    assert gate.index("PhoneRouter.shared.pair(token: record.token)") < gate.index("client.absorbBankedNotifications(")


def test_the_log_is_a_protected_file_written_off_the_main_actor():
    log = PHONE.joinpath("NotificationLog.swift").read_text()
    assert "FileProtectionType.complete" in log or "completeFileProtection" in log
    assert "Task.detached" in log
    assert "[.atomic]" in log
    assert "maxEntries = 200" in log and "maxAge: TimeInterval = 30 * 86400" in log
    assert 'fileName = "notification-log.json"' in log
    from_body = _block(_code(log), "static func from(content:")
    for word in ('"act"', '"request_id"', '"kind"', "body"):
        assert word not in from_body, word


def test_nothing_new_clips_and_the_widget_reads_none_of_it():
    for name in ("NotificationLog.swift", "HeldDestination.swift", "NotificationDestinationModel.swift"):
        text = PHONE.joinpath(name).read_text()
        assert ".lineLimit(" not in text, name
        assert "snapshot = " not in text, name
    for folder in (WIDGET, SHARED):
        for path in folder.glob("*.swift"):
            text = path.read_text()
            assert "NotificationLog" not in text and "HeldDestination" not in text, path.name
    # The doors gained nothing: no new verb, read or field on either tuple.
    api = (ROOT / "host" / "dark_army_daemon" / "api_server.py").read_text()
    for word in ("notification_log", "held_destination", "push_subject"):
        assert word not in api
