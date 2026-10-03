"""Getting a decided alert as far as macOS.

`test_alerts.py` covers the policy — whether something has earned an interrupt.
This covers the other half: handing it to the one process that can post it,
exactly once, and doing something honest when it cannot.
"""
from unittest.mock import MagicMock

import json
import time

import pytest

from dark_army_daemon.daemon import BobDaemon


def _daemon() -> BobDaemon:
    return BobDaemon.__new__(BobDaemon)


def _alert(sid="s1", rule="card"):
    return {"id": f"{sid}:{rule}:1", "session_id": sid, "title": "Vex needs you",
            "body": "Waiting for your input", "rule": rule, "severity": "warn"}


# ── the daemon's side: a queue, not a mirror ─────────────────────────────────

def test_alerts_reach_an_observer_that_can_post():
    d = _daemon()
    d._undelivered = [_alert()]
    seen = []
    d._observers = [type("Obs", (), {"on_alerts": lambda self, a: seen.append(a)})()]
    d._deliver_alerts()
    assert len(seen) == 1 and seen[0][0]["session_id"] == "s1"


def test_delivery_happens_once():
    """The queue is drained, not read. Re-posting an interruption every few
    seconds is how an app gets muted in a week."""
    d = _daemon()
    d._undelivered = [_alert()]
    seen = []
    d._observers = [type("Obs", (), {"on_alerts": lambda self, a: seen.extend(a)})()]
    d._deliver_alerts()
    d._deliver_alerts()
    assert len(seen) == 1
    assert d._undelivered == []


def test_undeliverable_alerts_are_dropped_not_hoarded():
    """With nothing able to post, the queue still clears: an alert is about now,
    and a deliverer that appears later must not be handed the afternoon."""
    d = _daemon()
    d._undelivered = [_alert()]
    d._observers = [object()]                 # implements nothing
    d._deliver_alerts()
    assert d._undelivered == []


def test_one_failing_observer_does_not_swallow_the_alert_for_another():
    d = _daemon()
    d._undelivered = [_alert()]
    seen = []

    class Angry:
        def on_alerts(self, alerts):
            raise RuntimeError("no")

    d._observers = [Angry(),
                    type("Obs", (), {"on_alerts": lambda self, a: seen.extend(a)})()]
    d._deliver_alerts()
    assert len(seen) == 1


def test_muting_a_session_reaches_the_policy():
    from dark_army_daemon import alerts as alerting

    d = _daemon()
    policy = alerting.AlertPolicy()
    d.__dict__["_alert_policy"] = policy
    d.mute_session_alerts("s1")
    assert policy.is_muted("s1")


def test_muting_before_any_snapshot_is_harmless():
    """The policy is created lazily on the first enrichment pass, and a banner
    can be tapped before one has run."""
    _daemon().mute_session_alerts("s1")       # must not raise


# ── the menu bar's side: posting, and the verbs ──────────────────────────────

def _app():
    from dark_army_menubar import app as A

    instance = object.__new__(A.BobCompanionApp)
    instance._notifier = MagicMock()
    instance._settings = {"notification_banners": True}
    instance._daemon = MagicMock()
    instance._loop = MagicMock()
    return instance


def test_banners_are_posted_when_switched_on():
    app = _app()
    app._post_alerts([_alert(), _alert("s2")])
    assert app._notifier.post.call_count == 2


def test_a_security_alert_is_a_notice_not_a_session_banner():
    """A security row has no session behind it, so the banner must carry no
    Open in Editor / Dismiss / Mute — `post_notice`, never `post`."""
    app = _app()
    row = {"id": "access:abc", "session_id": "", "kind": "security",
           "title": "Dark Army refused repeated connection attempts",
           "body": "5 refused attempts from 10.0.0.9 at the Wi-Fi door in 10 minutes",
           "rule": "access_burst", "severity": "crit"}
    app._post_alerts([row, _alert()])
    app._notifier.post_notice.assert_called_once_with(
        "access:abc", row["title"], row["body"])
    assert app._notifier.post.call_count == 1
    assert app._notifier.post.call_args[0][0]["session_id"] == "s1"


def test_the_preference_is_read_at_posting_time():
    """Same discipline as the chime: a switch flipped while an alert is in
    flight silences it, rather than silencing only the next one."""
    app = _app()
    app._settings["notification_banners"] = False
    app._post_alerts([_alert()])
    app._notifier.post.assert_not_called()


@pytest.mark.parametrize("action,method", [
    ("reveal", "reveal_in_vscode"),
    ("dismiss", "dismiss_notification"),
])
def test_a_tap_runs_the_daemon_verb_it_names(action, method, monkeypatch):
    from dark_army_menubar import app as A

    app = _app()
    scheduled = []
    monkeypatch.setattr(A.asyncio, "run_coroutine_threadsafe",
                        lambda coro, loop: scheduled.append(coro))
    app._on_notification_action(action, "s1")
    assert len(scheduled) == 1
    getattr(app._daemon, method).assert_called_once_with("s1")


def test_mute_does_not_go_through_the_loop(monkeypatch):
    """It only writes to the policy's own set, and it should take effect before
    the next snapshot rather than behind it."""
    from dark_army_menubar import app as A

    app = _app()
    monkeypatch.setattr(A.asyncio, "run_coroutine_threadsafe",
                        lambda coro, loop: pytest.fail("hopped for a mute"))
    app._on_notification_action("mute", "s1")
    app._daemon.mute_session_alerts.assert_called_once_with("s1")


def test_the_body_tap_opens_the_panel_on_that_agent(monkeypatch):
    """A tap on the banner itself goes to Dark Army's own panel, not to the editor:
    the banner is four seconds of one sentence, and the panel is where the rest
    of it is. `show`, never `toggle` — a tap that lands while the panel happens
    to be open must not close it."""
    from dark_army_menubar import app as A

    app = _app()
    app._panel = MagicMock()
    app._panel.available = True
    app._panel.show.return_value = True
    monkeypatch.setattr(A.BobCompanionApp, "_push_panel_context", lambda self: None)
    monkeypatch.setattr(A.BobCompanionApp, "_status_anchor", lambda self: (1, 2, 3, 4))
    monkeypatch.setattr(A.asyncio, "run_coroutine_threadsafe",
                        lambda coro, loop: pytest.fail("hopped for an open"))

    app._on_notification_action("open", "s1")

    app._panel.show.assert_called_once_with((1, 2, 3, 4), focus="s1")
    app._panel.toggle.assert_not_called()
    app._daemon.reveal_in_vscode.assert_not_called()


def test_an_unknown_verb_does_nothing():
    """The action identifier comes back from macOS; a name we do not recognise
    must not be guessed at."""
    from dark_army_menubar import app as A

    app = _app()
    app._on_notification_action("rm -rf", "s1")
    app._daemon.reveal_in_vscode.assert_not_called()
    app._daemon.dismiss_notification.assert_not_called()


def test_a_tap_before_the_daemon_is_up_is_ignored():
    app = _app()
    app._daemon = None
    app._on_notification_action("reveal", "s1")   # must not raise


# ── the notifier itself ──────────────────────────────────────────────────────

def test_a_notifier_with_no_centre_is_a_no_op():
    """Every failure mode here — no PyObjC, no framework, no bundle identity,
    a refusal — has to end in silence rather than in a traceback on a path that
    runs for every alert."""
    from dark_army_menubar import notifier

    n = notifier.Notifier.__new__(notifier.Notifier)
    n._ns, n._center, n._authorized, n._on_action = None, None, None, None
    assert n.available is False
    assert n.post(_alert()) is False
    n.start()                                  # must not raise


def test_a_refusal_stops_us_posting():
    from dark_army_menubar import notifier

    n = notifier.Notifier.__new__(notifier.Notifier)
    n._ns, n._center, n._on_action = {}, MagicMock(), None
    n._authorized = False
    assert n.post(_alert()) is False


def test_an_unanswered_prompt_does_not_stop_us_posting():
    """`None` means the user has not answered yet, which is not a no — macOS
    holds the notification and delivers it if they allow."""
    from dark_army_menubar import notifier

    n = notifier.Notifier.__new__(notifier.Notifier)
    n._center, n._on_action, n._authorized = MagicMock(), None, None
    n._ns = {k: MagicMock() for k in
             ("UNMutableNotificationContent", "UNNotificationRequest",
              "UNNotificationSound")}
    assert n.post(_alert()) is True
    n._center.addNotificationRequest_withCompletionHandler_.assert_called_once()


def _posted_content(alert):
    """Post an alert and hand back the content object macOS was given."""
    from dark_army_menubar import notifier

    n = notifier.Notifier.__new__(notifier.Notifier)
    n._center, n._on_action, n._authorized = MagicMock(), None, True
    content = MagicMock()
    ns = {k: MagicMock() for k in
          ("UNMutableNotificationContent", "UNNotificationRequest",
           "UNNotificationSound")}
    ns["UNMutableNotificationContent"].alloc.return_value.init.return_value = content
    n._ns = ns
    n.post(alert)
    return content


def test_a_banner_says_which_agent_and_where():
    """"Vex needs you" does not say *which* Vex when three repos are
    running, and a banner is read in the second it slides past."""
    alert = dict(_alert(), subtitle="dark-army · feat/panel")
    content = _posted_content(alert)
    content.setTitle_.assert_called_once_with("Vex needs you")
    content.setSubtitle_.assert_called_once_with("dark-army · feat/panel")
    content.setBody_.assert_called_once_with("Waiting for your input")


