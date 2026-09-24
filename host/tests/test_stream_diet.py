# host/tests/test_stream_diet.py
"""The live stream sends only what changed.

Every case is a sequence of scripted pictures in and a list of (client,
frame) out, on `test_api_server.py`'s seams: queues registered straight on
`srv._clients` (slim or full) plus membership of `_done_review_clients` /
`_card_delta_clients`, `_flush` driven directly, the floors lapsed by
writing `_last_sent` / `_last_echo_sent` rather than by sleeping, and the
picture scripted through `srv._board`, `srv._counts` and a monkeypatched
`daemon.devices_snapshot`. Two socket-level cases go through a real
`/api/events` subscription with `_isolate_limiter`.

What is pinned: a clock-only picture sends the panel nothing; a phone's
check-in stamp alone waits out `BROADCAST_ECHO_INTERVAL` and is never
stripped; one card's figures ticking sends a `?cards=delta` client that
card and the card order, on both board variants; the first board after an
attach or an overflow is whole; an assembled frame is byte-identical to
`json.dumps(state)`; the delta adds exactly two keys and no secret; and the
review variant is derived, never rebuilt with a second `state()`.
"""

from __future__ import annotations

import asyncio
import copy
import json
import re
import time
from pathlib import Path

import pytest
import pytest_asyncio

from dark_army_daemon import api_server as api_mod
from dark_army_daemon import event_log
from dark_army_daemon.api_server import ApiServer
from dark_army_daemon.daemon import BobDaemon
from tests.free_ports import free_port

SRC = Path(api_mod.__file__)


def _free_port() -> int:
    """From this run's and worker's block below the ephemeral range, not a
    bound-then-closed `:0` another worker's connection can take
    (`tests/free_ports.py`)."""
    return free_port()


PORT = _free_port()


# --- `test_api_server.py`'s stream helpers, the same seam -------------------


async def _open_stream(port=PORT, query=""):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(f"GET /api/events{query} HTTP/1.1\r\n"
                 f"Host: 127.0.0.1\r\n\r\n".encode("latin-1"))
    await writer.drain()
    head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
    return reader, writer, head


async def _next_event(reader):
    raw = await asyncio.wait_for(reader.readuntil(b"\n\n"), timeout=5)
    return json.loads(raw.split(b"data: ", 1)[1])


async def _quiet(reader, within):
    try:
        await asyncio.wait_for(reader.readuntil(b"\n\n"), timeout=within)
    except asyncio.TimeoutError:
        return
    raise AssertionError("a frame went out inside the floor")


async def _isolate_limiter(srv, reader):
    """Leave the limiter the only thing driving the stream: detach the
    observer, cancel any booking already made, read until the wire is
    quiet (`test_api_server.py`'s own helper, for its own reason)."""
    srv._daemon.remove_observer(srv)
    if srv._flush_handle is not None:
        srv._flush_handle.cancel()
        srv._flush_handle = None
    srv._flush_due = 0.0
    while True:
        try:
            await asyncio.wait_for(reader.readuntil(b"\n\n"), timeout=0.3)
        except asyncio.TimeoutError:
            return


def _rows(**extra):
    row = {"session_id": "s", "idle_seconds": 1.0, "nickname": "Forge"}
    row.update(extra)
    return {"running": [row], "sleeping": [], "waiting": [],
            "abandoned": [], "finished": []}


@pytest.fixture(autouse=True)
def _no_fleet_snapshot(monkeypatch):
    monkeypatch.setattr(BobDaemon, "_schedule_agents_push", lambda self: None)


# --- the scripted picture ----------------------------------------------------


def _card(card_id: str, cost: float = 1.0, minutes: int = 5, **extra) -> dict:
    card = {"id": card_id, "title": f"Card {card_id}",
            "column_name": "in_progress", "revision": 2,
            "run_figures": {"cost_usd": cost, "cost_coverage": "complete",
                            "active_seconds": minutes * 60,
                            "active_ticking": True, "ctx_percent": 40,
                            "attempts": 1}}
    card.update(extra)
    return card


def _board(*cards, stamp: float = 1.0) -> dict:
    return {"generated_at": stamp, "available": True,
            "counts": {"prep": 0, "backlog": 0,
                       "in_progress": len(cards), "done": 0},
            "cards": list(cards)}


