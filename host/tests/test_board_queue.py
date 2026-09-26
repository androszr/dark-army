# host/tests/test_board_queue.py
"""The per-project work queue: who may go next, and the gate in front of
`dispatch.spawn`.

The policy half (`board_queue`) is pure and is tested directly. The daemon half
is tested through `BobDaemon.dispatch_card` with `dispatch.spawn` stubbed, which
is the only honest place to assert "no terminal opened".
"""

import asyncio
import time

import pytest

from dark_army_daemon import board_queue


# --- claims ----------------------------------------------------------------


def test_only_starting_and_running_cards_hold_a_project():
    """`ended` deliberately holds nothing: a dead session cannot edit anything,
    and holding the claim until Done would wedge the pipeline behind human
    verification, which can take a day."""
    cards = [
        {"id": "a", "project": "bob", "link_state": "live"},
        {"id": "b", "project": "bob", "link_state": "dispatching"},
        {"id": "c", "project": "bob", "link_state": "ended"},
        {"id": "d", "project": "bob", "link_state": ""},
        {"id": "e", "project": "other", "link_state": "live"},
    ]
    assert [c["id"] for c in board_queue.claims(cards, "bob")] == ["a", "b"]
    assert [c["id"] for c in board_queue.claims(cards, "other")] == ["e"]


def test_a_card_in_done_holds_nothing():
    """Done means somebody said the work was finished. A session left sitting
    in its terminal afterwards is not editing anything, and until the Done
    drag stopped typing `/clear` that wipe was the only thing releasing the
    claim — by accident."""
    cards = [
        {"id": "a", "project": "bob", "link_state": "live",
         "column_name": "done", "session_id": "s1"},
        {"id": "b", "project": "bob", "link_state": "live",
         "column_name": "in_progress", "session_id": "s1"},
    ]
    assert [c["id"] for c in board_queue.claims(cards, "bob")] == ["b"]


def test_a_card_flagged_for_a_manual_check_holds_nothing():
    """`manual_steps` is the assistant saying its work is over and a person
    still has to look — the same sentence Done makes, one gate earlier. The
    PASS-only close gate is what keeps such a card out of Done, so it cannot
    lean on the Done narrowing; without this, six cards waited on one card
    whose only outstanding item was an on-screen check."""
    cards = [
        {"id": "a", "project": "bob", "link_state": "live",
         "column_name": "in_progress", "session_id": "s1",
         "manual_steps": "1. Open the board and look at the banner."},
        {"id": "b", "project": "bob", "link_state": "live",
         "column_name": "in_progress", "session_id": "s1"},
    ]
    assert [c["id"] for c in board_queue.claims(cards, "bob")] == ["b"]


def test_a_blank_manual_steps_field_still_holds():
    """The field defaults to the empty string on every card, so only a
    non-blank flag may release a claim — whitespace is not a declaration."""
    for value in ("", "   ", "\n"):
        card = {"id": "a", "project": "bob", "link_state": "live",
                "column_name": "in_progress", "session_id": "s1",
                "manual_steps": value}
        assert board_queue.holds(card) is True, repr(value)
        # And the two answers agree wherever the flag is not raised.
        assert board_queue.run_active(card) is True, repr(value)


def test_run_active_ignores_the_manual_check_release():
    """The split, at the unit it lives in: `holds` is the gate's question and
    releases on `manual_steps` so queued work flows past a card waiting only
    on a hand-check; `run_active` is "an assistant is working" and does not —
    a flagged card whose session is still audible is running, not done. The
    flag is the only term the two disagree on."""
    flagged = {"id": "a", "project": "bob", "link_state": "live",
               "column_name": "in_progress", "session_id": "s1",
               "manual_steps": "1. Open the board and look at the banner."}
    assert board_queue.run_active(flagged) is True
    assert board_queue.holds(flagged) is False
    assert board_queue.run_active(flagged, {"s1"}) is True


def test_run_active_shares_the_other_releases_with_holds():
    """One implementation of the shared releases: a done-column card, a
    non-claiming link state, and a live card whose session went quiet all
    release both answers — the sibling only removes the `manual_steps` term."""
    base = {"id": "a", "project": "bob", "link_state": "live",
            "column_name": "in_progress", "session_id": "s1"}
    for gone in ({**base, "column_name": "done"},
                 {**base, "link_state": "ended"},
                 {**base, "link_state": ""}):
        assert board_queue.run_active(gone) is False, gone
        assert board_queue.holds(gone) is False, gone
    # An audible session keeps both; an inaudible one releases both.
    assert board_queue.run_active(base, {"s1"}) is True
    assert board_queue.run_active(base, set()) is False
    # A `dispatching` card has no session yet and is exempt, as in `holds`.
    disp = {**base, "link_state": "dispatching", "session_id": ""}
    assert board_queue.run_active(disp, set()) is True


def test_a_live_card_whose_session_went_quiet_holds_nothing():
    """The wedge: a dispatched session that finishes and goes quiet is evicted
    from the hook stream and then comes *back* as a roster row for as long as
    its terminal stays open, so `link_state` never reaches `ended`. A card
    still `dispatching` has no session yet and is exempt."""
    cards = [
        {"id": "a", "project": "bob", "link_state": "live", "session_id": "s1"},
        {"id": "b", "project": "bob", "link_state": "live", "session_id": "s2"},
        {"id": "c", "project": "bob", "link_state": "dispatching",
         "session_id": ""},
    ]
    assert [c["id"] for c in board_queue.claims(cards, "bob", {"s1"})] == \
        ["a", "c"]
    # No set at all keeps the old behaviour, for a caller that cannot say.
    assert [c["id"] for c in board_queue.claims(cards, "bob")] == \
        ["a", "b", "c"]


# --- the daemon: the gate, the enqueue, the drain ---------------------------

from dark_army_daemon import dispatch                       # noqa: E402
from dark_army_daemon.board import BoardStore               # noqa: E402
from dark_army_daemon.daemon import BobDaemon               # noqa: E402
from dark_army_daemon.daemon import PLAN_CHANGED_REFUSAL  # noqa: E402
from dark_army_daemon.daemon import PLAN_GATE_REFUSAL     # noqa: E402


@pytest.fixture
def project(tmp_path):
    """A real directory to be the card's root. Real because `declared_files`
    resolves a plan *inside the card's own root*, and the containment test is
    the interesting half of that reader."""
    root = tmp_path / "proj"
    (root / "plans").mkdir(parents=True)
    return root


@pytest.fixture
def daemon(tmp_path):
    d = BobDaemon(sessions_path=tmp_path / "sessions.json")
    # The default dial: one agent per project. Every test below that wants a
    # second place says so by hand, so the number a case runs at is always
    # visible in the case rather than in the fixture.
    store = BoardStore(tmp_path / "board.db")
    store.connect()
    d._board = store
    try:
        yield d, store
    finally:
        store.close()


def _plan(project, name, files):
    rows = "\n".join(f"| `{f}` | change it |" for f in files)
    text = (f"# {name}\n\n- **Stages:** bc-implementer\n\n"
            "## Files to change\n\n| File | Change |\n|---|---|\n"
            f"{rows}\n\n## Out of scope\n\nnothing\n")
    path = project / "plans" / f"{name}.md"
    path.write_text(text)
    return str(path)


def _make(store, plan_path=None, **kw):
    """A card, optionally planned. `plan_path` is outside `_WRITABLE` by
    design — `attach_plan` is its one writer — so a test that wants a planned
    card has to go through the same door a refinement does."""
    fields = {"title": "do the thing", "project": "bob",
              "prompt": "go", "tool": "claude", "column_name": "backlog"}
    fields.update(kw)
    if plan_path:
        fields["column_name"] = "prep"
    card, detail = store.create(fields)
    assert card is not None, detail
    if plan_path:
        card, detail = store.attach_plan(card["id"], plan_path, "")
        assert card is not None, detail
        if kw.get("column_name") not in (None, "prep", "backlog"):
            card, detail = store.update(card["id"],
                                        {"column_name": kw["column_name"]})
            assert card is not None, detail
    return card


def _hear(d, *session_ids):
    """Register hook state for a session, so the daemon counts it as one it
    can still hear. `claims` releases a `live` card whose session has gone
    silent, so a fixture that binds a card to a session id and never tells the
    daemon about it is testing the released case by accident."""
    for sid in session_ids:
        d._session_states[sid] = {"state": "working", "last_event": time.time()}


def _stub_spawn(d, monkeypatch, opened, root):
    monkeypatch.setattr(d, "_known_project_roots", lambda: {str(root)})
    monkeypatch.setattr(dispatch, "resolve_executable", lambda tool: "/bin/claude")

    async def accept(root, argv, name, **_kw):
        opened.append(name)
        return True, "bob", None

    monkeypatch.setattr(dispatch, "spawn", accept)