def test_an_empty_body_never_reaches_macos():
    """macOS renders a body-less banner as a bare "Notification" — the app name
    and nothing else, which is what this looked like the first time it ran."""
    content = _posted_content(dict(_alert(), body=""))
    body = content.setBody_.call_args[0][0]
    assert body.strip()


def test_repeat_alerts_from_one_agent_stack_under_it():
    content = _posted_content(_alert(sid="abc"))
    content.setThreadIdentifier_.assert_called_once_with("abc")


def test_the_session_id_rides_in_user_info():
    """Not parsed back out of the request identifier, which is a composite
    (`<sid>:<rule>:<n>`) split on a character session ids do not promise to lack."""
    from dark_army_menubar import notifier

    n = notifier.Notifier.__new__(notifier.Notifier)
    n._center, n._on_action, n._authorized = MagicMock(), None, True
    content = MagicMock()
    ns = {k: MagicMock() for k in
          ("UNMutableNotificationContent", "UNNotificationRequest",
           "UNNotificationSound")}
    ns["UNMutableNotificationContent"].alloc.return_value.init.return_value = content
    n._ns = ns
    n.post(_alert(sid="abc"))
    content.setUserInfo_.assert_called_once_with({"session_id": "abc"})
    content.setCategoryIdentifier_.assert_called_once_with(notifier.CATEGORY_ID)


# ── suppression: the window you are already looking at ───────────────────────

def _suppressible(sid="s1", pid=4242):
    d = _daemon()
    d._session_states = {sid: {"pid": pid, "state": "idle"}}
    d._active_notifications = {sid: {"message": "Waiting for your input"}}
    d._frontmost_pids = set()
    d._frontmost_at = 0.0
    return d


def test_a_session_in_the_terminal_on_screen_is_suppressed():
    import time
    d = _suppressible()
    d._frontmost_pids = {4242}
    d._frontmost_at = time.time()
    assert d._alert_suppressed() == {"s1"}


def test_another_sessions_terminal_being_frontmost_suppresses_nothing():
    import time
    d = _suppressible()
    d._frontmost_pids = {9999}
    d._frontmost_at = time.time()
    assert d._alert_suppressed() == set()


def test_a_stale_reading_suppresses_nothing():
    """A reading that has aged out speaks for a window the user may have left
    minutes ago, and a withheld interruption is the expensive way to be wrong."""
    import time
    from dark_army_daemon import daemon as dmod

    d = _suppressible()
    d._frontmost_pids = {4242}
    d._frontmost_at = time.time() - dmod.FRONTMOST_TRUST_SECONDS - 1
    assert d._alert_suppressed() == set()


def test_only_sessions_an_alert_could_be_about_are_polled_for():
    """The poll costs an HTTP fan-out and a ps walk per pid. `AlertPolicy` fires
    on `waiting` and `crit` only, so a fleet of quietly-working sessions is not
    a question anybody asked."""
    d = _daemon()
    d._session_states = {
        "waiting": {"pid": 11},
        "carded": {"pid": 22},
        "busy": {"pid": 33},
        "pidless": {},
    }
    d._active_notifications = {"carded": {}}
    d._permission_requests = {}
    d._reconciled_categories = lambda: {
        "waiting": "waiting", "carded": "running",
        "busy": "running", "pidless": "waiting",
    }
    assert d._suppression_candidates() == {"waiting": 11, "carded": 22}


def test_a_session_blocked_on_a_permission_prompt_is_polled_too():
    """The prompt may land while the categoriser still says `running` — the
    hook event that flips it to `waiting` races the relay — and its session is
    exactly the one whose window the user may be staring at."""
    d = _daemon()
    d._session_states = {"blocked": {"pid": 44}}
    d._active_notifications = {}
    # The channel the prompt arrived on, still attached. A relayed prompt with
    # no live channel behind it cannot exist and is not what this is about:
    # `_reap_permissions` drops one, because there is no longer anything that
    # could carry the answer back.
    now = time.time()
    d._channels = {51000: {"session_id": "blocked", "pid": 44,
                           "is_channel": True, "last_seen": now}}
    d._permission_requests = {"r1": {"request_id": "r1", "session_id": "blocked",
                                     "port": 51000, "asked_at": now}}
    d._reconciled_categories = lambda: {"blocked": "running"}
    assert d._suppression_candidates() == {"blocked": 44}


# ── the phone leg: the same drain, wake-and-count only ───────────────────────


class _Connector:
    """Records `push_alert` calls; the real one is exercised in
    test_relay_client.py. Awaitable because the daemon `create_task`s it."""

    def __init__(self):
        self.pushed = []

    async def push_alert(self, device_id, body):
        self.pushed.append((device_id, body))
        return True


def _card(sid, title, key="session_id", state_key="link_state", state="live"):
    """One published board card naming a session, in `_board_state`'s shape."""
    return {"id": f"card-{sid}", "title": title, key: sid, state_key: state}


def _pushable(monkeypatch, devices=("phone-1",), tokens=None):
    """A daemon with the three gates open and one token-holding phone.

    The phone leg's two always-on gates are opened too: the Mac-presence
    probe reads `None` (fail-open — or the developer's own keyboard would
    withhold every test buzz) and the card grace is zero (a grace of zero
    holds nothing). The S6 and S7 cases below set their own.

    The phone's Needs you list reads the waiting bucket (`s1`, `s2`) with
    no open prompt and no published card; a case wanting either stubs
    `_prompts_by_session` / `_notification_snapshot` itself, and an alert
    about any other session (`s3`) is unlisted."""
    from dark_army_daemon import buzz_ledger, relay
    from dark_army_daemon import daemon as daemon_module

    monkeypatch.setattr(buzz_ledger, "mac_idle_seconds", lambda: None)
    monkeypatch.setattr(daemon_module, "PUSH_GRACE_SECONDS", 0.0)
    d = _daemon()
    d._undelivered = []
    d._observers = []
    d.phone_push_enabled = True
    d.remote_access_enabled = True
    d._relay_connector = _Connector()
    d._agents_snapshot_cache = {"waiting": [{"session_id": "s1"},
                                            {"session_id": "s2"}]}
    monkeypatch.setattr(d, "_prompts_by_session", lambda: {}, raising=False)
    monkeypatch.setattr(d, "_notification_snapshot", lambda: [], raising=False)
    held = dict(tokens or {did: "ab" * 32 for did in devices})
    monkeypatch.setattr(relay, "channel_ids",
                        lambda: {did: "c" * 32 for did in devices})
    monkeypatch.setattr(relay, "push_token", lambda did: held.get(did))
    return d


async def _settle(d=None):
    """Let the drain's detached work run: the phone leg's task (gathered,
    with its executor hop for the presence reading) and the POST tasks it
    creates. Without a daemon, two loop turns — the old shape."""
    import asyncio

    await asyncio.sleep(0)
    await asyncio.sleep(0)
    tasks = list(getattr(d, "_phone_leg_tasks", None) or ()) if d else []
    if tasks:
        await asyncio.gather(*tasks)
        await asyncio.sleep(0)
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_the_push_fires_with_zero_banner_observers(monkeypatch):
    """A daemon whose menu-bar process is mid-restart is exactly when the
    phone is the only surface left — the old no-observer early return must
    not starve the push leg."""
    d = _pushable(monkeypatch)
    d._observers = [object()]                 # implements nothing
    d._undelivered = [_alert()]
    d._deliver_alerts()
    await _settle(d)
    assert len(d._relay_connector.pushed) == 1
    assert d._undelivered == []


@pytest.mark.asyncio
async def test_the_drain_is_still_exactly_once(monkeypatch):
    d = _pushable(monkeypatch)
    seen = []
    d._observers = [type("Obs", (), {
        "on_alerts": lambda self, a: seen.extend(a)})()]
    d._undelivered = [_alert()]
    d._deliver_alerts()
    d._deliver_alerts()
    await _settle(d)
    assert len(seen) == 1
    assert len(d._relay_connector.pushed) == 1


@pytest.mark.asyncio
async def test_the_push_carries_a_name_and_a_count_never_the_words(monkeypatch):
    """The payload transits Vercel and Apple in plaintext, so the agent's
    question, the project and the branch stay home. With no card and no name
    to add, the payload is exactly the one sent before `work` existed."""
    d = _pushable(monkeypatch)
    alert = dict(_alert(), nickname="Vex",
                 body="Should I drop the production table?",
                 subtitle="secret-project · main")
    d._undelivered = [alert]
    d._deliver_alerts()
    await _settle(d)
    ((_did, body),) = d._relay_connector.pushed
    assert body["title"] == "Vex needs you"
    assert body["badge"] == 2                 # the waiting bucket, same snapshot
    flat = json.dumps(body)
    assert "production table" not in flat
    assert "secret-project" not in flat
    # Plus the subject since 2026-09-20: the alert's own opaque session id
    # (`_compose_push_subject`), no card bound so no `card_id`. Still no words.
    assert set(body) == {"title", "badge", "kind", "session_id"}
    assert body["session_id"] == alert["session_id"]


