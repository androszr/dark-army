# host/tests/test_daemon_hardening.py
"""Pinning tests for six narrow fixes, 1 Sep 2026.

One test (or a small cluster) per fix, each written against the seam the bug
lived at: the channel registry, the pending-question tracker, the alert
policy's forget sweep, the roster-refresh memo, and the dispatch baselines.
"""

import asyncio
import time

import pytest

from dark_army_daemon import daemon as daemon_mod
from dark_army_daemon import dispatch
from dark_army_daemon.alerts import AlertPolicy
from dark_army_daemon.board import BoardStore
from dark_army_daemon.daemon import BobDaemon
from dark_army_daemon.protocol import hook_payload_to_daemon_message


# ── A. a known port's record is not overwritten by a mismatched attach ───────

def _attach(daemon, *, port=51000, pid=111, session_id="sess-a",
            secret="first"):
    return daemon._handle_channel_message(
        {"type": "channel_attach", "port": port, "pid": pid, "cwd": "/tmp",
         "session_id": session_id, "is_channel": True, "secret": secret})


def test_a_mismatched_attach_on_a_live_port_is_refused():
    """The hook socket authenticates nothing, so a second attach naming a port
    already in `_channels` used to overwrite the incumbent's session id and
    secret — misrouting its replies and breaking the real channel's password
    check. A live incumbent keeps its port; the forger's record never lands."""
    d = BobDaemon(headless=True)
    _attach(d)
    _attach(d, pid=222, session_id="sess-b", secret="second")
    held = d._channels[51000]
    assert held["pid"] == 111
    assert held["session_id"] == "sess-a"
    assert held["secret"] == "first"


def test_a_matching_heartbeat_still_refreshes_a_known_port():
    d = BobDaemon(headless=True)
    _attach(d)
    d._channels[51000]["last_seen"] = time.time() - 30.0
    _attach(d)                                     # same pid/session/secret
    assert d._channels[51000]["last_seen"] > time.time() - 5.0


def test_a_lapsed_incumbent_is_replaceable():
    """The honest restart case: a channel that stopped heartbeating past
    CHANNEL_CLAIM_SECONDS loses its claim on the port, exactly as it loses
    its claim on the session in `_attach_is_displaced`."""
    d = BobDaemon(headless=True)
    _attach(d)
    d._channels[51000]["last_seen"] = (
        time.time() - BobDaemon.CHANNEL_CLAIM_SECONDS - 5.0)
    _attach(d, pid=222, session_id="sess-b", secret="second")
    assert d._channels[51000]["session_id"] == "sess-b"
    assert d._channels[51000]["secret"] == "second"


# ── B. idle_prompt does not pop the pending question ─────────────────────────

def test_idle_prompt_notification_leaves_the_pending_question_standing():
    """Claude Code fires the idle_prompt Notification ~60s into any quiet
    spell — a standing AskUserQuestion dialog is exactly that. Its converted
    `add` names no tool, so like the nameless permission companion it may not
    clear the question; a real tool call afterwards still does."""
    d = BobDaemon()
    d._session_states["s1"] = {"state": "waiting", "last_event": time.time()}
    d._pending_questions["s1"] = {"question": "which one?"}
    msg = hook_payload_to_daemon_message(
        {"hook_event_name": "Notification", "session_id": "s1",
         "notification_type": "idle_prompt", "cwd": "/tmp"})
    # Pin the discriminator: the idle_prompt converts to an `add` whose hook
    # is "Notification" — that pair is what the tracker now ignores.
    assert msg["event"] == "add"
    assert msg["hook"] == "Notification"
    d._track_pending_question(msg["event"], "s1", msg)
    assert "s1" in d._pending_questions

    d._track_pending_question("tool_use", "s1", {"tool_name": "Bash"})
    assert "s1" not in d._pending_questions


def test_a_typed_prompt_still_clears_the_question():
    d = BobDaemon()
    d._pending_questions["s1"] = {"question": "which one?"}
    msg = hook_payload_to_daemon_message(
        {"hook_event_name": "UserPromptSubmit", "session_id": "s1",
         "cwd": "/tmp"})
    d._track_pending_question(msg["event"], "s1", msg)
    assert "s1" not in d._pending_questions


# ── C. a mute survives the session going quiet into `finished` ───────────────

def test_a_muted_agent_quiet_into_finished_keeps_its_mute():
    """The `finished` bucket also holds live sessions quiet past
    FINISHED_IDLE_GRACE_SECONDS, flagged `alive`. `evaluate` skips the whole
    bucket, so those sids fell out of `live` and `_forget_gone` dropped their
    mute — a muted agent re-bannered on its very next wait."""
    policy = AlertPolicy()
    policy.mute("s1")
    policy._fired[("s1", "card")] = 900.0
    snapshot = {"running": [],
                "finished": [{"session_id": "s1", "alive": True}]}
    assert policy.evaluate(snapshot, {}, now=1000.0) == []
    assert policy.is_muted("s1")
    assert ("s1", "card") in policy._fired


