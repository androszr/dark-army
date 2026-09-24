"""Dark Army compacting a full session instead of interrupting about it."""
import pytest

from dark_army_daemon import alerts, autocompact


def ctx_signal(rule="ctx-full", severity="crit",
               text="context 91%, full in ~5m — /compact or wrap up"):
    return {"rule": rule, "severity": severity, "text": text, "action": "reveal"}


def test_a_full_reachable_session_is_compacted_not_announced():
    policy = autocompact.AutoCompactPolicy()
    signals = [ctx_signal()]
    assert policy.consider("s1", signals, 91.0, True, 0.0) == autocompact.SEND


def test_a_full_unreachable_session_is_left_to_the_banner():
    policy = autocompact.AutoCompactPolicy()
    assert policy.consider("s1", [ctx_signal()], 91.0, False, 0.0) == autocompact.PASS
    # And no state was kept: nothing to expire, nothing to hand back.
    assert not policy.pending("s1")


def test_the_command_is_a_bare_slash_compact():
    # Anything appended becomes the summary's instructions, which is a decision
    # a timer must not make on somebody's behalf. Same spelling on both
    # clients — Grok's built-in is `/compact` too.
    assert autocompact.COMMAND == "/compact"
    assert autocompact.command_for("claude") == "/compact"
    assert autocompact.command_for("grok") == "/compact"


def test_a_second_tick_holds_rather_than_sending_again():
    policy = autocompact.AutoCompactPolicy()
    policy.consider("s1", [ctx_signal()], 91.0, True, 0.0)
    assert policy.consider("s1", [ctx_signal()], 91.0, True, 5.0) == autocompact.HOLD
    assert policy.consider("s1", [ctx_signal()], 91.0, True, 60.0) == autocompact.HOLD


def test_a_compact_that_did_nothing_hands_the_session_back():
    policy = autocompact.AutoCompactPolicy()
    policy.consider("s1", [ctx_signal()], 91.0, True, 0.0)
    later = autocompact.SETTLE_SECONDS + 1
    assert policy.consider("s1", [ctx_signal()], 91.0, True, later) == autocompact.PASS
    # And stays handed back — no second push at a session that ignored the first.
    assert policy.consider("s1", [ctx_signal()], 91.0, True,
                           later + 600) == autocompact.PASS


def test_a_failed_push_re_arms_the_banner_immediately():
    policy = autocompact.AutoCompactPolicy()
    policy.consider("s1", [ctx_signal()], 91.0, True, 0.0)
    policy.note_failed("s1")
    assert policy.consider("s1", [ctx_signal()], 91.0, True, 5.0) == autocompact.PASS


def test_the_warning_going_away_closes_the_episode():
    policy = autocompact.AutoCompactPolicy()
    policy.consider("s1", [ctx_signal()], 91.0, True, 0.0)
    # First quiet tick starts the clear clock; the episode is still live.
    assert policy.consider("s1", [], 12.0, True, 5.0) == autocompact.PASS
    assert policy.pending("s1")
    # Only a sustained gap pops it, so a later refill is a new attempt.
    later = 5.0 + autocompact.CLEAR_SECONDS
    assert policy.consider("s1", [], 12.0, True, later) == autocompact.PASS
    assert not policy.pending("s1")
    assert policy.consider("s1", [ctx_signal()], 91.0, True,
                           later + 1) == autocompact.SEND


def test_a_one_tick_flicker_does_not_open_a_new_episode():
    """The live failure: a ctx signal blinks off for one statusline and the
    next tick was a fresh SEND, forever, so `failed` was never reached."""
    policy = autocompact.AutoCompactPolicy()
    assert policy.consider("s1", [ctx_signal()], 91.0, True, 0.0) == autocompact.SEND
    assert policy.consider("s1", [], 91.0, True, 2.0) == autocompact.PASS
    assert policy.consider("s1", [ctx_signal()], 91.0, True,
                           4.0) == autocompact.HOLD
    # And settle still counts from the original send.
    later = autocompact.SETTLE_SECONDS + 1
    assert policy.consider("s1", [ctx_signal()], 91.0, True, later) == autocompact.PASS
    assert not policy.pending("s1")


def test_an_absence_longer_than_clear_seconds_is_a_new_episode():
    policy = autocompact.AutoCompactPolicy()
    policy.consider("s1", [ctx_signal()], 91.0, True, 0.0)
    policy.consider("s1", [], 12.0, True, 1.0)
    gone = 1.0 + autocompact.CLEAR_SECONDS + 1
    policy.consider("s1", [], 12.0, True, gone)
    assert policy.consider("s1", [ctx_signal()], 91.0, True,
                           gone + 1) == autocompact.SEND