@pytest.mark.asyncio
async def test_the_work_line_rides_but_the_words_still_do_not(monkeypatch):
    """The 2026-09-06 relaxation: the card's name joins the payload. The
    alert's own question, project and branch still must not."""
    d = _pushable(monkeypatch)
    d._board_state = {"cards": [_card("s1", "Rename the strip ladder")]}
    alert = dict(_alert(), nickname="Vex",
                 body="Should I drop the production table?",
                 subtitle="secret-project · main")
    d._undelivered = [alert]
    d._deliver_alerts()
    await _settle(d)
    ((_did, body),) = d._relay_connector.pushed
    assert body["work"] == "Rename the strip ladder"
    # The bound card's id rides beside the session id as the subject.
    assert set(body) == {"title", "badge", "kind", "work", "session_id", "card_id"}
    assert body["card_id"] == "card-s1"
    flat = json.dumps(body)
    assert "production table" not in flat
    assert "secret-project" not in flat


@pytest.mark.asyncio
async def test_the_kind_word_rides_the_same_drain(monkeypatch):
    """Two agents in one buzz: the words collapse to a count and the kind is
    the most urgent one present — the sound of the one that can least
    wait."""
    d = _pushable(monkeypatch)
    # Question, not finished, beside the permission: a finished alert never
    # reaches the phone since 22 Sep 2026 (S3), so it could not collapse.
    d._undelivered = [dict(_alert(), kind="question"),
                      dict(_alert("s2"), kind="permission")]
    d._deliver_alerts()
    await _settle(d)
    ((_did, body),) = d._relay_connector.pushed
    assert body["title"] == "2 agents need you"
    assert body["kind"] == "permission"


def test_the_compose_helpers_never_read_the_alerts_own_words():
    """The grep half of the rule: a future edit that puts the alert's own
    `title`/`subtitle`/`body` into the push text verbatim leaks through
    Apple. No helper's source may touch those keys — `_compose_push_work`
    included, whose line comes from the board and the agents snapshot, never
    from the alert."""
    import inspect

    for helper in (BobDaemon._compose_push_title, BobDaemon._compose_push_kind,
                   BobDaemon._compose_push_work, BobDaemon._compose_push_need,
                   BobDaemon._compose_push_face):
        source = inspect.getsource(helper)
        for banned in ('"title"', "'title'", '"subtitle"', "'subtitle'",
                       '"body"', "'body'"):
            assert banned not in source, helper.__name__
    for helper in (BobDaemon._compose_push_title, BobDaemon._compose_push_kind,
                   BobDaemon._compose_push_need):
        assert '"kind"' in inspect.getsource(helper)
    assert '"nickname"' in inspect.getsource(BobDaemon._compose_push_title)
    assert '"session_id"' in inspect.getsource(BobDaemon._compose_push_work)
    # The third line reads the two facts the gate stamped for it — and
    # `body`, the command preview or the harness error, is banned above.
    assert '"need"' in inspect.getsource(BobDaemon._compose_push_need)
    assert '"tool"' in inspect.getsource(BobDaemon._compose_push_need)


@pytest.mark.parametrize("alert,expected", [
    (dict(rule="permission", kind="permission", tool="Bash"), "Approve running Bash"),
    (dict(rule="permission", kind="permission", tool=""), "Approve a tool call"),
    (dict(rule="permission", kind="permission"), "Approve a tool call"),
    (dict(rule="card", kind="question", need="Which database?"), "Which database?"),
    (dict(rule="card", kind="question", need=""), "Answer the question it asked"),
    # A relayed AskUserQuestion: a permission *rule* with kind `question`
    # — the question branch must win, or the phone reads "Approve running
    # AskUserQuestion".
    (dict(rule="permission", kind="question", tool="AskUserQuestion",
          need="Ship it or iterate?"), "Ship it or iterate?"),
    (dict(rule="permission", kind="question", tool="AskUserQuestion"),
     "Answer the question it asked"),
    (dict(rule="card", kind="attention", need="Ready for review."), "Ready for review."),
    (dict(rule="waiting", kind="attention", need=""), ""),
    (dict(rule="card", kind="finished", need=""), ""),
    (dict(rule="card", kind="finished", need="Done with the strip."), "Done with the strip."),
    (dict(rule="security", kind="security", need="1.2.3.4 knocked"), ""),
    (dict(rule="card", kind="question", need="  Which   one\n please? "), "Which one please?"),
])
def test_the_need_line_says_what_to_do(alert, expected):
    """The buzz's third line, per kind: a question's own text, the bare
    tool name for an approval, the summary otherwise — never the command."""
    assert BobDaemon._compose_push_need([dict(_alert(), **alert)]) == expected


def test_a_long_need_is_cut_to_a_banners_worth():
    from dark_army_daemon import relay_client

    line = BobDaemon._compose_push_need(
        [dict(_alert(), kind="question", need="x" * 200)])
    assert len(line) == relay_client.PUSH_NEED_CHARS == 120
    assert line.endswith("\u2026")


@pytest.mark.parametrize("kind,character,extra,expected", [
    ("question", "vex", [], "vex"),
    ("permission", "cipher", [], "cipher"),
    ("finished", "ptys", [], "ptys"),
    ("attention", "vex", [], ""),
    ("security", "vex", [], ""),
    ("question", "", [], ""),
    ("question", "mrrobot", [], ""),
    ("question", "../x", [], ""),
    ("question", "vex", ["s2"], ""),
])
def test_the_face_names_one_agent_of_the_three_kinds(kind, character, extra, expected):
    """A question, a permission ask or a finished run from one agent carries
    that agent's slug. Attention, security, an empty character, a name that
    is not on the roster, and two agents at once carry none."""
    pending = [dict(_alert(), kind=kind, character=character)]
    pending.extend(_alert(sid) for sid in extra)
    assert BobDaemon._compose_push_face(pending) == expected


def test_a_collapsed_buzz_says_what_nobody_needs():
    two = [dict(_alert(), kind="permission", rule="permission", tool="Bash"),
           dict(_alert("s2"), kind="question", need="Which database?")]
    assert BobDaemon._compose_push_need(two) == ""
    assert BobDaemon._compose_push_need([]) == ""


@pytest.mark.asyncio
async def test_the_need_line_rides_but_the_command_preview_does_not(monkeypatch):
    """The success criterion, on the wire: agent name, card title and one
    line saying what to do — and neither the command being approved, the
    project nor the branch anywhere in the flat JSON."""
    d = _pushable(monkeypatch)
    d._board_state = {"cards": [_card("s1", "Rename the strip ladder")]}
    alert = dict(_alert(rule="permission"), nickname="Vex", kind="permission",
                 tool="Bash", request_id="req-1",
                 title="Vex wants to run Bash",
                 body='{"command": "rm -rf /"}',
                 subtitle="secret-project · main")
    d._undelivered = [alert]
    d._deliver_alerts()
    await _settle(d)
    ((_did, body),) = d._relay_connector.pushed
    assert body["title"] == "Vex wants to run a tool"
    assert body["work"] == "Rename the strip ladder"
    assert body["need"] == "Approve running Bash"
    assert set(body) == {"title", "badge", "kind", "work", "need", "session_id", "card_id"}
    flat = json.dumps(body)
    assert "rm -rf" not in flat
    assert "req-1" not in flat  # the act leg is per device; the subject carries no rid
    assert "secret-project" not in flat
    assert "main" not in flat


@pytest.mark.asyncio
async def test_a_question_alert_rides_its_own_words(monkeypatch):
    d = _pushable(monkeypatch)
    alert = dict(_alert(), nickname="Vex", kind="question",
                 need="Which database?", body="Waiting for input",
                 subtitle="secret-project · main")
    d._undelivered = [alert]
    d._deliver_alerts()
    await _settle(d)
    ((_did, body),) = d._relay_connector.pushed
    assert body["title"] == "Vex asked you a question"
    assert body["need"] == "Which database?"
    assert set(body) == {"title", "badge", "kind", "need", "session_id"}
    assert "secret-project" not in json.dumps(body)


@pytest.mark.asyncio
async def test_an_attention_with_nothing_to_say_is_todays_buzz(monkeypatch):
    """No `need` key at all: byte-for-byte the payload sent before the
    third line existed, so an older mailbox or phone build keeps working.
    (A finished turn was this case until 22 Sep 2026; it no longer reaches
    the phone at all — `test_a_finished_turn_never_buzzes_the_phone`.)"""
    d = _pushable(monkeypatch)
    d._undelivered = [dict(_alert(), nickname="Vex", kind="attention")]
    d._deliver_alerts()
    await _settle(d)
    ((_did, body),) = d._relay_connector.pushed
    # `session_id` is the subject, not a word: the buzz still names which
    # agent it is about, so the phone can open it from its held picture.
    assert set(body) == {"title", "badge", "kind", "session_id"}


def test_a_permission_prompt_names_the_verb_not_the_tool():
    assert BobDaemon._compose_push_title(
        [dict(_alert(rule="permission"), nickname="Vex", kind="permission",
              title="Vex wants to run Bash")]) \
        == "Vex wants to run a tool"


