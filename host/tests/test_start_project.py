"""One press, one project: `board_start_project` and the two screens behind it.

The verb is N hand presses under one lock, so the honest place to assert it is
the same seam `test_board_queue.py` uses — a real `BoardStore` on a temp file
with `dispatch.spawn` stubbed, where "no terminal opened" is a fact rather than
a button's label. The Swift half is a lint over source (`ios/` has no test
target), `test_phone_pipeline_controls.py`'s pattern.
"""

import asyncio
import json
import time
from pathlib import Path

import pytest

from dark_army_daemon import board
from dark_army_daemon import daemon as daemon_mod
from dark_army_daemon import dispatch
from dark_army_daemon import event_log
from dark_army_daemon.api_server import ApiServer
from dark_army_daemon.board import BoardStore
from dark_army_daemon.daemon import BobDaemon

ROOT = Path(__file__).resolve().parents[2]
PHONE = ROOT / "ios" / "BobPhone"
PANEL = ROOT / "panel" / "Sources" / "BobPanel"

VERB = "board_start_project"


def _read(path: Path) -> str:
    assert path.is_file(), f"missing file: {path}"
    return path.read_text()


# --- the fixtures, copied from `test_board_queue.py` ------------------------


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "proj"
    (root / "plans").mkdir(parents=True)
    return root


@pytest.fixture
def daemon(tmp_path):
    d = BobDaemon(sessions_path=tmp_path / "sessions.json")
    store = BoardStore(tmp_path / "board.db")
    store.connect()
    d._board = store
    try:
        yield d, store
    finally:
        store.close()


def _plan(project, name):
    path = project / "plans" / f"{name}.md"
    path.write_text(f"# {name}\n\n- **Stages:** bc-implementer\n")
    return str(path)


def _make(store, plan_path=None, **kw):
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
    return card


def _stub_spawn(d, monkeypatch, opened, root):
    monkeypatch.setattr(d, "_known_project_roots", lambda: {str(root)})
    monkeypatch.setattr(dispatch, "resolve_executable", lambda tool: "/bin/claude")

    async def accept(root, argv, name, **_kw):
        opened.append(name)
        return True, "bob", None

    monkeypatch.setattr(dispatch, "spawn", accept)


def _planned(store, project, n, **kw):
    """`n` planned Backlog cards in board order, oldest first."""
    made = []
    for i in range(n):
        made.append(_make(store, root=str(project), title=f"card {i}",
                          plan_path=_plan(project, f"p{i}"), **kw))
        # `position` is not set by `create`, so board order falls back to
        # `created_at` — which needs to differ to be an order at all.
        time.sleep(0.002)
    return made


def _queued_ids(store, project_label="bob"):
    return [c["id"] for c in store.cards()
            if c.get("project") == project_label
            and c.get("queue_state") == "queued"]


# --- the walk ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_press_starts_one_card_and_lines_the_rest_up_in_board_order(
        daemon, project, monkeypatch):
    """The whole feature. At the default dial of one place, the first card
    dispatches and the rest join the queue — one process, not five — and their
    queue order is the order they sat on the board."""
    d, store = daemon
    opened: list = []
    _stub_spawn(d, monkeypatch, opened, project)
    cards = _planned(store, project, 5)

    ok, detail = await d.start_project(str(project))
    assert ok, detail
    assert opened == ["card 0"], "a batch spawns one process, never five"
    started = store.get(cards[0]["id"])
    assert started["link_state"] == "dispatching"
    queued = [store.get(c["id"]) for c in cards[1:]]
    assert all(c["queue_state"] == "queued" for c in queued)
    # Board order in, drain order out.
    import dark_army_daemon.board_queue as board_queue
    keys = [board_queue.queue_key(c) for c in queued]
    assert keys == sorted(keys)
    assert [c["title"] for c in sorted(queued, key=board_queue.queue_key)] == \
        ["card 1", "card 2", "card 3", "card 4"]


@pytest.mark.asyncio
async def test_a_bigger_dial_still_spawns_one_because_the_drain_starts_the_rest(
        daemon, project, monkeypatch):
    """Three places does not mean three terminals at once. The per-project
    in-flight rule (`PROJECT_BUSY_REFUSAL`) is transient, so the cards behind
    the first queue and the drain starts them as each binds."""
    d, store = daemon
    opened: list = []
    _stub_spawn(d, monkeypatch, opened, project)
    d.set_board_parallel(3)
    cards = _planned(store, project, 4)

    ok, detail = await d.start_project(str(project))
    assert ok, detail
    assert opened == ["card 0"]
    assert len(_queued_ids(store)) == 3
    assert store.get(cards[0]["id"])["queue_state"] == ""


@pytest.mark.asyncio
async def test_an_unplanned_card_is_left_exactly_where_it_was(
        daemon, project, monkeypatch):
    """The plan gate is a confirmation a person gives about **one** card, so
    the batch skips rather than overriding — and it paints nothing orange:
    a Backlog card with no plan is the gate working, not a failure."""
    d, store = daemon
    opened: list = []
    _stub_spawn(d, monkeypatch, opened, project)
    bare = _make(store, root=str(project), title="no plan here")

    ok, detail = await d.start_project(str(project))
    assert not ok
    assert opened == []
    got = store.get(bare["id"])
    assert got["column_name"] == "backlog"
    assert got["queue_state"] == ""
    assert got["dispatch_error"] == "", "a skip must not paint the card"
    assert "no plan here" in detail
    assert daemon_mod.PLAN_GATE_REFUSAL in detail