@pytest.mark.asyncio
async def test_a_full_project_queues_the_next_card_and_opens_no_terminal(
        daemon, project, monkeypatch):
    """The whole point, asserted where it can actually be asserted: not "the
    button said no" but "`dispatch.spawn` was never called".

    At the default dial of one, a project with an agent working has no free
    place, whatever either card's plan declares."""
    d, store = daemon
    opened: list = []
    _stub_spawn(d, monkeypatch, opened, project)
    shared = _plan(project, "shared", ["host/a.py"])
    running = _make(store, root=str(project), title="running", plan_path=shared)
    store.update(running["id"], {"link_state": "live", "session_id": "s1",
                                 "column_name": "in_progress"})
    _hear(d, "s1")
    later = _make(store, root=str(project), title="later", plan_path=shared)

    ok, detail = await d.dispatch_card(later["id"])
    assert not ok
    # The refusal is the same sentence the card's own badge will wear a
    # snapshot later, composed from the same count — a queue whose reply and
    # whose badge disagree is one nobody can read.
    assert detail == ("Queued — Dark Army will start it when the agent working in "
                      "this project finishes")
    assert opened == [], "a card with no free place must not open a terminal"
    got = store.get(later["id"])
    assert got["queue_state"] == "queued" and got["queued_at"] is not None
    # And it does not move: In progress keeps meaning "an assistant is working".
    assert got["column_name"] == "backlog"


@pytest.mark.asyncio
async def test_at_one_disjoint_work_waits_and_at_two_it_starts(
        daemon, project, monkeypatch):
    """The one place the dial at 1 is **stricter** than the file gate it
    replaced, pinned so nobody rediscovers it as a bug: two cards whose plans
    declare disjoint tables used to run together, because the old gate asked
    about files. The dial asks about places, so the second waits — and raising
    the dial to two is what starts it, with the files never consulted either
    way."""
    d, store = daemon
    opened: list = []
    _stub_spawn(d, monkeypatch, opened, project)
    running = _make(store, root=str(project), title="running",
                    plan_path=_plan(project, "a", ["host/a.py"]))
    store.update(running["id"], {"link_state": "live", "session_id": "s1",
                                 "column_name": "in_progress"})
    _hear(d, "s1")
    other = _make(store, root=str(project), title="other",
                  plan_path=_plan(project, "b", ["panel/b.swift"]))

    ok, _detail = await d.dispatch_card(other["id"])
    assert not ok
    assert opened == [], "one place means one agent, disjoint plans included"
    assert store.get(other["id"])["queue_state"] == "queued"

    # Two places. The same card, the same plans, and now it goes.
    d.set_board_parallel(2)
    d._dispatch_attempts.pop(other["id"], None)
    ok, detail = await d.dispatch_card(other["id"], queued_replay=True)
    assert ok, detail
    assert opened == ["other"]
    assert store.get(other["id"])["queue_state"] == ""


@pytest.mark.asyncio
async def test_two_projects_never_wait_on_each_other(
        daemon, project, monkeypatch):
    d, store = daemon
    opened: list = []
    _stub_spawn(d, monkeypatch, opened, project)
    shared = _plan(project, "shared", ["host/a.py"])
    running = _make(store, root=str(project), title="running", plan_path=shared)
    store.update(running["id"], {"link_state": "live", "session_id": "s1",
                                 "column_name": "in_progress"})
    _hear(d, "s1")
    elsewhere = _make(store, root=str(project), title="elsewhere", project="other",
                      plan_path=shared)

    ok, detail = await d.dispatch_card(elsewhere["id"])
    assert ok, detail
    assert opened == ["elsewhere"]


@pytest.mark.asyncio
async def test_an_undeclared_card_queues_behind_everything(
        daemon, project, monkeypatch):
    """No plan means the card claims the whole project — the conservative
    direction, and the one that makes the gate safe when a plan is missing."""
    d, store = daemon
    opened: list = []
    _stub_spawn(d, monkeypatch, opened, project)
    running = _make(store, root=str(project), title="running",
                    plan_path=_plan(project, "a", ["host/a.py"]))
    store.update(running["id"], {"link_state": "live", "session_id": "s1",
                                 "column_name": "in_progress"})
    _hear(d, "s1")
    blind = _make(store, root=str(project), title="blind")

    ok, _ = await d.dispatch_card(blind["id"], allow_unplanned=True)
    assert not ok
    assert opened == []
    assert store.get(blind["id"])["queue_state"] == "queued"


@pytest.mark.asyncio
async def test_an_ended_session_frees_the_project_immediately(
        daemon, project, monkeypatch):
    """A dead session cannot edit anything. Holding the claim until the card
    reaches Done would wedge the pipeline behind human verification."""
    d, store = daemon
    opened: list = []
    _stub_spawn(d, monkeypatch, opened, project)
    shared = _plan(project, "shared", ["host/a.py"])
    done_with = _make(store, root=str(project), title="finished", plan_path=shared)
    store.update(done_with["id"], {"link_state": "ended", "session_id": "s1",
                                   "column_name": "in_progress"})
    next_up = _make(store, root=str(project), title="next", plan_path=shared)

    ok, detail = await d.dispatch_card(next_up["id"])
    assert ok, detail
    assert opened == ["next"]


@pytest.mark.asyncio
async def test_a_finished_session_that_went_quiet_frees_the_project(
        daemon, project, monkeypatch):
    """The wedge, end to end. A dispatched session finishes, goes quiet and is
    evicted from the hook stream — but its terminal stays open, so
    `claude agents --json` goes on listing it and the row comes back in the
    `running` bucket, which is why `link_state` is still `live` here. The
    claim has to end with the work, not with the terminal."""
    d, store = daemon
    opened: list = []
    _stub_spawn(d, monkeypatch, opened, project)
    shared = _plan(project, "shared", ["host/a.py"])
    running = _make(store, root=str(project), title="running", plan_path=shared)
    store.update(running["id"], {"link_state": "live", "session_id": "s1",
                                 "column_name": "in_progress"})
    _hear(d, "s1")
    waiting = _make(store, root=str(project), title="waiting", plan_path=shared)
    await d.dispatch_card(waiting["id"])
    assert store.get(waiting["id"])["queue_state"] == "queued"
    d._decide_queue_dispatches(store.cards())
    assert d._queue_candidates == []

    # Silent past the staleness window: the hook state is gone, the card is
    # untouched and still says `live`.
    d._session_states.pop("s1")
    assert store.get(running["id"])["link_state"] == "live"
    d._decide_queue_dispatches(store.cards())
    assert d._queue_candidates == [waiting["id"]]


@pytest.mark.asyncio
async def test_a_card_dragged_to_done_frees_the_project(
        daemon, project, monkeypatch):
    """The other release, and the one a person can make deliberately: Done
    means somebody said the work is finished, whatever the terminal is still
    doing."""
    d, store = daemon
    opened: list = []
    _stub_spawn(d, monkeypatch, opened, project)
    shared = _plan(project, "shared", ["host/a.py"])
    running = _make(store, root=str(project), title="running", plan_path=shared)
    store.update(running["id"], {"link_state": "live", "session_id": "s1",
                                 "column_name": "in_progress"})
    _hear(d, "s1")
    waiting = _make(store, root=str(project), title="waiting", plan_path=shared)
    await d.dispatch_card(waiting["id"])
    d._decide_queue_dispatches(store.cards())
    assert d._queue_candidates == []

    store.update(running["id"], {"column_name": "done"})
    assert store.get(running["id"])["link_state"] == "live"
    d._decide_queue_dispatches(store.cards())
    assert d._queue_candidates == [waiting["id"]]


@pytest.mark.asyncio
async def test_a_queued_card_says_what_it_is_waiting_on(
        daemon, project, monkeypatch):
    """The reason has to reach the card. Four cards once sat marked Queued
    with nothing on screen naming the session holding them, and the answer had
    to be dug out of the daemon log an hour later."""
    d, store = daemon
    opened: list = []
    _stub_spawn(d, monkeypatch, opened, project)
    shared = _plan(project, "shared", ["host/a.py"])
    running = _make(store, root=str(project), title="the running one",
                    plan_path=shared)
    store.update(running["id"], {"link_state": "live", "session_id": "s1",
                                 "column_name": "in_progress"})
    _hear(d, "s1")
    waiting = _make(store, root=str(project), title="waiting", plan_path=shared)
    await d.dispatch_card(waiting["id"])

    state = d._build_board_state()
    by_id = {c["id"]: c for c in state["cards"]}
    assert by_id[waiting["id"]]["queue_reason"] == (
        "Queued — Dark Army will start it when the agent working in this project "
        "finishes")
    # No holder is named because under the slot rule none exists: the project
    # is full, not a particular card in the way.
    assert "queue_holder" not in by_id[waiting["id"]]
    # Never on a card that is not queued, and never stored.
    assert "queue_reason" not in by_id[running["id"]]
    assert "queue_reason" not in store.get(waiting["id"])


@pytest.mark.asyncio
async def test_the_snapshot_says_whether_the_work_is_still_running(
        daemon, project, monkeypatch):
    """The gate's own answer, published per card, so the rail can stop drawing
    a finished card as RUN. One predicate, two readers — the panel deriving
    its own would be the two-surfaces-disagree failure, with the band claiming
    an assistant is working on a card the queue has already moved past."""
    d, store = daemon
    card = _make(store, root=str(project), title="running",
                 plan_path=_plan(project, "a", ["host/a.py"]))
    store.update(card["id"], {"link_state": "live", "session_id": "s1",
                              "column_name": "in_progress"})
    _hear(d, "s1")
    assert d._decorate_card_for_snapshot(store.get(card["id"]), {})["work_active"]

    # Silent past the staleness window: still bound, no longer working.
    d._session_states.pop("s1")
    assert not d._decorate_card_for_snapshot(
        store.get(card["id"]), {})["work_active"]

    # And a card in Done is over whatever its terminal is still doing.
    _hear(d, "s1")
    store.update(card["id"], {"column_name": "done"})
    assert not d._decorate_card_for_snapshot(
        store.get(card["id"]), {})["work_active"]