@pytest.mark.parametrize("kind,expected", [
    ("question", "Vex asked you a question"),
    ("permission", "Vex wants to run a tool"),
    ("finished", "Vex finished"),
    ("attention", "Vex needs you"),
    ("", "Vex needs you"),
    (None, "Vex needs you"),
])
def test_the_title_says_which_kind(kind, expected):
    alert = dict(_alert(), nickname="Vex")
    if kind is None:
        alert.pop("kind", None)
    else:
        alert["kind"] = kind
    assert BobDaemon._compose_push_title([alert]) == expected


def test_the_title_reads_the_kind_not_the_rule():
    """`rule` names the path that raised the alert; only `kind` decides the
    words. A permission-rule alert whose kind says question is a relayed
    AskUserQuestion, and it reads as one."""
    alert = dict(_alert(rule="permission"), nickname="Vex", kind="question")
    assert BobDaemon._compose_push_title([alert]) == "Vex asked you a question"


@pytest.mark.parametrize("kind", ["permission", "question", "attention", "finished"])
def test_a_single_kind_round_trips(kind):
    assert BobDaemon._compose_push_kind([dict(_alert(), kind=kind)]) == kind


@pytest.mark.parametrize("kinds,expected", [
    (["finished", "permission"], "permission"),
    (["finished", "question"], "question"),
    (["finished", "attention"], "attention"),
    (["question", "permission", "finished"], "permission"),
    ([], "attention"),
    ([None], "attention"),
    (["loud"], "attention"),
])
def test_the_most_urgent_kind_present_wins(kinds, expected):
    pending = []
    for i, kind in enumerate(kinds):
        alert = _alert(f"s{i}")
        if kind is not None:
            alert["kind"] = kind
        pending.append(alert)
    assert BobDaemon._compose_push_kind(pending) == expected


def test_several_alerts_are_one_counted_buzz():
    pending = [_alert(), _alert("s2"), _alert("s3")]
    assert BobDaemon._compose_push_title(pending) == "3 agents need you"


def test_a_nameless_alert_is_still_a_sentence():
    assert BobDaemon._compose_push_title(
        [dict(_alert(), nickname="")]) == "An agent needs you"


@pytest.mark.parametrize("gate", ["phone_push", "remote", "connector"])
@pytest.mark.asyncio
async def test_each_gate_alone_stops_the_push(monkeypatch, gate):
    d = _pushable(monkeypatch)
    if gate == "phone_push":
        d.phone_push_enabled = False
    elif gate == "remote":
        d.remote_access_enabled = False
    else:
        d._relay_connector = None
    d._undelivered = [_alert()]
    d._deliver_alerts()
    await _settle(d)
    if gate != "connector":
        assert d._relay_connector.pushed == []


@pytest.mark.asyncio
async def test_the_push_leg_says_what_became_of_it(monkeypatch):
    """The outcome word the buzz ledger records: `sent:<n>` counts the
    phones a POST was created for, and the body of the leg is unchanged."""
    d = _pushable(monkeypatch)
    assert d._push_phone_alerts([_alert()]) == "sent:1"
    await _settle(d)
    assert len(d._relay_connector.pushed) == 1
    assert d._push_phone_alerts([]) == "empty"


@pytest.mark.asyncio
async def test_a_channel_without_a_token_is_skipped(monkeypatch):
    """A paired phone that never allowed notifications (or unregistered on
    forget) holds a channel but no token — no POST is even attempted."""
    d = _pushable(monkeypatch, devices=("with", "without"),
                  tokens={"with": "ab" * 32})
    d._undelivered = [_alert()]
    d._deliver_alerts()
    await _settle(d)
    assert [did for did, _ in d._relay_connector.pushed] == ["with"]


@pytest.mark.asyncio
async def test_a_raising_push_leg_never_takes_the_banner_leg_down(monkeypatch):
    d = _pushable(monkeypatch)
    seen = []
    d._observers = [type("Obs", (), {
        "on_alerts": lambda self, a: seen.extend(a)})()]

    def boom(pending):
        raise RuntimeError("no")

    d._push_phone_alerts = boom
    d._undelivered = [_alert()]
    d._deliver_alerts()                        # must not raise
    assert len(seen) == 1
    assert d._undelivered == []
    await _settle(d)                           # the leg's own raise is caught
    assert len(seen) == 1


# ── the second line: what the agent is working on ────────────────────────────


def _worker(board=None, snapshot=None):
    d = _daemon()
    d._board_state = board or {}
    d._agents_snapshot_cache = snapshot or {}
    return d


def test_the_work_line_is_the_bound_cards_title():
    d = _worker(board={"cards": [_card("s1", "Rename the strip ladder")]})
    assert d._compose_push_work([_alert()]) == "Rename the strip ladder"


def test_a_refinement_link_names_the_card_too():
    """Both link kinds count: Refine binds `refine_session_id`, never
    `session_id`."""
    d = _worker(board={"cards": [_card("s1", "Plan the queue drain",
                                       key="refine_session_id",
                                       state_key="refine_state")]})
    assert d._compose_push_work([_alert()]) == "Plan the queue drain"


def test_no_card_falls_back_to_the_sessions_own_name():
    d = _worker(snapshot={"waiting": [{"session_id": "s1",
                                       "name": "Fix the strip ladder"}]})
    assert d._compose_push_work([_alert()]) == "Fix the strip ladder"


def test_an_unnamed_session_says_nothing():
    """`UNNAMED_SESSION` is the naming ladder's own "no name", not a name."""
    from dark_army_daemon.daemon import UNNAMED_SESSION

    d = _worker(snapshot={"waiting": [{"session_id": "s1",
                                       "name": UNNAMED_SESSION}]})
    assert d._compose_push_work([_alert()]) == ""


def test_a_session_the_snapshot_has_never_heard_of_says_nothing():
    d = _worker(snapshot={"waiting": [{"session_id": "other", "name": "Elsewhere"}]})
    assert d._compose_push_work([_alert()]) == ""


def test_the_card_wins_over_the_sessions_own_name():
    d = _worker(board={"cards": [_card("s1", "Rename the strip ladder")]},
                snapshot={"waiting": [{"session_id": "s1", "name": "Some row name"}]})
    assert d._compose_push_work([_alert()]) == "Rename the strip ladder"


def test_a_collapsed_buzz_names_no_work():
    """Naming one of several agents' work would mislead on a glance."""
    d = _worker(board={"cards": [_card("s1", "Rename the strip ladder"),
                                 _card("s2", "Bake the app icon")]})
    assert d._compose_push_work([_alert(), _alert("s2")]) == ""


def test_an_alert_with_no_session_says_nothing():
    d = _worker(board={"cards": [_card("s1", "Rename the strip ladder")]})
    assert d._compose_push_work([dict(_alert(), session_id="")]) == ""


def test_a_long_card_title_is_cut_to_a_banners_worth():
    from dark_army_daemon import relay_client

    d = _worker(board={"cards": [_card("s1", "x" * 300)]})
    line = d._compose_push_work([_alert()])
    assert len(line) == relay_client.PUSH_WORK_CHARS
    assert line.endswith("\u2026")


def test_the_work_line_is_whitespace_collapsed():
    d = _worker(snapshot={"waiting": [
        {"session_id": "s1", "name": "  Fix\n\n  the   ladder \t"}]})
    assert d._compose_push_work([_alert()]) == "Fix the ladder"


@pytest.mark.asyncio
async def test_a_collapsed_buzz_leaves_the_field_off_the_wire(monkeypatch):
    d = _pushable(monkeypatch)
    d._board_state = {"cards": [_card("s1", "Rename the strip ladder"),
                                _card("s2", "Bake the app icon")]}
    d._undelivered = [_alert(), _alert("s2")]
    d._deliver_alerts()
    await _settle(d)
    ((_did, body),) = d._relay_connector.pushed
    assert body["title"] == "2 agents need you"
    assert "work" not in body


@pytest.mark.asyncio
async def test_nothing_to_say_leaves_the_field_off_the_wire(monkeypatch):
    """Absent, never empty: `push.js` renders `subtitle: ""` as a blank
    second line."""
    d = _pushable(monkeypatch)
    d._board_state = {"cards": []}
    d._undelivered = [_alert()]
    d._deliver_alerts()
    await _settle(d)
    ((_did, body),) = d._relay_connector.pushed
    assert "work" not in body


# ── the phone leg: filtered, gated and held (22 Sep 2026) ────────────────────
#
# `docs/transport-contract.md`, *The phone leg is filtered, gated and held*.
# S3: a finished turn never reaches the phone. S6: no buzz while the Mac was
# touched inside `MAC_PRESENT_SECONDS`, and an unreadable reading sends. S7:
# a card buzz waits `PUSH_GRACE_SECONDS` and is dropped when its card went or
# its session was muted. The banner leg receives the whole batch every time.


def _banner_seen(d):
    seen = []
    d._observers = [type("Obs", (), {
        "on_alerts": lambda self, a: seen.extend(a)})()]
    return seen


def _idle(monkeypatch, seconds):
    from dark_army_daemon import buzz_ledger

    monkeypatch.setattr(buzz_ledger, "mac_idle_seconds", lambda: seconds)


def _grace(monkeypatch, seconds):
    from dark_army_daemon import daemon as daemon_module

    monkeypatch.setattr(daemon_module, "PUSH_GRACE_SECONDS", seconds)