@pytest.mark.asyncio
async def test_a_card_with_no_assistant_is_skipped_in_the_guards_own_words(
        daemon, project, monkeypatch):
    d, store = daemon
    opened: list = []
    _stub_spawn(d, monkeypatch, opened, project)
    _make(store, root=str(project), title="nobody's card", tool="",
          plan_path=_plan(project, "p"))

    ok, detail = await d.start_project(str(project))
    assert not ok
    assert opened == []
    assert "this card does not say which assistant should take it" in detail


@pytest.mark.asyncio
async def test_a_card_already_being_worked_on_is_never_touched(
        daemon, project, monkeypatch):
    d, store = daemon
    opened: list = []
    _stub_spawn(d, monkeypatch, opened, project)
    live = _make(store, root=str(project), title="running",
                 plan_path=_plan(project, "p"))
    store.update(live["id"], {"link_state": "live", "session_id": "s1"})
    d._session_states["s1"] = {"state": "working", "last_event": time.time()}

    ok, detail = await d.start_project(str(project))
    assert opened == []
    assert not ok
    got = store.get(live["id"])
    assert got["session_id"] == "s1" and got["queue_state"] == ""
    assert "already being worked on" in detail


@pytest.mark.asyncio
async def test_a_card_already_queued_is_counted_and_not_restamped(
        daemon, project, monkeypatch):
    """Re-pressing an already-queued card would only earn
    `_already_queued_reason` and re-stamping it would move it to the back of
    a line it is at the front of, so the batch does not walk it at all."""
    d, store = daemon
    opened: list = []
    _stub_spawn(d, monkeypatch, opened, project)
    waiting = _make(store, root=str(project), title="waiting",
                    plan_path=_plan(project, "p"))
    store.update(waiting["id"], {"queue_state": "queued",
                                 "queued_at": 1000.0})

    ok, detail = await d.start_project(str(project))
    assert not ok, "nothing started and nothing newly queued"
    assert opened == []
    got = store.get(waiting["id"])
    assert got["queued_at"] == 1000.0, "the queue slot must not be re-stamped"
    assert "1 already waiting" in detail


@pytest.mark.asyncio
async def test_the_walk_stops_at_the_stores_own_queue_ceiling(
        daemon, project, monkeypatch):
    """Nine planned cards at one place: eight fit (one dispatching, seven
    queued), the ninth meets `MAX_QUEUED_PER_PROJECT`. The walk stops there,
    the reply names the ceiling in the store's own words, and the cards
    behind it are untouched."""
    d, store = daemon
    opened: list = []
    _stub_spawn(d, monkeypatch, opened, project)
    cards = _planned(store, project, 11)

    ok, detail = await d.start_project(str(project))
    assert ok
    assert opened == ["card 0"]
    assert len(_queued_ids(store)) == board.MAX_QUEUED_PER_PROJECT
    assert f"{board.MAX_QUEUED_PER_PROJECT} cards queued" in detail
    assert "queue is full" in detail
    # Everything past the refusal is exactly as it was.
    for card in cards[9:]:
        got = store.get(card["id"])
        assert got["queue_state"] == "" and got["dispatch_error"] == ""


@pytest.mark.asyncio
async def test_the_preference_refuses_the_batch_outright(
        daemon, project, monkeypatch):
    d, store = daemon
    opened: list = []
    _stub_spawn(d, monkeypatch, opened, project)
    _planned(store, project, 3)
    d.board_dispatch_enabled = False

    ok, detail = await d.start_project(str(project))
    assert not ok
    assert opened == []
    assert detail == "Dark Army is not allowed to start sessions (see the ⋯ menu)"
    assert _queued_ids(store) == []


@pytest.mark.asyncio
async def test_the_batch_is_matched_by_root_and_never_by_the_project_label(
        daemon, project, monkeypatch, tmp_path):
    """Two folders can wear one name. A batch that picked up the other one's
    cards would start work in a project nobody pressed anything in."""
    d, store = daemon
    other = tmp_path / "other"
    (other / "plans").mkdir(parents=True)
    opened: list = []
    monkeypatch.setattr(d, "_known_project_roots",
                        lambda: {str(project), str(other)})
    monkeypatch.setattr(dispatch, "resolve_executable", lambda tool: "/bin/claude")

    async def accept(root, argv, name, **_kw):
        opened.append(name)
        return True, "bob", None

    monkeypatch.setattr(dispatch, "spawn", accept)
    elsewhere = _make(store, root=str(other), title="elsewhere",
                      plan_path=_plan(other, "p"))

    ok, detail = await d.start_project(str(project))
    assert not ok
    assert detail == "nothing here to start"
    assert opened == []
    assert store.get(elsewhere["id"])["queue_state"] == ""


