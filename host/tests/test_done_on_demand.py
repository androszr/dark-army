# host/tests/test_done_on_demand.py
"""Finished cards are served on demand, not on every frame.

Measured on the live daemon before this change: `/api/state` was 173 KB, its
`board` section 140 KB, and the 38 Done cards inside it 88 KB — 51 % of the
whole frame, re-sent every few seconds for cards nobody was looking at. What
this file pins is the bargain that removed them: the daemon keeps sending the
finished cards that still want a person, stamps the rest `done_preview`, and
serves the whole column separately to a client that asks.
"""

import asyncio
import json
import urllib.error
import urllib.request

import pytest
import pytest_asyncio

from dark_army_daemon import api_server as api_mod
from dark_army_daemon import board as board_mod
from dark_army_daemon.api_server import ApiServer
from dark_army_daemon.board import BoardStore
from dark_army_daemon.daemon import BobDaemon
from tests.free_ports import free_port


def _free_port() -> int:
    """From this run's and worker's block below the ephemeral range, not a
    bound-then-closed `:0` another worker's connection can take
    (`tests/free_ports.py`)."""
    return free_port()


@pytest.fixture(autouse=True)
def _no_fleet_snapshot(monkeypatch):
    """`test_api_server.py`'s fixture and its reason: the real fleet is real
    work on the real machine, and nothing here asserts anything about it."""
    monkeypatch.setattr(BobDaemon, "_schedule_agents_push", lambda self: None)


@pytest.fixture
def daemon_with_board(tmp_path, monkeypatch):
    monkeypatch.setattr(api_mod, "API_TOKEN_PATH", tmp_path / "api-token")
    monkeypatch.setattr(api_mod, "ensure_state_dir", lambda: tmp_path)
    daemon = BobDaemon(sessions_path=tmp_path / "sessions.json")
    store = BoardStore(tmp_path / "board.db")
    store.connect()
    daemon._board = store
    try:
        yield daemon, store
    finally:
        store.close()


def _close(store, card_id: str, session_id: str) -> None:
    """Walk a card the way a dispatched session does: bound, in progress, then
    closed by its own session. `declare_done` guards on both in its WHERE
    clause, so there is no shortcut past this."""
    store.update(card_id, {"session_id": session_id,
                           "column_name": "in_progress"})
    card, detail = store.declare_done(card_id, session_id, "finished")
    assert card is not None, detail


def _three_cards(store):
    """One Done card an assistant closed and nobody has reviewed, one Done card
    a person has acknowledged, one card still in Backlog."""
    waiting, _ = store.create({"title": "waiting", "project": "p"})
    _close(store, waiting["id"], "sess-a")
    reviewed, _ = store.create({"title": "reviewed", "project": "p"})
    _close(store, reviewed["id"], "sess-b")
    store.mark_reviewed(reviewed["id"])
    live, _ = store.create({"title": "live", "project": "p"})
    return waiting["id"], reviewed["id"], live["id"]


# --- the two tokens -------------------------------------------------------

def test_done_tokens_agrees_with_done_scope(daemon_with_board):
    _, store = daemon_with_board
    _three_cards(store)
    count, clear_token = store.done_scope()
    tokens = store.done_tokens()
    assert tokens[0] == count
    assert tokens[1] == clear_token
    assert len(tokens[2]) == 64
    assert tokens[2] != tokens[1]


def test_a_review_moves_the_view_token_and_not_the_membership_one(
        daemon_with_board):
    """The whole reason there are two. A review is exactly the change that
    moves a card out of the always-carried set, and it touches no id."""
    _, store = daemon_with_board
    waiting, _, _ = _three_cards(store)
    _, before_clear, before_view = store.done_tokens()
    store.mark_reviewed(waiting)
    _, after_clear, after_view = store.done_tokens()
    assert after_clear == before_clear
    assert after_view != before_view


def test_an_edit_moves_the_view_token_and_not_the_membership_one(
        daemon_with_board):
    _, store = daemon_with_board
    _, reviewed, _ = _three_cards(store)
    _, before_clear, before_view = store.done_tokens()
    store.update(reviewed, {"title": "renamed"})
    _, after_clear, after_view = store.done_tokens()
    assert after_clear == before_clear
    assert after_view != before_view


def test_a_new_done_card_moves_both_tokens(daemon_with_board):
    _, store = daemon_with_board
    _three_cards(store)
    _, before_clear, before_view = store.done_tokens()
    extra, _ = store.create({"title": "third", "project": "p"})
    _close(store, extra["id"], "sess-c")
    count, after_clear, after_view = store.done_tokens()
    assert count == 3
    assert after_clear != before_clear
    assert after_view != before_view