def _outcomes(d, monkeypatch):
    """Record every `_ledger_buzz` call as (alert ids, outcome, kwargs)."""
    calls = []

    def record(pending, outcome, *, banner, idle_seconds=None,
               held_seconds=0.0):
        calls.append(([a.get("id") for a in pending], outcome,
                      {"banner": banner, "idle_seconds": idle_seconds,
                       "held_seconds": held_seconds}))

    monkeypatch.setattr(d, "_ledger_buzz", record)
    return calls


@pytest.mark.asyncio
async def test_a_finished_turn_never_buzzes_the_phone(monkeypatch):
    """S3: no switch — the banner leg still receives it, the phone does
    not, and the ledger says why."""
    d = _pushable(monkeypatch)
    seen = _banner_seen(d)
    calls = _outcomes(d, monkeypatch)
    d._undelivered = [dict(_alert(), nickname="Vex", kind="finished")]
    d._deliver_alerts()
    await _settle(d)
    assert d._relay_connector.pushed == []
    assert [a["kind"] for a in seen] == ["finished"]
    assert calls == [(["s1:card:1"], "withheld:finished",
                      {"banner": True, "idle_seconds": None,
                       "held_seconds": 0.0})]


@pytest.mark.asyncio
async def test_a_finished_report_banner_never_buzzes_the_phone(monkeypatch):
    """The `report` rule's quiet banner is a `finished` kind, so S3 takes it
    exactly as it takes a finished turn: the banner leg receives it, the
    phone does not, and the ledger line says why."""
    d = _pushable(monkeypatch)
    seen = _banner_seen(d)
    calls = _outcomes(d, monkeypatch)
    d._undelivered = [dict(_alert(rule="report"), nickname="Vex",
                           title="Vex finished", kind="finished",
                           severity="info")]
    d._deliver_alerts()
    await _settle(d)
    assert d._relay_connector.pushed == []
    assert [a["rule"] for a in seen] == ["report"]
    assert calls == [(["s1:report:1"], "withheld:finished",
                      {"banner": True, "idle_seconds": None,
                       "held_seconds": 0.0})]


def test_a_finished_banner_arrives_silently():
    """No sound for a finished run; every other kind keeps it."""
    quiet = _posted_content(dict(_alert(rule="report"), kind="finished"))
    quiet.setSound_.assert_not_called()
    loud = _posted_content(dict(_alert(), kind="attention"))
    loud.setSound_.assert_called_once()
    assert _posted_content(_alert()).setSound_.call_count == 1   # no kind


@pytest.mark.asyncio
async def test_a_finished_turn_beside_a_question_leaves_the_question_alone(
        monkeypatch):
    d = _pushable(monkeypatch)
    seen = _banner_seen(d)
    d._undelivered = [dict(_alert(), kind="finished", nickname="Vex"),
                      dict(_alert("s2"), kind="question", nickname="Mobley")]
    d._deliver_alerts()
    await _settle(d)
    ((_did, body),) = d._relay_connector.pushed
    assert body["title"] == "Mobley asked you a question"
    assert len(seen) == 2                     # the banner leg: the whole batch


@pytest.mark.asyncio
@pytest.mark.parametrize("idle, pushes, outcome", [
    (0.0, 0, "withheld:mac_active"),
    (59.0, 0, "withheld:mac_active"),
    (61.0, 1, "sent:1"),
    (None, 1, "sent:1"),                      # unreadable: fail open
])
async def test_the_phone_is_quiet_while_the_mac_is_in_use(
        monkeypatch, idle, pushes, outcome):
    """S6: the reading is taken at the moment of sending; inside the minute
    the buzz is withheld, outside it — or with no reading — it goes."""
    d = _pushable(monkeypatch)
    _idle(monkeypatch, idle)
    calls = _outcomes(d, monkeypatch)
    d._undelivered = [dict(_alert(), kind="question")]
    d._deliver_alerts()
    await _settle(d)
    assert len(d._relay_connector.pushed) == pushes
    ((ids, got, extra),) = calls
    assert ids == ["s1:card:1"] and got == outcome
    assert extra["idle_seconds"] == idle      # the line and the gate agree


@pytest.mark.asyncio
@pytest.mark.parametrize("kind, rule", [
    ("permission", "permission"), ("security", "access_burst"),
    ("attention", "stall"),
])
async def test_the_presence_gate_covers_every_kind(monkeypatch, kind, rule):
    """Decision (a): the person at the Mac sees a permission ask and a
    security row on the Mac; the phone stays quiet for those too."""
    d = _pushable(monkeypatch)
    _idle(monkeypatch, 5.0)
    d._undelivered = [dict(_alert(rule=rule), kind=kind)]
    d._deliver_alerts()
    await _settle(d)
    assert d._relay_connector.pushed == []


@pytest.mark.asyncio
async def test_the_presence_reading_runs_on_the_executor(monkeypatch):
    """Never a blocking `Quartz` call on the loop."""
    import threading

    from dark_army_daemon import buzz_ledger

    d = _pushable(monkeypatch)
    threads = []

    monkeypatch.setattr(buzz_ledger, "mac_idle_seconds",
                        lambda: threads.append(threading.current_thread()))
    d._undelivered = [dict(_alert(), kind="question")]
    d._deliver_alerts()
    await _settle(d)
    assert threads and threads[0] is not threading.main_thread()
    assert len(d._relay_connector.pushed) == 1   # the probe answered None


async def _wait_for(pred, timeout=2.0):
    import asyncio
    import time as _time

    deadline = _time.monotonic() + timeout
    while not pred() and _time.monotonic() < deadline:
        await asyncio.sleep(0.005)


@pytest.mark.asyncio
async def test_a_card_buzz_is_held_and_sent_when_the_card_stands(monkeypatch):
    """S7: nothing is pushed inside the grace; a card still standing at its
    end goes, recorded with how long it was held."""
    import asyncio

    d = _pushable(monkeypatch)
    _grace(monkeypatch, 0.05)
    calls = _outcomes(d, monkeypatch)
    d._active_notifications = {"s1": {"hook": "Stop"}}
    d._undelivered = [dict(_alert(), kind="question")]
    d._deliver_alerts()
    await asyncio.sleep(0.01)
    assert d._relay_connector.pushed == []    # still inside the grace
    await _settle(d)
    assert len(d._relay_connector.pushed) == 1
    ((ids, outcome, extra),) = calls
    assert outcome == "sent:1" and extra["held_seconds"] == 0.05


@pytest.mark.asyncio
async def test_a_card_answered_inside_the_grace_never_buzzes(monkeypatch):
    import asyncio

    d = _pushable(monkeypatch)
    _grace(monkeypatch, 0.05)
    calls = _outcomes(d, monkeypatch)
    d._active_notifications = {"s1": {"hook": "Stop"}}
    d._undelivered = [dict(_alert(), kind="question")]
    d._deliver_alerts()
    await asyncio.sleep(0.01)
    d._active_notifications.pop("s1")         # answered / dismissed
    await _settle(d)
    assert d._relay_connector.pushed == []
    assert calls == [(["s1:card:1"], "dropped:card_gone",
                      {"banner": False, "idle_seconds": None,
                       "held_seconds": 0.05})]


@pytest.mark.asyncio
async def test_a_session_muted_inside_the_grace_never_buzzes(monkeypatch):
    import asyncio
    from dark_army_daemon import alerts as alerting

    d = _pushable(monkeypatch)
    _grace(monkeypatch, 0.05)
    calls = _outcomes(d, monkeypatch)
    policy = alerting.AlertPolicy()
    d.__dict__["_alert_policy"] = policy
    d._active_notifications = {"s1": {"hook": "Stop"}}
    d._undelivered = [dict(_alert(), kind="question")]
    d._deliver_alerts()
    await asyncio.sleep(0.01)
    policy.mute("s1")
    await _settle(d)
    assert d._relay_connector.pushed == []
    assert [c[1] for c in calls] == ["dropped:muted"]


@pytest.mark.asyncio
async def test_a_permission_ask_is_not_held_behind_a_cards_grace(monkeypatch):
    """Two batches in one drain: the permission at once, the card after its
    grace — never the permission waiting on the card."""
    d = _pushable(monkeypatch)
    _grace(monkeypatch, 0.2)
    seen = _banner_seen(d)
    d._active_notifications = {"s1": {"hook": "Stop"}}
    d._undelivered = [dict(_alert(), kind="question"),
                      dict(_alert("s2", rule="permission"), kind="permission",
                           nickname="Mobley", tool="Bash")]
    d._deliver_alerts()
    assert len(seen) == 2                     # the banner leg: at once, whole
    await _wait_for(lambda: len(d._relay_connector.pushed) >= 1, 0.15)
    assert [b["kind"] for _, b in d._relay_connector.pushed] == ["permission"]
    await _settle(d)
    assert [b["kind"] for _, b in d._relay_connector.pushed] == \
        ["permission", "question"]


@pytest.mark.asyncio
async def test_a_security_row_is_not_held(monkeypatch):
    d = _pushable(monkeypatch)
    _grace(monkeypatch, 5.0)                  # would time the test out if held
    d._undelivered = [dict(_alert(sid="", rule="access_burst"),
                           kind="security")]
    d._deliver_alerts()
    await _wait_for(lambda: len(d._relay_connector.pushed) >= 1, 1.0)
    assert [b["kind"] for _, b in d._relay_connector.pushed] == ["security"]