@pytest.mark.asyncio
async def test_a_folder_that_names_nothing_is_refused_before_the_lock(daemon):
    d, _store = daemon
    ok, detail = await d.start_project("")
    assert not ok and detail == "no folder named"


# --- the report -------------------------------------------------------------


def test_the_report_is_composed_in_one_place_and_omits_zero_terms():
    say = daemon_mod._start_project_report
    assert say(1, 1, 0, []) == "2 started or queued"
    assert say(0, 0, 0, []) == "nothing here to start"
    text = say(1, 1, 1, [("a", "no plan"), ("", "no tool")])
    assert text.splitlines()[0] == \
        "2 started or queued · 1 already waiting · 2 left alone"
    assert "a — no plan" in text
    assert "untitled — no tool" in text


def test_the_report_caps_its_lines_and_names_an_unwalked_tail():
    say = daemon_mod._start_project_report
    skips = [(f"c{i}", "no plan") for i in range(11)]
    text = say(0, 0, 0, skips)
    assert text.count("no plan") == daemon_mod.START_PROJECT_MAX_LINES
    assert "…and 3 more" in text
    assert "one press walks at most 12" in say(1, 0, 0, [], remaining=4,
                                               max_cards=12)
    assert "queue is full" in say(1, 0, 0, [], remaining=4,
                                  remaining_reason="queue_full")


# --- the snapshot marker ----------------------------------------------------


def test_every_board_shape_states_the_new_marker(daemon, monkeypatch):
    """`_pipeline_writable` is spread into all three shapes — open, shut and
    read-failed — under this file's never-a-missing-key rule."""
    d, store = daemon
    assert d._pipeline_writable()["start_project_writable"] is True
    assert d._build_board_state()["start_project_writable"] is True
    d._board = None
    assert d._build_board_state()["start_project_writable"] is True
    d._board = store
    monkeypatch.setattr(store, "cards",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError))
    assert d._build_board_state()["start_project_writable"] is True


# --- the four chosen lists --------------------------------------------------


def test_the_verb_is_on_each_list_exactly_once():
    assert ApiServer.BOARD_ACTIONS.count(VERB) == 1
    assert ApiServer.LAN_ACTIONS.count(VERB) == 1
    assert ApiServer.REMOTE_ACTIONS.count(VERB) == 1
    assert VERB in ApiServer._LAN_BOARD
    assert set(ApiServer.REMOTE_ACTIONS) <= set(ApiServer.LAN_ACTIONS)


class _StubDaemon:
    def __init__(self):
        self.roots = []

    async def start_project(self, root):
        self.roots.append(root)
        return True, "1 started or queued"


def _server(daemon):
    srv = object.__new__(ApiServer)
    srv._daemon = daemon
    return srv


def test_a_press_with_no_root_is_400_and_reaches_nothing():
    stub = _StubDaemon()
    status, _, body = asyncio.run(_server(stub)._board_action(VERB, {}))
    assert status == 400
    assert stub.roots == []
    assert json.loads(body)["error"] == "no root"


def test_a_press_with_a_root_reaches_the_daemon_through_the_lan_door():
    stub = _StubDaemon()
    status, _, body = asyncio.run(
        _server(stub)._lan_run(VERB, {"root": "/Users/x/proj"}))
    assert status == 200
    assert stub.roots == ["/Users/x/proj"]
    assert json.loads(body)["detail"] == "1 started or queued"


def test_the_batch_writes_no_diary_kind():
    """N hand presses write one `card_dispatched` and nothing else; a
    synthetic batch kind would be a sentence no other path can write."""
    assert VERB not in event_log.KINDS
    assert VERB not in _read(ROOT / "host" / "dark_army_daemon"
                             / "event_log.py")


# --- the Swift half ---------------------------------------------------------


def test_the_phone_decodes_the_marker_false_by_default():
    """The phone can meet an older Mac; the panel ships with its daemon and
    decodes no version markers (20 Sep 2026)."""
    text = _read(PHONE / "Models.swift")
    assert 'case startProjectWritable = "start_project_writable"' in text
    assert "startProjectWritable = c.value(.startProjectWritable, false)" in text
    for models in (PHONE / "Models.swift", PANEL / "BoardModels.swift"):
        assert "func backlog(in project: String)" in _read(models), models
    assert "startProjectWritable" not in _read(PANEL / "BoardModels.swift")


def test_both_clients_name_the_press():
    assert f'"{VERB}"' in _read(PHONE / "Actions.swift")
    assert f'"{VERB}"' in _read(PANEL / "BoardClient.swift")
    for view in (PHONE / "PipelineView.swift",
                 PANEL / "BoardProjectControls.swift"):
        assert "START PROJECT" in _read(view), view


def test_neither_start_project_source_carries_a_second_plan_gate():
    """Which cards actually start is the daemon's judgment. A `plan_path`
    test in Swift is a second copy of it that will drift."""
    for view in (PHONE / "PipelineView.swift",
                 PANEL / "BoardProjectControls.swift"):
        text = _read(view)
        assert "plan_path" not in text, view
        assert "planPath" not in text, view