# --- done_preview ---------------------------------------------------------

def test_the_snapshot_stamps_only_the_withheld_done_cards(daemon_with_board):
    """All three cards ride the full frame, and the stamp lands on exactly the
    finished card nobody is waiting on."""
    daemon, store = daemon_with_board
    waiting, reviewed, live = _three_cards(store)
    state = daemon._build_board_state()
    by_id = {c["id"]: c for c in state["cards"]}
    assert set(by_id) == {waiting, reviewed, live}
    assert by_id[reviewed].get("done_preview") is True
    assert not by_id[waiting].get("done_preview")
    assert not by_id[live].get("done_preview")


def test_review_only_drops_exactly_the_stamped_cards(daemon_with_board):
    daemon, store = daemon_with_board
    waiting, reviewed, live = _three_cards(store)
    state = daemon._build_board_state()
    slim = board_mod.review_only(state)
    assert {c["id"] for c in slim["cards"]} == {waiting, live}
    # And it never touches the rest of the section: the count a heading draws
    # is store-wide, so a shortened list must not shorten it.
    assert slim["counts"]["done"] == 2
    assert slim["done_clear_token"] == state["done_clear_token"]
    assert slim["done_view_token"] == state["done_view_token"]


def test_both_tokens_ride_the_board_section(daemon_with_board):
    daemon, store = daemon_with_board
    _three_cards(store)
    state = daemon._build_board_state()
    assert len(state["done_clear_token"]) == 64
    assert len(state["done_view_token"]) == 64
    assert state["done_view_token"] != state["done_clear_token"]


def test_the_shut_board_still_states_both_tokens(tmp_path):
    """`counts`' never-a-missing-key rule: a surface reading the field must
    never have to handle its absence."""
    daemon = BobDaemon(sessions_path=tmp_path / "sessions.json")
    daemon._board = None
    state = daemon._build_board_state()
    assert state["done_clear_token"] == ""
    assert state["done_view_token"] == ""


def test_state_done_review_narrows_only_the_board(daemon_with_board):
    daemon, store = daemon_with_board
    waiting, reviewed, live = _three_cards(store)
    daemon._refresh_board_state()
    srv = ApiServer(daemon, port=_free_port())
    full = srv.state()
    slim = srv.state(done_review=True)
    assert {c["id"] for c in full["board"]["cards"]} == {waiting, reviewed, live}
    assert {c["id"] for c in slim["board"]["cards"]} == {waiting, live}
    assert set(full) == set(slim)
    for key in set(full) - {"board", "generated_at"}:
        assert json.dumps(full[key], sort_keys=True, default=str) == \
            json.dumps(slim[key], sort_keys=True, default=str)


# --- the on-demand read ---------------------------------------------------

def test_the_archive_comes_back_in_the_snapshots_own_shape(daemon_with_board):
    """A client splices these into `board.cards`, so they must be the frame's
    shape and not raw store rows — or it needs a second decode path and two
    ideas of what a card is."""
    daemon, store = daemon_with_board
    waiting, reviewed, _ = _three_cards(store)
    report = daemon.done_archive_cards()
    assert report["available"] is True
    assert {c["id"] for c in report["cards"]} == {waiting, reviewed}
    assert report["count"] == 2
    assert report["more"] is False
    assert report["done_clear_token"] == store.done_scope()[1]
    assert len(report["done_view_token"]) == 64
    for card in report["cards"]:
        # The decoration the raw `?range=` read does not do.
        assert "closed_by_name" in card
        assert "thread_count" in card
        assert "work_active" in card
        # Retired with the waiting-on feature: no surface reads it.
        assert "blockers" not in card


@pytest_asyncio.fixture
async def board_server(daemon_with_board):
    daemon, store = daemon_with_board
    daemon._refresh_board_state()
    port = _free_port()
    srv = ApiServer(daemon, port=port)
    await srv.start()
    try:
        yield srv, daemon, store, port
    finally:
        await srv.stop()


def _get(port, path):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}") as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


@pytest.mark.asyncio
async def test_column_done_serves_the_whole_finished_column(board_server):
    _, _, store, port = board_server
    waiting, reviewed, _ = _three_cards(store)
    status, body = await asyncio.to_thread(_get, port, "/api/board?column=done")
    assert status == 200
    report = json.loads(body)
    assert {c["id"] for c in report["cards"]} == {waiting, reviewed}
    assert len(report["done_view_token"]) == 64


@pytest.mark.asyncio
async def test_an_unknown_column_is_refused_in_words(board_server):
    _, _, _, port = board_server
    status, body = await asyncio.to_thread(
        _get, port, "/api/board?column=backlog")
    assert status == 400
    assert json.loads(body)["allowed"] == ["done"]