@pytest.mark.asyncio
async def test_a_held_card_that_outlives_the_grace_still_meets_the_mac(
        monkeypatch):
    """Held, still standing, but the person sat down in the meantime."""
    d = _pushable(monkeypatch)
    _grace(monkeypatch, 0.02)
    _idle(monkeypatch, 3.0)
    calls = _outcomes(d, monkeypatch)
    d._active_notifications = {"s1": {"hook": "Stop"}}
    d._undelivered = [dict(_alert(), kind="question")]
    d._deliver_alerts()
    await _settle(d)
    assert d._relay_connector.pushed == []
    ((_ids, outcome, extra),) = calls
    assert outcome == "withheld:mac_active"
    assert extra == {"banner": False, "idle_seconds": 3.0,
                     "held_seconds": 0.02}


@pytest.mark.asyncio
async def test_shutdown_cancels_a_held_buzz_and_it_writes_nothing(monkeypatch):
    import asyncio

    d = _pushable(monkeypatch)
    _grace(monkeypatch, 30.0)
    calls = _outcomes(d, monkeypatch)
    d._active_notifications = {"s1": {"hook": "Stop"}}
    d._undelivered = [dict(_alert(), kind="question")]
    d._deliver_alerts()
    await asyncio.sleep(0)
    (task,) = list(d._phone_leg_tasks)
    await d._cancel_phone_legs()             # the body `_shutdown` runs
    await asyncio.sleep(0)
    assert task.cancelled()
    assert d._phone_leg_tasks == set()        # the done-callback let it go
    assert d._relay_connector.pushed == [] and calls == []


def test_shutdown_sweeps_the_phone_legs_before_the_stores_close():
    """The sweep sits above the `_CLOSED_AT_SHUTDOWN` loop in `_shutdown`."""
    import inspect

    source = inspect.getsource(BobDaemon._shutdown)
    assert source.index("_cancel_phone_legs") < source.index("_CLOSED_AT_SHUTDOWN")


@pytest.mark.asyncio
async def test_a_drain_during_shutdown_leaves_no_orphan_task(monkeypatch):
    import asyncio

    d = _pushable(monkeypatch)
    seen = _banner_seen(d)
    d._shutdown_event = asyncio.Event()
    d._shutdown_event.set()
    d._undelivered = [dict(_alert(), kind="question")]
    d._deliver_alerts()
    assert len(seen) == 1                     # the banner leg is untouched
    assert not getattr(d, "_phone_leg_tasks", set())
    await _settle(d)
    assert d._relay_connector.pushed == []


def test_off_the_loop_the_leg_filters_and_gates_but_holds_nothing(monkeypatch):
    """`_phone_leg_now`: a sync drain still writes its lines."""
    d = _pushable(monkeypatch)
    _grace(monkeypatch, 30.0)
    _idle(monkeypatch, None)
    calls = _outcomes(d, monkeypatch)
    d._undelivered = [dict(_alert(), kind="finished"),
                      dict(_alert("s2"), kind="question")]
    d._deliver_alerts()
    assert [(c[0], c[1], c[2]["held_seconds"]) for c in calls] == [
        (["s1:card:1"], "withheld:finished", 0.0),
        (["s2:card:1"], "no_loop", 0.0),
    ]


# ── the phone leg names only what the phone lists (23 Sep 2026) ──────────────
#
# `docs/transport-contract.md`, *A buzz names only what the phone lists*. The
# phone sweeps every delivered banner once its Needs you list is empty
# (`clearDeliveredIfQuiet`), so a buzz about a session it does not list is an
# appear-then-vanish by construction. `_pushable` lists `s1` and `s2`
# (waiting); `s3` is a busy agent.


def _signal(sid, rule="stall", kind="attention"):
    return dict(_alert(sid, rule=rule), kind=kind, nickname="Vex")


@pytest.mark.asyncio
async def test_a_stall_on_a_busy_agent_never_buzzes_the_phone(monkeypatch):
    d = _pushable(monkeypatch)
    seen = _banner_seen(d)
    calls = _outcomes(d, monkeypatch)
    d._undelivered = [_signal("s3")]
    d._deliver_alerts()
    await _settle(d)
    assert d._relay_connector.pushed == []
    assert [a["session_id"] for a in seen] == ["s3"]   # the banner leg still
    assert calls == [(["s3:stall:1"], "withheld:unlisted",
                      {"banner": True, "idle_seconds": None,
                       "held_seconds": 0.0})]


@pytest.mark.asyncio
async def test_a_stall_on_an_agent_waiting_on_you_still_buzzes(monkeypatch):
    d = _pushable(monkeypatch)
    d._undelivered = [_signal("s1")]
    d._deliver_alerts()
    await _settle(d)
    assert len(d._relay_connector.pushed) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("rule, stub, value", [
    ("ctx-full", "_prompts_by_session", {"s3": {"request_id": "r1"}}),
    ("ctx-full", "_notification_snapshot", [{"session_id": "s3"}]),
    ("permission", "_prompts_by_session", {"s3": {"request_id": "r1"}}),
])
async def test_an_open_prompt_or_a_published_card_lists_a_busy_agent(
        monkeypatch, rule, stub, value):
    """The phone lists a running row with a prompt or a notification row;
    so does the gate. A permission ask is listed by the prompt that raised
    it."""
    d = _pushable(monkeypatch)
    d._agents_snapshot_cache["running"] = [{"session_id": "s3"}]   # busy
    monkeypatch.setattr(d, stub, lambda: value)
    kind = "permission" if rule == "permission" else "attention"
    d._undelivered = [_signal("s3", rule=rule, kind=kind)]
    d._deliver_alerts()
    await _settle(d)
    assert len(d._relay_connector.pushed) == 1


@pytest.mark.asyncio
async def test_the_security_row_is_pushed_not_withheld_unlisted(monkeypatch):
    """No session and never on Needs you, yet it still buzzes (the person's
    decision, 23 Sep 2026): the phone's sweep runs only on a foreground
    snapshot, so the buzz stays until the app is opened, and with Mac
    banners off it is the only interrupt for a door burst."""
    d = _pushable(monkeypatch)
    seen = _banner_seen(d)
    calls = _outcomes(d, monkeypatch)
    d._undelivered = [dict(_alert("", rule="access_burst"), id="security:1",
                           kind="security")]
    d._deliver_alerts()
    await _settle(d)
    ((_did, body),) = d._relay_connector.pushed
    assert body["kind"] == "security"
    assert [a["kind"] for a in seen] == ["security"]
    assert [(ids, got) for ids, got, _ in calls] == [(["security:1"], "sent:1")]


@pytest.mark.asyncio
async def test_the_collapse_counts_only_what_is_sent(monkeypatch):
    d = _pushable(monkeypatch)
    calls = _outcomes(d, monkeypatch)
    d._undelivered = [_signal("s3"), dict(_alert("s1"), nickname="Vex",
                                          kind="question")]
    d._deliver_alerts()
    await _settle(d)
    ((_did, body),) = d._relay_connector.pushed
    assert body["session_id"] == "s1"
    assert "2 agents" not in body["title"]
    assert [(ids, got) for ids, got, _ in calls] == [
        (["s3:stall:1"], "withheld:unlisted"), (["s1:card:1"], "sent:1")]


def test_the_sync_leg_withholds_the_same_way(monkeypatch):
    """`_phone_leg_now` — no running loop — reads the same list."""
    d = _pushable(monkeypatch)
    calls = _outcomes(d, monkeypatch)
    d._undelivered = [_signal("s3"), _signal("s1")]
    d._deliver_alerts()
    assert d._relay_connector.pushed == []
    assert [(ids, got) for ids, got, _ in calls] == [
        (["s3:stall:1"], "withheld:unlisted"), (["s1:stall:1"], "no_loop")]


def test_the_bare_daemons_list_is_its_waiting_bucket_alone():
    """With nothing stubbed, the three inputs are read for real."""
    d = _daemon()
    d._agents_snapshot_cache = {"waiting": [{"session_id": "s1"}],
                                "running": [{"session_id": "s3"}]}
    d._active_notifications = {}
    d._permission_requests = {}
    d._session_states = {}
    assert d._phone_listed_sessions() == {"s1"}


def test_an_unreadable_list_gates_nothing(monkeypatch):
    """S6's rule: a fault never silences the phone."""
    d = _pushable(monkeypatch)
    calls = _outcomes(d, monkeypatch)

    def boom():
        raise RuntimeError("no")

    monkeypatch.setattr(d, "_notification_snapshot", boom)
    assert d._phone_listed_sessions() is None
    d._undelivered = [_signal("s3")]
    d._deliver_alerts()
    assert [(ids, got) for ids, got, _ in calls] == [(["s3:stall:1"], "no_loop")]


def test_a_persisting_fault_warns_once(monkeypatch, caplog):
    import logging

    d = _pushable(monkeypatch)

    def boom():
        raise RuntimeError("no")

    monkeypatch.setattr(d, "_notification_snapshot", boom)
    with caplog.at_level(logging.DEBUG, logger="dark-army"):
        assert d._phone_listed_sessions() is None
        assert d._phone_listed_sessions() is None
    levels = [r.levelno for r in caplog.records
              if "Needs you list unreadable" in r.getMessage()]
    assert levels == [logging.WARNING, logging.DEBUG]