@pytest.mark.asyncio
async def test_the_snapshot_publishes_run_active_beside_work_active(
        daemon, project, monkeypatch):
    """The band's answer split from the gate's. A hand-check flag frees the
    card's files (`work_active` False, so queued work flows past it) while
    its session is still audible and busy — and the band must keep calling
    that RUN (`run_active` True), or the strip files live work under done,
    which is the bug the split retires: every manual-flagged live card landed
    in DONE and RUN was structurally empty."""
    d, store = daemon
    card = _make(store, root=str(project), title="flagged",
                 plan_path=_plan(project, "a", ["host/a.py"]))
    store.update(card["id"], {"link_state": "live", "session_id": "s1",
                              "column_name": "in_progress"})
    flagged, detail = store.flag_manual(
        card["id"], "s1", "1. Open the panel and look at the strip.")
    assert flagged is not None, detail
    _hear(d, "s1")

    drawn = d._decorate_card_for_snapshot(store.get(card["id"]), {})
    assert drawn["run_active"] is True
    assert drawn["work_active"] is False

    # The shared releases still close both answers: a session Dark Army can no
    # longer hear is not working, flag or no flag.
    d._session_states.pop("s1")
    drawn = d._decorate_card_for_snapshot(store.get(card["id"]), {})
    assert drawn["run_active"] is False
    assert drawn["work_active"] is False

    # An ended link, and a card in Done, are over for both answers too.
    _hear(d, "s1")
    store.update(card["id"], {"link_state": "ended"})
    assert not d._decorate_card_for_snapshot(
        store.get(card["id"]), {})["run_active"]
    store.update(card["id"], {"link_state": "live", "column_name": "done"})
    assert not d._decorate_card_for_snapshot(
        store.get(card["id"]), {})["run_active"]


@pytest.mark.asyncio
async def test_with_the_drain_off_a_queued_card_says_press_start(
        daemon, project, monkeypatch):
    """The switches remove the *decision*, not the queue: that is precisely
    the state in which somebody has to press Start themselves, and a card
    that goes on promising "Dark Army will start it" is the app promising an action
    it will not take."""
    d, store = daemon
    opened: list = []
    _stub_spawn(d, monkeypatch, opened, project)
    shared = _plan(project, "shared", ["host/a.py"])
    running = _make(store, root=str(project), title="the running one",
                    plan_path=shared)
    store.update(running["id"], {"link_state": "live", "session_id": "s1",
                                 "column_name": "in_progress"})
    _hear(d, "s1")
    waiting = _make(store, root=str(project), title="waiting", plan_path=shared)
    await d.dispatch_card(waiting["id"])

    d.board_autostart_enabled = False
    d._decide_queue_dispatches(store.cards())
    assert d._queue_candidates == []
    # Composed at decoration time, not remembered from the reconcile, which is
    # what lets the flip reword every queued card on the very next frame.
    state = d._build_board_state()
    by_id = {c["id"]: c for c in state["cards"]}
    assert by_id[waiting["id"]]["queue_reason"] == (
        "Queued — press Start when the agent working in this project "
        "finishes")


@pytest.mark.asyncio
async def test_the_drain_starts_the_head_when_the_files_come_free(
        daemon, project, monkeypatch):
    d, store = daemon
    opened: list = []
    _stub_spawn(d, monkeypatch, opened, project)
    shared = _plan(project, "shared", ["host/a.py"])
    running = _make(store, root=str(project), title="running", plan_path=shared)
    store.update(running["id"], {"link_state": "live", "session_id": "s1",
                                 "column_name": "in_progress"})
    _hear(d, "s1")
    waiting = _make(store, root=str(project), title="waiting", plan_path=shared)
    await d.dispatch_card(waiting["id"])
    assert store.get(waiting["id"])["queue_state"] == "queued"

    # Still live: the decision picks nothing.
    d._decide_queue_dispatches(store.cards())
    assert d._queue_candidates == []

    # The session ends. The next reconcile frees the files and the drain fires.
    store.update(running["id"], {"link_state": "ended"})
    d._decide_queue_dispatches(store.cards())
    assert d._queue_candidates == [waiting["id"]]
    d._dispatch_attempts.pop(waiting["id"], None)
    await d._flush_queue_dispatches()
    assert opened == ["waiting"]
    got = store.get(waiting["id"])
    assert got["queue_state"] == "" and got["link_state"] == "dispatching"
    assert got["column_name"] == "in_progress"


@pytest.mark.asyncio
async def test_the_drain_starts_the_head_once_the_runner_flags_a_manual_check(
        daemon, project, monkeypatch):
    """A card whose work is done and which waits only on a person's hand
    check counts as done for the queue: the next card starts while the
    flagged one stays In progress, its session still audible. End to end
    through the store's own `flag_manual`, not a hand-set field."""
    d, store = daemon
    opened: list = []
    _stub_spawn(d, monkeypatch, opened, project)
    shared = _plan(project, "shared", ["host/a.py"])
    running = _make(store, root=str(project), title="running", plan_path=shared)
    store.update(running["id"], {"link_state": "live", "session_id": "s1",
                                 "column_name": "in_progress"})
    _hear(d, "s1")
    waiting = _make(store, root=str(project), title="waiting", plan_path=shared)
    await d.dispatch_card(waiting["id"])
    assert store.get(waiting["id"])["queue_state"] == "queued"
    d._decide_queue_dispatches(store.cards())
    assert d._queue_candidates == []

    flagged, detail = store.flag_manual(running["id"], "s1",
                                        "1. Open the page and look.")
    assert flagged is not None, detail
    d._decide_queue_dispatches(store.cards())
    assert d._queue_candidates == [waiting["id"]]
    d._dispatch_attempts.pop(waiting["id"], None)
    await d._flush_queue_dispatches()
    assert opened == ["waiting"]
    assert store.get(waiting["id"])["link_state"] == "dispatching"
    assert store.get(running["id"])["column_name"] == "in_progress"


@pytest.mark.asyncio
async def test_the_drain_holds_on_a_transient_refusal_and_does_not_dequeue(
        daemon, project, monkeypatch):
    """The cooldown and the in-flight bounds pass on their own. Dequeuing on
    one would take a card out of the queue for a condition that was about to
    clear."""
    d, store = daemon
    opened: list = []
    _stub_spawn(d, monkeypatch, opened, project)
    card = _make(store, root=str(project), title="waiting",
                 plan_path=_plan(project, "a", ["host/a.py"]))
    store.update(card["id"], {"queue_state": "queued", "queued_at": 1.0})
    d._dispatch_attempts[card["id"]] = time.time()   # inside the cooldown

    ok, _ = await d.dispatch_card(card["id"], queued_replay=True)
    assert not ok
    assert opened == []
    got = store.get(card["id"])
    assert got["queue_state"] == "queued", "a transient refusal must hold"
    assert got["dispatch_error"] == ""


@pytest.mark.asyncio
async def test_the_drain_dequeues_with_an_error_on_a_hard_refusal(
        daemon, project, monkeypatch):
    """The queue may never hold a card it can never start, and an automatic
    action that fails must fail visibly."""
    d, store = daemon
    opened: list = []
    _stub_spawn(d, monkeypatch, opened, project)
    card = _make(store, root=str(project), title="toolless", tool="")
    store.update(card["id"], {"queue_state": "queued", "queued_at": 1.0})

    ok, detail = await d.dispatch_card(card["id"], queued_replay=True,
                                       allow_unplanned=True)
    assert not ok
    assert opened == []
    got = store.get(card["id"])
    assert got["queue_state"] == "", "a hard refusal must dequeue"
    assert got["dispatch_error"] == detail
    assert "which assistant" in got["dispatch_error"]


@pytest.mark.asyncio
async def test_a_queued_card_whose_plan_vanished_leaves_the_queue(
        daemon, project, monkeypatch):
    """The one plan-gate rung that survives a replay: a `plan_path` naming a
    file that has gone. A queued card is a confirmed card and the drain asks
    no confirmation again, but this is not a confirmation — there is nothing
    to hand the assistant, and `dispatch.start_prompt` would otherwise build
    `/ship implement <missing path>`. So the card leaves the queue with the
    "no plan yet" note, exactly as before."""
    d, store = daemon
    opened: list = []
    _stub_spawn(d, monkeypatch, opened, project)
    plan = _plan(project, "gone", ["host/a.py"])
    card = _make(store, root=str(project), title="planless", plan_path=plan)
    store.update(card["id"], {"queue_state": "queued", "queued_at": 1.0})
    import os
    os.unlink(plan)

    ok, detail = await d.dispatch_card(card["id"], queued_replay=True)
    assert not ok
    assert detail == PLAN_GATE_REFUSAL
    assert opened == []
    got = store.get(card["id"])
    assert got["queue_state"] == ""
    assert got["dispatch_error"] == detail