class _Picture:
    """The daemon's `devices` section, scripted per step."""

    def __init__(self):
        self.devices = {"available": True, "lan_enabled": False,
                        "devices": [{"id": "d1", "name": "Phone",
                                     "last_frame_at": 1.0}]}


@pytest.fixture
def srv(monkeypatch):
    daemon = BobDaemon()
    server = ApiServer(daemon, port=0)
    daemon._api = server
    picture = _Picture()
    monkeypatch.setattr(daemon, "devices_snapshot",
                        lambda: copy.deepcopy(picture.devices))
    server.picture = picture
    server._board = _board(_card("c1"), _card("c2"), _card("c3"))
    return server


def _client(srv, *, slim=True, review=False, delta=False,
            maxsize=0) -> asyncio.Queue:
    queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
    srv._clients[queue] = slim
    if review:
        srv._done_review_clients.add(queue)
    if delta:
        srv._card_delta_clients.add(queue)
    return queue


def _drain(queue: asyncio.Queue) -> list:
    out = []
    while True:
        try:
            out.append(json.loads(queue.get_nowait()))
        except asyncio.QueueEmpty:
            return out


def _lapse(srv) -> None:
    """Every floor lapsed, without sleeping."""
    srv._last_sent = 0.0
    srv._last_echo_sent = 0.0


def _send(srv) -> None:
    _lapse(srv)
    srv._flush()


def _cancel_booking(srv) -> bool:
    booked = srv._flush_handle is not None
    if booked:
        srv._flush_handle.cancel()
        srv._flush_handle = None
    return booked


# --- (i) a clock-only picture ------------------------------------------------


@pytest.mark.asyncio
async def test_a_clock_only_picture_sends_the_panel_nothing(srv):
    slim = _client(srv)
    full = _client(srv, slim=False)
    _send(srv)
    assert len(_drain(slim)) == 1 and len(_drain(full)) == 1
    # The same picture a moment later: deferred to the quiet floor, both.
    srv._last_sent = time.monotonic()
    srv._flush()
    assert _drain(slim) == [] and _drain(full) == []
    assert _cancel_booking(srv), "deferred, not dropped"
    # Past the floor: the full client gets today's frame, the slim one none.
    _send(srv)
    assert _drain(slim) == [], "no frame whose only content is the clock"
    frames = _drain(full)
    assert len(frames) == 1 and "board" in frames[0] and "agents" in frames[0]


def test_a_clock_only_picture_with_only_slim_clients_renders_nothing(srv,
                                                                     monkeypatch):
    slim = _client(srv)
    _send(srv)
    _drain(slim)
    rendered = []
    real = api_mod._Frame.render
    monkeypatch.setattr(api_mod._Frame, "render",
                        lambda self, *a, **k: rendered.append(1) or real(self, *a, **k))
    _send(srv)
    assert _drain(slim) == [] and rendered == []


# --- (ii) the echo floor -----------------------------------------------------


@pytest.mark.asyncio
async def test_a_poll_echo_alone_waits_out_the_echo_floor(srv):
    slim = _client(srv)
    _send(srv)
    _drain(slim)
    # A phone checked in: `last_frame_at` alone moved.
    srv.picture.devices["devices"][0]["last_frame_at"] = 2.0
    srv._last_sent = 0.0
    srv._last_echo_sent = time.monotonic()
    srv._flush()
    assert _drain(slim) == [], "an echo-only frame waits out the floor"
    assert _cancel_booking(srv), "deferred, never dropped"
    # The floor lapsed: the devices section rides, stamp and all.
    _send(srv)
    frames = _drain(slim)
    assert len(frames) == 1
    assert frames[0]["devices"]["devices"][0]["last_frame_at"] == 2.0
    assert "board" not in frames[0] and "agents" not in frames[0]
    # Inside the floor again, but with real news beside the echo: at once.
    srv.picture.devices["devices"][0]["last_frame_at"] = 3.0
    srv._counts = {"working": 4, "idle": 0, "attention": 0, "subagents": 0}
    srv._last_sent = 0.0
    srv._last_echo_sent = time.monotonic()
    srv._flush()
    frames = _drain(slim)
    assert len(frames) == 1
    assert frames[0]["counts"]["working"] == 4
    assert frames[0]["devices"]["devices"][0]["last_frame_at"] == 3.0