# ── a buzz about an agent whose entry was dismissed stays on the Mac ─────────
#
# `live_activity.shown_sessions`: the phone removes a dismissed entry before
# it counts Needs you, so a reminder about that agent would be swept the
# moment it landed. The gate reads the ack store's own records and the
# published board's cards; `_pushable` lists `s1` and `s2` (waiting).


def _acked(monkeypatch, cards=()):
    from dark_army_daemon import inbox_ack

    d = _pushable(monkeypatch)
    d._inbox_acks = inbox_ack.InboxAckStore()
    d._board_state = {"cards": list(cards)}
    return d


@pytest.mark.asyncio
async def test_a_reminder_about_a_dismissed_agent_is_withheld_acked(monkeypatch):
    d = _acked(monkeypatch)
    d._inbox_acks.ack("s:s1", "waiting", "waiting")
    seen = _banner_seen(d)
    calls = _outcomes(d, monkeypatch)
    d._undelivered = [_signal("s1", rule="waiting")]
    d._deliver_alerts()
    await _settle(d)
    assert d._relay_connector.pushed == []
    assert [a["session_id"] for a in seen] == ["s1"]   # the banner leg still
    assert [(ids, got) for ids, got, _ in calls] == [
        (["s1:waiting:1"], "withheld:unlisted")]


@pytest.mark.asyncio
async def test_a_reminder_about_an_agent_not_dismissed_still_buzzes_acked(monkeypatch):
    d = _acked(monkeypatch)
    d._inbox_acks.ack("s:s1", "waiting", "waiting")
    d._undelivered = [_signal("s2", rule="waiting")]
    d._deliver_alerts()
    await _settle(d)
    ((_did, body),) = d._relay_connector.pushed
    assert body["session_id"] == "s2"


@pytest.mark.asyncio
async def test_a_dismissed_old_question_does_not_hide_a_new_one(monkeypatch):
    from dark_army_daemon import inbox_ack

    d = _acked(monkeypatch)
    d._agents_snapshot_cache["waiting"][0]["questions"] = [{"text": "New?"}]
    d._inbox_acks.ack("s:s1", "question",
                      inbox_ack.fingerprint("question", "Old?"))
    d._undelivered = [_signal("s1", rule="waiting")]
    d._deliver_alerts()
    await _settle(d)
    assert len(d._relay_connector.pushed) == 1


@pytest.mark.asyncio
async def test_a_permission_ask_is_never_hidden_by_a_dismissed_entry(monkeypatch):
    d = _acked(monkeypatch)
    d._inbox_acks.ack("s:s1", "waiting", "waiting")
    monkeypatch.setattr(d, "_prompts_by_session",
                        lambda: {"s1": {"request_id": "r1"}})
    d._undelivered = [_signal("s1", rule="permission", kind="permission")]
    d._deliver_alerts()
    await _settle(d)
    assert len(d._relay_connector.pushed) == 1


@pytest.mark.asyncio
async def test_a_dismissed_agent_whose_card_still_asks_keeps_buzzing(monkeypatch):
    card = {"id": "c1", "needs_you": True, "session_id": "s1"}
    d = _acked(monkeypatch, cards=[card])
    d._inbox_acks.ack("s:s1", "waiting", "waiting")
    d._undelivered = [_signal("s1", rule="waiting")]
    d._deliver_alerts()
    await _settle(d)
    assert len(d._relay_connector.pushed) == 1


@pytest.mark.asyncio
async def test_a_dismissed_agent_and_its_dismissed_card_are_withheld(monkeypatch):
    from dark_army_daemon import inbox_ack

    card = {"id": "c1", "needs_you": True, "session_id": "s1"}
    d = _acked(monkeypatch, cards=[card])
    d._inbox_acks.ack("s:s1", "waiting", "waiting")
    d._inbox_acks.ack("c:c1", "ended_work", inbox_ack.fingerprint("ended_work", ""))
    calls = _outcomes(d, monkeypatch)
    d._undelivered = [_signal("s1", rule="waiting")]
    d._deliver_alerts()
    await _settle(d)
    assert d._relay_connector.pushed == []
    assert [got for _, got, _ in calls] == ["withheld:unlisted"]


@pytest.mark.asyncio
async def test_a_daemon_with_no_ack_store_gates_as_before_acked(monkeypatch):
    """`BobDaemon.__new__` has no `_inbox_acks`: the gate reads no acks and
    must not fall into the fault branch, which would gate nothing."""
    d = _pushable(monkeypatch)
    assert not hasattr(d, "_inbox_acks")
    calls = _outcomes(d, monkeypatch)
    d._undelivered = [_signal("s3"), _signal("s1")]
    d._deliver_alerts()
    await _settle(d)
    ((_did, body),) = d._relay_connector.pushed
    assert body["session_id"] == "s1"
    assert [(ids, got) for ids, got, _ in calls] == [
        (["s3:stall:1"], "withheld:unlisted"), (["s1:stall:1"], "sent:1")]


def test_the_sync_leg_withholds_an_acked_agent_the_same_way(monkeypatch):
    d = _acked(monkeypatch)
    d._inbox_acks.ack("s:s1", "waiting", "waiting")
    calls = _outcomes(d, monkeypatch)
    d._undelivered = [_signal("s1", rule="waiting"), _signal("s2", rule="waiting")]
    d._deliver_alerts()
    assert d._relay_connector.pushed == []
    assert [(ids, got) for ids, got, _ in calls] == [
        (["s1:waiting:1"], "withheld:unlisted"), (["s2:waiting:1"], "no_loop")]


def test_the_gate_reads_the_store_and_never_prunes_it_acked(monkeypatch):
    """`records()` is the read; `inbox_snapshot()`'s prune is a write the
    leg must not own, so an ack about a subject gone from the list stays."""
    d = _acked(monkeypatch)
    d._inbox_acks.ack("s:s1", "waiting", "waiting")
    d._inbox_acks.ack("s:gone", "waiting", "waiting")
    assert d._phone_listed_sessions() == {"s2"}
    assert {r["key"] for r in d._inbox_acks.records()} == {"s:s1", "s:gone"}


# ── a work report waits for a frontmost reading taken after it landed ────────

def _reporter_daemon(monkeypatch, frontmost):
    """A daemon with one sleeping reporter (`s1`, pid 4242) and the real
    candidate / frontmost / hold path; `frontmost` is what VS Code answers."""
    from dark_army_daemon import daemon as daemon_module
    from dark_army_daemon import vscode_reveal

    async def answer(pids):
        return set(frontmost) & set(pids)

    monkeypatch.setattr(vscode_reveal, "frontmost_session_pids", answer)
    monkeypatch.setattr(daemon_module, "FRONTMOST_POLL_SECONDS", 0.0)
    d = _daemon()
    d._session_states = {"s1": {"pid": 4242, "state": "idle"}}
    d._active_notifications = {}
    d._permission_requests = {}
    d._channels = {}
    d._frontmost_pids = set()
    d._frontmost_at = 0.0
    d._frontmost_asked = set()
    d._report_candidates = set()
    d._reconciled_categories = lambda: {"s1": "sleeping"}
    return d


def _reporter_snapshot(quiet):
    from dark_army_daemon import work_report
    report = ("## Work done\n**Changed:** the notifier is quiet.\n"
              "**Verified:** the suite.\n**Unchecked:** Nothing - every check above ran.\n")
    entry = {"session_id": "s1", "nickname": "Vex", "project": "repo",
             "signals": [], "last_report": report, "idle_seconds": 3,
             "quiet_since": quiet, "work_report": work_report.parse(report)}
    return {"running": [], "waiting": [], "sleeping": [entry], "finished": []}


def _tick(d, policy, snap):
    """One evaluation exactly as `_enrich_agent_stubs` runs it."""
    raised = policy.evaluate(snap, {}, time.time(),
                             suppressed=d._alert_suppressed(snap),
                             prompts={}, panel_focused=(),
                             report_hold=d._report_hold(policy, snap))
    d._update_report_candidates(policy, snap)
    return raised


async def _one_poll(d):
    """One pass of the real `_frontmost_checker`; the push it schedules for
    a held report is where the loop stops."""
    pushed = []

    def push():
        pushed.append(True)
        d._running = False

    d._schedule_agents_push = push
    d._running = True
    await d._frontmost_checker()
    return pushed


@pytest.mark.asyncio
async def test_a_frontmost_reporter_is_stamped_delivered_and_never_banners(monkeypatch):
    from dark_army_daemon.alerts import AlertPolicy
    d = _reporter_daemon(monkeypatch, frontmost={4242})
    policy = AlertPolicy()
    d._frontmost_at = time.time() - 60          # a reading from before the Stop
    snap = _reporter_snapshot(quiet=time.time() - 3)
    # The first tick after the Stop waits: no reading has asked about s1.
    assert _tick(d, policy, snap) == []
    assert d._report_candidates == {"s1"}
    assert d._suppression_candidates() == {"s1": 4242}
    # The poll asks VS Code about it, and asks for the evaluation at once.
    assert await _one_poll(d) == [True]
    assert d._frontmost_asked == {"s1"} and d._frontmost_pids == {4242}
    # You are looking at it: stamped delivered, no banner, now or later.
    assert _tick(d, policy, snap) == []
    assert ("s1", "report") in policy._fired
    assert d._report_candidates == set()
    d._frontmost_pids = set()
    assert _tick(d, policy, snap) == []