@pytest.mark.asyncio
async def test_the_drain_starts_a_queued_card_that_has_no_plan(
        daemon, project, monkeypatch):
    """A queued card is a confirmed card. The only way an unplanned card gets
    into the queue is a press confirmed past the plan gate, so the drain's
    replay must not ask "start without a plan?" a second time — and must not
    answer "no" on the person's behalf with an orange note."""
    d, store = daemon
    opened: list = []
    _stub_spawn(d, monkeypatch, opened, project)
    card = _make(store, root=str(project), title="unplanned")
    assert card["plan_path"] == ""
    store.update(card["id"], {"queue_state": "queued", "queued_at": 1.0})

    ok, detail = await d.dispatch_card(card["id"], queued_replay=True)
    assert ok, detail
    assert opened == ["unplanned"]
    got = store.get(card["id"])
    assert got["queue_state"] == ""
    assert got["link_state"] == "dispatching"
    assert got["column_name"] == "in_progress"
    assert got["dispatch_error"] == ""


@pytest.mark.asyncio
async def test_the_drain_starts_a_queued_card_whose_approved_plan_changed(
        daemon, project, monkeypatch):
    """The second confirmation, "start with a changed plan?", is the same
    story: the plan either matched at the press or the press was confirmed
    past it, and the queue is the record of that answer."""
    d, store = daemon
    opened: list = []
    _stub_spawn(d, monkeypatch, opened, project)
    card = _make(store, root=str(project), title="drifted",
                 plan_path=_plan(project, "drift", ["host/a.py"]))
    approved, detail = store.approve_plan(card["id"], card["plan_path"],
                                          "f" * 64, 1.0)
    assert approved is not None, detail
    store.update(card["id"], {"queue_state": "queued", "queued_at": 1.0})

    ok, detail = await d.dispatch_card(card["id"], queued_replay=True)
    assert ok, detail
    assert opened == ["drifted"]
    got = store.get(card["id"])
    assert got["queue_state"] == ""
    assert got["link_state"] == "dispatching"
    assert got["dispatch_error"] == ""


@pytest.mark.asyncio
async def test_a_persons_press_on_an_unplanned_card_is_still_confirmed(
        daemon, project, monkeypatch):
    """The hand-pressed route is byte-for-byte what it was: no replay, no
    `allow_unplanned`, so the gate asks — and a person's refusal writes
    nothing on the card."""
    d, store = daemon
    opened: list = []
    _stub_spawn(d, monkeypatch, opened, project)
    card = _make(store, root=str(project), title="unplanned")

    ok, detail = await d.dispatch_card(card["id"])
    assert not ok
    assert detail == PLAN_GATE_REFUSAL
    assert opened == []
    got = store.get(card["id"])
    assert got["queue_state"] == ""
    assert got["dispatch_error"] == ""
    assert got["column_name"] == "backlog"


@pytest.mark.asyncio
async def test_a_persons_press_on_a_changed_plan_is_still_confirmed(
        daemon, project, monkeypatch):
    """Same shape on the approval rung: a person pressing Start on a card
    whose approved plan has changed still gets the "plan changed" words."""
    d, store = daemon
    opened: list = []
    _stub_spawn(d, monkeypatch, opened, project)
    card = _make(store, root=str(project), title="drifted",
                 plan_path=_plan(project, "drift", ["host/a.py"]))
    approved, detail = store.approve_plan(card["id"], card["plan_path"],
                                          "f" * 64, 1.0)
    assert approved is not None, detail

    ok, detail = await d.dispatch_card(card["id"])
    assert not ok
    assert detail == PLAN_CHANGED_REFUSAL
    assert opened == []
    got = store.get(card["id"])
    assert got["queue_state"] == ""
    assert got["dispatch_error"] == ""


@pytest.mark.asyncio
async def test_either_switch_off_stops_the_drain_without_dequeuing(
        daemon, project, monkeypatch):
    """With the drain off the queue is still the truth about what a person
    asked for and in what order; only the press is missing."""
    d, store = daemon
    opened: list = []
    _stub_spawn(d, monkeypatch, opened, project)
    card = _make(store, root=str(project), title="waiting",
                 plan_path=_plan(project, "a", ["host/a.py"]))
    store.update(card["id"], {"queue_state": "queued", "queued_at": 1.0})

    d.board_autostart_enabled = False
    d._decide_queue_dispatches(store.cards())
    assert d._queue_candidates == []
    d._queue_candidates = [card["id"]]
    await d._flush_queue_dispatches()
    assert opened == []
    assert store.get(card["id"])["queue_state"] == "queued"

    d.board_autostart_enabled = True
    d.board_dispatch_enabled = False
    d._decide_queue_dispatches(store.cards())
    assert d._queue_candidates == []
    d._queue_candidates = [card["id"]]
    await d._flush_queue_dispatches()
    assert opened == []
    assert store.get(card["id"])["queue_state"] == "queued"


@pytest.mark.asyncio
async def test_unqueue_clears_the_slot_and_refuses_a_card_that_is_not_queued(
        daemon, project):
    d, store = daemon
    card = _make(store, root=str(project))
    ok, detail = await d.unqueue_card(card["id"])
    assert not ok and "not queued" in detail
    store.update(card["id"], {"queue_state": "queued", "queued_at": 1.0})
    ok, _ = await d.unqueue_card(card["id"])
    assert ok
    got = store.get(card["id"])
    assert got["queue_state"] == "" and got["queued_at"] is None
    ok, detail = await d.unqueue_card("nope")
    assert not ok and "no such card" in detail


@pytest.mark.asyncio
async def test_the_drain_picks_at_most_one_card_per_project(
        daemon, project, monkeypatch):
    """Binding by elimination needs "the first new session in this project" to
    be singular; `dispatch.guard`'s per-project refusal enforces it a second
    time downstream."""
    d, store = daemon
    for n, files in enumerate((["host/a.py"], ["panel/b.swift"])):
        c = _make(store, root=str(project), title=f"q{n}",
                  plan_path=_plan(project, f"p{n}", files))
        store.update(c["id"], {"queue_state": "queued", "queued_at": float(n)})
    other = _make(store, root=str(project), title="elsewhere", project="other",
                  plan_path=_plan(project, "p9", ["host/a.py"]))
    store.update(other["id"], {"queue_state": "queued", "queued_at": 0.5})
    monkeypatch.setattr(d, "_known_project_roots",
                        lambda: {str(project)})

    d._decide_queue_dispatches(store.cards())
    assert len(d._queue_candidates) == 2, "one per project, both projects"


@pytest.mark.asyncio
async def test_the_queue_survives_a_store_reopen(tmp_path):
    """Membership and order live in `board.db`, so a restart needs no
    bookkeeping anywhere."""
    path = tmp_path / "board.db"
    store = BoardStore(path)
    store.connect()
    a = _make(store, title="first")
    b = _make(store, title="second")
    store.update(a["id"], {"queue_state": "queued", "queued_at": 10.0})
    store.update(b["id"], {"queue_state": "queued", "queued_at": 20.0})
    store.close()

    again = BoardStore(path)
    again.connect()
    try:
        assert [c["title"] for c in again.queued_cards("bob")] == \
            ["first", "second"]
    finally:
        again.close()


@pytest.mark.asyncio
async def test_two_colliding_queued_cards_the_head_starts(
        daemon, project, monkeypatch):
    """The head of a queue is held only by what is *ahead* of it.

    Held against the cards behind it too, each of a colliding pair is in the
    other's way and neither ever starts — the decision names the head, the
    re-check refuses it, and `_enqueue_card` returns quietly on a replay, so
    the pair spins at reconcile cadence in silence. Confirmed as a live bug on
    23 Aug 2026; this is the test that was missing, the existing two-card
    drain test having given its cards non-overlapping files.
    """
    d, store = daemon
    opened: list = []
    _stub_spawn(d, monkeypatch, opened, project)
    shared = _plan(project, "shared", ["host/a.py"])
    first = _make(store, root=str(project), title="first", plan_path=shared)
    second = _make(store, root=str(project), title="second", plan_path=shared)
    store.update(first["id"], {"queue_state": "queued", "queued_at": 10.0})
    store.update(second["id"], {"queue_state": "queued", "queued_at": 20.0})

    d._decide_queue_dispatches(store.cards())
    assert d._queue_candidates == [first["id"]]
    await d._flush_queue_dispatches()

    assert opened, "the head must start when nothing is running"
    got = store.get(first["id"])
    assert got["queue_state"] == "" and got["queued_at"] is None
    assert got["link_state"] == "dispatching"
    behind = store.get(second["id"])
    assert behind["queue_state"] == "queued", "the one behind keeps waiting"