@pytest.mark.asyncio
async def test_the_range_and_card_reads_are_untouched(board_server):
    _, _, store, port = board_server
    _three_cards(store)
    status, body = await asyncio.to_thread(_get, port, "/api/board?range=all")
    assert status == 200
    assert json.loads(body)["range"] == "all"


# --- the two SSE variants from one flush ----------------------------------

def test_one_flush_serves_both_kinds_of_client(daemon_with_board):
    """Drive `_flush` directly against two queues — `test_api_server.py`'s own
    seam. The full client must keep every card it gets today; the opted-in one
    must lose exactly the withheld finished cards."""
    daemon, store = daemon_with_board
    waiting, reviewed, live = _three_cards(store)
    daemon._refresh_board_state()
    srv = ApiServer(daemon, port=_free_port())
    plain: asyncio.Queue = asyncio.Queue()
    opted: asyncio.Queue = asyncio.Queue()
    srv._clients[plain] = False
    srv._clients[opted] = False
    srv._done_review_clients.add(opted)
    srv._flush()
    plain_board = json.loads(plain.get_nowait())["board"]
    opted_board = json.loads(opted.get_nowait())["board"]
    assert {c["id"] for c in plain_board["cards"]} == {waiting, reviewed, live}
    assert {c["id"] for c in opted_board["cards"]} == {waiting, live}
    # And the store-wide figures are the same in both: a shortened list must
    # never shorten the count a heading draws.
    assert plain_board["counts"]["done"] == opted_board["counts"]["done"] == 2


def test_the_two_variants_keep_separate_news_slots(daemon_with_board):
    """Sharing one slot would suppress a frame a client needed, or send one it
    did not — the expensive way to be wrong here."""
    daemon, store = daemon_with_board
    _three_cards(store)
    daemon._refresh_board_state()
    srv = ApiServer(daemon, port=_free_port())
    plain: asyncio.Queue = asyncio.Queue()
    opted: asyncio.Queue = asyncio.Queue()
    srv._clients[plain] = True
    srv._clients[opted] = True
    srv._done_review_clients.add(opted)
    srv._flush()
    # The ordinary board rides `_last_section_news`, one entry among the nine
    # sections; the review-only variant keeps its own slot, because the two go
    # stale at different moments.
    assert srv._last_section_news["board"]
    assert srv._last_review_board_news
    assert srv._last_section_news["board"] != srv._last_review_board_news


# --- the saving, measured -------------------------------------------------

def test_the_slim_board_is_less_than_half_the_size(daemon_with_board):
    """The plan's own figure, asserted rather than judged: 38 reviewed Done
    cards plus 23 live ones, and the opted-in board under 45 % of the full
    one — with every finished card that still wants a person still in it."""
    daemon, store = daemon_with_board
    prompt = "x" * 2000
    for n in range(38):
        card, _ = store.create({"title": f"done {n}", "project": "p",
                                "prompt": prompt, "summary": "s" * 300})
        _close(store, card["id"], f"sess-{n}")
        store.mark_reviewed(card["id"])
    waiting, _ = store.create({"title": "wants a person", "project": "p",
                               "prompt": prompt})
    _close(store, waiting["id"], "sess-waiting")
    for n in range(22):
        store.create({"title": f"live {n}", "project": "p", "prompt": prompt})
    daemon._refresh_board_state()
    srv = ApiServer(daemon, port=_free_port())
    full = json.dumps(srv.state()["board"])
    slim_board = srv.state(done_review=True)["board"]
    slim = json.dumps(slim_board)
    assert len(slim) < 0.45 * len(full), (len(slim), len(full))
    assert waiting["id"] in {c["id"] for c in slim_board["cards"]}
    assert slim_board["counts"]["done"] == 39


def test_the_measured_ratio_is_reported(daemon_with_board, capsys):
    """Not a gate — the gate is the test above. This prints the figure so a
    person changing the bounds can see what the saving actually became."""
    daemon, store = daemon_with_board
    prompt = "x" * 2000
    for n in range(38):
        card, _ = store.create({"title": f"done {n}", "project": "p",
                                "prompt": prompt, "summary": "s" * 300})
        _close(store, card["id"], f"sess-{n}")
        store.mark_reviewed(card["id"])
    for n in range(23):
        store.create({"title": f"live {n}", "project": "p", "prompt": prompt})
    daemon._refresh_board_state()
    srv = ApiServer(daemon, port=_free_port())
    full = len(json.dumps(srv.state()["board"]))
    slim = len(json.dumps(srv.state(done_review=True)["board"]))
    print(f"\nboard section: {full} -> {slim} bytes ({slim / full:.0%})")
    assert slim < full