@pytest.mark.asyncio
async def test_a_reporter_nobody_is_looking_at_banners_once_after_the_reading(monkeypatch):
    from dark_army_daemon.alerts import AlertPolicy
    d = _reporter_daemon(monkeypatch, frontmost=set())
    policy = AlertPolicy()
    snap = _reporter_snapshot(quiet=time.time() - 3)
    assert _tick(d, policy, snap) == []          # held for a fresh reading
    await _one_poll(d)
    out = _tick(d, policy, snap)
    assert [a.rule for a in out] == ["report"]
    assert _tick(d, policy, snap) == []


def test_the_hold_fails_open_when_no_reading_comes(monkeypatch):
    from dark_army_daemon import daemon as daemon_module
    from dark_army_daemon.alerts import AlertPolicy
    d = _reporter_daemon(monkeypatch, frontmost=set())
    policy = AlertPolicy()
    old = time.time() - daemon_module.REPORT_FRONTMOST_WAIT_SECONDS - 1
    snap = _reporter_snapshot(quiet=old)
    assert d._report_hold(policy, snap) == set()
    assert [a.rule for a in _tick(d, policy, snap)] == ["report"]


def test_a_pidless_reporter_is_not_held(monkeypatch):
    from dark_army_daemon.alerts import AlertPolicy
    d = _reporter_daemon(monkeypatch, frontmost=set())
    d._session_states = {"s1": {"state": "idle"}}
    snap = _reporter_snapshot(quiet=time.time())
    assert d._report_hold(AlertPolicy(), snap) == set()


# ── a review run waiting on picks: the buzz and its wire ─────────────────────

def _picks_alert(sid="", **over):
    return dict({"id": "review:r-1:picks:1", "session_id": sid,
                 "nickname": "repo", "title": "repo is waiting on your picks",
                 "body": "2 findings — pick the fixes", "rule": "review_picks",
                 "severity": "warn", "kind": "picks", "actions": [],
                 "need": "Pick the fixes: 2 findings"}, **over)


@pytest.mark.asyncio
async def test_a_picks_alert_with_no_session_is_not_withheld_unlisted(monkeypatch):
    """Exempt from the unlisted rung by kind, as `security` is: its listing
    is the run's own `picks` state, not a session the phone's list shows."""
    d = _pushable(monkeypatch)
    calls = _outcomes(d, monkeypatch)
    d._undelivered = [_picks_alert("")]
    d._deliver_alerts()
    await _settle(d)
    ((_did, body),) = d._relay_connector.pushed
    assert body["kind"] == "picks" and body["title"] == "repo is waiting on your picks"
    assert [(ids, got) for ids, got, _ in calls] == [
        (["review:r-1:picks:1"], "sent:1")]


@pytest.mark.asyncio
async def test_a_picks_alert_on_an_unlisted_bound_session_is_still_sent(monkeypatch):
    d = _pushable(monkeypatch)
    d._undelivered = [_picks_alert("s3")]          # `s3` is not on the list
    d._deliver_alerts()
    await _settle(d)
    assert len(d._relay_connector.pushed) == 1


@pytest.mark.asyncio
async def test_a_picks_alert_is_not_held_for_the_card_grace(monkeypatch):
    from dark_army_daemon import daemon as daemon_module

    d = _pushable(monkeypatch)
    monkeypatch.setattr(daemon_module, "PUSH_GRACE_SECONDS", 3600.0)
    calls = _outcomes(d, monkeypatch)
    d._undelivered = [_picks_alert("s1")]
    d._deliver_alerts()
    await _settle(d)
    assert len(d._relay_connector.pushed) == 1
    assert [got for _, got, _ in calls] == ["sent:1"]


@pytest.mark.asyncio
async def test_a_picks_alert_is_still_withheld_while_the_mac_is_in_use(monkeypatch):
    d = _pushable(monkeypatch)
    _idle(monkeypatch, 5.0)
    calls = _outcomes(d, monkeypatch)
    d._undelivered = [_picks_alert("s1")]
    d._deliver_alerts()
    await _settle(d)
    assert d._relay_connector.pushed == []
    assert [got for _, got, _ in calls] == ["withheld:mac_active"]


def test_the_picks_buzz_is_composed_from_its_own_closed_fields():
    alert = _picks_alert("s1", nickname="Vex")
    assert BobDaemon._compose_push_title([alert]) == "Vex is waiting on your picks"
    assert BobDaemon._compose_push_kind([alert]) == "picks"
    assert BobDaemon._compose_push_need([alert]) == "Pick the fixes: 2 findings"
    assert BobDaemon._compose_push_act([alert]) == {}


def test_picks_ranks_below_a_question_and_above_attention():
    q, p, a = (dict(_alert(), kind=k) for k in ("question", "picks", "attention"))
    assert BobDaemon._compose_push_kind([a, p]) == "picks"
    assert BobDaemon._compose_push_kind([a, p, q]) == "question"


@pytest.mark.asyncio
async def test_the_picks_buzz_carries_no_finding_text(monkeypatch):
    d = _pushable(monkeypatch)
    alert = _picks_alert("s1", body="leaks the api token in utils.py",
                         subtitle="secret-project")
    d._undelivered = [alert]
    d._deliver_alerts()
    await _settle(d)
    ((_did, body),) = d._relay_connector.pushed
    flat = json.dumps(body)
    assert "token" not in flat and "utils.py" not in flat
    assert "secret-project" not in flat
    assert body["need"] == "Pick the fixes: 2 findings"


@pytest.fixture
def _relay_store(tmp_path, monkeypatch):
    from dark_army_daemon import devices, paths, relay

    monkeypatch.setattr(paths, "RELAY_PATH", tmp_path / "relay.json")
    monkeypatch.setattr(paths, "DEVICES_PATH", tmp_path / "devices.json")
    monkeypatch.setattr(paths, "STATE_DIR", tmp_path / "state")
    relay.reset()
    devices.reset()
    yield
    relay.reset()
    devices.reset()


@pytest.mark.asyncio
async def test_a_review_run_entering_picks_buzzes_once_and_the_live_card_names_it(
        tmp_path, monkeypatch, _relay_store):
    """(success criterion) A paired phone, the Mac idle, a published run
    moving reviewing -> picks -> fixing over three snapshots: one buzz
    ("<who> is waiting on your picks", kind picks), one live-card update
    naming the run, then the card comes down once the run reads `fixing`."""
    import asyncio

    from dark_army_daemon import alerts as alerting
    from dark_army_daemon import buzz_ledger, relay
    from tests.test_live_activity import _connector

    monkeypatch.setattr(BobDaemon, "_schedule_agents_push", lambda self: None)
    monkeypatch.setattr(buzz_ledger, "mac_idle_seconds", lambda: None)
    conn, d, calls = _connector(tmp_path)
    relay.note_push_token("dev-1", "ab" * 32, "prod")
    relay.note_activity_token("dev-1", "cd" * 32, "prod")
    d.phone_push_enabled = True
    d.remote_access_enabled = True
    d._relay_connector = conn
    d._observers = []
    d._board_state = {"cards": []}
    policy = alerting.AlertPolicy()
    row = {"session_id": "s1", "nickname": "Vex", "project": "repo",
           "idle_seconds": 0.0, "signals": []}

    async def settle():
        for _ in range(6):
            await asyncio.sleep(0)
        tasks = list(getattr(d, "_phone_leg_tasks", None) or ())
        if tasks:
            await asyncio.gather(*tasks)
        for _ in range(6):
            await asyncio.sleep(0)

    def tick(state, now):
        d._review_published = [{
            "id": "r-1", "state": state, "session_id": "s1", "project": "repo",
            "findings_at": 1_700_000_000.0,
            "findings": [{"index": 1, "line": "leaks the api token"},
                         {"index": 2, "line": "drops a table"}]}]
        snapshot = {"running": [dict(row)], "waiting": [], "sleeping": [],
                    "finished": []}
        d._agents_snapshot_cache = snapshot
        raised = policy.evaluate(snapshot, {}, now,
                                 review_runs=d._published_review_runs())
        d._undelivered.extend(a.as_dict() for a in raised)
        d._deliver_alerts()
        return snapshot

    d._undelivered = []
    for state, now in (("reviewing", 100.0), ("picks", 110.0), ("picks", 120.0),
                       ("fixing", 130.0)):
        snapshot = tick(state, now)
        await settle()
        if state == "reviewing":
            continue                    # no card up: the phone has none yet
        if now == 110.0:
            # The phone starts its own card for the run and registers the
            # activity's token, which clears the Mac's "ended" mark.
            d.forget_live_activity("dev-1")
        d._push_live_activity(snapshot)
        await settle()

    buzzes = [c[2] for c in calls if "title" in c[2]]
    cards = [c[2] for c in calls if "event" in c[2]]
    assert len(buzzes) == 1
    assert buzzes[0]["kind"] == "picks"
    assert buzzes[0]["title"] == "Vex is waiting on your picks"
    assert "token" not in json.dumps(buzzes[0])
    assert [c["event"] for c in cards] == ["update", "end"]
    assert cards[0]["kind"] == "picks" and cards[0]["run_id"] == "r-1"
    assert cards[0]["session_id"] == "s1" and cards[0]["nickname"] == "Vex"
    assert "token" not in json.dumps(cards[0])