@pytest.mark.asyncio
async def test_a_fresh_press_still_queues_behind_an_already_queued_card(
        daemon, project, monkeypatch):
    """The other half of the ordering rule, and the more expensive one to get
    wrong: a card nobody has queued yet is joining the *back* of the line, so
    every queued card is ahead of it. Left to the sort key this fails silently
    — an unqueued card's `queued_at` is None, which as 0.0 sorts first and
    would let a press jump the whole queue."""
    d, store = daemon
    opened: list = []
    _stub_spawn(d, monkeypatch, opened, project)
    shared = _plan(project, "shared", ["host/a.py"])
    waiting = _make(store, root=str(project), title="waiting", plan_path=shared)
    store.update(waiting["id"], {"queue_state": "queued", "queued_at": 10.0})
    pressed = _make(store, root=str(project), title="pressed", plan_path=shared)

    ok, detail = await d.dispatch_card(pressed["id"])

    assert not ok
    assert detail.startswith("Queued —")
    assert opened == [], "a press may not jump the queue"
    assert store.get(pressed["id"])["queue_state"] == "queued"


# --- the dial --------------------------------------------------------------


def test_slot_head_is_the_count_and_nothing_else():
    """The policy, in the file where the policy is auditable with no I/O."""
    rows = [{"id": "a"}, {"id": "b"}]
    assert board_queue.slot_head(rows, 0, 1) is rows[0]
    assert board_queue.slot_head(rows, 1, 1) is None
    assert board_queue.slot_head(rows, 1, 2) is rows[0]
    assert board_queue.slot_head(rows, 3, 2) is None
    assert board_queue.slot_head([], 0, 4) is None
    # Floored at one: a zero would park every queue for ever, which is
    # `board_autostart` off wearing a number's clothes.
    assert board_queue.slot_head(rows, 0, 0) is rows[0]
    assert board_queue.slot_head(rows, 1, 0) is None


# --- reorder: queue_rank, move_queued, the drain obeys the key --------------


@pytest.fixture
def store(tmp_path):
    s = BoardStore(tmp_path / "board.db")
    s.connect()
    try:
        yield s
    finally:
        s.close()


def test_move_queued_places_before_a_target_and_interleaves_ranks(store):
    """A hand-set rank lives on the enqueue-stamp axis, so NULL and non-NULL
    interleave. The stamp itself is never restamped."""
    a = _make(store, title="a")
    b = _make(store, title="b")
    c = _make(store, title="c")
    store.update(a["id"], {"queue_state": "queued", "queued_at": 10.0})
    store.update(b["id"], {"queue_state": "queued", "queued_at": 20.0})
    store.update(c["id"], {"queue_state": "queued", "queued_at": 30.0})
    assert [x["title"] for x in store.queued_cards("bob")] == ["a", "b", "c"]
    got, detail = store.move_queued(c["id"], a["id"])
    assert got is not None, detail
    assert [x["title"] for x in store.queued_cards("bob")] == ["c", "a", "b"]
    assert got["queue_rank"] == 9.0
    assert got["queued_at"] == 30.0


def test_move_queued_empty_before_id_appends(store):
    a = _make(store, title="a")
    b = _make(store, title="b")
    store.update(a["id"], {"queue_state": "queued", "queued_at": 10.0})
    store.update(b["id"], {"queue_state": "queued", "queued_at": 20.0})
    got, _ = store.move_queued(a["id"], "")
    assert got is not None
    assert [x["title"] for x in store.queued_cards("bob")] == ["b", "a"]
    assert got["queue_rank"] == pytest.approx(20.0 + 1e-3)


def test_an_append_then_a_start_still_lands_at_the_back(store):
    """+1.0 on an enqueue stamp would be up to a second in the future, and a
    Start in that window would jump the card just put last. The step is 1e-3."""
    a = _make(store, title="a")
    b = _make(store, title="b")
    now = time.time()
    store.update(a["id"], {"queue_state": "queued", "queued_at": now})
    store.update(b["id"], {"queue_state": "queued", "queued_at": now + 0.01})
    store.move_queued(a["id"], "")
    later = _make(store, title="later")
    store.update(later["id"], {"queue_state": "queued", "queued_at": now + 0.5})
    assert [x["title"] for x in store.queued_cards("bob")] == ["b", "a", "later"]


def test_move_queued_refusals(store):
    a = _make(store, title="a")
    b = _make(store, title="b")
    other = _make(store, title="other", project="other", root="/tmp/other")
    store.update(a["id"], {"queue_state": "queued", "queued_at": 10.0})
    store.update(b["id"], {"queue_state": "queued", "queued_at": 20.0})
    store.update(other["id"], {"queue_state": "queued", "queued_at": 15.0})
    got, detail = store.move_queued("nope", a["id"])
    assert got is None and "no such card" in detail
    idle = _make(store, title="idle")
    got, detail = store.move_queued(idle["id"], a["id"])
    assert got is None and "not queued" in detail
    got, detail = store.move_queued(a["id"], "gone")
    assert got is None and "no longer queued" in detail
    got, detail = store.move_queued(a["id"], other["id"])
    assert got is None and "no longer queued" in detail


def test_move_queued_before_self_is_an_ok_noop(store):
    a = _make(store, title="a")
    store.update(a["id"], {"queue_state": "queued", "queued_at": 10.0})
    got, detail = store.move_queued(a["id"], a["id"])
    assert got is not None and detail == "ok"
    assert got["queue_rank"] is None
    assert got["queued_at"] == 10.0


def test_move_queued_refuses_when_the_card_leaves_between_read_and_write(store):
    card = _make(store, title="a")
    store.update(card["id"], {"queue_state": "queued", "queued_at": 1.0})
    real_queued = store.queued_cards

    def unqueue_during_read(project=None):
        rows = real_queued(project)
        store.update(card["id"], {"queue_state": "", "queued_at": None})
        return rows

    store.queued_cards = unqueue_during_read
    got, detail = store.move_queued(card["id"], "")
    assert got is None
    assert "moved" in detail


def test_gap_exhaustion_reindexes_only_this_project(store):
    a = _make(store, title="a")
    b = _make(store, title="b")
    c = _make(store, title="c")
    other = _make(store, title="elsewhere", project="other", root="/tmp/other")
    store.update(a["id"], {"queue_state": "queued", "queued_at": 1.0})
    store.update(b["id"], {"queue_state": "queued", "queued_at": 2.0})
    store.update(c["id"], {"queue_state": "queued", "queued_at": 3.0})
    store.update(other["id"], {"queue_state": "queued", "queued_at": 50.0})
    store._conn.execute(
        "UPDATE cards SET queue_rank = 1.0 WHERE id = ?", (a["id"],))
    store._conn.execute(
        "UPDATE cards SET queue_rank = ? WHERE id = ?",
        (1.0 + 1e-7, b["id"]))
    store._conn.execute(
        "UPDATE cards SET queue_rank = 3.0 WHERE id = ?", (c["id"],))
    store._conn.commit()
    got, detail = store.move_queued(c["id"], b["id"])
    assert got is not None, detail
    assert [x["title"] for x in store.queued_cards("bob")] == ["a", "c", "b"]
    assert store.get(a["id"])["queue_rank"] == 1.0
    assert store.get(b["id"])["queue_rank"] == 2.0
    assert store.get(other["id"])["queue_rank"] is None
    assert store.get(other["id"])["queued_at"] == 50.0
    later = _make(store, title="later")
    store.update(later["id"], {"queue_state": "queued",
                               "queued_at": time.time()})
    assert [x["title"] for x in store.queued_cards("bob")][-1] == "later"


def test_leaving_the_queue_clears_the_rank(store):
    """unqueue, a hand-drag, a dispatch-success write and a hard refusal
    all write `queue_state: ""` through `update`, so the store drops the
    rank with zero extra daemon edits."""
    card = _make(store, title="a")
    store.update(card["id"], {"queue_state": "queued", "queued_at": 10.0})
    store.move_queued(card["id"], "")
    assert store.get(card["id"])["queue_rank"] is not None
    store.update(card["id"], {"queue_state": "", "queued_at": None})
    assert store.get(card["id"])["queue_rank"] is None

    store.update(card["id"], {"queue_state": "queued", "queued_at": 11.0})
    store.move_queued(card["id"], "")
    moved, _ = store.update(card["id"], {"column_name": "in_progress"})
    assert moved["queue_rank"] is None
    assert moved["queue_state"] == ""


def test_queue_key_agrees_with_queued_cards_sql_order(store):
    """One order, two spellings: the SQL COALESCE and Python `queue_key`."""
    a = _make(store, title="a")
    b = _make(store, title="b")
    c = _make(store, title="c")
    store.update(a["id"], {"queue_state": "queued", "queued_at": 10.0})
    store.update(b["id"], {"queue_state": "queued", "queued_at": 20.0})
    store.update(c["id"], {"queue_state": "queued", "queued_at": 30.0})
    store.move_queued(c["id"], a["id"])
    rows = store.queued_cards("bob")
    assert [x["id"] for x in rows] == \
        [x["id"] for x in sorted(rows, key=board_queue.queue_key)]
    assert [x["title"] for x in rows] == ["c", "a", "b"]


@pytest.mark.asyncio
async def test_unqueue_card_clears_the_rank(daemon):
    d, store = daemon
    card = _make(store, title="a")
    store.update(card["id"], {"queue_state": "queued", "queued_at": 10.0})
    store.move_queued(card["id"], "")
    assert store.get(card["id"])["queue_rank"] is not None
    ok, _ = await d.unqueue_card(card["id"])
    assert ok
    assert store.get(card["id"])["queue_rank"] is None