def test_a_genuinely_finished_session_is_still_forgotten():
    policy = AlertPolicy()
    policy.mute("s1")
    snapshot = {"running": [], "finished": [{"session_id": "s1"}]}
    policy.evaluate(snapshot, {}, now=1000.0)
    assert not policy.is_muted("s1")


# ── D. the roster refreshes are memoised on a short interval ─────────────────

def test_reconciled_categories_reuses_the_rosters_inside_the_interval(
        monkeypatch):
    """The Grok/Codex refreshes glob rollouts and walk the process table on
    the event loop, and `_reconciled_categories` runs from every snapshot and
    every statusline message. Two calls inside ROSTER_REFRESH_INTERVAL must
    hit the underlying refresh once; a later call refreshes again."""
    d = BobDaemon()
    calls = {"grok": 0, "codex": 0}
    monkeypatch.setattr(
        d, "_refresh_grok_records",
        lambda: calls.__setitem__("grok", calls["grok"] + 1))
    monkeypatch.setattr(
        d, "_refresh_codex_records",
        lambda: calls.__setitem__("codex", calls["codex"] + 1))

    d._reconciled_categories()
    d._reconciled_categories()
    assert calls == {"grok": 1, "codex": 1}

    d._roster_refreshed_at -= daemon_mod.ROSTER_REFRESH_INTERVAL
    d._reconciled_categories()
    assert calls == {"grok": 2, "codex": 2}


@pytest.mark.asyncio
async def test_reconciled_categories_on_the_loop_schedules_the_refresh(
        monkeypatch):
    """The sibling under a running loop: a lapsed window hands the glob, the
    parse and the process scan to the executor and answers from the last
    rosters — nothing is refreshed inline on the loop."""
    d = BobDaemon()
    calls = {"grok": 0, "codex": 0, "load": 0}
    monkeypatch.setattr(
        d, "_refresh_grok_records",
        lambda recs=None: calls.__setitem__("grok", calls["grok"] + 1))
    monkeypatch.setattr(
        d, "_refresh_codex_records",
        lambda records=None: calls.__setitem__("codex", calls["codex"] + 1))

    def load():
        calls["load"] += 1
        return [], []
    monkeypatch.setattr(d, "_load_rosters", load)

    d._reconciled_categories()
    assert calls == {"grok": 0, "codex": 0, "load": 0}
    for _ in range(100):
        task = d._roster_refresh_task
        if task is not None and task.done():
            break
        await asyncio.sleep(0.01)
    assert calls == {"grok": 1, "codex": 1, "load": 1}
    d._reconciled_categories()          # inside the window: nothing scheduled
    await asyncio.sleep(0.05)
    assert calls["load"] == 1


# ── E. the bind baseline is taken before the spawn await ─────────────────────

@pytest.mark.asyncio
async def test_a_session_that_beats_the_spawn_reply_is_bindable(
        tmp_path, monkeypatch):
    """A dispatched session's own SessionStart can arrive before the
    extension's spawn reply. A baseline captured after the await then contains
    the very session it exists to single out, and the card never binds — so
    the baseline is captured before the spawn and assigned on success."""
    d = BobDaemon(sessions_path=tmp_path / "sessions.json")
    store = BoardStore(tmp_path / "board.db")
    store.connect()
    d._board = store
    try:
        card, detail = store.create(
            {"title": "do the thing", "project": "bob", "root": "/tmp",
             "prompt": "go", "tool": "claude", "column_name": "backlog"})
        assert card is not None, detail
        monkeypatch.setattr(d, "_known_project_roots",
                            lambda: {"/private/tmp", "/tmp"})
        monkeypatch.setattr(dispatch, "resolve_executable",
                            lambda tool: "/bin/claude")

        async def spawn_with_early_session(root, argv, name, **_kw):
            # The race, made deterministic: the new session's hooks land
            # while the spawn call is still awaiting the extension's reply.
            d._session_states["racer"] = {"state": "working",
                                          "last_event": time.time()}
            return True, "bob", None

        monkeypatch.setattr(dispatch, "spawn", spawn_with_early_session)
        ok, detail = await d.dispatch_card(card["id"], allow_unplanned=True)
        assert ok, detail
        assert "racer" not in d._dispatch_baseline[card["id"]]

        # And it actually binds: the racer is the first session not in the
        # baseline with the card's provider and project.
        now = time.time()
        snapshot = {"running": [
            {"session_id": "racer", "provider": "claude",
             "kind": "interactive", "project": "bob", "cwd": "/tmp",
             "started_at": now},
        ]}
        assert d._reconcile_board(snapshot) is True
        got = store.get(card["id"])
        assert got["session_id"] == "racer"
        assert got["link_state"] == "live"
    finally:
        store.close()