def test_a_failed_episode_survives_the_warning_flapping():
    # A session that ignored the push must not win a second one by having its
    # signal blink out for one evaluation.
    policy = autocompact.AutoCompactPolicy()
    policy.consider("s1", [ctx_signal()], 91.0, True, 0.0)
    policy.note_failed("s1")
    policy.consider("s1", [], 91.0, True, 10.0)
    assert policy.consider("s1", [ctx_signal()], 91.0, True, 20.0) == autocompact.PASS


def test_runway_only_qualifies_when_it_is_critical():
    policy = autocompact.AutoCompactPolicy()
    soon = ctx_signal("ctx-runway", "warn",
                      "context 62% — full in ~18m, /compact soon")
    assert policy.consider("s1", [soon], 62.0, True, 0.0) == autocompact.PASS
    now_ = ctx_signal("ctx-runway", "crit",
                      "context 78% — full in ~4m, /compact soon")
    assert policy.consider("s1", [now_], 78.0, True, 0.0) == autocompact.SEND


def test_other_rules_are_none_of_its_business():
    policy = autocompact.AutoCompactPolicy()
    stall = ctx_signal("stall", "crit", "no activity for 12m — check the terminal")
    assert policy.consider("s1", [stall], 30.0, True, 0.0) == autocompact.PASS


def test_the_switch_being_off_leaves_everything_as_it_was():
    policy = autocompact.AutoCompactPolicy(enabled=False)
    assert policy.consider("s1", [ctx_signal()], 91.0, True, 0.0) == autocompact.PASS


def test_forget_drops_sessions_that_are_gone():
    policy = autocompact.AutoCompactPolicy()
    policy.consider("s1", [ctx_signal()], 91.0, True, 0.0)
    policy.forget({"s2"})
    assert not policy.pending("s1")


# ── the mark on the row ──────────────────────────────────────────────────────

def test_mark_replaces_the_advice_with_who_is_handling_it():
    signals = [ctx_signal()]
    autocompact.mark(signals)
    assert signals[0]["handled"] == "compact"
    assert signals[0]["text"] == "context 91%, full in ~5m — Dark Army is compacting it"


def test_mark_leaves_unrelated_signals_alone():
    signals = [ctx_signal("stall", "crit", "no activity for 12m — check the terminal")]
    autocompact.mark(signals)
    assert "handled" not in signals[0]


def test_a_handled_signal_does_not_interrupt():
    policy = alerts.AlertPolicy()
    signal = ctx_signal()
    autocompact.mark([signal])
    snapshot = {"running": [{"session_id": "s1", "nickname": "Gil",
                             "project": "dark-army", "signals": [signal]}]}
    assert policy.evaluate(snapshot, {}, 0.0) == []


def test_the_same_signal_unhandled_does_interrupt():
    policy = alerts.AlertPolicy()
    snapshot = {"running": [{"session_id": "s1", "nickname": "Gil",
                             "project": "dark-army",
                             "signals": [ctx_signal()]}]}
    raised = policy.evaluate(snapshot, {}, 0.0)
    assert [a.rule for a in raised] == ["ctx-full"]


@pytest.mark.parametrize("rule,severity,expected", [
    ("ctx-full", "crit", True),
    ("ctx-full", "warn", True),        # full but climbing slowly is still full
    ("ctx-runway", "crit", True),
    ("ctx-runway", "warn", False),
    ("churn", "crit", False),
])
def test_qualifies(rule, severity, expected):
    assert autocompact.qualifies([ctx_signal(rule, severity)]) is expected


# ── the daemon end of it ─────────────────────────────────────────────────────

def _snapshot(signals, provider="claude", sid="s1", pid=4242):
    return {"running": [{"session_id": sid, "provider": provider, "pid": pid,
                         "metrics": {"ctx_used_pct": 91.0},
                         "signals": signals}],
            "finished": []}


def _daemon_reachable(monkeypatch, reachable=True):
    from dark_army_daemon.daemon import BobDaemon
    from dark_army_daemon import vscode_reveal as vr

    monkeypatch.setattr(vr, "can_send_text", lambda pid, tty="": reachable)
    daemon = BobDaemon(headless=True)
    daemon._session_states["s1"] = {"pid": 4242, "last_event": 0}
    return daemon


def test_the_daemon_queues_a_compact_and_marks_the_row(monkeypatch):
    daemon = _daemon_reachable(monkeypatch)
    signals = [ctx_signal()]
    daemon._decide_auto_compacts(_snapshot(signals))
    assert daemon._compact_queue == [("s1", 4242, "claude")]
    assert signals[0]["handled"] == "compact"