def test_every_sent_frame_stamps_the_echo_floor(srv):
    _client(srv)
    srv._last_echo_sent = 0.0
    srv._counts = {"working": 9, "idle": 0, "attention": 0, "subagents": 0}
    srv._last_sent = 0.0
    before = time.monotonic()
    srv._flush()
    assert srv._last_echo_sent >= before


def test_the_echo_names_are_never_clock_fields():
    assert not (api_mod._POLL_ECHO_FIELDS & api_mod._CLOCK_FIELDS)
    assert api_mod.BROADCAST_ECHO_INTERVAL == 15.0


# --- (iii)-(v) the per-card delta --------------------------------------------


def test_figures_tick_send_that_card_alone(srv):
    """The success criterion, in process: a card whose figures tick arrives
    as that card alone rather than the whole board."""
    delta = _client(srv, delta=True)
    plain = _client(srv)
    _send(srv)
    first = _drain(delta)[0]["board"]
    assert "cards_delta" not in first and len(first["cards"]) == 3
    _drain(plain)
    srv._board = _board(_card("c1"), _card("c2", cost=1.1, minutes=6),
                        _card("c3"), stamp=2.0)
    _send(srv)
    board = _drain(delta)[0]["board"]
    assert board["cards_delta"] is True
    assert [c["id"] for c in board["cards"]] == ["c2"]
    assert board["cards"][0]["run_figures"]["cost_usd"] == 1.1
    assert board["card_order"] == ["c1", "c2", "c3"]
    assert board["counts"] == srv._board["counts"], "board-wide facts ride too"
    # A slim client that did not ask gets the whole board in the same flush.
    whole = _drain(plain)[0]["board"]
    assert "cards_delta" not in whole and "card_order" not in whole
    assert [c["id"] for c in whole["cards"]] == ["c1", "c2", "c3"]


def test_a_removed_card_leaves_the_order_and_rides_nowhere(srv):
    delta = _client(srv, delta=True)
    _send(srv)
    _drain(delta)
    srv._board = _board(_card("c1"), _card("c2"), stamp=2.0)
    _send(srv)
    board = _drain(delta)[0]["board"]
    assert board["cards_delta"] is True
    assert board["card_order"] == ["c1", "c2"]
    assert board["cards"] == []
    assert set(srv._last_cards_sent_full) == {"c1", "c2"}, "replaced, not updated"


def test_the_review_variant_has_its_own_order_and_its_own_map(srv):
    srv._board = _board(_card("c1"), _card("c2"),
                        _card("c3", column_name="done", done_preview=True))
    review = _client(srv, review=True, delta=True)
    plain = _client(srv, delta=True)
    _send(srv)
    assert [c["id"] for c in _drain(review)[0]["board"]["cards"]] == ["c1", "c2"]
    assert len(_drain(plain)[0]["board"]["cards"]) == 3
    srv._board = _board(_card("c1", cost=2.0), _card("c2"),
                        _card("c3", column_name="done", done_preview=True),
                        stamp=2.0)
    _send(srv)
    review_board = _drain(review)[0]["board"]
    plain_board = _drain(plain)[0]["board"]
    assert review_board["card_order"] == ["c1", "c2"]
    assert plain_board["card_order"] == ["c1", "c2", "c3"]
    assert [c["id"] for c in review_board["cards"]] == ["c1"]
    assert [c["id"] for c in plain_board["cards"]] == ["c1"]
    assert set(srv._last_cards_sent_review) == {"c1", "c2"}
    assert set(srv._last_cards_sent_full) == {"c1", "c2", "c3"}


def test_no_delta_client_clears_both_maps(srv):
    delta = _client(srv, delta=True)
    _send(srv)
    assert srv._last_cards_sent_full
    del srv._clients[delta]
    srv._card_delta_clients.discard(delta)
    _client(srv)
    srv._board = _board(_card("c1", cost=3.0), stamp=3.0)
    _send(srv)
    assert srv._last_cards_sent_full == {} and srv._last_cards_sent_review == {}


def test_cards_without_ids_ride_whole(srv):
    delta = _client(srv, delta=True)
    srv._board = _board({"title": "no id"}, _card("c1"))
    _send(srv)
    _drain(delta)
    srv._board = _board({"title": "no id, edited"}, _card("c1"), stamp=2.0)
    _send(srv)
    board = _drain(delta)[0]["board"]
    assert "cards_delta" not in board and len(board["cards"]) == 2


# --- (vi) the first board after an attach is whole ---------------------------