@pytest.mark.asyncio
async def test_dispatch_success_clears_the_rank(
        daemon, project, monkeypatch):
    d, store = daemon
    opened: list = []
    _stub_spawn(d, monkeypatch, opened, project)
    card = _make(store, root=str(project), title="waiting",
                 plan_path=_plan(project, "a", ["host/a.py"]))
    store.update(card["id"], {"queue_state": "queued", "queued_at": 1.0})
    store.move_queued(card["id"], "")
    assert store.get(card["id"])["queue_rank"] is not None
    ok, _ = await d.dispatch_card(card["id"], queued_replay=True)
    assert ok
    got = store.get(card["id"])
    assert got["queue_state"] == "" and got["queue_rank"] is None


@pytest.mark.asyncio
async def test_hard_refusal_clears_the_rank(daemon, project, monkeypatch):
    d, store = daemon
    opened: list = []
    _stub_spawn(d, monkeypatch, opened, project)
    card = _make(store, root=str(project), title="toolless", tool="")
    store.update(card["id"], {"queue_state": "queued", "queued_at": 1.0})
    store.move_queued(card["id"], "")
    assert store.get(card["id"])["queue_rank"] is not None
    ok, _ = await d.dispatch_card(card["id"], queued_replay=True,
                                  allow_unplanned=True)
    assert not ok
    got = store.get(card["id"])
    assert got["queue_state"] == "" and got["queue_rank"] is None


@pytest.mark.asyncio
async def test_promoting_the_third_disjoint_card_makes_it_the_drain_head(
        daemon, project, monkeypatch):
    d, store = daemon
    _stub_spawn(d, monkeypatch, [], project)
    cards = []
    for n, files in enumerate((["host/a.py"], ["panel/b.swift"],
                               ["tools/c.py"])):
        c = _make(store, root=str(project), title=f"q{n}",
                  plan_path=_plan(project, f"p{n}", files))
        store.update(c["id"], {"queue_state": "queued",
                               "queued_at": float(10 + n)})
        cards.append(c)
    store.move_queued(cards[2]["id"], cards[0]["id"])
    d._decide_queue_dispatches(store.cards())
    assert d._queue_candidates == [cards[2]["id"]]


@pytest.mark.asyncio
async def test_a_promoted_card_becomes_the_head_whatever_it_declares(
        daemon, project, monkeypatch):
    """Reordering feeds `slot_head` a different input order, never a different
    rule — and the rule no longer looks at files, so the card a person
    promoted is the head even where it declares exactly what the running one
    does. One free place makes it go; none holds the whole line."""
    d, store = daemon
    d.set_board_parallel(2)
    opened: list = []
    _stub_spawn(d, monkeypatch, opened, project)
    shared = _plan(project, "shared", ["host/a.py"])
    running = _make(store, root=str(project), title="running",
                    plan_path=shared)
    store.update(running["id"], {"link_state": "live", "session_id": "s1",
                                 "column_name": "in_progress"})
    _hear(d, "s1")
    colliding = _make(store, root=str(project), title="colliding",
                      plan_path=shared)
    disjoint = _make(store, root=str(project), title="disjoint",
                     plan_path=_plan(project, "other", ["panel/b.swift"]))
    store.update(disjoint["id"], {"queue_state": "queued", "queued_at": 10.0})
    store.update(colliding["id"], {"queue_state": "queued", "queued_at": 20.0})
    store.move_queued(colliding["id"], disjoint["id"])
    d._decide_queue_dispatches(store.cards())
    assert d._queue_candidates == [colliding["id"]]

    # And with the one place already taken, nothing goes at all.
    d.set_board_parallel(1)
    d._decide_queue_dispatches(store.cards())
    assert d._queue_candidates == []


@pytest.mark.asyncio
async def test_ranked_colliding_pair_the_ranked_head_starts(
        daemon, project, monkeypatch):
    """Ranked variant of the deadlock test: the ahead-cut uses `queue_key`,
    so a promoted card is held only by what is ahead of it, not by the
    card behind it."""
    d, store = daemon
    opened: list = []
    _stub_spawn(d, monkeypatch, opened, project)
    shared = _plan(project, "shared", ["host/a.py"])
    first = _make(store, root=str(project), title="first", plan_path=shared)
    second = _make(store, root=str(project), title="second", plan_path=shared)
    store.update(first["id"], {"queue_state": "queued", "queued_at": 10.0})
    store.update(second["id"], {"queue_state": "queued", "queued_at": 20.0})
    store.move_queued(second["id"], first["id"])
    d._decide_queue_dispatches(store.cards())
    assert d._queue_candidates == [second["id"]]
    await d._flush_queue_dispatches()
    assert opened, "the ranked head must start when nothing is running"
    got = store.get(second["id"])
    assert got["queue_state"] == "" and got["queued_at"] is None
    behind = store.get(first["id"])
    assert behind["queue_state"] == "queued"


@pytest.mark.asyncio
async def test_a_full_project_still_hard_refuses_a_card_guard_would_refuse(
        daemon, project, monkeypatch):
    """The queue gate runs *after* `guard` (2026-09-01). With the order the
    other way round, a full project turned every hard refusal — here, no
    assistant named — into a queued card that would sit in the line for ever,
    because the drain re-runs the same guard and dequeues it only on replay.
    Refused in guard's own words, and never queued."""
    d, store = daemon
    opened: list = []
    _stub_spawn(d, monkeypatch, opened, project)
    shared = _plan(project, "shared", ["host/a.py"])
    running = _make(store, root=str(project), title="running", plan_path=shared)
    store.update(running["id"], {"link_state": "live", "session_id": "s1",
                                 "column_name": "in_progress"})
    _hear(d, "s1")
    toolless = _make(store, root=str(project), title="toolless", tool="",
                     plan_path=shared)

    ok, detail = await d.dispatch_card(toolless["id"])

    assert not ok
    assert detail == "this card does not say which assistant should take it"
    assert opened == []
    got = store.get(toolless["id"])
    assert got["queue_state"] == "", "a card guard hard-refuses must not queue"
    assert got["queued_at"] is None


def test_the_three_binders_share_one_candidate_predicate(daemon, monkeypatch):
    """`_candidate_matches` is the one join (2026-09-01): the dispatch binder,
    the refine binder and the consult binder all ask it, so the predicates
    cannot drift apart again (a pid miss used to be a refusal in one of the
    three and a skip in the others)."""
    d, store = daemon
    calls: list = []

    def spy(row, **kw):
        calls.append(dict(kw))
        return "no"

    monkeypatch.setattr(d, "_candidate_matches", spy)
    snapshot = {"running": [{"session_id": "sX", "kind": "interactive",
                             "provider": "claude", "project": "bob"}],
                "waiting": [], "sleeping": []}

    card = {"id": "c1", "tool": "claude", "project": "bob", "root": "/tmp/bob",
            "dispatched_at": time.time(), "plan_path": ""}
    d._bind_dispatched_card(card, snapshot, time.time())
    d._bind_refining_card(card, snapshot, time.time())
    entry = {"baseline": set(), "project": "bob", "root": "/tmp/bob",
             "started": time.time(), "shell_pid": None}
    d._consult_accepts(entry, snapshot["running"][0])

    assert len(calls) == 3, "all three binders must consult the shared predicate"


# --- What the phone is told the pipeline controls can do here --------------


class _PreferenceObserver:
    def on_preference_request(self, key, value):  # pragma: no cover - a stub
        pass


def _writability(state):
    return state["queue_writable"], state["preferences_writable"]


def test_the_snapshot_states_the_two_pipeline_flags(daemon):
    """Stated on every shape, under the file's never-a-missing-key rule: a
    surface must never handle an absent key, and absent must never read as a
    control that is drawn and silently does nothing."""
    d, _store = daemon
    # `queue_writable` is a version marker — this daemon carries the two
    # queue verbs on the phone's own tuples. `preferences_writable` is a live
    # fact: with no menu-bar app there is nobody to store a press.
    assert _writability(d._build_board_state()) == (True, False)

    d.add_observer(_PreferenceObserver())
    assert _writability(d._build_board_state()) == (True, True)


def test_the_flags_are_on_the_board_shut_and_read_failed_shapes_too(daemon):
    d, store = daemon
    observer = _PreferenceObserver()
    d.add_observer(observer)

    live = d._build_board_state()
    d._board = None
    shut = d._build_board_state()
    assert shut["available"] is False
    assert _writability(shut) == (True, True)

    # The read-failed fallback: a store that raises, with no banked state to
    # copy forward, so the literal dict is what is returned.
    class _Broken:
        def done_scope(self):
            raise RuntimeError("no")

    d._board = _Broken()
    d._board_state = {}
    failed = d._build_board_state()
    assert failed["available"] is False
    assert _writability(failed) == (True, True)

    d._board = store
    assert _writability(d._build_board_state()) == _writability(live)


# --- a card that started itself joins the same line ---------------------------