def test_the_daemon_leaves_an_unreachable_session_to_the_banner(monkeypatch):
    daemon = _daemon_reachable(monkeypatch, reachable=False)
    signals = [ctx_signal()]
    daemon._decide_auto_compacts(_snapshot(signals))
    assert daemon._compact_queue == []
    assert "handled" not in signals[0]


def test_a_reachable_grok_session_is_auto_compacted(monkeypatch):
    """Grok's client expands `/compact` on the input line the same way
    Claude Code does; the exclusion was the channel, which neither can use."""
    daemon = _daemon_reachable(monkeypatch)
    daemon._session_states["s1"]["provider"] = "grok"
    signals = [ctx_signal()]
    daemon._decide_auto_compacts(_snapshot(signals, provider="grok"))
    assert daemon._compact_queue == [("s1", 4242, "grok")]
    assert signals[0]["handled"] == "compact"


def test_the_daemon_types_the_command_into_the_terminal(monkeypatch):
    import asyncio

    daemon = _daemon_reachable(monkeypatch)
    sent = []

    async def _send(pid, tty, text, newline=True):
        sent.append((pid, text))
        return {"matched": True, "sent": True, "terminalName": "zsh"}

    monkeypatch.setattr(
        "dark_army_daemon.vscode_reveal.send_text", _send)
    daemon._decide_auto_compacts(_snapshot([ctx_signal()]))
    asyncio.run(daemon._flush_auto_compacts())
    # Ctrl-U first: the input line may hold a half-typed draft, and
    # appending to it would submit something the user never wrote.
    assert sent == [(4242, "\x15/compact")]
    assert daemon._compact_queue == []


def test_a_grok_flush_sends_the_same_slash_command(monkeypatch):
    import asyncio

    daemon = _daemon_reachable(monkeypatch)
    sent = []

    async def _send(pid, tty, text, newline=True):
        sent.append((pid, text))
        return {"matched": True, "sent": True}

    monkeypatch.setattr(
        "dark_army_daemon.vscode_reveal.send_text", _send)
    daemon._decide_auto_compacts(_snapshot([ctx_signal()], provider="grok"))
    asyncio.run(daemon._flush_auto_compacts())
    # Ctrl-U first: the input line may hold a half-typed draft, and
    # appending to it would submit something the user never wrote.
    assert sent == [(4242, "\x15/compact")]


def test_a_push_that_does_not_land_re_arms_the_banner_next_tick(monkeypatch):
    import asyncio

    daemon = _daemon_reachable(monkeypatch)

    async def _send(pid, tty, text, newline=True):
        return None

    monkeypatch.setattr(
        "dark_army_daemon.vscode_reveal.send_text", _send)
    daemon._decide_auto_compacts(_snapshot([ctx_signal()]))
    asyncio.run(daemon._flush_auto_compacts())
    signals = [ctx_signal()]
    daemon._decide_auto_compacts(_snapshot(signals))
    assert daemon._compact_queue == []
    assert "handled" not in signals[0]


def test_the_switch_off_stops_the_daemon_deciding_anything(monkeypatch):
    daemon = _daemon_reachable(monkeypatch)
    daemon.auto_compact_enabled = False
    signals = [ctx_signal()]
    daemon._decide_auto_compacts(_snapshot(signals))
    assert daemon._compact_queue == []
    assert "handled" not in signals[0]


def test_the_flush_does_not_import_the_channel(monkeypatch):
    """A slash command on the channel is how this feature silently failed."""
    import inspect
    from dark_army_daemon.daemon import BobDaemon

    src = inspect.getsource(BobDaemon._flush_auto_compacts)
    assert "push_channel_event" not in src
    assert "channel_server" not in src
    assert "send_text" in src


# ── the input line belongs to whoever is typing on it ────────────────────────

def test_a_session_on_a_permission_prompt_is_not_compacted(monkeypatch):
    """`sendText` ends in a newline, and a tool-approval dialog reads a
    newline as "confirm the highlighted choice". Dark Army would be approving a tool
    call nobody read, from a feature switched on to save context. Re-checked at
    flush rather than at decide: the queue drains a tick later, and a prompt
    can land in between."""
    import asyncio

    daemon = _daemon_reachable(monkeypatch)
    sent = []

    async def _send(pid, tty, text, newline=True):
        sent.append((pid, text))
        return {"matched": True, "sent": True}

    monkeypatch.setattr(
        "dark_army_daemon.vscode_reveal.send_text", _send)
    daemon._decide_auto_compacts(_snapshot([ctx_signal()]))
    monkeypatch.setattr(daemon, "_prompts_by_session", lambda: {"s1": {}})
    asyncio.run(daemon._flush_auto_compacts())
    assert sent == []
    # And handed back, so the human gets the banner instead of silence.
    assert daemon.__dict__["_autocompact"].pending("s1") is False