@pytest.fixture
def token_path(tmp_path, monkeypatch):
    path = tmp_path / "api-token"
    monkeypatch.setattr(api_mod, "API_TOKEN_PATH", path)
    monkeypatch.setattr(api_mod, "ensure_state_dir", lambda: tmp_path)
    return path


@pytest_asyncio.fixture
async def live(token_path):
    daemon = BobDaemon()
    server = ApiServer(daemon, port=PORT)
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


DELTA_QUERY = "?sections=changed&done=review&cards=delta"


@pytest.mark.asyncio
async def test_after_attach_the_first_board_is_whole_then_deltas(live):
    srv = live
    srv._last_cards_sent_full = {"stale": "x"}
    srv._last_cards_sent_review = {"stale": "x"}
    reader, writer, _ = await _open_stream(port=PORT, query=DELTA_QUERY)
    try:
        assert srv._last_cards_sent_review == {} and \
            srv._last_cards_sent_full == {}, "an attach clears both maps"
        await _next_event(reader)
        await _isolate_limiter(srv, reader)
        srv.on_board_change(_board(_card("c1"), _card("c2"), _card("c3")))
        event = await _next_event(reader)
        assert "cards_delta" not in event["board"]
        assert len(event["board"]["cards"]) == 3
        srv.on_board_change(_board(_card("c1"), _card("c2", cost=4.0),
                                   _card("c3"), stamp=2.0))
        event = await _next_event(reader)
        assert event["board"]["cards_delta"] is True
        assert [c["id"] for c in event["board"]["cards"]] == ["c2"]
        assert event["board"]["card_order"] == ["c1", "c2", "c3"]
    finally:
        writer.close()


@pytest.mark.asyncio
async def test_the_panels_subscription_gets_no_clock_only_frame(live, monkeypatch):
    srv = live
    monkeypatch.setattr(api_mod, "BROADCAST_QUIET_INTERVAL", 0.2)
    reader, writer, _ = await _open_stream(port=PORT, query=DELTA_QUERY)
    await _isolate_limiter(srv, reader)
    try:
        srv.on_agents_change(_rows(idle_seconds=1.0))
        await _next_event(reader)
        srv.on_agents_change(_rows(idle_seconds=2.0))     # the clock alone
        await _quiet(reader, 1.0)
    finally:
        writer.close()


def test_a_full_client_asking_for_deltas_is_ignored():
    src = SRC.read_text()
    assert 'cards_delta = slim and params.get("cards") == "delta"' in src


# --- (vii) overflow ----------------------------------------------------------


def test_an_overflowing_delta_client_gets_a_whole_variant(srv):
    cap = api_mod.SSE_CLIENT_QUEUE_MAX
    stuck = _client(srv, review=True, delta=True, maxsize=cap)
    _send(srv)
    for n in range(cap + 3):
        srv._board = _board(_card("c1", cost=float(n + 2)), _card("c2"),
                            _card("c3"), stamp=float(n + 2))
        _send(srv)
    frames = _drain(stuck)
    assert len(frames) == cap
    assert frames[0]["board"].get("cards_delta") is True
    tail = frames[-1]
    assert "cards_delta" not in tail["board"], "the overflow put is whole"
    assert len(tail["board"]["cards"]) == 3
    assert set(tail) == set(srv.state()), "every section, not a slim frame"


def test_the_overflow_replacement_is_rendered_only_on_overflow(srv):
    queue: asyncio.Queue = asyncio.Queue(maxsize=1)
    calls = []

    def whole():
        calls.append(1)
        return "whole"

    srv._enqueue_client(queue, "a", whole)
    assert calls == []
    srv._enqueue_client(queue, "b", whole)
    assert calls == [1] and queue.get_nowait() == "whole"


# --- (viii) byte identity ----------------------------------------------------


def test_an_assembled_frame_is_byte_identical_to_json_dumps(srv):
    srv._board = _board(_card("c1", title="Zażółć — naïve ✓"), _card("c2"))
    srv._notifications = [{"session_id": "s", "message": "héllo\n\"q\""}]
    state = srv.state()
    frame = api_mod._Frame(state)
    assert frame.render() == json.dumps(state)
    assert frame.news == json.dumps(api_mod._news(state), sort_keys=True)
    omitted = {"agents", "board", "devices"}
    slim = frame.render(omit=omitted)
    assert slim == json.dumps({k: v for k, v in state.items()
                               if k not in omitted})
    assert json.loads(slim) == {k: v for k, v in state.items()
                                if k not in omitted}
    swapped = frame.render(overrides={"board": json.dumps({"cards": []})})
    assert swapped == json.dumps(dict(state, board={"cards": []}))
    assert api_mod._Frame({}).render() == json.dumps({})