@pytest.mark.asyncio
async def test_an_auto_start_on_a_full_project_queues_and_the_drain_starts_it(
        daemon, project, monkeypatch):
    """The auto-start is `dispatch_card` and nothing else, so a full project
    queues it exactly as a press would — no new code on this path."""
    d, store = daemon
    opened: list = []
    _stub_spawn(d, monkeypatch, opened, project)
    running = _make(store, root=str(project), title="running")
    store.update(running["id"], {"link_state": "live", "session_id": "s1",
                                 "column_name": "in_progress"})
    _hear(d, "s1")

    # A ticked Prep card whose plan a refinement now attaches.
    plan = project / "plans" / "auto.md"
    plan.write_text("# auto\n\n- **Stages:** bc-implementer\n")
    card = _make(store, root=str(project), title="auto",
                 column_name="prep", start_when_planned="1")
    store.update(card["id"], {"refine_session_id": "planner-1",
                              "refine_state": "live"})
    got, detail = await d.attach_plan_by_session("planner-1", str(plan))
    assert got is not None, detail
    await asyncio.gather(*list(d._auto_start_tasks))

    queued = store.get(card["id"])
    assert queued["queue_state"] == "queued"
    assert queued["dispatch_error"] == ""
    assert opened == []
    # The same sentence a hand press would have got, composed per frame.
    state = d._build_board_state()
    by_id = {c["id"]: c for c in state["cards"]}
    assert by_id[card["id"]]["queue_reason"]

    # A place frees up: the drain starts it, with no further human press.
    store.update(running["id"], {"link_state": "ended"})
    d._decide_queue_dispatches(store.cards())
    assert d._queue_candidates == [card["id"]]
    d._dispatch_attempts.pop(card["id"], None)
    await d._flush_queue_dispatches()
    assert opened == ["auto"]
    after = store.get(card["id"])
    assert after["column_name"] == "in_progress"
    assert after["link_state"] == "dispatching"


# --- card dependencies: the gate queues, the drain starts it once met ---------
#
# `docs/card-dependencies.md`. Every case drives `dispatch_card` with
# `dispatch.spawn` stubbed, so "no terminal opened" and "it started" are
# asserted where they can be: on the list the stub appends to.

from dark_army_daemon import board as board_mod            # noqa: E402
from dark_army_daemon.daemon import _already_queued_reason  # noqa: E402


def _dependent_pair(store, project, *, dep_column="backlog"):
    """A planned card and the planned card it waits on, one project."""
    dep = _make(store, root=str(project), title="the foundation",
                plan_path=_plan(project, "foundation", ["host/a.py"]))
    if dep_column != "backlog":
        store.update(dep["id"], {"column_name": dep_column})
    card = _make(store, root=str(project), title="the waiter",
                 plan_path=_plan(project, "waiter", ["host/b.py"]))
    got, detail = store.update(card["id"], {"blocked_by": dep["id"]})
    assert got is not None, detail
    return dep, card


@pytest.mark.asyncio
async def test_start_on_a_card_whose_dependency_is_not_done_queues_it_and_names_the_dependency(
        daemon, project, monkeypatch):
    """The success criterion's first half: the press is accepted, the card
    is queued where it stands, the sentence names what it waits for, and no
    terminal opens — although the project has a free place."""
    d, store = daemon
    opened: list = []
    _stub_spawn(d, monkeypatch, opened, project)
    dep, card = _dependent_pair(store, project)
    assert store.get(card["id"])["queue_state"] == ""

    ok, detail = await d.dispatch_card(card["id"])
    assert not ok
    assert detail == ('Queued — Dark Army will start it once "the foundation" '
                      "is done")
    assert opened == [], "a card waiting on another must not open a terminal"
    got = store.get(card["id"])
    assert got["queue_state"] == "queued"
    assert got["column_name"] == "backlog"
    # The badge a frame later is the same sentence.
    state = d._build_board_state()
    by_id = {c["id"]: c for c in state["cards"]}
    assert by_id[card["id"]]["queue_reason"] == detail


@pytest.mark.asyncio
async def test_a_second_press_on_a_dependency_held_card_says_it_is_already_queued(
        daemon, project, monkeypatch):
    d, store = daemon
    opened: list = []
    _stub_spawn(d, monkeypatch, opened, project)
    _dep, card = _dependent_pair(store, project)
    await d.dispatch_card(card["id"])
    stamp = store.get(card["id"])["queued_at"]

    ok, detail = await d.dispatch_card(card["id"])
    assert not ok
    assert detail == _already_queued_reason(1, True)
    assert store.get(card["id"])["queued_at"] == stamp
    assert opened == []


@pytest.mark.asyncio
async def test_the_drain_holds_a_dependency_held_card_silently(
        daemon, project, monkeypatch):
    """No candidate while the dependency is unfinished, and a replay says
    nothing and writes nothing — the card stays queued, no orange line."""
    d, store = daemon
    opened: list = []
    _stub_spawn(d, monkeypatch, opened, project)
    _dep, card = _dependent_pair(store, project)
    await d.dispatch_card(card["id"])
    assert store.get(card["id"])["queue_state"] == "queued"

    d._decide_queue_dispatches(store.cards())
    assert d._queue_candidates == []
    before = store.get(card["id"])
    ok, detail = await d.dispatch_card(card["id"], queued_replay=True)
    assert (ok, detail) == (False, "")
    after = store.get(card["id"])
    assert after["queue_state"] == "queued"
    assert after["dispatch_error"] == ""
    assert after["queued_at"] == before["queued_at"]
    assert opened == []


@pytest.mark.asyncio
async def test_a_dependency_held_card_starts_when_done(
        daemon, project, monkeypatch):
    """The success criterion's second half: the dependency reaches Done and
    the next reconcile starts the queued card, with no second press."""
    d, store = daemon
    opened: list = []
    _stub_spawn(d, monkeypatch, opened, project)
    dep, card = _dependent_pair(store, project)
    await d.dispatch_card(card["id"])
    assert store.get(card["id"])["queue_state"] == "queued"
    assert opened == []

    store.update(dep["id"], {"column_name": "done"})
    d._decide_queue_dispatches(store.cards())
    assert d._queue_candidates == [card["id"]]
    await d._flush_queue_dispatches()
    assert opened == ["the waiter"]
    got = store.get(card["id"])
    assert got["queue_state"] == "" and got["link_state"] == "dispatching"
    assert got["column_name"] == "in_progress"


@pytest.mark.asyncio
async def test_a_dependency_held_card_starts_on_manual_check(
        daemon, project, monkeypatch):
    """Finished and waiting only on a person's check counts as met — the
    moment the dependency's own MANUAL CHECK badge lights, not when it
    reaches Done."""
    d, store = daemon
    opened: list = []
    _stub_spawn(d, monkeypatch, opened, project)
    dep, card = _dependent_pair(store, project)
    store.update(dep["id"], {"link_state": "live", "session_id": "s1",
                             "column_name": "in_progress"})
    _hear(d, "s1")
    await d.dispatch_card(card["id"])
    assert store.get(card["id"])["queue_state"] == "queued"
    d._decide_queue_dispatches(store.cards())
    assert d._queue_candidates == []

    flagged, detail = store.flag_manual(dep["id"], "s1", "1. Open it and look.")
    assert flagged is not None, detail
    # The session is heard but not in the `running` bucket: it has stopped.
    d._agents_snapshot_cache = {"running": [], "waiting": [{"session_id": "s1"}]}
    d._decide_queue_dispatches(store.cards())
    assert d._queue_candidates == [card["id"]]
    await d._flush_queue_dispatches()
    assert opened == ["the waiter"]
    assert store.get(dep["id"])["column_name"] == "in_progress"


@pytest.mark.asyncio
async def test_a_dependency_flagged_while_its_session_still_works_holds(
        daemon, project, monkeypatch):
    """A flag raised mid-turn is not finished: while the dependency's
    session is in the `running` bucket and heard, the card keeps waiting —
    the badge's own predicate, so the tile and the gate agree."""
    d, store = daemon
    opened: list = []
    _stub_spawn(d, monkeypatch, opened, project)
    dep, card = _dependent_pair(store, project)
    store.update(dep["id"], {"link_state": "live", "session_id": "s1",
                             "column_name": "in_progress"})
    _hear(d, "s1")
    store.flag_manual(dep["id"], "s1", "1. Open it and look.")
    d._agents_snapshot_cache = {"running": [{"session_id": "s1"}]}

    ok, detail = await d.dispatch_card(card["id"])
    assert not ok and "the foundation" in detail
    assert store.get(card["id"])["queue_state"] == "queued"
    d._decide_queue_dispatches(store.cards())
    assert d._queue_candidates == []
    assert opened == []
    by_id = {c["id"]: c for c in d._build_board_state()["cards"]}
    assert by_id[card["id"]]["dependencies"][0]["met"] is False


@pytest.mark.asyncio
async def test_a_deleted_dependency_counts_as_met(
        daemon, project, monkeypatch):
    d, store = daemon
    opened: list = []
    _stub_spawn(d, monkeypatch, opened, project)
    dep, card = _dependent_pair(store, project)
    ok, _detail = store.delete(dep["id"])
    assert ok
    assert store.get(card["id"])["queue_state"] == ""

    ok, detail = await d.dispatch_card(card["id"])
    assert ok, detail
    assert opened == ["the waiter"]
    # The stored id is left alone: a restore should still mean something.
    assert dep["id"] in board_mod.parse_ids(store.get(card["id"])["blocked_by"])