def test_a_live_session_parked_in_finished_keeps_its_episode(monkeypatch):
    """FINISHED_IDLE_GRACE_SECONDS (120s) is shorter than SETTLE_SECONDS
    (180s), and compacting is quiet — which is exactly what being promoted to
    the finished bucket looks like. Dropping the episode there meant a second
    `/compact` on the next tick."""
    daemon = _daemon_reachable(monkeypatch)
    daemon._decide_auto_compacts(_snapshot([ctx_signal()]))
    assert daemon.__dict__["_autocompact"].pending("s1")

    quiet = {"running": [],
             "finished": [{"session_id": "s1", "alive": True, "signals": []}]}
    daemon._decide_auto_compacts(quiet)
    assert daemon.__dict__["_autocompact"].pending("s1")


def test_a_genuinely_finished_session_is_forgotten(monkeypatch):
    """The leak this skip was written for is still closed: a row that really
    ended keeps nothing."""
    daemon = _daemon_reachable(monkeypatch)
    daemon._decide_auto_compacts(_snapshot([ctx_signal()]))
    gone = {"running": [],
            "finished": [{"session_id": "s1", "alive": False, "signals": []}]}
    daemon._decide_auto_compacts(gone)
    assert not daemon.__dict__["_autocompact"].pending("s1")


@pytest.mark.parametrize('identity,pid', [('unique_cwd', 4242), ('explicit_resume', 4242), ('app', 4242), ('ide', None)])
def test_codex_never_probed_or_marked_even_with_old_episode(monkeypatch, identity, pid):
    daemon = _daemon_reachable(monkeypatch)
    daemon._autocompact = autocompact.AutoCompactPolicy()
    daemon._autocompact.consider('s1', [ctx_signal()], 91, True, 0)
    probes = []
    monkeypatch.setattr('dark_army_daemon.vscode_reveal.can_send_text', lambda pid: probes.append(pid) or True)
    snap = _snapshot([ctx_signal()], provider='codex', pid=pid)
    snap['running'][0].update(can_type=False, match_kind=identity)
    daemon._decide_auto_compacts(snap)
    assert probes == [] and daemon._compact_queue == []
    assert 's1' not in daemon._autocompact._state
    assert 'handled' not in snap['running'][0]['signals'][0]
    assert alerts.AlertPolicy().evaluate(snap, {}, 0)


@pytest.mark.parametrize('queue', ['s1', ('s1', 4242), ('s1', 4242, 'claude'), ('s1', 4242, 'codex')])
@pytest.mark.parametrize('lookup', [None, 'claude', 'codex'])
def test_stale_codex_compact_queue_never_sends_and_rearms(monkeypatch, queue, lookup):
    import asyncio
    daemon = _daemon_reachable(monkeypatch)
    daemon._codex_records['s1'] = object()  # stronger than stale provider metadata
    daemon._compact_queue = [queue]
    daemon._autocompact = autocompact.AutoCompactPolicy()
    daemon._autocompact.consider('s1', [ctx_signal()], 91, True, 0)
    monkeypatch.setattr(daemon, '_session_provider', lambda sid: lookup)
    sends, refreshes = [], []
    async def send(*args, **kwargs):
        sends.append(args)
    monkeypatch.setattr('dark_army_daemon.vscode_reveal.send_text', send)
    monkeypatch.setattr(daemon, '_schedule_agents_push', lambda: refreshes.append(True))
    asyncio.run(daemon._flush_auto_compacts())
    assert sends == [] and refreshes
    assert not daemon._autocompact.pending('s1')
    snap = _snapshot([ctx_signal()], provider='codex')
    daemon._decide_auto_compacts(snap)
    assert alerts.AlertPolicy().evaluate(snap, {}, 0)


@pytest.mark.asyncio
async def test_codex_navigation_proof_does_not_deliver_compact(tmp_path, monkeypatch):
    from tests.test_agents_poll import _navigation_daemon
    from unittest.mock import AsyncMock
    from dark_army_daemon import vscode_reveal
    daemon, roots, _processes = _navigation_daemon(tmp_path, monkeypatch)
    send = AsyncMock()
    monkeypatch.setattr(vscode_reveal, "send_text", send)
    daemon._compact_queue = [(roots[0].session_id, 701, "codex")]
    await daemon._flush_auto_compacts()
    send.assert_not_called()
