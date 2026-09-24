"""What the daemon tells its observers: the card count as cards come and go,
the activity figures as sessions and subagents start and stop, and that no
observer at all (or half of one) is fine."""

import asyncio

import pytest

from dark_army_daemon.daemon import BobDaemon


class Recorder:
    """Keeps every card list length and every activity tuple it was sent."""

    def __init__(self):
        self.card_counts = []
        self.activity = []

    def on_notification_change(self, notifications: list) -> None:
        self.card_counts.append(len(notifications))

    def on_activity_change(self, working, idle, attention, subagents):
        self.activity.append((working, idle, attention, subagents))


def _card(sid):
    return {"event": "add", "session_id": sid, "project": "demo", "message": "ready"}


async def _feed(daemon, *messages):
    for message in messages:
        await daemon._handle_message(message)


@pytest.mark.parametrize("messages, counts", [
    ([_card("one")], [1]),
    ([_card("one"), _card("two")], [1, 2]),
    ([_card("one"), {"event": "dismiss", "session_id": "one"}], [1, 0]),
], ids=["one-card", "two-cards", "card-then-dismiss"])
@pytest.mark.asyncio
async def test_every_card_change_reaches_the_observer_with_the_new_count(messages, counts):
    seen = Recorder()
    await _feed(BobDaemon(observer=seen), *messages)
    assert seen.card_counts == counts


@pytest.mark.asyncio
async def test_activity_counts_follow_subagents_across_sessions():
    seen = Recorder()
    daemon = BobDaemon(observer=seen)
    await _feed(daemon,
                {"event": "subagent_start", "session_id": "one", "agent_id": "kid-1"},
                {"event": "subagent_start", "session_id": "two", "agent_id": "kid-2"})
    # Both sessions were created working by their first subagent.
    assert seen.activity[-1] == (2, 0, 0, 2)
    await _feed(daemon, {"event": "subagent_stop", "session_id": "two", "agent_id": "kid-2"})
    # The session stays working; only its subagent figure goes down.
    assert seen.activity[-1] == (2, 0, 0, 1)


@pytest.mark.asyncio
async def test_no_observer_activity_does_not_crash():
    """A partial observer without on_activity_change must not crash the daemon."""
    class Partial:
        def on_notification_change(self, notifications):
            pass

    daemon = BobDaemon(observer=Partial())
    await _feed(daemon, {"event": "subagent_start", "session_id": "s1", "agent_id": "a1"})
    assert "s1" in daemon._session_states


@pytest.mark.asyncio
async def test_a_daemon_nobody_observes_still_keeps_its_cards():
    daemon = BobDaemon()
    await _feed(daemon, _card("lonely"))
    assert list(daemon._active_notifications) == ["lonely"]


# --- Fan-out: more than one surface listens now (menu bar + Pond Control API) ---


class ExplodingObserver:
    """Implements the full protocol and fails at everything."""

    def on_notification_change(self, notifications: list) -> None:
        raise RuntimeError("boom")

    def on_activity_change(self, w, i, a, s) -> None:
        raise RuntimeError("boom")

    def on_agents_change(self, snapshot: dict) -> None:
        raise RuntimeError("boom")


@pytest.mark.asyncio
async def test_two_observers_both_receive_notifications():
    a, b = Recorder(), Recorder()
    daemon = BobDaemon(observer=a)
    daemon.add_observer(b)
    await _feed(daemon, _card("s1"))
    assert a.card_counts == [1]
    assert b.card_counts == [1]


@pytest.mark.asyncio
async def test_agents_snapshot_reaches_every_observer():
    good = Recorder()
    good.snapshots = []
    good.on_agents_change = lambda snap: good.snapshots.append(snap)
    daemon = BobDaemon(observer=ExplodingObserver())
    daemon.add_observer(good)

    await daemon._push_agents_snapshot()

    assert len(good.snapshots) == 1
    assert set(good.snapshots[0]) == {"running", "sleeping", "waiting",
                                      "abandoned", "finished", "project_facts"}


@pytest.mark.asyncio
async def test_add_remove_observer():
    a, b = Recorder(), Recorder()
    daemon = BobDaemon(observer=a)
    daemon.add_observer(b)
    daemon.add_observer(b)  # idempotent
    assert len(daemon._observers) == 2

    daemon.remove_observer(b)
    await _feed(daemon, _card("s1"))
    assert a.card_counts == [1]
    assert b.card_counts == []
    daemon.remove_observer(b)  # removing a non-observer is a no-op


@pytest.mark.asyncio
async def test_observer_property_still_reads_and_writes():
    """Existing callers (and tests) set daemon._observer directly."""
    a, b = Recorder(), Recorder()
    daemon = BobDaemon(observer=a)
    assert daemon._observer is a

    daemon._observer = b  # replaces, does not append
    assert daemon._observers == [b]
    assert daemon._observer is b

    daemon._observer = None
    assert daemon._observers == []
    assert daemon._observer is None

    # A daemon with no observers must still handle events.
    await _feed(daemon, _card("s1"))


@pytest.mark.asyncio
async def test_a_change_during_an_in_flight_push_is_not_lost():
    """The subagent-missing-from-the-menu bug. A push snapshots session state when
    it starts, so a SubagentStart arriving mid-push was dropped until some
    unrelated event triggered another one — while the window's HUD, fed by the
    un-throttled counts path, showed it immediately."""
    seen = Recorder()
    seen.snapshots = []
    seen.on_agents_change = lambda snap: seen.snapshots.append(snap)
    daemon = BobDaemon(observer=seen)

    gate = asyncio.Event()
    real_enrich = daemon._enrich_agent_stubs
    first = {"done": False}

    def slow_enrich(stubs):
        if not first["done"]:
            first["done"] = True
            # Simulate the executor still working while new state lands.
            daemon._session_states["late"] = {"state": "working", "last_event": 0}
            daemon._schedule_agents_push()
            gate.set()
        return real_enrich(stubs)

    daemon._enrich_agent_stubs = slow_enrich
    daemon._schedule_agents_push()
    await asyncio.wait_for(gate.wait(), timeout=5)
    for _ in range(20):                      # let the trailing push land
        await asyncio.sleep(0.02)
        if len(seen.snapshots) >= 2:
            break

    assert len(seen.snapshots) >= 2, "the change made mid-push was never pushed"
    latest = seen.snapshots[-1]
    ids = [e["session_id"] for group in latest.values() for e in group
           if isinstance(e, dict) and e.get("session_id")]
    assert "late" in ids, "trailing push did not carry the newer state"


@pytest.mark.asyncio
async def test_trailing_push_does_not_run_forever():
    """One trailing push, not a pile: the flag clears before the re-run."""
    seen = Recorder()
    seen.snapshots = []
    seen.on_agents_change = lambda snap: seen.snapshots.append(snap)
    daemon = BobDaemon(observer=seen)

    daemon._schedule_agents_push()
    daemon._schedule_agents_push()
    daemon._schedule_agents_push()
    for _ in range(20):
        await asyncio.sleep(0.02)
    assert len(seen.snapshots) <= 2, f"{len(seen.snapshots)} pushes for one burst"