@pytest.mark.asyncio
async def test_a_dependency_held_card_never_blocks_the_card_behind_it(
        daemon, project, monkeypatch):
    """A waits on B and was queued first. Were A counted as "ahead", B could
    never start and A would wait for B for ever — the 23 Aug deadlock one
    rung on. B starts; A stays queued."""
    d, store = daemon
    opened: list = []
    _stub_spawn(d, monkeypatch, opened, project)
    running = _make(store, root=str(project), title="running",
                    plan_path=_plan(project, "running", ["host/r.py"]))
    store.update(running["id"], {"link_state": "live", "session_id": "s1",
                                 "column_name": "in_progress"})
    _hear(d, "s1")
    b = _make(store, root=str(project), title="B",
              plan_path=_plan(project, "b", ["host/b.py"]))
    a = _make(store, root=str(project), title="A",
              plan_path=_plan(project, "a", ["host/a.py"]))
    store.update(a["id"], {"blocked_by": b["id"]})
    await d.dispatch_card(a["id"])
    await d.dispatch_card(b["id"])
    assert store.get(a["id"])["queue_state"] == "queued"
    assert store.get(b["id"])["queue_state"] == "queued"
    assert store.get(a["id"])["queued_at"] <= store.get(b["id"])["queued_at"]

    store.update(running["id"], {"link_state": "ended"})
    d._decide_queue_dispatches(store.cards())
    assert d._queue_candidates == [b["id"]]
    await d._flush_queue_dispatches()
    assert opened == ["B"]
    assert store.get(a["id"])["queue_state"] == "queued"


@pytest.mark.asyncio
async def test_a_fresh_press_is_not_held_behind_a_dependency_held_card(
        daemon, project, monkeypatch):
    """The "no overtaking" rung counts only cards that could start: with a
    free place, a press on B starts at once although A — waiting on B — is
    already in the line."""
    d, store = daemon
    opened: list = []
    _stub_spawn(d, monkeypatch, opened, project)
    b = _make(store, root=str(project), title="B",
              plan_path=_plan(project, "b", ["host/b.py"]))
    a = _make(store, root=str(project), title="A",
              plan_path=_plan(project, "a", ["host/a.py"]))
    store.update(a["id"], {"blocked_by": b["id"]})
    await d.dispatch_card(a["id"])
    assert store.get(a["id"])["queue_state"] == "queued"

    ok, detail = await d.dispatch_card(b["id"])
    assert ok, detail
    assert opened == ["B"]


@pytest.mark.asyncio
async def test_a_dependency_hold_in_one_project_leaves_another_alone(
        daemon, project, monkeypatch):
    d, store = daemon
    opened: list = []
    _stub_spawn(d, monkeypatch, opened, project)
    _dep, card = _dependent_pair(store, project)
    await d.dispatch_card(card["id"])
    other = _make(store, root=str(project), title="elsewhere", project="other",
                  plan_path=_plan(project, "other", ["host/o.py"]))
    other_q = _make(store, root=str(project), title="other queued",
                    project="other",
                    plan_path=_plan(project, "other-q", ["host/q.py"]))
    store.update(other_q["id"], {"queue_state": "queued", "queued_at": 1.0})

    d._decide_queue_dispatches(store.cards())
    assert d._queue_candidates == [other_q["id"]]
    ok, detail = await d.dispatch_card(other["id"])
    assert not ok  # behind its own project's queued card, not the hold
    assert store.get(card["id"])["queue_state"] == "queued"


@pytest.mark.asyncio
async def test_the_ninth_press_on_a_dependency_held_card_is_refused_in_the_stores_words(
        daemon, project, monkeypatch):
    """`MAX_QUEUED_PER_PROJECT` stands: the ninth press is refused with the
    store's sentence and the card is left with no `queue_state`."""
    d, store = daemon
    opened: list = []
    _stub_spawn(d, monkeypatch, opened, project)
    _dep, card = _dependent_pair(store, project)
    for i in range(board_mod.MAX_QUEUED_PER_PROJECT):
        filler = _make(store, root=str(project), title=f"filler {i}")
        store.update(filler["id"], {"queue_state": "queued",
                                    "queued_at": float(i + 1)})

    ok, detail = await d.dispatch_card(card["id"])
    assert not ok
    assert detail == (f"bob already has {board_mod.MAX_QUEUED_PER_PROJECT} "
                      "cards queued")
    assert store.get(card["id"])["queue_state"] == ""
    assert opened == []


@pytest.mark.asyncio
async def test_start_project_queues_a_dependency_held_card_and_reports_it(
        daemon, project, monkeypatch):
    d, store = daemon
    opened: list = []
    _stub_spawn(d, monkeypatch, opened, project)
    dep, card = _dependent_pair(store, project)

    ok, detail = await d.start_project(str(project))
    assert ok, detail
    assert detail.splitlines()[0] == "2 started or queued"
    assert opened == ["the foundation"]
    assert store.get(card["id"])["queue_state"] == "queued"
    assert store.get(dep["id"])["link_state"] == "dispatching"


def _flagged_dependency(d, store, project):
    """A dependency bound to `s1`, flagged for a manual check by `s1`."""
    dep, card = _dependent_pair(store, project)
    store.update(dep["id"], {"link_state": "live", "session_id": "s1",
                             "column_name": "in_progress"})
    _hear(d, "s1")
    flagged, detail = store.flag_manual(dep["id"], "s1", "1. Open it and look.")
    assert flagged is not None, detail
    assert flagged["manual_session_id"] == "s1"
    return dep, card


@pytest.mark.asyncio
async def test_a_dependency_flagged_then_reset_to_backlog_holds(
        daemon, project, monkeypatch):
    """The check failed and the card went back to Backlog; its steps stay on
    the card, but it is not finished."""
    d, store = daemon
    opened: list = []
    _stub_spawn(d, monkeypatch, opened, project)
    dep, card = _flagged_dependency(d, store, project)
    store.update(dep["id"], {"column_name": "backlog", "link_state": "",
                             "session_id": ""})
    d._agents_snapshot_cache = {"running": [], "waiting": []}
    assert store.get(dep["id"])["manual_steps"]

    ok, detail = await d.dispatch_card(card["id"])
    assert not ok and '"the foundation"' in detail
    assert store.get(card["id"])["queue_state"] == "queued"
    d._decide_queue_dispatches(store.cards())
    assert d._queue_candidates == []
    assert opened == []


@pytest.mark.asyncio
async def test_a_dependency_flagged_then_restarted_for_rework_holds(
        daemon, project, monkeypatch):
    """A rework run re-binds a new session while the old steps stay; that
    session waiting between turns is not the flagged run finishing — and
    `dispatching` on the way there is not either."""
    d, store = daemon
    opened: list = []
    _stub_spawn(d, monkeypatch, opened, project)
    dep, card = _flagged_dependency(d, store, project)
    await d.dispatch_card(card["id"])
    store.update(dep["id"], {"link_state": "dispatching"})
    d._agents_snapshot_cache = {"running": [], "waiting": []}
    d._decide_queue_dispatches(store.cards())
    assert d._queue_candidates == []

    store.bind_session(dep["id"], "s2")
    _hear(d, "s2")
    d._agents_snapshot_cache = {"running": [], "waiting": [{"session_id": "s2"}]}
    d._decide_queue_dispatches(store.cards())
    assert d._queue_candidates == []
    by_id = {c["id"]: c for c in d._build_board_state()["cards"]}
    assert by_id[card["id"]]["dependencies"][0]["met"] is False
    assert opened == []


@pytest.mark.asyncio
async def test_a_dependency_is_not_read_as_met_before_the_first_agents_snapshot(
        daemon, project, monkeypatch):
    """In the seconds after a restart no agents snapshot has arrived, so who
    is working is unknown: a flagged dependency whose session is bound reads
    as still working, and a person's press waits rather than starting."""
    d, store = daemon
    opened: list = []
    _stub_spawn(d, monkeypatch, opened, project)
    _dep, card = _flagged_dependency(d, store, project)
    d._agents_snapshot_cache = {}

    ok, detail = await d.dispatch_card(card["id"])
    assert not ok and '"the foundation"' in detail
    assert opened == []
    assert store.get(card["id"])["queue_state"] == "queued"
    # Once the snapshot arrives and the session has stopped, it goes.
    d._agents_snapshot_cache = {"running": [], "waiting": [{"session_id": "s1"}]}
    d._decide_queue_dispatches(store.cards())
    assert d._queue_candidates == [card["id"]]


@pytest.mark.asyncio
async def test_a_dependency_held_card_pressed_during_the_cooldown_names_the_dependency(
        daemon, project, monkeypatch):
    """`dispatch.guard`'s transient refusal (here the cooldown) still queues,
    and a card that also waits on an unfinished card says which one rather
    than blaming a full project."""
    d, store = daemon
    opened: list = []
    _stub_spawn(d, monkeypatch, opened, project)
    _dep, card = _dependent_pair(store, project)
    d._dispatch_attempts[card["id"]] = time.time()

    ok, detail = await d.dispatch_card(card["id"])
    assert not ok
    assert detail == ('Queued — Dark Army will start it once "the foundation" '
                      "is done")
    assert store.get(card["id"])["queue_state"] == "queued"
    assert opened == []