def test_a_flushed_frame_is_the_state_it_was_built_from(srv, monkeypatch):
    built = []
    real = srv.state
    monkeypatch.setattr(srv, "state",
                        lambda **k: built.append(real(**k)) or built[-1])
    full = _client(srv, slim=False)
    review = _client(srv, slim=False, review=True)
    _send(srv)
    (frame,) = _drain(full)
    assert frame == json.loads(json.dumps(built[0]))
    (review_frame,) = _drain(review)
    assert review_frame["generated_at"] == frame["generated_at"], \
        "both variants of one frame share its clock"


# --- (ix) the security shape of the two new keys -----------------------------


def _all_keys(value) -> set:
    if isinstance(value, dict):
        out = set(value)
        for v in value.values():
            out |= _all_keys(v)
        return out
    if isinstance(value, list):
        out = set()
        for v in value:
            out |= _all_keys(v)
        return out
    return set()


def test_the_delta_adds_two_keys_and_no_secret(srv):
    delta = _client(srv, delta=True)
    plain = _client(srv)
    _send(srv)
    _drain(delta), _drain(plain)
    srv._board = _board(_card("c1"), _card("c2", cost=9.0), _card("c3"),
                        stamp=2.0)
    _send(srv)
    (delta_frame,) = _drain(delta)
    (plain_frame,) = _drain(plain)
    added = _all_keys(delta_frame) - _all_keys(plain_frame)
    assert added == {api_mod.CARDS_DELTA_KEY, api_mod.CARD_ORDER_KEY}
    board_ids = {c["id"] for c in srv.state()["board"]["cards"]}
    assert set(delta_frame["board"]["card_order"]) <= board_ids
    assert all(isinstance(i, str) for i in delta_frame["board"]["card_order"])
    for name in added:
        for word in ("key", "digest", "claim", "token", "channel"):
            assert word not in name
        assert name not in event_log.FORBIDDEN_KEYS
    assert set(delta_frame) - set(plain_frame) == set()


# --- (x) the review variant is derived ---------------------------------------


def test_flush_never_builds_the_review_state(srv, monkeypatch):
    calls = []
    real = srv.state
    monkeypatch.setattr(srv, "state",
                        lambda **k: calls.append(k) or real(**k))
    _client(srv, review=True)
    _client(srv, review=True, delta=True)
    _client(srv, slim=False, review=True)
    _send(srv)
    assert calls == [{}], "one state() per flush, the review board derived"
    assert "self.state(done_review=True)" not in SRC.read_text()


# --- (xi) the poll-echo names live under devices alone -----------------------


class _Health:
    def health_snapshot(self):
        return {"state": "ok", "last_ok_at": 5.0, "failing_for": 0.0}


def _paths_of(value, names, path=()):
    if isinstance(value, dict):
        for k, v in value.items():
            if k in names:
                yield path + (k,)
            yield from _paths_of(v, names, path + (k,))
    elif isinstance(value, list):
        for v in value:
            yield from _paths_of(v, names, path)


def test_poll_echo_fields_live_only_under_devices():
    daemon = BobDaemon()
    server = ApiServer(daemon, port=0)
    daemon._api = server
    daemon._relay_connector = _Health()
    daemon.remote_access_enabled = True
    state = server.state()
    found = list(_paths_of(state, api_mod._POLL_ECHO_FIELDS))
    assert found, "the health record put the echo names in the picture"
    assert all(p[0] == "devices" for p in found), found
    # And the producers: in the daemon module, the three names are reached
    # from `devices_snapshot` alone.
    daemon_src = (SRC.parent / "daemon.py").read_text()
    body = daemon_src.split("    def devices_snapshot(self)", 1)[1]
    body = body.split("\n    def ", 1)[0]
    rest = daemon_src.replace(body, "")
    for needle in ("health_snapshot()", "relay.last_frame_at(",
                   '"last_frame_at"'):
        assert needle in body
        assert needle not in rest, needle
    assert not re.search(r"\blast_ok_at\b|\bfailing_for\b", rest)
