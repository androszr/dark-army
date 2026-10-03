# host/tests/test_api_server.py
"""Pond Control API: routing, authorisation, SSE.

The client runs in a worker thread throughout. urllib is blocking, and the server
shares this test's event loop — calling it inline deadlocks the server rather than
testing it.
"""

import asyncio
import json
import time
import urllib.error
import urllib.request

import pytest
import pytest_asyncio

from dark_army_daemon import api_server as api_mod
from dark_army_daemon.api_server import ApiServer, load_or_create_token
from dark_army_daemon.daemon import BobDaemon
from tests.free_ports import free_port, free_ports


def _free_ports(count: int) -> list[int]:
    """From this run's and worker's block below the ephemeral range, not a
    bound-then-closed `:0` another worker's connection can take
    (`tests/free_ports.py`)."""
    return free_ports(count)


def _free_port() -> int:
    """From this run's and worker's block below the ephemeral range, not a
    bound-then-closed `:0` another worker's connection can take
    (`tests/free_ports.py`)."""
    return free_port()


# One for the plain server fixture, one for the board's own listener.
PORT, BOARD_PORT = _free_ports(2)


@pytest.fixture(autouse=True)
def _no_fleet_snapshot(monkeypatch):
    """Keep the real fleet out of these tests, and out of their teardown.

    `ApiServer.start()` asks the daemon for one agents snapshot, and that
    snapshot is real work on the real machine: every Claude transcript under
    ~/.claude read in an executor, then `ps` and a write to each live terminal's
    tty. Cancelling a test's event loop cannot cancel a job already running on a
    worker thread, so pytest-asyncio's teardown waits for it — with a busy fleet
    that wait was minutes per test, and several agents running this file at once
    sat behind each other for an hour. Nothing here asserts anything about the
    fleet: the API serves the state that was pushed into it."""
    monkeypatch.setattr(BobDaemon, "_schedule_agents_push", lambda self: None)


@pytest.fixture
def token_path(tmp_path, monkeypatch):
    path = tmp_path / "api-token"
    monkeypatch.setattr(api_mod, "API_TOKEN_PATH", path)
    monkeypatch.setattr(api_mod, "ensure_state_dir", lambda: tmp_path)
    return path


@pytest_asyncio.fixture
async def server(token_path):
    daemon = BobDaemon()
    srv = ApiServer(daemon, port=PORT)
    await srv.start()
    try:
        yield srv, daemon
    finally:
        await srv.stop()


def _fetch(path, data=None, headers=None, port=PORT):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=data, headers=headers or {},
        method="POST" if data is not None else "GET",
    )
    try:
        resp = urllib.request.urlopen(req, timeout=5)
        return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


async def fetch(*args, **kwargs):
    return await asyncio.to_thread(_fetch, *args, **kwargs)


def _fetch_headers(path, port=PORT):
    """(status, headers as one lowercased string). Separate from `_fetch`, which
    keeps its two-value shape for the dozens of callers that only want the body."""
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method="GET")
    try:
        resp = urllib.request.urlopen(req, timeout=5)
        return resp.status, str(resp.headers).lower()
    except urllib.error.HTTPError as exc:
        return exc.code, str(exc.headers).lower()


async def fetch_headers(*args, **kwargs):
    return await asyncio.to_thread(_fetch_headers, *args, **kwargs)


# --- token -------------------------------------------------------------------


def test_token_is_created_once_and_locked_down(token_path):
    """The file is the **session token** since the desk/session split; it is
    still created once, 0600, and read back unchanged across restarts."""
    first = load_or_create_token()
    assert first and token_path.exists()
    assert token_path.stat().st_mode & 0o777 == 0o600
    assert load_or_create_token() == first, "token must be stable across restarts"


def test_blank_token_file_is_replaced(token_path):
    token_path.write_text("   \n")
    assert load_or_create_token().strip()


# --- routing -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_state_endpoint_shape(server):
    status, body = await fetch("/api/state")
    assert status == 200
    state = json.loads(body)
    assert set(state) >= {"counts", "notifications", "agents", "generated_at",
                          "reconciler_available"}
    assert set(state["counts"]) == {"working", "idle", "attention", "subagents"}
    # Every bucket is present from the first frame, before any push. The panel
    # renders sections off these keys, and a missing one would read as "nothing
    # finished" rather than as "not asked yet".
    assert set(state["agents"]) == {"running", "sleeping", "waiting",
                                    "abandoned", "finished"}


@pytest.mark.asyncio
async def test_responses_refuse_to_be_framed(server):
    """The token and the Origin allowlist stop a cross-origin page *reading* a
    response. Framing needs neither, and the headers cost nothing to keep now
    that the API no longer serves a page of its own."""
    for path in ("/api/state",):
        status, head = await fetch_headers(path)
        assert status == 200
        assert "x-frame-options: deny" in head, path
        assert "frame-ancestors 'none'" in head, path


@pytest.mark.asyncio
async def test_reads_need_no_token(server):
    """A cross-origin page can issue this request but cannot read the response —
    no CORS headers — so leaving reads open costs nothing and keeps curl working."""
    assert (await fetch("/api/state"))[0] == 200


@pytest.mark.asyncio
async def test_unknown_path_is_404(server):
    assert (await fetch("/nope"))[0] == 404


@pytest.mark.asyncio
async def test_action_rejects_get(server):
    assert (await fetch("/api/action"))[0] == 405


# --- authorisation -----------------------------------------------------------


@pytest.mark.asyncio
async def test_write_without_token_is_forbidden(server):
    assert (await fetch("/api/action", b'{"action":"dismiss","session_id":"nobody"}'))[0] == 403


@pytest.mark.asyncio
async def test_write_with_wrong_token_is_forbidden(server):
    status, _ = await fetch("/api/action", b'{"action":"dismiss","session_id":"nobody"}',
                            {"X-Bob-Token": "guessed"})
    assert status == 403


@pytest.mark.asyncio
async def test_write_with_token_succeeds(server):
    srv, _ = server
    status, body = await fetch("/api/action", b'{"action":"dismiss","session_id":"nobody"}',
                               {"X-Bob-Token": srv.token})
    assert status == 200 and json.loads(body) == {"ok": True}


@pytest.mark.asyncio
async def test_foreign_origin_is_forbidden_even_with_the_token(server):
    """Defence in depth: a page that somehow learned the token still cannot drive
    the API from another origin."""
    srv, _ = server
    status, _ = await fetch("/api/action", b'{"action":"dismiss","session_id":"nobody"}',
                            {"X-Bob-Token": srv.token, "Origin": "https://evil.example"})
    assert status == 403


@pytest.mark.asyncio
async def test_our_own_origin_is_allowed(server):
    srv, _ = server
    status, _ = await fetch(
        "/api/action", b'{"action":"dismiss","session_id":"nobody"}',
        {"X-Bob-Token": srv.token, "Origin": f"http://127.0.0.1:{PORT}"},
    )
    assert status == 200


@pytest.mark.asyncio
async def test_unknown_action_is_rejected(server):
    srv, _ = server
    status, body = await fetch("/api/action", b'{"action":"launch-missiles"}',
                               {"X-Bob-Token": srv.token})
    assert status == 400 and json.loads(body)["action"] == "launch-missiles"


@pytest.mark.asyncio
async def test_dismiss_action_reaches_the_daemon(server):
    srv, daemon = server
    dismissed = []
    daemon.dismiss_notification = lambda sid: _noop(dismissed.append(sid))

    status, _ = await fetch("/api/action", b'{"action":"dismiss","session_id":"s1"}',
                            {"X-Bob-Token": srv.token})
    await asyncio.sleep(0.05)      # the handler fires it as a task
    assert status == 200 and dismissed == ["s1"]


async def _noop(_value=None):
    return None


def test_codex_action_routing_without_a_socket():
    """Hermetic contract for sandboxes that deny even loopback bind."""
    daemon = BobDaemon()
    called = []
    daemon.stop_codex_session = lambda sid: (called.append(("stop", sid)),
                                              (True, "stopping"))[1]
    daemon.hide_codex_session = lambda sid: (called.append(("hide", sid)),
                                              (True, "hidden"))[1]
    daemon.stop_session = lambda sid: pytest.fail("Codex reached generic stop")
    srv = ApiServer(daemon, port=0)

    status, _, body = srv._action({
        "action": "stop_session", "session_id": "codex:abc",
    })
    assert status == 200 and json.loads(body)["detail"] == "stopping"
    status, _, body = srv._action({
        "action": "hide_session", "session_id": "codex:abc",
    })
    assert status == 200 and json.loads(body)["detail"] == "hidden"
    assert called == [("stop", "codex:abc"), ("hide", "codex:abc")]


def test_hide_action_direct_refusal_and_missing_id():
    daemon = BobDaemon()
    daemon.hide_codex_session = lambda sid: (False, "only Codex rows can be hidden")
    srv = ApiServer(daemon, port=0)
    status, _, body = srv._action({
        "action": "hide_session", "session_id": "claude",
    })
    assert status == 409 and json.loads(body)["ok"] is False
    assert srv._action({"action": "hide_session"})[0] == 400


@pytest.mark.asyncio
async def test_delete_agent_answers_the_outcome_rather_than_accepting_blindly(server):
    """The one destructive action, and so the one that answers synchronously: the
    panel cannot tell a refused delete from a stale frame otherwise."""
    srv, daemon = server
    daemon.delete_abandoned_agent = lambda sid: (True, "deleted")

    status, body = await fetch("/api/action",
                               b'{"action":"delete_agent","session_id":"z"}',
                               {"X-Bob-Token": srv.token})
    assert status == 200 and json.loads(body) == {"ok": True, "detail": "deleted"}


@pytest.mark.asyncio
async def test_a_refused_delete_is_409_with_the_reason(server):
    srv, daemon = server
    daemon.delete_abandoned_agent = lambda sid: (False, "only abandoned agents "
                                                        "can be deleted")
    status, body = await fetch("/api/action",
                               b'{"action":"delete_agent","session_id":"live"}',
                               {"X-Bob-Token": srv.token})
    # Well-formed request, declined by the daemon — not a 400, and emphatically
    # not a 200 the panel would read as "gone".
    assert status == 409
    assert json.loads(body)["ok"] is False
    assert "abandoned" in json.loads(body)["detail"]


@pytest.mark.asyncio
async def test_stop_session_answers_the_outcome(server):
    """Synchronous for the same reason as delete: signalled or refused, and the
    row alone cannot tell those apart."""
    srv, daemon = server
    daemon.stop_session = lambda sid: (True, "stopping")

    status, body = await fetch("/api/action",
                               b'{"action":"stop_session","session_id":"z"}',
                               {"X-Bob-Token": srv.token})
    assert status == 200
    assert json.loads(body) == {"ok": True, "detail": "stopping"}


@pytest.mark.asyncio
async def test_a_refused_stop_is_409_with_the_reason(server):
    srv, daemon = server
    daemon.stop_session = lambda sid: (False, "PID 4242 is no longer a Claude "
                                              "session")
    status, body = await fetch("/api/action",
                               b'{"action":"stop_session","session_id":"x"}',
                               {"X-Bob-Token": srv.token})
    assert status == 409
    assert json.loads(body)["ok"] is False
    assert "no longer a Claude session" in json.loads(body)["detail"]


@pytest.mark.asyncio
async def test_reveal_answers_the_outcome(server):
    """Jump used to be fire-and-forget. A failed reveal leaves the screen exactly
    as it was, which is indistinguishable from a dead button — so the panel is
    told, and can say so."""
    srv, daemon = server

    async def reveal(sid):
        return True, "revealed (tab via ancestry, window raised via repo)"

    daemon.reveal_in_vscode = reveal
    status, body = await fetch("/api/action",
                               b'{"action":"reveal_session","session_id":"z"}',
                               {"X-Bob-Token": srv.token})
    assert status == 200
    assert json.loads(body)["ok"] is True


@pytest.mark.asyncio
async def test_a_failed_reveal_is_409_with_the_reason(server):
    srv, daemon = server

    async def reveal(sid):
        return False, "terminal tab focused, but the window would not come forward"

    daemon.reveal_in_vscode = reveal
    status, body = await fetch("/api/action",
                               b'{"action":"reveal_session","session_id":"z"}',
                               {"X-Bob-Token": srv.token})
    assert status == 409
    assert "would not come forward" in json.loads(body)["detail"]


@pytest.mark.asyncio
async def test_a_slow_reveal_stops_being_waited_on_but_keeps_going(server, monkeypatch):
    """Past the timeout we stop waiting rather than cancel: a reveal one slow
    raise from succeeding must not be killed to report a failure we do not know
    about, and the button must not sit there either."""
    srv, daemon = server
    finished = asyncio.Event()

    async def reveal(sid):
        try:
            await asyncio.sleep(0.5)
            return True, "eventually"
        finally:
            finished.set()

    daemon.reveal_in_vscode = reveal
    monkeypatch.setattr(api_mod, "REVEAL_TIMEOUT", 0.05)
    status, body = await fetch("/api/action",
                               b'{"action":"reveal_session","session_id":"z"}',
                               {"X-Bob-Token": srv.token})
    assert status == 200                     # nothing to report, not a failure
    assert json.loads(body)["detail"] == ""
    await asyncio.wait_for(finished.wait(), timeout=2.0)


@pytest.mark.asyncio
async def test_reveal_needs_the_token(server):
    srv, daemon = server
    called = []

    async def reveal(sid):
        called.append(sid)
        return True, ""

    daemon.reveal_in_vscode = reveal
    status, _ = await fetch("/api/action",
                            b'{"action":"reveal_session","session_id":"z"}')
    assert status == 403
    assert called == []


@pytest.mark.asyncio
async def test_stop_session_needs_the_token(server):
    """It sends a signal to one of the user's processes. A browser tab that can
    reach localhost must not be able to reach this."""
    srv, daemon = server
    called = []
    daemon.stop_session = lambda sid: (called.append(sid), (True, "stopping"))[1]

    status, _ = await fetch("/api/action",
                            b'{"action":"stop_session","session_id":"z"}')
    assert status == 403
    assert called == []


@pytest.mark.asyncio
async def test_codex_stop_dispatches_only_to_the_strict_handler(server):
    srv, daemon = server
    called = []
    daemon.stop_codex_session = lambda sid: (called.append(("codex", sid)),
                                              (True, "stopping"))[1]
    daemon.stop_session = lambda sid: pytest.fail("generic stop received Codex id")
    status, body = await fetch(
        "/api/action",
        b'{"action":"stop_session","session_id":"codex:abc"}',
        {"X-Bob-Token": srv.token},
    )
    assert status == 200
    assert json.loads(body)["detail"] == "stopping"
    assert called == [("codex", "codex:abc")]


@pytest.mark.asyncio
async def test_hide_session_returns_daemon_detail_and_refusal(server):
    srv, daemon = server
    daemon.hide_codex_session = lambda sid: (True, "hidden until this thread changes")
    status, body = await fetch(
        "/api/action",
        b'{"action":"hide_session","session_id":"codex:abc"}',
        {"X-Bob-Token": srv.token},
    )
    assert status == 200
    assert json.loads(body) == {
        "ok": True, "detail": "hidden until this thread changes",
    }

    daemon.hide_codex_session = lambda sid: (False, "only Codex rows can be hidden")
    status, body = await fetch(
        "/api/action",
        b'{"action":"hide_session","session_id":"claude"}',
        {"X-Bob-Token": srv.token},
    )
    assert status == 409
    assert json.loads(body)["ok"] is False


@pytest.mark.asyncio
async def test_hide_session_needs_token_origin_and_session_id(server):
    srv, daemon = server
    called = []
    daemon.hide_codex_session = lambda sid: (called.append(sid), (True, "hidden"))[1]
    payload = b'{"action":"hide_session","session_id":"codex:abc"}'
    assert (await fetch("/api/action", payload))[0] == 403
    assert (await fetch(
        "/api/action", payload,
        {"X-Bob-Token": srv.token, "Origin": "https://evil.example"},
    ))[0] == 403
    assert (await fetch(
        "/api/action", b'{"action":"hide_session"}',
        {"X-Bob-Token": srv.token},
    ))[0] == 400
    assert called == []


@pytest.mark.asyncio
async def test_an_action_without_a_session_id_is_not_an_action(server):
    """Falls through to the unknown-action branch rather than reaching the daemon
    with an empty id.

    Written against `delete_agent` until that action was removed, at which point
    it passed for the wrong reason — every branch rejects a name that no longer
    exists, so it proved nothing about the id check. Pointed at a live action.
    """
    srv, daemon = server
    daemon.stop_session = lambda sid: pytest.fail("reached the daemon")
    status, _ = await fetch("/api/action", b'{"action":"stop_session"}',
                            {"X-Bob-Token": srv.token})
    assert status == 400


@pytest.mark.asyncio
async def test_malformed_body_does_not_crash_the_server(server):
    srv, _ = server
    status, _ = await fetch("/api/action", b"{not json",
                            {"X-Bob-Token": srv.token})
    assert status == 400
    assert (await fetch("/api/state"))[0] == 200, "server died on bad input"


# --- observer plumbing -------------------------------------------------------


@pytest.mark.asyncio
async def test_server_registers_and_unregisters_as_an_observer(token_path):
    daemon = BobDaemon()
    srv = ApiServer(daemon, port=_free_port())
    await srv.start()
    assert srv in daemon._observers
    await srv.stop()
    assert srv not in daemon._observers


@pytest.mark.asyncio
async def test_pushed_state_is_reflected_without_recomputing(server):
    srv, daemon = server
    srv.on_activity_change(4, 1, 2, 3)
    srv.on_notification_change([{"session_id": "s1", "message": "hi"}])
    state = srv.state()
    assert state["counts"] == {"working": 4, "idle": 1, "attention": 2, "subagents": 3}
    assert state["notifications"][0]["message"] == "hi"


# --- SSE ---------------------------------------------------------------------


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


@pytest.mark.asyncio
async def test_stream_sends_current_state_immediately(server):
    reader, writer, head = await _open_stream()
    try:
        assert b"text/event-stream" in head
        event = await _next_event(reader)
        assert "counts" in event
    finally:
        writer.close()


@pytest.mark.asyncio
async def test_an_event_is_exactly_one_line(server):
    """One `data:` line per snapshot, and the panel depends on it.

    `AsyncLineSequence` — what the panel reads the stream with — never yields
    the blank line that ends an SSE event, so the panel dispatches on the
    `data:` line itself (see `API.readEvents`). That is only correct while an
    event cannot span lines, which holds because `json.dumps` emits no raw
    newline. This test is the guard: put a pretty-printer or a multi-line frame
    on that write and the panel silently stops updating, which is exactly the
    failure it took a flight recorder to find.
    """
    reader, writer, _ = await _open_stream()
    try:
        raw = await asyncio.wait_for(reader.readuntil(b"\n\n"), timeout=5)
        body = raw[: -len(b"\n\n")]
        assert body.startswith(b"data: ")
        assert b"\n" not in body
    finally:
        writer.close()


@pytest.mark.asyncio
async def test_stream_pushes_on_state_change(server):
    srv, _ = server
    reader, writer, _ = await _open_stream()
    try:
        await _next_event(reader)                 # initial
        srv.on_activity_change(7, 0, 0, 0)
        # Read until the change shows rather than asserting on the very next
        # frame: any observer callback broadcasts the *whole* state, so a
        # background agent-enrichment landing between the initial read and this
        # push puts a frame carrying the old counts in front of ours. The
        # promise is that the change reaches the client, not that it is the next
        # thing down the wire.
        for _ in range(10):
            event = await _next_event(reader)
            if event["counts"]["working"] == 7:
                break
        assert event["counts"]["working"] == 7
    finally:
        writer.close()


# --- a stuck client's queue --------------------------------------------------
#
# Seam: a queue built the way `_serve_events` builds it, registered on
# `srv._clients` with **no getter waiting**, and `_flush` driven directly after
# writing distinct `srv._counts`. A live socket cannot fill the queue —
# `_serve_events` parked on `queue.get()` takes every `put_nowait` straight
# off, so `qsize()` reads 0 (see `_quiet`), and `_broadcast` coalesces a burst
# of `on_activity_change`s into one flush plus one booked flush.


def _stalled_client(srv) -> asyncio.Queue:
    queue: asyncio.Queue = asyncio.Queue(maxsize=api_mod.SSE_CLIENT_QUEUE_MAX)
    srv._clients[queue] = False
    return queue


def _flush_with_working(srv, working: int) -> None:
    """One frame whose `counts["working"]` is `working` — distinct every time,
    so `_news()` moves and nothing here is a clock-only frame."""
    srv._counts = {"working": working, "idle": 0, "attention": 0, "subagents": 0}
    srv._flush()


def _drain_working(queue: asyncio.Queue) -> list:
    out = []
    while True:
        try:
            item = queue.get_nowait()
        except asyncio.QueueEmpty:
            return out
        out.append(None if item is None else json.loads(item)["counts"]["working"])


@pytest.mark.asyncio
async def test_slow_client_queue_is_bounded(server):
    """A panel that stops reading must not grow a queue without bound; it only
    ever needs the newest state. Driven on the `_flush` / registered-queue
    seam above: fifty frames land, the cap is what is left."""
    srv, _ = server
    queue = _stalled_client(srv)
    for n in range(50):
        _flush_with_working(srv, n)
    assert queue.qsize() == api_mod.SSE_CLIENT_QUEUE_MAX, \
        f"backlog is {queue.qsize()}"


@pytest.mark.asyncio
async def test_stalled_client_resumes_on_newest_state(server):
    """A reader that starts again sees the current picture, not a replay: the
    newest frames are kept and the oldest are the ones dropped."""
    srv, _ = server
    queue = _stalled_client(srv)
    for n in range(20):
        _flush_with_working(srv, n)
    cap = api_mod.SSE_CLIENT_QUEUE_MAX
    assert _drain_working(queue) == list(range(20 - cap, 20))


@pytest.mark.asyncio
async def test_stalled_slim_client_is_not_left_behind(server):
    """A `?sections=changed` client's frames omit a section against the last
    frame flushed server-wide, so if the one frame carrying a changed section
    is the oldest and gets dropped, every surviving slim frame would omit it.
    The overflow put is therefore a whole frame: a reader that catches up to
    the tail holds the current notifications, not the stale copy."""
    srv, _ = server
    queue = _stalled_client(srv)
    srv._clients[queue] = True                       # slim
    _flush_with_working(srv, 0)                      # a plain first frame
    srv._notifications = [{"session_id": "s1", "message": "hi"}]
    _flush_with_working(srv, 1)                      # the one frame carrying it
    for n in range(2, api_mod.SSE_CLIENT_QUEUE_MAX + 4):
        _flush_with_working(srv, n)                  # count-only, past the cap
    frames = []
    while True:
        try:
            frames.append(json.loads(queue.get_nowait()))
        except asyncio.QueueEmpty:
            break
    assert len(frames) == api_mod.SSE_CLIENT_QUEUE_MAX
    assert frames[0]["counts"]["working"] > 1, "frame 1 was dropped"
    tail = frames[-1]
    assert tail["notifications"] == [{"session_id": "s1", "message": "hi"}]
    assert "agents" in tail and "board" in tail, "the overflow put is whole"


@pytest.mark.asyncio
async def test_stop_unblocks_a_full_queue(server):
    """`stop()` on a full queue must not raise; the `None` sentinel that lets
    the writer exit still lands, as the newest item."""
    srv, _ = server
    queue = _stalled_client(srv)
    for n in range(api_mod.SSE_CLIENT_QUEUE_MAX * 2):
        _flush_with_working(srv, n)
    assert queue.full()
    await srv.stop()
    drained = _drain_working(queue)
    assert drained[-1] is None
    assert not srv._clients
    assert not srv._overflow_logged


@pytest.mark.asyncio
async def test_queue_overflow_logs_once_per_client(server, caplog):
    """The detailed log names a stuck reader once, by queue id — never per
    dropped frame, never the write token, never the payload."""
    srv, _ = server
    queue = _stalled_client(srv)
    caplog.set_level("DEBUG", logger="dark-army.api")
    for n in range(api_mod.SSE_CLIENT_QUEUE_MAX + 2):   # two overflows
        _flush_with_working(srv, n)
    lines = [r.getMessage() for r in caplog.records
             if "dropping its oldest frames" in r.getMessage()]
    assert len(lines) == 1, lines
    assert str(id(queue)) in lines[0]
    assert srv.token not in lines[0]
    assert '"counts"' not in lines[0]


@pytest.mark.asyncio
async def test_stop_releases_streaming_clients(server):
    srv, _ = server
    reader, writer, _ = await _open_stream()
    try:
        await _next_event(reader)
        await srv.stop()
        assert not srv._clients
    finally:
        writer.close()


# --- broadcast coalescing ----------------------------------------------------
#
# Four observer callbacks land on `_broadcast` and one hook event trips two of
# them, so the stream used to carry pairs of full snapshots milliseconds apart,
# two thirds of which said nothing new. The panel redraws its whole list per
# frame, and a list relaid out under the pointer loses clicks.


async def _quiet(reader, within):
    """Assert nothing comes down the wire for `within` seconds. Read from the
    socket rather than from the client's queue: `_serve_events` is parked on
    `queue.get()` between frames, so a `put_nowait` hands straight to the getter
    and `qsize()` is 0 whether a frame went out or not."""
    try:
        await asyncio.wait_for(reader.readuntil(b"\n\n"), timeout=within)
    except asyncio.TimeoutError:
        return
    raise AssertionError("a frame went out inside the floor")


async def _isolate_limiter(srv, reader):
    """Leave the limiter the only thing driving the stream.

    Detaching the observer stops the daemon's *next* enrichment push, but not
    the one it already made: `_broadcast` books its frame with `call_later`,
    so a push that landed before the test began still fires — arriving inside
    whichever quiet window the test is about to assert, which is the whole of
    this file's flakiness under full-suite load. So cancel the booking as well,
    then read until the wire goes quiet: from here every frame is the test's
    own.
    """
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


@pytest.mark.asyncio
async def test_a_burst_of_changes_is_one_frame(server):
    """Ten changes inside the floor buy one frame, carrying the last of them."""
    srv, _ = server
    reader, writer, _ = await _open_stream()
    try:
        await _next_event(reader)
        for n in range(10):
            srv.on_activity_change(n, 0, 0, 0)
        for _ in range(10):
            event = await _next_event(reader)
            if event["counts"]["working"] == 9:
                break
        assert event["counts"]["working"] == 9, "the newest state never arrived"
        # One frame, not ten: the rest of the burst was folded into it.
        await _quiet(reader, api_mod.BROADCAST_MIN_INTERVAL * 2)
    finally:
        writer.close()


@pytest.mark.asyncio
async def test_a_frame_that_is_only_clocks_waits_for_the_quiet_floor(server,
                                                                    monkeypatch):
    """Two snapshots differing only in elapsed time are the same news, and the
    second is held to the wider floor — deferred rather than dropped, because
    the durations on the rows are real and have to keep counting."""
    srv, _ = server
    monkeypatch.setattr(api_mod, "BROADCAST_QUIET_INTERVAL", 30.0)
    reader, writer, _ = await _open_stream()
    # The daemon's own enrichment pushes land on the same method and would read
    # as news arriving from nowhere. This test is about the limiter, so it is
    # the only thing left driving it.
    await _isolate_limiter(srv, reader)
    try:
        srv.on_agents_change(_rows())
        await _next_event(reader)
        # The same row, a second older.
        srv.on_agents_change(_rows(idle_seconds=2.0))
        await _quiet(reader, api_mod.BROADCAST_MIN_INTERVAL * 3)
    finally:
        writer.close()


@pytest.mark.asyncio
async def test_news_is_not_held_back_behind_a_deferred_clock_frame(server,
                                                                  monkeypatch):
    """The failure mode of the fix itself: a clock-only frame books a flush a
    whole quiet floor away, and a real change arriving behind it must pull that
    booking forward rather than queue up behind it."""
    srv, _ = server
    monkeypatch.setattr(api_mod, "BROADCAST_QUIET_INTERVAL", 30.0)
    reader, writer, _ = await _open_stream()
    # The daemon's own enrichment pushes land on the same method and would read
    # as news arriving from nowhere. This test is about the limiter, so it is
    # the only thing left driving it.
    await _isolate_limiter(srv, reader)
    try:
        srv.on_agents_change(_rows())
        await _next_event(reader)
        srv.on_agents_change(_rows(idle_seconds=2.0))          # clocks only
        srv.on_agents_change(_rows(idle_seconds=2.0, state="waiting"))   # news
        event = await asyncio.wait_for(_next_event(reader), timeout=2)
        assert event["agents"]["running"][0]["state"] == "waiting"
    finally:
        writer.close()


# --- slim frames -------------------------------------------------------------
#
# A client that asked for `?sections=changed` is not re-sent a *section* that
# has not changed, clocks aside — every one of `_OMITTABLE_SECTIONS`, the fleet
# and the board included. On an idle machine that leaves the clock and the two
# reconciler scalars and nothing else. Key *absence* is the omission marker — an
# empty board is a real state that only ever appears inside a present section —
# and the attach frame is always a full `state()`, so a client holds every
# section from its first frame. A client that never sends the query gets today's
# frames forever.


@pytest.mark.asyncio
async def test_a_slim_stream_omits_an_unchanged_board(server):
    srv, _ = server
    reader, writer, _ = await _open_stream(query="?sections=changed")
    try:
        first = await _next_event(reader)
        assert "board" in first, "the attach frame must always be full"
        await _isolate_limiter(srv, reader)
        srv.on_board_change({"counts": {"prep": 1}, "cards": [],
                             "generated_at": 1.0})
        event = await _next_event(reader)
        assert "board" in event, "a changed board rides the frame"
        srv.on_activity_change(7, 0, 0, 0)         # news that is not the board
        event = await _next_event(reader)
        assert event["counts"]["working"] == 7
        assert "board" not in event, \
            "an unchanged board must not ride a slim frame"
    finally:
        writer.close()


@pytest.mark.asyncio
async def test_a_slim_stream_carries_the_board_when_it_changes(server):
    """The must-see-writes-immediately property: a board edit between pushes
    puts the new board on the very next slim frame."""
    srv, _ = server
    reader, writer, _ = await _open_stream(query="?sections=changed")
    try:
        await _next_event(reader)
        await _isolate_limiter(srv, reader)
        srv.on_board_change({"counts": {"prep": 1}, "cards": [],
                             "generated_at": 1.0})
        await _next_event(reader)
        srv.on_board_change({"counts": {"prep": 2}, "cards": [],
                             "generated_at": 2.0})
        event = await _next_event(reader)
        assert event["board"]["counts"]["prep"] == 2
    finally:
        writer.close()


@pytest.mark.asyncio
async def test_a_stream_without_the_slim_query_always_carries_the_board(server):
    """No query means today's full frames, byte for byte — the shape today's
    panel expects, and what an older panel keeps getting forever."""
    srv, _ = server
    reader, writer, _ = await _open_stream()
    try:
        await _next_event(reader)
        await _isolate_limiter(srv, reader)
        srv.on_board_change({"counts": {"prep": 1}, "cards": [],
                             "generated_at": 1.0})
        event = await _next_event(reader)
        assert "board" in event
        srv.on_activity_change(7, 0, 0, 0)         # news that is not the board
        event = await _next_event(reader)
        assert event["counts"]["working"] == 7
        assert "board" in event
    finally:
        writer.close()


@pytest.mark.asyncio
async def test_the_attach_frame_is_always_full(server):
    """A slim client's first frame carries every top-level key `/api/state`
    serves, so it holds a board before any omission can happen. This write
    lives in `_serve_events`, deliberately not in `_flush` — the slim decision
    is global, the attach frame is per-client."""
    _, state_body = await fetch("/api/state")
    reader, writer, _ = await _open_stream(query="?sections=changed")
    try:
        first = await _next_event(reader)
        assert set(first) == set(json.loads(state_body))
    finally:
        writer.close()


@pytest.mark.asyncio
async def test_state_endpoint_still_serves_a_complete_snapshot(server):
    """The resync contract: `/api/state` stays complete with slim clients
    attached — it is what the panel GETs after a write and on every open."""
    srv, _ = server
    reader, writer, _ = await _open_stream(query="?sections=changed")
    try:
        await _next_event(reader)
        await _isolate_limiter(srv, reader)
        srv.on_board_change({"counts": {"prep": 1}, "cards": [],
                             "generated_at": 1.0})
        await _next_event(reader)
        srv.on_activity_change(3, 0, 0, 0)
        event = await _next_event(reader)
        assert "board" not in event               # the stream omitted it
        status, body = await fetch("/api/state")
        assert status == 200
        assert json.loads(body)["board"]["counts"]["prep"] == 1
    finally:
        writer.close()


@pytest.mark.asyncio
async def test_an_empty_board_is_still_sent_as_a_board(server):
    """A board that *changed to* `cards: []` is news, not omission — emptiness
    only ever appears inside a present section, never as an absent key."""
    srv, _ = server
    reader, writer, _ = await _open_stream(query="?sections=changed")
    try:
        await _next_event(reader)
        await _isolate_limiter(srv, reader)
        srv.on_board_change({"counts": {"prep": 1},
                             "cards": [{"id": "c1", "title": "x"}],
                             "generated_at": 1.0})
        await _next_event(reader)
        srv.on_board_change({"counts": {"prep": 0}, "cards": [],
                             "generated_at": 2.0})
        event = await _next_event(reader)
        assert "board" in event
        assert event["board"]["cards"] == []
    finally:
        writer.close()


@pytest.mark.asyncio
async def test_a_slim_stream_omits_an_unchanged_fleet(server):
    """The section this change is really about: `agents` is the frame's other
    large section, and a board edit must not drag the whole fleet along."""
    srv, _ = server
    reader, writer, _ = await _open_stream(query="?sections=changed")
    try:
        first = await _next_event(reader)
        assert "agents" in first, "the attach frame must always be full"
        await _isolate_limiter(srv, reader)
        srv.on_agents_change(_rows())
        event = await _next_event(reader)
        assert "agents" in event, "a changed fleet rides the frame"
        srv.on_board_change({"counts": {"prep": 1}, "cards": [],
                             "generated_at": 1.0})
        event = await _next_event(reader)
        assert "board" in event
        assert "agents" not in event, \
            "an unchanged fleet must not ride a slim frame"
    finally:
        writer.close()


@pytest.mark.asyncio
async def test_a_slim_stream_omits_a_fleet_whose_only_change_is_a_clock(server):
    """The whole point: a row a second older is not news, so the fleet is
    omitted and the panel ages the row off its own second hand."""
    srv, _ = server
    reader, writer, _ = await _open_stream(query="?sections=changed")
    try:
        await _next_event(reader)
        await _isolate_limiter(srv, reader)
        srv.on_agents_change(_rows(idle_seconds=1.0))
        await _next_event(reader)
        srv.on_agents_change(_rows(idle_seconds=2.0))
        srv.on_activity_change(7, 0, 0, 0)      # news that is not the fleet
        event = await _next_event(reader)
        assert event["counts"]["working"] == 7
        assert "agents" not in event
    finally:
        writer.close()


@pytest.mark.asyncio
async def test_a_slim_stream_omits_every_unchanged_section(server):
    """One list, audited in one place: after a frame that carried everything, a
    frame whose only news is `counts` carries `counts` and no other section."""
    srv, _ = server
    reader, writer, _ = await _open_stream(query="?sections=changed")
    try:
        await _next_event(reader)
        await _isolate_limiter(srv, reader)
        srv.on_agents_change(_rows())
        await _next_event(reader)
        srv.on_activity_change(7, 0, 0, 0)
        event = await _next_event(reader)
        assert event["counts"]["working"] == 7
        for section in api_mod._OMITTABLE_SECTIONS:
            if section == "counts":
                continue
            assert section not in event, f"{section} was unchanged"
    finally:
        writer.close()


@pytest.mark.asyncio
async def test_each_section_rides_the_frame_that_changes_it(server):
    """Per section, the must-see-writes-immediately property."""
    srv, daemon = server
    reader, writer, _ = await _open_stream(query="?sections=changed")
    try:
        await _next_event(reader)
        await _isolate_limiter(srv, reader)
        cases = [
            ("counts", lambda: srv.on_activity_change(4, 0, 0, 0)),
            ("agents", lambda: srv.on_agents_change(_rows(nickname="Relay"))),
            ("notifications", lambda: srv.on_notification_change(
                [{"session_id": "s", "question": "?"}])),
            ("board", lambda: srv.on_board_change(
                {"counts": {"prep": 3}, "cards": [], "generated_at": 3.0})),
            ("signals", lambda: (
                setattr(daemon, "_global_signals", ["five hour budget"]),
                srv._broadcast())),
            ("mesh", lambda: (setattr(daemon, "_mesh", [{"from": "a",
                                                         "to": "b"}]),
                              srv._broadcast())),
        ]
        for section, change in cases:
            change()
            event = await _next_event(reader)
            assert section in event, f"{section} changed and must ride"
    finally:
        writer.close()


@pytest.mark.asyncio
async def test_an_idle_slim_client_is_sent_no_frame(server, monkeypatch):
    """The saving, taken to its end: a second in which nothing at all
    happened costs a slim client nothing. The frame it used to get — the
    clock and the two reconciler scalars — said nothing it could use, since
    no clock on the panel depends on `generated_at` advancing
    (`test_stream_diet.py` pins the full client's deferred frame)."""
    srv, _ = server
    monkeypatch.setattr(api_mod, "BROADCAST_QUIET_INTERVAL", 0.2)
    reader, writer, _ = await _open_stream(query="?sections=changed")
    try:
        await _next_event(reader)
        await _isolate_limiter(srv, reader)
        srv.on_activity_change(7, 0, 0, 0)
        await _next_event(reader)
        # Nothing changed at all — deferred to the quiet floor, then sent to
        # full clients alone.
        srv.on_activity_change(7, 0, 0, 0)
        await _quiet(reader, 1.0)
    finally:
        writer.close()


@pytest.mark.asyncio
async def test_the_two_reconciler_scalars_ride_every_slim_frame(server):
    """They are top-level scalars, not sections: a few bytes each, no carry
    story in a decoder, and `reconciler_available` is a tri-state where absent
    already means "the daemon did not say"."""
    srv, _ = server
    reader, writer, _ = await _open_stream(query="?sections=changed")
    try:
        first = await _next_event(reader)
        await _isolate_limiter(srv, reader)
        assert "reconciler_available" in first
        assert "reconciler_error" in first
        srv.on_activity_change(7, 0, 0, 0)
        event = await _next_event(reader)
        assert "reconciler_available" in event
        assert "reconciler_error" in event
        assert "generated_at" in event
    finally:
        writer.close()


@pytest.mark.asyncio
async def test_an_emptied_section_is_present_not_omitted(server):
    """Emptiness is a real state and only absence is the marker — for the
    fleet and the notifications exactly as for the board."""
    srv, _ = server
    reader, writer, _ = await _open_stream(query="?sections=changed")
    try:
        await _next_event(reader)
        await _isolate_limiter(srv, reader)
        srv.on_agents_change(_rows())
        await _next_event(reader)
        srv.on_notification_change([{"session_id": "s", "question": "?"}])
        await _next_event(reader)
        srv.on_agents_change({"running": [], "sleeping": [], "waiting": [],
                              "abandoned": [], "finished": []})
        event = await _next_event(reader)
        assert event["agents"]["running"] == []
        srv.on_notification_change([])
        event = await _next_event(reader)
        assert event["notifications"] == []
    finally:
        writer.close()


@pytest.mark.asyncio
async def test_a_stream_without_the_slim_query_carries_every_section(server):
    """An older panel never sends the query and keeps today's frames, byte for
    byte — every section on every frame, however quiet the machine."""
    srv, _ = server
    reader, writer, _ = await _open_stream()
    try:
        first = await _next_event(reader)
        await _isolate_limiter(srv, reader)
        srv.on_activity_change(7, 0, 0, 0)
        event = await _next_event(reader)
        for section in api_mod._OMITTABLE_SECTIONS:
            assert section in first
            assert section in event, f"{section} must ride a full frame"
    finally:
        writer.close()


def test_the_omittable_sections_are_state_s_own_sections():
    """The list is auditable in one place, and it is exactly `state()`'s
    top level minus the clock and the two scalars."""
    assert len(api_mod._OMITTABLE_SECTIONS) == 15
    assert "review" in api_mod._OMITTABLE_SECTIONS
    assert "inbox" in api_mod._OMITTABLE_SECTIONS
    assert "security" in api_mod._OMITTABLE_SECTIONS
    assert "mission" in api_mod._OMITTABLE_SECTIONS
    assert "power" in api_mod._OMITTABLE_SECTIONS
    assert "generated_at" not in api_mod._OMITTABLE_SECTIONS
    assert "reconciler_available" not in api_mod._OMITTABLE_SECTIONS
    assert "reconciler_error" not in api_mod._OMITTABLE_SECTIONS


def test_news_drops_the_clocks_and_keeps_everything_else():
    state = {"generated_at": 1.0,
             "agents": {"running": [{"session_id": "s", "idle_seconds": 3.0,
                                     "nickname": "Forge",
                                     "metrics": {"received_at": 9.0,
                                                 "cost_usd": 1.5}}]}}
    assert api_mod._news(state) == {
        "agents": {"running": [{"session_id": "s", "nickname": "Forge",
                                "metrics": {"cost_usd": 1.5}}]}}


# --- panel assets in a frozen bundle -----------------------------------------


@pytest.mark.asyncio
async def test_a_taken_port_leaves_no_token_behind(server, token_path):
    """The menu decides Pond Control is reachable by checking the token. A
    second server losing the bind must not look reachable — the user would get a
    browser tab pointed at a refused connection instead of an honest alert."""
    second = ApiServer(BobDaemon(), port=PORT)
    with pytest.raises(OSError):
        await second.start()
    assert second.token == ""


# --- static assets & host spelling -------------------------------------------


@pytest.mark.asyncio
async def test_static_serving_is_an_allowlist_not_a_path(server):
    """No request path ever reaches the filesystem, so there is nothing to
    traverse out of."""
    for path in ("/../setup.py", "/index.html", "/../../etc/passwd",
                 "/%2e%2e/setup.py"):
        assert (await fetch(path))[0] == 404, path


@pytest.mark.asyncio
async def test_url_uses_localhost(server):
    srv, _ = server
    assert srv.url == f"http://localhost:{PORT}/"


@pytest.mark.asyncio
async def test_bound_on_both_loopback_families(server, token_path):
    """macOS resolves localhost to ::1 first. Handing out a localhost URL while
    binding only the IPv4 literal would send the browser to a refused connection."""
    for host in ("127.0.0.1", "::1"):
        reader, writer = await asyncio.open_connection(host, PORT)
        writer.write(b"GET /api/state HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
        await writer.drain()
        head = await asyncio.wait_for(reader.readuntil(b"\r\n"), timeout=5)
        assert b"200" in head, f"no listener on {host}"
        writer.close()


@pytest.mark.asyncio
async def test_ipv6_origin_is_accepted(server):
    srv, _ = server
    status, _ = await fetch("/api/action", b'{"action":"dismiss","session_id":"nobody"}',
                            {"X-Bob-Token": srv.token,
                             "Origin": f"http://[::1]:{PORT}"})
    assert status == 200


# --- history endpoint --------------------------------------------------------


@pytest.mark.asyncio
async def test_history_says_so_when_the_database_is_closed(server):
    _, daemon = server
    assert daemon._history is None
    status, body = await fetch("/api/history")
    payload = json.loads(body)
    assert status == 200 and payload["available"] is False
    assert "reason" in payload, "an unavailable report must say why"


@pytest.mark.asyncio
async def test_history_report_shape(server, tmp_path):
    from dark_army_daemon.history import HistoryStore
    import time as _time

    _, daemon = server
    daemon._history = HistoryStore(tmp_path / "h.db")
    daemon._history.connect()
    daemon._history.add_turn("s1", _time.time(), message_id="m1",
                             model="claude-opus-4-8", output_tokens=1_000_000)
    daemon._history.upsert_session("s1", project="proj", cost_usd=25.0)

    status, body = await fetch("/api/history?range=30d")
    report = json.loads(body)
    assert status == 200 and report["available"] is True
    assert report["range"] == "30d"
    # Every section the panel's history tab renders. Named explicitly because a
    # missing key is not a visible failure there — the section just quietly draws
    # its empty state, which reads as "nothing happened" rather than "not served".
    assert set(report) >= {"totals", "daily", "by_project", "by_model", "waiting",
                           "limits", "hourly", "context", "sessions"}
    assert report["by_project"][0]["project"] == "proj"
    assert report["daily"][0]["cost_usd"] == pytest.approx(25.0)
    assert len(report["hourly"]) == 24, "every hour, present or not"
    assert report["sessions"][0]["session_id"] == "s1"
    assert set(report["limits"]) >= {"series", "resets", "current",
                                     "burn_pct_per_hour", "projected_full_at"}
    assert set(report["waiting"]) >= {"by_day", "by_project", "longest"}
    daemon._history.close()


@pytest.mark.asyncio
async def test_history_rejects_an_unknown_range(server):
    status, body = await fetch("/api/history?range=since-tuesday")
    assert status == 400
    assert "allowed" in json.loads(body)


@pytest.mark.asyncio
async def test_history_defaults_to_thirty_days(server):
    _, body = await fetch("/api/history")
    assert json.loads(body).get("range", "30d") == "30d"


_AGGREGATE_KEYS = ("totals", "daily", "sessions", "by_project", "by_model",
                   "waiting", "limits", "hourly", "context", "range")


@pytest.mark.asyncio
async def test_history_session_report(server, tmp_path):
    from dark_army_daemon.history import HistoryStore
    import time as _time

    _, daemon = server
    daemon._history = HistoryStore(tmp_path / "h.db")
    daemon._history.connect()
    daemon._history.upsert_session("s1", project="proj", title="T", cost_usd=1.5)
    daemon._history.add_turn("s1", _time.time(), message_id="m1",
                             agent_id="", tool_name="Bash", output_tokens=10)
    daemon._session_states["s1"] = {"state": "working"}

    status, body = await fetch("/api/history?session=s1")
    report = json.loads(body)
    assert status == 200 and report["available"] is True
    live = report["live"]
    assert live is True
    assert report["session"]["session_id"] == "s1"
    assert report["session"]["title"] == "T"
    assert report["session"]["turns"] == 1
    assert report["session"]["tools"] == [["Bash", 1]]
    for key in _AGGREGATE_KEYS:
        assert key not in report, f"session response must not carry {key}"
    daemon._history.close()


@pytest.mark.asyncio
async def test_history_session_live_from_the_codex_roster(server, tmp_path):
    """Codex fires no hooks, so it never enters `_session_states`; the fleet
    keeps its row from `_codex_records`, and the card's record must agree."""
    from dark_army_daemon.history import HistoryStore
    from dark_army_daemon import codex_rollouts

    _, daemon = server
    daemon._history = HistoryStore(tmp_path / "h.db")
    daemon._history.connect()
    daemon._history.upsert_session("codex:abc", project="proj", title="T")
    daemon._session_states.clear()
    daemon._codex_records["codex:abc"] = codex_rollouts.CodexRecord(
        session_id="codex:abc", thread_id="abc", path=tmp_path / "abc.jsonl")

    status, body = await fetch("/api/history?session=codex:abc")
    report = json.loads(body)
    assert status == 200 and report["available"] is True
    live = report["live"]
    assert live is True
    daemon._codex_records.pop("codex:abc", None)
    daemon._history.close()


@pytest.mark.asyncio
async def test_history_session_finished_is_not_live(server, tmp_path):
    """A tombstoned session is over even while its journal is still inside the
    Codex grace window: a Done card must not be captioned as still working."""
    from dark_army_daemon.history import HistoryStore
    from dark_army_daemon import codex_rollouts

    _, daemon = server
    daemon._history = HistoryStore(tmp_path / "h.db")
    daemon._history.connect()
    daemon._history.upsert_session("codex:abc", project="proj", title="T")
    daemon._session_states.clear()
    daemon._codex_records["codex:abc"] = codex_rollouts.CodexRecord(
        session_id="codex:abc", thread_id="abc", path=tmp_path / "abc.jsonl")
    daemon._finished["codex:abc"] = {"project": "proj"}

    status, body = await fetch("/api/history?session=codex:abc")
    report = json.loads(body)
    assert status == 200 and report["available"] is True
    assert report["live"] is False
    daemon._codex_records.pop("codex:abc", None)
    daemon._finished.pop("codex:abc", None)
    daemon._history.close()


@pytest.mark.asyncio
async def test_history_session_unknown_is_null(server, tmp_path):
    from dark_army_daemon.history import HistoryStore

    _, daemon = server
    daemon._history = HistoryStore(tmp_path / "h.db")
    daemon._history.connect()
    status, body = await fetch("/api/history?session=nope")
    report = json.loads(body)
    assert status == 200 and report["available"] is True
    assert report["session"] is None
    assert report["live"] is False
    for key in _AGGREGATE_KEYS:
        assert key not in report
    daemon._history.close()


@pytest.mark.asyncio
async def test_history_session_says_so_when_the_database_is_closed(server):
    _, daemon = server
    assert daemon._history is None
    status, body = await fetch("/api/history?session=s1")
    payload = json.loads(body)
    assert status == 200 and payload["available"] is False
    assert "reason" in payload
    for key in _AGGREGATE_KEYS:
        assert key not in payload


@pytest.mark.asyncio
async def test_history_session_rejects_a_malformed_id(server):
    status, body = await fetch("/api/history?session=bad/id")
    assert status == 400
    assert "error" in json.loads(body)
    status, body = await fetch("/api/history?session=")
    assert status == 400
    too_long = "a" * 129
    status, body = await fetch(f"/api/history?session={too_long}")
    assert status == 400


@pytest.mark.asyncio
async def test_history_session_accepts_a_codex_id(server, tmp_path):
    """Codex ids are `codex:{thread}`. A 400 here made the sheet say it
    couldn't reach Dark Army, which is the wrong absence — Codex has no sessions
    row, so the honest answer is `session: null`."""
    from dark_army_daemon.history import HistoryStore
    from urllib.parse import quote

    _, daemon = server
    daemon._history = HistoryStore(tmp_path / "h.db")
    daemon._history.connect()
    status, body = await fetch("/api/history?session=codex:abc")
    report = json.loads(body)
    assert status == 200 and report["available"] is True
    assert report["session"] is None
    encoded = quote("codex:abc", safe="")
    status, body = await fetch("/api/history?session=" + encoded)
    report = json.loads(body)
    assert status == 200 and report["available"] is True
    assert report["session"] is None
    daemon._history.close()


@pytest.mark.asyncio
async def test_history_session_unquotes_a_hyphenated_id(server, tmp_path):
    """The panel percent-encodes with `.alphanumerics`, so a UUID's hyphens
    arrive as %2D. The charset check is on the decoded id."""
    from dark_army_daemon.history import HistoryStore
    from urllib.parse import quote

    _, daemon = server
    daemon._history = HistoryStore(tmp_path / "h.db")
    daemon._history.connect()
    sid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    daemon._history.upsert_session(sid, title="U")
    status, body = await fetch("/api/history?session=" + quote(sid, safe=""))
    report = json.loads(body)
    assert status == 200 and report["session"]["session_id"] == sid
    daemon._history.close()


# --- /api/usage --------------------------------------------------------------


def _claude_config(tmp_path, monkeypatch, percent=25):
    """Stand in for ~/.claude.json. Patched rather than written to the real home:
    the tests must not depend on whether Claude Code has run on this machine."""
    from dark_army_daemon import limits as limits_mod

    path = tmp_path / ".claude.json"
    path.write_text(json.dumps({
        "cachedUsageUtilization": {
            "fetchedAtMs": 1_785_071_303_936,
            "utilization": {"limits": [
                {"kind": "session", "group": "session", "percent": percent,
                 "resets_at": "2026-07-26T16:30:00+00:00", "scope": None},
                {"kind": "weekly_scoped", "group": "weekly", "percent": 11,
                 "resets_at": "2026-08-01T23:59:59+00:00",
                 "scope": {"model": {"display_name": "Fable"}}},
            ]},
        },
        "cachedGrowthBookFeatures": {"tengu_rate_limit_promo_notices": []},
    }))
    monkeypatch.setattr(limits_mod, "CLAUDE_CONFIG_PATH", path)
    return path


@pytest.mark.asyncio
async def test_usage_serves_bars_without_a_history_database(server, tmp_path,
                                                            monkeypatch):
    """The two halves fail independently: no history means no attribution, and the
    bars are still worth the request."""
    _claude_config(tmp_path, monkeypatch)
    _, daemon = server
    daemon._history = None

    status, body = await fetch("/api/usage")
    report = json.loads(body)
    # The default window is the five-hour one — the shortest limit, and the one
    # a reader opening this view is usually asking about.
    assert status == 200 and report["window"] == "session"
    assert [b["kind"] for b in report["limits"]["bars"]] == ["session", "weekly_scoped"]
    assert report["attribution"]["available"] is False
    assert "reason" in report["attribution"]


@pytest.mark.asyncio
async def test_usage_includes_codex_rate_limit_bars(server, tmp_path, monkeypatch):
    _claude_config(tmp_path, monkeypatch)
    _, daemon = server
    daemon._history = None
    monkeypatch.setattr(daemon, "codex_usage_snapshot", lambda: {
        "available": True, "provider": "codex", "bars": [{
            "kind": "codex_primary", "group": "weekly",
            "provider": "codex", "title": "Codex, 7d", "label": "7d",
            "percent": 48.0, "resets_at": None, "stale": False,
        }],
    })
    status, body = await fetch("/api/usage")
    bars = json.loads(body)["limits"]["bars"]
    assert status == 200
    # `provider` is absent on a Claude bar, not empty: `limits.py` builds only
    # Claude windows and never writes the key, and the panel's `UsageBar`
    # decodes it as `c.value(.provider, "claude")` — an omission *means* Claude.
    # Subscripting it here read that contract as a bug and blew up on the first
    # Claude bar before ever reaching the Codex one.
    assert next(b for b in bars if b.get("provider") == "codex")["percent"] == 48.0
    # The other half of the same contract, and the reason this is `.get`: the
    # Claude bars really do travel without the key.
    assert all("provider" not in b for b in bars if b["kind"] != "codex_primary")


@pytest.mark.asyncio
async def test_usage_includes_grok_weekly_bar(server, tmp_path, monkeypatch):
    _claude_config(tmp_path, monkeypatch)
    _, daemon = server
    daemon._history = None
    monkeypatch.setattr(daemon, "codex_usage_snapshot", lambda: {
        "available": False, "bars": [],
    })
    monkeypatch.setattr(
        "dark_army_daemon.api_server.grok_billing.get_snapshot",
        lambda: {"percent": 26.0, "resets_at": 1788559030.0, "stale": False,
                 "cycle": "weekly"},
    )
    status, body = await fetch("/api/usage")
    bars = json.loads(body)["limits"]["bars"]
    assert status == 200
    grok = next(b for b in bars if b.get("provider") == "grok")
    assert grok["kind"] == "grok_weekly"
    assert grok["percent"] == 26.0
    assert grok["label"] == "7d"


@pytest.mark.asyncio
async def test_usage_report_shape(server, tmp_path, monkeypatch):
    from dark_army_daemon.history import HistoryStore
    import time as _time

    _claude_config(tmp_path, monkeypatch)
    _, daemon = server
    daemon._history = HistoryStore(tmp_path / "h.db")
    daemon._history.connect()
    daemon._history.add_turn("s1", _time.time(), message_id="m1",
                             model="claude-opus-4-8", output_tokens=100_000,
                             attr_skill="dataviz", attr_mcp_server="gitnexus")

    status, body = await fetch("/api/usage?window=week")
    report = json.loads(body)
    assert status == 200 and report["window"] == "week"
    a = report["attribution"]
    # Every dimension the panel renders. Named explicitly: a missing key draws an
    # empty table, which reads as "nothing used it" rather than "not served".
    assert set(a) >= {"available", "skills", "agents", "plugins", "mcp_servers",
                      "mcp_tools", "behaviours", "window_cost_usd", "turns"}
    assert [r["name"] for r in a["skills"]] == ["dataviz"]
    assert [r["name"] for r in a["mcp_servers"]] == ["gitnexus"]
    daemon._history.close()


@pytest.mark.asyncio
async def test_usage_prefers_the_live_statusline_figure(server, tmp_path,
                                                       monkeypatch):
    """Rate limits are per account, so the highest figure any session reports is
    the newest one — and it is fresher than a cache Claude Code refreshes on its
    own schedule."""
    _claude_config(tmp_path, monkeypatch, percent=25)
    srv, daemon = server
    daemon._history = None
    # start() schedules a push that lands on the next tick and replaces _agents
    # wholesale — which is how it gets filled in production. Set the fixture after
    # it, or the real (empty) snapshot quietly wins the race.
    await asyncio.sleep(0.1)
    srv._agents = {"running": [{"metrics": {"five_hour_pct": 41}},
                               {"metrics": {"five_hour_pct": 39}},
                               {"metrics": {}}],
                   "sleeping": [], "waiting": [], "abandoned": []}

    status, body = await fetch("/api/usage")
    session_bar = json.loads(body)["limits"]["bars"][0]
    assert (session_bar["percent"], session_bar["source"]) == (41, "statusline")


# --- the live scoped window --------------------------------------------------
#
# `weekly_scoped` — "Current week (Fable)" — has no live source on this machine
# except the endpoint itself: `~/.claude.json` is the only local file that names
# it and it is routinely hours old. `claude_usage` fetches it and merges over
# `limits.snapshot()`; these pin the merge at the door, where all three clients
# (the Mac panel, the phone on the LAN, the phone away) read it.


def _live_scoped(percent=64.0, as_of=1_788_264_000.0):
    return {"bars": [{"kind": "weekly_scoped", "group": "weekly",
                      "title": "Current week (Fable)", "percent": percent,
                      "resets_at": 1_788_588_000.0, "severity": "normal",
                      "source": "oauth", "as_of": as_of, "stale": False}],
            "fetched_at": as_of}


@pytest.mark.asyncio
async def test_usage_serves_the_live_scoped_window(server, tmp_path, monkeypatch):
    """One Fable bar, not two: the fresh reading replaces the cached one rather
    than sitting beside it under the same title."""
    _claude_config(tmp_path, monkeypatch)
    _, daemon = server
    daemon._history = None
    monkeypatch.setattr(
        "dark_army_daemon.api_server.claude_usage.get_snapshot",
        lambda **kw: _live_scoped())

    status, body = await fetch("/api/usage")
    bars = json.loads(body)["limits"]["bars"]
    assert status == 200
    scoped = [b for b in bars if b.get("kind") == "weekly_scoped"]
    assert len(scoped) == 1
    assert scoped[0]["percent"] == 64.0
    assert scoped[0]["source"] == "oauth"
    assert scoped[0]["as_of"] == 1_788_264_000.0
    # ...and the account-wide bar beside it is untouched.
    assert [b["kind"] for b in bars] == ["session", "weekly_scoped"]


@pytest.mark.asyncio
async def test_a_failed_fetch_leaves_the_cached_bar_exactly_as_it_was(
        server, tmp_path, monkeypatch):
    """The whole point of freshening rather than replacing: a figure that could
    not be refreshed stays on screen, unchanged, rather than vanishing."""
    _claude_config(tmp_path, monkeypatch)
    _, daemon = server
    daemon._history = None

    monkeypatch.setattr(
        "dark_army_daemon.api_server.claude_usage.get_snapshot",
        lambda **kw: {})
    _, body = await fetch("/api/usage")
    held = json.loads(body)["limits"]["bars"]

    def boom(**kw):
        raise RuntimeError("keychain said no")

    monkeypatch.setattr(
        "dark_army_daemon.api_server.claude_usage.get_snapshot", boom)
    status, body = await fetch("/api/usage")
    assert status == 200
    assert json.loads(body)["limits"]["bars"] == held
    assert [b["kind"] for b in held] == ["session", "weekly_scoped"]
    assert next(b for b in held if b["kind"] == "weekly_scoped")["source"] == "cache"


@pytest.mark.asyncio
async def test_the_live_bar_publishes_only_allow_listed_keys(server, tmp_path,
                                                             monkeypatch):
    """`/api/usage` is ungated on loopback and is served on the LAN door and
    through the relay, so a bar's keys are a published surface."""
    from dark_army_daemon import claude_usage as cu

    _claude_config(tmp_path, monkeypatch)
    _, daemon = server
    daemon._history = None
    monkeypatch.setattr(
        "dark_army_daemon.api_server.claude_usage.get_snapshot",
        lambda **kw: _live_scoped())

    _, body = await fetch("/api/usage")
    scoped = next(b for b in json.loads(body)["limits"]["bars"]
                  if b["kind"] == "weekly_scoped")
    # `promos` is carried over from the local bar by the merge; everything else
    # is the parser's own allow-list.
    assert set(scoped) <= set(cu.BAR_KEYS) | {"promos", "overage", "provider",
                                              "label"}


@pytest.mark.asyncio
async def test_codex_and_grok_bars_survive_the_merge(server, tmp_path, monkeypatch):
    _claude_config(tmp_path, monkeypatch)
    _, daemon = server
    daemon._history = None
    monkeypatch.setattr(
        "dark_army_daemon.api_server.claude_usage.get_snapshot",
        lambda **kw: _live_scoped())
    monkeypatch.setattr(
        daemon, "codex_usage_snapshot",
        lambda: {"available": True,
                 "bars": [{"kind": "codex_primary", "provider": "codex",
                           "percent": 5.0, "label": "5h"}]})
    monkeypatch.setattr(
        "dark_army_daemon.api_server.grok_billing.get_snapshot",
        lambda: {"percent": 26.0, "resets_at": 1788559030.0, "stale": False})

    _, body = await fetch("/api/usage")
    bars = json.loads(body)["limits"]["bars"]
    assert [b["kind"] for b in bars] == [
        "session", "weekly_scoped", "codex_primary", "grok_weekly"]


@pytest.mark.asyncio
async def test_usage_rejects_an_unknown_window(server):
    status, body = await fetch("/api/usage?window=quarter")
    assert status == 400
    # `day` is the unlisted legacy alias — still resolvable so an old bookmark
    # does not 400, still offered here so the error names everything that works.
    assert json.loads(body)["allowed"] == ["day", "session", "week"]


# --- the push log ------------------------------------------------------------
#
# A panel showing a stale fleet has three possible causes — nothing was pushed,
# the push never arrived, or it arrived and was rejected — and the only way to
# tell them apart after the fact is for both ends to say so in one log. This is
# the daemon's half; the panel's is `Trace.swift`, filed under the same log by
# `panel_process._read_errors`.

def test_the_push_log_names_the_rows_carrying_a_summary(caplog):
    payload = json.dumps({
        "notifications": [{"session_id": "s1"}],
        "agents": {
            "waiting": [{"session_id": "abcdef0123", "nickname": "Mira",
                         "idle_seconds": 48.2,
                         "last_summary": "Approve the plan?"}],
            "running": [{"session_id": "b", "nickname": "Audit"}],
            "sleeping": [],
        },
    })
    # DEBUG, not INFO: the line fires per frame and was 86% of a day's log, so
    # it is obtainable (`BOB_COMPANION_LOG_LEVEL=DEBUG`) rather than default.
    with caplog.at_level("DEBUG", logger="dark-army.api"):
        api_mod._log_broadcast(payload, 1)
    line = caplog.messages[-1]
    assert "clients=1" in line and "runni=1" in line and "waiti=1" in line
    assert "cards=1" in line
    # The row that has something to say, and what it has — a push whose
    # `says=[]` is empty is the answer to "the panel never showed my tldr".
    assert "says=[Mira:waiti/48s/tldr]" in line
    # A row with nothing to caption is counted, never named: this line runs on
    # every push and a fleet-sized dump would not be read.
    assert "Audit" not in line


def test_the_push_log_never_breaks_a_push(caplog):
    """It is a log line on the path every surface update takes."""
    with caplog.at_level("DEBUG", logger="dark-army.api"):
        api_mod._log_broadcast("not json at all", 2)
    assert not any("clients=2" in m for m in caplog.messages)


# --- DNS rebinding -----------------------------------------------------------


async def _raw(request: bytes, port=PORT):
    """One hand-written request, first response line back. `urllib` will not let
    the Host header be set to something that contradicts the URL, and a
    contradicting Host is the entire thing under test."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(request)
        await writer.drain()
        return await asyncio.wait_for(reader.readuntil(b"\r\n"), timeout=5)
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass


@pytest.mark.asyncio
async def test_a_rebound_host_cannot_read_the_state(server):
    """The hole in "no CORS headers, so the body is withheld". Under DNS
    rebinding the browser believes it *is* same-origin, so that defence stops
    applying and every project path, question and cost is readable. The Host
    header still says what the page asked for, which is what catches it."""
    for path in ("/api/state", "/api/history", "/api/usage", "/api/events"):
        head = await _raw(f"GET {path} HTTP/1.1\r\nHost: evil.com\r\n\r\n".encode())
        assert b"403" in head, f"{path} answered a rebound host: {head!r}"


@pytest.mark.asyncio
async def test_every_spelling_of_loopback_is_still_served(server):
    """The check must be invisible to the panel (127.0.0.1), to `curl
    localhost:19874`, and to an IPv6 client — macOS resolves localhost to ::1
    first, so the bracketed literal is not a curiosity here."""
    for host in (b"127.0.0.1:19887", b"localhost:19887", b"localhost",
                 b"[::1]:19887", b"[::1]"):
        head = await _raw(b"GET /api/state HTTP/1.1\r\nHost: " + host + b"\r\n\r\n")
        assert b"200" in head, f"refused {host!r}: {head!r}"


@pytest.mark.asyncio
async def test_a_request_with_no_host_is_still_served(server):
    """HTTP/1.0 clients and hand-rolled sockets omit it; a browser never does,
    so refusing them would cost compatibility and buy nothing."""
    head = await _raw(b"GET /api/state HTTP/1.0\r\n\r\n")
    assert b"200" in head


@pytest.mark.asyncio
async def test_a_rebound_host_cannot_act_either(server, token_path):
    """Belt and braces: writes already needed the token and an allowed Origin,
    and now they need an honest Host as well."""
    head = await _raw(
        b"POST /api/action HTTP/1.1\r\nHost: evil.com\r\n"
        b"X-Bob-Token: " + token_path.read_text().strip().encode() + b"\r\n"
        b"Content-Length: 2\r\n\r\n{}")
    assert b"403" in head


# --- the token ---------------------------------------------------------------


def test_the_token_is_created_private(tmp_path, monkeypatch):
    """Created 0600, not created-then-chmodded: the window between the two is
    the whole file readable at whatever the umask allows. The file is the
    session token; the desk token is never written anywhere."""
    path = tmp_path / "api-token"
    monkeypatch.setattr(api_mod, "API_TOKEN_PATH", path)
    monkeypatch.setattr(api_mod, "ensure_state_dir", lambda: tmp_path)
    token = api_mod.load_or_create_token()
    assert token and (path.stat().st_mode & 0o777) == 0o600
    # Idempotent: a second call reads the first one back rather than minting.
    assert api_mod.load_or_create_token() == token


@pytest.mark.asyncio
async def test_an_empty_token_authorises_nothing(server):
    """A daemon that failed to bind drops its token to "" so callers can tell.
    An empty secret that compares equal to an absent header would then open
    every write on the way down."""
    srv, _ = server
    srv.token = ""
    req = api_mod._Request("POST", "/api/action", {},
                           {"x-bob-token": "", "host": "localhost"}, b"{}")
    assert srv._authorised(req) is False


# --- two tokens: the desk token and the session token ------------------------
#
# `srv.token` is the desk token (memory only); `token_path.read_text()` is the
# session token — the file every same-user process can read. A request made
# with only what is on disk may close a terminal, pop the panel and read, and
# nothing else (`docs/transport-contract.md`, *The loopback door has two
# tokens*).


def _post(action: dict, token: str, **headers):
    return fetch("/api/action", json.dumps(action).encode(),
                 {"X-Bob-Token": token, **headers})


@pytest.mark.asyncio
async def test_desk_and_session_tokens_differ_and_only_the_session_one_is_on_disk(
        server, token_path):
    srv, _ = server
    assert srv.token and srv.session_token
    assert srv.token != srv.session_token
    assert token_path.read_text().strip() == srv.session_token
    assert srv.token not in token_path.read_text()


@pytest.mark.asyncio
async def test_the_session_token_is_the_file_that_was_already_there(token_path):
    """The downgrade seam: the value already on disk becomes the session
    token and the file is never re-minted, so an older build after a
    downgrade reads exactly what it wrote."""
    token_path.write_text("olddisk\n")
    before = token_path.read_bytes()
    srv = ApiServer(BobDaemon(), port=PORT)
    await srv.start()
    try:
        assert srv.session_token == "olddisk"
        assert srv.token and srv.token != "olddisk"
    finally:
        await srv.stop()
    assert token_path.read_bytes() == before


@pytest.mark.asyncio
async def test_the_session_token_cannot_answer_a_permission_prompt(
        server, token_path):
    """(success criterion) The side door the split closes: `request_id` is
    public on `/api/state`, so the verdict must need the desk token."""
    srv, daemon = server
    answered = []

    async def answer(request_id, behavior):
        answered.append((request_id, behavior))
        return True, ""

    daemon.answer_permission = answer
    verdict = {"action": "permission_verdict", "request_id": "req-1",
               "behavior": "allow"}
    status, body = await _post(verdict, token_path.read_text().strip())
    assert status == 403
    assert "desk token" in json.loads(body)["detail"]
    assert answered == [], "the verdict must still be unset"
    status, _ = await _post(verdict, srv.token)
    assert status != 403
    assert answered == [("req-1", "allow")]


@pytest.mark.asyncio
async def test_the_session_token_cannot_dispatch_or_start_a_card(
        board_server, token_path, monkeypatch):
    """(success criterion) Starting a card spends money and opens a terminal:
    the file's token is refused before the daemon sees the press."""
    from dark_army_daemon import dispatch
    srv, _daemon, store = board_server
    spawned = []
    monkeypatch.setattr(dispatch, "spawn",
                        lambda *a, **k: spawned.append(a) or (False, "stub"))
    monkeypatch.setattr(dispatch, "spawn_local",
                        lambda *a, **k: spawned.append(a) or (False, "stub"))
    card, _ = store.create({"title": "x", "tool": "claude", "root": "/tmp",
                            "column_name": "backlog", "project": "bob"})
    session = token_path.read_text().strip()
    for action in ({"action": "board_dispatch", "card_id": card["id"]},
                   {"action": "board_create", "title": "agent-made",
                    "prompt": "go"}):
        status, body = await board_fetch(
            "/api/action", json.dumps(action).encode(),
            {"X-Bob-Token": session})
        assert status == 403, (action, body)
        assert "desk token" in json.loads(body)["detail"]
    assert spawned == []
    assert [c["title"] for c in store.cards()] == ["x"]
    # The desk token gets past the gate (the daemon then answers in its own
    # words — here a folder that is not a known project).
    status, _ = await board_fetch(
        "/api/action",
        json.dumps({"action": "board_dispatch", "card_id": card["id"]}).encode(),
        {"X-Bob-Token": srv.token})
    assert status != 403


@pytest.mark.asyncio
async def test_the_session_token_cannot_type_into_a_terminal(server, token_path):
    """(success criterion) Typing into another agent's pty is a desk verb."""
    srv, daemon = server
    typed = []

    async def terminal_input(session_id, text, **kwargs):
        typed.append((session_id, text))
        return True, ""

    daemon.terminal_input = terminal_input
    press = {"action": "terminal_input", "session_id": "sess-1",
             "text": "yes"}
    status, body = await _post(press, token_path.read_text().strip())
    assert status == 403
    assert "desk token" in json.loads(body)["detail"]
    assert typed == [], "the broker must see no write"
    status, _ = await _post(press, srv.token)
    assert status != 403
    assert typed == [("sess-1", "yes")]


@pytest.mark.asyncio
async def test_the_session_token_closes_a_terminal_but_never_by_person(
        server, token_path):
    """The agent's own close-out keeps working with the file's token, but
    `by_person` — the board's third door into Done — is the desk's alone."""
    srv, daemon = server
    closes = []

    async def close_session_terminal(session_id, by_person=False):
        closes.append((session_id, by_person))
        return True, ""

    daemon.close_session_terminal = close_session_terminal
    session = token_path.read_text().strip()
    close = {"action": "close_terminal", "session_id": "sess-1"}
    status, _ = await _post(close, session)
    assert status == 200
    assert closes == [("sess-1", False)]
    status, body = await _post(dict(close, by_person=True), session)
    assert status == 403
    assert "desk token" in json.loads(body)["detail"]
    assert closes == [("sess-1", False)], "no close, no card finished"
    status, _ = await _post(dict(close, by_person=True), srv.token)
    assert status == 200
    assert closes[-1] == ("sess-1", True)


@pytest.mark.asyncio
async def test_the_session_token_reveals_the_panel_and_reads_a_conversation(
        server, token_path):
    srv, daemon = server
    session = token_path.read_text().strip()

    async def reveal_panel_for_terminal(shell_pid, tty):
        return True, ""

    async def conversation(query):
        return 200, "application/json", b'{"ok":true}'

    daemon.reveal_panel_for_terminal = reveal_panel_for_terminal
    srv._conversation_report_for = conversation
    status, _ = await _post({"action": "reveal_panel", "shell_pid": 1,
                             "tty": ""}, session)
    assert status == 200
    status, _ = await fetch("/api/conversation?session=s",
                            headers={"X-Bob-Token": session})
    assert status == 200
    # The stream types and the access log names every phone: desk only.
    for path in ("/api/terminal/stream?session=s", "/api/access-log"):
        status, _ = await fetch(path, headers={"X-Bob-Token": session})
        assert status == 403, path
    assert set(api_mod.SESSION_READS) >= {"/api/conversation"}
    assert "/api/terminal/stream" not in api_mod.SESSION_READS
    assert "/api/access-log" not in api_mod.SESSION_READS


@pytest.mark.asyncio
async def test_the_forbidden_body_names_the_desk_token_only_for_the_session_tier(
        server):
    """A guessed token hears nothing more than `forbidden`."""
    status, body = await _post({"action": "noop"}, "guessed-token")
    assert status == 403
    assert json.loads(body) == {"error": "forbidden"}


@pytest.mark.asyncio
async def test_an_empty_session_token_authorises_nothing(server):
    """A failed bind empties both tokens; an empty secret must never equal an
    absent header on the session tier either."""
    srv, _ = server
    srv.token = ""
    srv.session_token = ""
    req = api_mod._Request("POST", "/api/action", {},
                           {"x-bob-token": "", "host": "localhost"}, b"{}")
    assert srv._session_authorised(req) is False
    assert srv._authorised(req) is False


@pytest.mark.asyncio
async def test_neither_token_is_on_the_snapshot(server):
    srv, _ = server
    frame = json.dumps(srv.state())
    assert srv.token not in frame
    assert srv.session_token not in frame
    status, body = await fetch("/api/state")
    assert status == 200
    assert srv.token.encode() not in body
    assert srv.session_token.encode() not in body


@pytest.mark.asyncio
async def test_origin_is_checked_on_the_session_tier_too(server, token_path):
    srv, daemon = server
    closes = []

    async def close_session_terminal(session_id, by_person=False):
        closes.append(session_id)
        return True, ""

    daemon.close_session_terminal = close_session_terminal
    status, _ = await _post({"action": "close_terminal", "session_id": "s"},
                            token_path.read_text().strip(),
                            Origin="https://evil.example")
    assert status == 403
    assert closes == []


def _every_action_name() -> set:
    """Every action name `api_server` dispatches on, read off the source and
    the named tuples — never a hand list that can drift from the routing."""
    import re
    src = open(api_mod.__file__, encoding="utf-8").read()
    names = set(re.findall(
        r'(?:action|name|kind)\s*(?:==|!=)\s*"([a-z_]+)"', src))
    for m in re.finditer(r'(?:action|name)\s+(?:not\s+)?in\s+\(([^)]*)\)', src):
        names |= set(re.findall(r'"([a-z_]+)"', m.group(1)))
    for tup in ("BOARD_ACTIONS", "LAN_ACTIONS", "REMOTE_ACTIONS",
                "MISSION_ACTIONS"):
        names |= set(getattr(ApiServer, tup, ()))
    names |= set(api_mod.SESSION_ACTIONS) | {"noop"}
    return names


@pytest.mark.asyncio
async def test_every_action_but_the_session_tier_refuses_the_file_token(
        board_server, token_path):
    """The sweep: every action the daemon knows, POSTed with only what is on
    disk, is 403 unless it is on `SESSION_ACTIONS` — and `close_terminal`
    with `by_person` is 403 too. A new verb lands on the desk tier unless
    someone deliberately widens the session tier."""
    srv, daemon, _store = board_server
    names = _every_action_name()
    assert len(names) > 40, "the source read found too few verbs to mean anything"
    assert {"permission_verdict", "board_dispatch", "terminal_input",
            "reply", "stop_session"} <= names

    async def ok(*_a, **_k):
        return True, ""

    daemon.close_session_terminal = ok
    daemon.close_refinement_terminal = ok
    daemon.reveal_panel_for_terminal = ok
    session = token_path.read_text().strip()
    fields = {"session_id": "sess-1", "card_id": "c1", "request_id": "r1",
              "behavior": "allow", "text": "yes", "root": "/tmp",
              "title": "t", "prompt": "p", "device_id": "d", "id": "x"}
    passed = []
    for name in sorted(names):
        for extra in ({}, {"by_person": True}):
            body = dict(fields, action=name, **extra)
            status, _ = await board_fetch(
                "/api/action", json.dumps(body).encode(),
                {"X-Bob-Token": session})
            if status != 403:
                passed.append((name, bool(extra)))
    assert {name for name, _ in passed} <= set(api_mod.SESSION_ACTIONS), passed
    assert ("close_terminal", True) not in passed


# --- the board ---------------------------------------------------------------


@pytest_asyncio.fixture
async def board_server(token_path, tmp_path):
    """A server whose daemon has a real board open, in a scratch directory."""
    from dark_army_daemon.board import BoardStore
    daemon = BobDaemon(sessions_path=tmp_path / "sessions.json")
    store = BoardStore(tmp_path / "board.db")
    store.connect()
    daemon._board = store
    daemon._refresh_board_state()
    srv = ApiServer(daemon, port=BOARD_PORT)
    await srv.start()
    try:
        yield srv, daemon, store
    finally:
        await srv.stop()
        store.close()


def _board_fetch(path, data=None, headers=None):
    return _fetch(path, data, headers, port=BOARD_PORT)


async def board_fetch(*args, **kwargs):
    return await asyncio.to_thread(_board_fetch, *args, **kwargs)


@pytest.mark.asyncio
async def test_the_board_is_in_the_state_payload(board_server):
    """The panel reads it off the same snapshot as everything else — there is no
    second stream and no per-frame database read."""
    status, body = await board_fetch("/api/state")
    assert status == 200
    board = json.loads(body)["board"]
    assert board["available"] is True
    assert set(board["counts"]) == {"prep", "backlog", "in_progress", "done",
                                    "ready"}
    assert board["counts"]["ready"] == board["counts"]["backlog"]
    assert "dispatch_enabled" in board
    assert len(board["done_clear_token"]) == 64


@pytest.mark.asyncio
async def test_run_health_supported_rides_the_state_payload(board_server):
    """The phone's version marker for the run-health line: a store-backed
    server publishes it true on `/api/state`, and an older Mac simply sends
    no key — which decodes false, so the phone draws the line *absent*
    rather than broken."""
    status, body = await board_fetch("/api/state")
    assert status == 200
    assert json.loads(body)["board"]["run_health_supported"] is True


@pytest.mark.asyncio
async def test_a_board_write_without_the_token_is_forbidden(board_server):
    status, _ = await board_fetch(
        "/api/action", b'{"action":"board_create","title":"x"}')
    assert status == 403


@pytest.mark.asyncio
async def test_board_ask_without_the_token_is_forbidden(board_server):
    _, _, store = board_server
    card, _ = store.create({"title": "x", "project": "bob"})
    status, _ = await board_fetch(
        "/api/action",
        json.dumps({"action": "board_ask", "card_id": card["id"],
                    "text": "hello"}).encode())
    assert status == 403
    assert store.messages(card["id"]) == []


@pytest.mark.asyncio
async def test_a_board_write_with_a_bearer_header_is_forbidden(board_server):
    """The mistake this codebase has already made: reads are ungated, so
    `Authorization: Bearer` looks like it works and every write silently 403s.
    Pinned so the panel cannot drift back to it."""
    srv, _, _ = board_server
    status, _ = await board_fetch(
        "/api/action", b'{"action":"board_create","title":"x"}',
        {"Authorization": f"Bearer {srv.token}"})
    assert status == 403


@pytest.mark.asyncio
async def test_a_board_write_from_a_foreign_origin_is_forbidden(board_server):
    srv, _, _ = board_server
    status, _ = await board_fetch(
        "/api/action", b'{"action":"board_create","title":"x"}',
        {"X-Bob-Token": srv.token, "Origin": "https://evil.example"})
    assert status == 403


@pytest.mark.asyncio
async def test_board_clear_done_requires_the_board_write_auth_gate(board_server):
    srv, _, store = board_server
    store.create({"title": "keep until authorised", "column_name": "done"})
    _, token = store.done_scope()
    body = json.dumps({"action": "board_clear_done",
                       "expected_count": "1",
                       "expected_done_token": token}).encode()

    assert (await board_fetch("/api/action", body))[0] == 403
    assert (await board_fetch(
        "/api/action", body,
        {"Authorization": f"Bearer {srv.token}"}))[0] == 403
    assert (await board_fetch(
        "/api/action", body,
        {"X-Bob-Token": srv.token,
         "Origin": "https://evil.example"}))[0] == 403
    assert store.counts()["done"] == 1


@pytest.mark.asyncio
async def test_board_clear_done_still_refuses_a_stale_membership_token(
        board_server):
    srv, _, store = board_server
    store.create({"title": "done a", "column_name": "done"})
    _, token = store.done_scope()
    store.create({"title": "done b", "column_name": "done"})
    status, body = await board_fetch(
        "/api/action",
        json.dumps({"action": "board_clear_done", "expected_count": "2",
                    "expected_done_token": token}).encode(),
        {"X-Bob-Token": srv.token})
    assert status == 409, body
    assert store.counts()["done"] == 2


@pytest.mark.asyncio
async def test_a_review_between_the_read_and_the_press_does_not_refuse(
        board_server):
    """The whole reason there are two tokens. A review moves `done_view_token`
    and nothing in the membership digest, and a Clear Done confirmed against
    the membership digest must therefore still land — a refusal with nothing
    behind it is worse than no confirmation at all."""
    srv, _, store = board_server
    card, _ = store.create({"title": "closed by an assistant",
                            "project": "p"})
    store.update(card["id"], {"session_id": "sess-1",
                              "column_name": "in_progress"})
    closed, detail = store.declare_done(card["id"], "sess-1", "finished")
    assert closed is not None, detail
    _, token, view_before = store.done_tokens()
    store.mark_reviewed(card["id"])
    _, token_after, view_after = store.done_tokens()
    assert token_after == token
    assert view_after != view_before
    status, body = await board_fetch(
        "/api/action",
        json.dumps({"action": "board_clear_done", "expected_count": "1",
                    "expected_done_token": token}).encode(),
        {"X-Bob-Token": srv.token})
    assert status == 200, body
    assert store.counts()["done"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    {"action": "board_clear_done"},
    {"action": "board_clear_done", "expected_count": "many"},
    {"action": "board_clear_done", "expected_count": -1},
    {"action": "board_clear_done", "expected_count": True},
    {"action": "board_clear_done", "expected_count": 1.5},
])
async def test_board_clear_done_rejects_invalid_expected_counts(
        board_server, payload):
    srv, _, store = board_server
    store.create({"title": "still done", "column_name": "done"})
    payload.setdefault("expected_done_token", store.done_scope()[1])

    status, body = await board_fetch(
        "/api/action", json.dumps(payload).encode(),
        {"X-Bob-Token": srv.token})

    assert status == 400, body
    assert "expected_count" in json.loads(body)["error"]
    assert store.counts()["done"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("token", [None, "", "a" * 63, "A" * 64,
                                    "g" * 64, 123])
async def test_board_clear_done_rejects_invalid_exact_scope_tokens(
        board_server, token):
    srv, _, store = board_server
    store.create({"title": "still done", "column_name": "done"})
    payload = {"action": "board_clear_done", "expected_count": "1"}
    if token is not None:
        payload["expected_done_token"] = token

    status, body = await board_fetch(
        "/api/action", json.dumps(payload).encode(),
        {"X-Bob-Token": srv.token})

    assert status == 400, body
    assert "expected_done_token" in json.loads(body)["error"]
    assert store.counts()["done"] == 1


@pytest.mark.asyncio
async def test_board_clear_done_is_atomic_and_preserves_non_done_rows(
        board_server):
    srv, _, store = board_server
    hand_done, _ = store.create({
        "title": "hand", "summary": "human", "column_name": "done"})
    agent_done, _ = store.create({
        "title": "agent", "tool": "claude", "column_name": "in_progress"})
    store.bind_session(agent_done["id"], "sess-api")
    store.declare_done(agent_done["id"], "sess-api", "verified")
    backlog, _ = store.create({
        "title": "backlog", "summary": "keep", "prompt": "byte for byte",
        "project": "bob", "root": "/tmp/bob", "tool": "codex",
        "column_name": "backlog", "workflow": "bc-verifier"})
    active, _ = store.create({
        "title": "active", "summary": "keep active", "project": "bob",
        "root": "/tmp/bob", "tool": "grok", "column_name": "in_progress"})
    survivors = {card["id"]: store.get(card["id"])
                 for card in (backlog, active)}
    expected_count, expected_token = store.done_scope()

    status, body = await board_fetch(
        "/api/action",
        json.dumps({"action": "board_clear_done",
                    "expected_count": str(expected_count),
                    "expected_done_token": expected_token}).encode(),
        {"X-Bob-Token": srv.token})

    assert status == 200, body
    assert json.loads(body) == {
        "ok": True, "detail": "deleted", "deleted_count": 2}
    assert store.get(hand_done["id"]) is None
    assert store.get(agent_done["id"]) is None
    assert {card_id: store.get(card_id) for card_id in survivors} == survivors
    refreshed = srv.state()["board"]
    assert refreshed["counts"]["done"] == 0
    assert refreshed["done_clear_token"] != expected_token

    empty_count, empty_token = store.done_scope()
    status, body = await board_fetch(
        "/api/action",
        json.dumps({"action": "board_clear_done",
                    "expected_count": empty_count,
                    "expected_done_token": empty_token}).encode(),
        {"X-Bob-Token": srv.token})
    assert status == 200, body
    assert json.loads(body)["deleted_count"] == 0


@pytest.mark.asyncio
async def test_board_clear_done_stale_count_is_409_and_deletes_nothing(
        board_server):
    srv, _, store = board_server
    first, _ = store.create({"title": "first", "column_name": "done"})
    second, _ = store.create({"title": "second", "column_name": "done"})
    _, token = store.done_scope()

    status, body = await board_fetch(
        "/api/action",
        json.dumps({"action": "board_clear_done",
                    "expected_count": "1",
                    "expected_done_token": token}).encode(),
        {"X-Bob-Token": srv.token})

    assert status == 409
    payload = json.loads(body)
    assert payload["ok"] is False and payload["deleted_count"] == 0
    assert "nothing was cleared" in payload["detail"]
    assert store.get(first["id"]) is not None
    assert store.get(second["id"]) is not None


@pytest.mark.asyncio
async def test_board_clear_done_equal_count_scope_swap_is_409(board_server):
    srv, _, store = board_server
    first, _ = store.create({"title": "first", "column_name": "done"})
    second, _ = store.create({"title": "second", "column_name": "done"})
    incoming, _ = store.create({
        "title": "incoming", "column_name": "backlog"})
    expected_count, expected_token = store.done_scope()
    store.move(first["id"], "backlog")
    store.move(incoming["id"], "done")

    status, body = await board_fetch(
        "/api/action",
        json.dumps({"action": "board_clear_done",
                    "expected_count": expected_count,
                    "expected_done_token": expected_token}).encode(),
        {"X-Bob-Token": srv.token})

    assert status == 409, body
    assert json.loads(body)["deleted_count"] == 0
    assert store.get(second["id"]) is not None
    assert store.get(incoming["id"]) is not None


@pytest.mark.asyncio
async def test_a_board_write_with_the_token_creates_a_card(board_server):
    srv, _, store = board_server
    status, body = await board_fetch(
        "/api/action",
        json.dumps({"action": "board_create", "title": "smoke test",
                    "prompt": "go"}).encode(),
        {"X-Bob-Token": srv.token})
    assert status == 200, body
    assert json.loads(body)["ok"] is True
    assert [c["title"] for c in store.cards()] == ["smoke test"]


@pytest.mark.asyncio
async def test_a_refused_board_write_answers_409(board_server):
    """The shape `delete_agent` uses: the request was well-formed and the daemon
    declined it, which is not the same thing as a bad request."""
    srv, _, _ = board_server
    status, body = await board_fetch(
        "/api/action",
        json.dumps({"action": "board_update", "card_id": "nope",
                    "title": "x"}).encode(),
        {"X-Bob-Token": srv.token})
    assert status == 409
    assert json.loads(body)["ok"] is False


@pytest.mark.asyncio
async def test_dispatch_from_the_api_is_refused_when_the_switch_is_off(board_server):
    srv, daemon, store = board_server
    card, _ = store.create({"title": "x", "tool": "claude", "root": "/tmp",
                            "column_name": "backlog", "project": "bob"})
    daemon.board_dispatch_enabled = False
    status, body = await board_fetch(
        "/api/action",
        json.dumps({"action": "board_dispatch", "card_id": card["id"]}).encode(),
        {"X-Bob-Token": srv.token})
    assert status == 409
    assert "not allowed to start sessions" in json.loads(body)["detail"]


@pytest.mark.asyncio
async def test_the_done_archive_is_served(board_server):
    _, _, store = board_server
    card, _ = store.create({"title": "finished", "project": "bob"})
    store.move(card["id"], "done")
    status, body = await board_fetch("/api/board?range=all")
    assert status == 200
    payload = json.loads(body)
    assert payload["available"] is True
    assert [c["title"] for c in payload["cards"]] == ["finished"]


@pytest.mark.asyncio
async def test_the_per_card_report_carries_messages_with_author_name(board_server):
    _, daemon, store = board_server
    card, _ = store.create({"title": "threaded", "project": "bob"})
    store.add_message(card["id"], "user", "did you?", "question", "")
    store.add_message(card["id"], "sess-abc", "yes", "answer", "session")
    status, body = await board_fetch(f"/api/board?card={card['id']}")
    assert status == 200, body
    payload = json.loads(body)
    assert payload["available"] is True
    got = payload["cards"][0]
    messages = got["messages"]
    assert [m["kind"] for m in messages] == ["question", "answer"]
    assert messages[0]["author_name"] == ""
    assert messages[1]["author"] == "sess-abc"
    assert messages[1]["author_name"] == daemon._identities.peek("sess-abc")
    snap = next(c for c in daemon._build_board_state()["cards"]
                if c["id"] == card["id"])
    assert "messages" not in snap
    assert snap["thread_count"] == 2


@pytest.mark.asyncio
async def test_an_unknown_board_range_is_a_bad_request(board_server):
    status, _ = await board_fetch("/api/board?range=forever")
    assert status == 400


@pytest.mark.asyncio
async def test_a_rebound_host_cannot_read_the_board(board_server):
    """`/api/board` is an ungated read like `/api/state`, and the `Host` check
    above the routing table is what makes that defensible."""
    head = await _raw(b"GET /api/board HTTP/1.1\r\nHost: evil.com\r\n\r\n",
                      port=BOARD_PORT)
    assert b"403" in head


@pytest.mark.asyncio
async def test_two_board_pushes_inside_the_floor_are_one_frame(server):
    """The board joins the limiter rather than defeating it: every field in it is
    static text or a stamp that moves only when something moved."""
    srv, _ = server
    reader, writer, _ = await _open_stream()
    try:
        await _next_event(reader)
        await _isolate_limiter(srv, reader)
        for n in range(1, 11):
            srv.on_board_change({"counts": {"ready": n}, "cards": [],
                                 "generated_at": float(n)})
        for _ in range(10):
            event = await _next_event(reader)
            if event["board"]["counts"]["ready"] == 10:
                break
        assert event["board"]["counts"]["ready"] == 10, \
            "the newest board never arrived"
        # Ten edits, not ten frames: the rest were folded into the trailing one.
        await _quiet(reader, api_mod.BROADCAST_MIN_INTERVAL * 2)
    finally:
        writer.close()


@pytest.mark.asyncio
async def test_a_reconcile_that_changed_nothing_buys_no_frame(server, monkeypatch):
    """The board's own `generated_at` is already in `_CLOCK_FIELDS` and is
    stripped with the outer one, so a reconcile that moved no card is not news.
    Nothing else about a board belongs in that set — a field wrongly in it costs
    a stale board."""
    srv, _ = server
    monkeypatch.setattr(api_mod, "BROADCAST_QUIET_INTERVAL", 30.0)
    reader, writer, _ = await _open_stream()
    await _isolate_limiter(srv, reader)
    try:
        srv.on_board_change({"counts": {"ready": 1}, "cards": [],
                             "generated_at": 1.0})
        await _next_event(reader)
        srv.on_board_change({"counts": {"ready": 1}, "cards": [],
                             "generated_at": 99.0})
        await _quiet(reader, api_mod.BROADCAST_MIN_INTERVAL * 3)
    finally:
        writer.close()


@pytest.mark.asyncio
async def test_a_board_update_naming_no_writable_field_is_a_client_error(board_server):
    """An update whose named fields were all dropped by the allow-list used to
    answer `200 ok:true` — the store returns "nothing to change" for an empty
    update — so the panel reported a write that never happened. `session_id`
    and `link_state` are exactly such fields, and Send back sent both."""
    srv, _, store = board_server
    card, _ = store.create({"title": "x", "project": "bob"})
    status, body = await board_fetch(
        "/api/action",
        json.dumps({"action": "board_update", "card_id": card["id"],
                    "session_id": "", "link_state": ""}).encode(),
        {"X-Bob-Token": srv.token})
    assert status == 400
    assert json.loads(body)["ok"] is False


@pytest.mark.asyncio
async def test_a_board_update_repeating_a_value_it_already_has_still_works(board_server):
    """The genuinely idempotent case must not be caught by the rule above."""
    srv, _, store = board_server
    card, _ = store.create({"title": "x", "project": "bob"})
    status, body = await board_fetch(
        "/api/action",
        json.dumps({"action": "board_update", "card_id": card["id"],
                    "title": "x"}).encode(),
        {"X-Bob-Token": srv.token})
    assert status == 200, body
    assert json.loads(body)["ok"] is True


@pytest.mark.asyncio
async def test_board_reset_unlinks_the_card_and_moves_it(board_server):
    """The recovery path, as one atomic write. The panel cannot set `session_id`
    or `link_state` — they are not in the field allow-list, deliberately — so
    Send back is a verb of its own rather than three more names in it."""
    srv, _, store = board_server
    card, _ = store.create({"title": "x", "project": "bob", "tool": "claude",
                            "column_name": "in_progress"})
    store.bind_session(card["id"], "dead")
    store.mark_ended(card["id"], when=time.time())
    store.update(card["id"], {"dispatch_error": "no session appeared"})
    status, body = await board_fetch(
        "/api/action",
        json.dumps({"action": "board_reset", "card_id": card["id"],
                    "column_name": "backlog"}).encode(),
        {"X-Bob-Token": srv.token})
    assert status == 200, body
    got = store.get(card["id"])
    assert got["column_name"] == "backlog"
    assert got["session_id"] == ""
    assert got["link_state"] == ""
    assert got["dispatch_error"] == ""


@pytest.mark.asyncio
async def test_a_board_write_cannot_set_the_link_fields(board_server):
    """They are Dark Army's own bookkeeping. A request that could set them could claim
    a card was live on somebody else's session."""
    srv, _, store = board_server
    card, _ = store.create({"title": "x", "project": "bob"})
    await board_fetch(
        "/api/action",
        json.dumps({"action": "board_update", "card_id": card["id"],
                    "title": "y", "session_id": "not-mine",
                    "link_state": "live"}).encode(),
        {"X-Bob-Token": srv.token})
    got = store.get(card["id"])
    assert got["title"] == "y"
    assert got["session_id"] == ""
    assert got["link_state"] == ""


def test_the_declared_stages_are_writable_and_the_trail_is_not():
    """`workflow` is what a card *declares* it expects and belongs to whoever
    writes the card. `agent_trail` is Dark Army's record of what it saw run: a surface
    that could write it could claim a stage had happened that never did, which
    is exactly the lie the stage track is built to be incapable of. Same
    argument that keeps `session_id` and `link_state` out."""
    assert "workflow" in ApiServer._BOARD_FIELDS
    assert "agent_trail" not in ApiServer._BOARD_FIELDS
    # `crew_trail` is out for exactly the same reason, one rung on: it says
    # *who* was on a stage, and a face on a stage that never ran is the same
    # lie with a portrait attached.
    assert "crew_trail" not in ApiServer._BOARD_FIELDS
    assert "session_id" not in ApiServer._BOARD_FIELDS
    # Cards wait on cards again: a thing a person states about their card,
    # open at the door, with its refusals at the store.
    assert "blocked_by" in ApiServer._BOARD_FIELDS
    assert "position" not in ApiServer._BOARD_FIELDS
    assert "board_reorder" in ApiServer.BOARD_ACTIONS


@pytest.mark.asyncio
async def test_board_reorder_changes_store_order(board_server):
    srv, _, store = board_server
    a, _ = store.create({"title": "a", "project": "bob", "column_name": "backlog"})
    b, _ = store.create({"title": "b", "project": "bob", "column_name": "backlog"})
    c, _ = store.create({"title": "c", "project": "bob", "column_name": "backlog"})
    status, body = await board_fetch(
        "/api/action",
        json.dumps({"action": "board_reorder", "card_id": c["id"],
                    "column_name": "backlog", "before_id": a["id"]}).encode(),
        {"X-Bob-Token": srv.token})
    assert status == 200, body
    assert json.loads(body)["ok"] is True
    ids = [x["id"] for x in store.cards(columns=["backlog"])]
    assert ids == [c["id"], a["id"], b["id"]]


@pytest.mark.asyncio
async def test_board_reorder_without_the_token_is_forbidden(board_server):
    srv, _, store = board_server
    card, _ = store.create({"title": "x", "project": "bob", "column_name": "backlog"})
    status, _ = await board_fetch(
        "/api/action",
        json.dumps({"action": "board_reorder", "card_id": card["id"],
                    "before_id": ""}).encode())
    assert status == 403


@pytest.mark.asyncio
async def test_board_reorder_does_not_dispatch(board_server, monkeypatch):
    srv, daemon, store = board_server
    a, _ = store.create({"title": "a", "project": "bob", "column_name": "backlog"})
    b, _ = store.create({"title": "b", "project": "bob", "column_name": "backlog"})

    async def boom(*a, **k):
        raise AssertionError("board_reorder must not dispatch")

    monkeypatch.setattr(daemon, "dispatch_card", boom)
    status, body = await board_fetch(
        "/api/action",
        json.dumps({"action": "board_reorder", "card_id": b["id"],
                    "column_name": "backlog", "before_id": a["id"]}).encode(),
        {"X-Bob-Token": srv.token})
    assert status == 200, body


@pytest.mark.asyncio
async def test_a_blocked_by_update_stores_the_ids_and_steps_the_revision(
        board_server):
    """`board_update` naming `blocked_by` stores the joined ids — one string,
    newline-separated — and moves the card's change number, which is what
    both editors' `expected_revision` guards on."""
    srv, _, store = board_server
    a, _ = store.create({"title": "a", "project": "bob", "column_name": "backlog"})
    b, _ = store.create({"title": "b", "project": "bob", "column_name": "backlog"})
    c, _ = store.create({"title": "c", "project": "bob", "column_name": "backlog"})
    before = store.get(a["id"])["revision"]
    status, body = await board_fetch(
        "/api/action",
        json.dumps({"action": "board_update", "card_id": a["id"],
                    "blocked_by": b["id"] + "\n" + c["id"],
                    "expected_revision": str(before)}).encode(),
        {"X-Bob-Token": srv.token})
    assert status == 200, body
    got = store.get(a["id"])
    assert got["blocked_by"] == b["id"] + "\n" + c["id"]
    assert got["revision"] == before + 1


@pytest.mark.asyncio
async def test_a_blocked_by_cycle_is_a_409_in_the_stores_words(board_server):
    srv, _, store = board_server
    a, _ = store.create({"title": "a", "project": "bob", "column_name": "backlog"})
    b, _ = store.create({"title": "b", "project": "bob", "column_name": "backlog"})
    store.update(a["id"], {"blocked_by": b["id"]})
    status, body = await board_fetch(
        "/api/action",
        json.dumps({"action": "board_update", "card_id": b["id"],
                    "blocked_by": a["id"]}).encode(),
        {"X-Bob-Token": srv.token})
    assert status == 409, body
    assert "already wait on each other" in json.loads(body)["detail"]
    assert store.get(b["id"])["blocked_by"] == ""


@pytest.mark.asyncio
async def test_a_stale_blocked_by_update_is_refused_as_changed(board_server):
    """The stale-copy guard covers links like any other stated field: a list
    written against a copy somebody changed since is refused in
    `CARD_CHANGED_REFUSAL`'s words, and nothing is written."""
    from dark_army_daemon.board import CARD_CHANGED_REFUSAL
    srv, _, store = board_server
    a, _ = store.create({"title": "a", "project": "bob", "column_name": "backlog"})
    b, _ = store.create({"title": "b", "project": "bob", "column_name": "backlog"})
    stale = store.get(a["id"])["revision"]
    store.update(a["id"], {"title": "renamed"})
    status, body = await board_fetch(
        "/api/action",
        json.dumps({"action": "board_update", "card_id": a["id"],
                    "blocked_by": b["id"],
                    "expected_revision": str(stale)}).encode(),
        {"X-Bob-Token": srv.token})
    assert status == 409, body
    assert json.loads(body)["detail"] == CARD_CHANGED_REFUSAL
    assert store.get(a["id"])["blocked_by"] == ""


@pytest.mark.asyncio
async def test_prepare_card_with_a_good_token_returns_prompt_and_workflow(server):
    srv, daemon = server

    async def fake(fields):
        fake.calls.append(fields)
        return {"prompt": "Do the thing.", "workflow": "bc-planner"}, ""
    fake.calls = []
    daemon.prepare_card_text = fake

    status, body = await fetch(
        "/api/action",
        json.dumps({"action": "prepare_card", "title": "t", "summary": "s",
                    "tool": "claude", "project": "p", "root": "/tmp"}).encode(),
        {"X-Bob-Token": srv.token})
    assert status == 200, body
    payload = json.loads(body)
    assert payload["ok"] is True
    assert payload["prompt"] == "Do the thing."
    assert payload["workflow"] == "bc-planner"
    assert fake.calls and fake.calls[0]["summary"] == "s"


@pytest.mark.asyncio
async def test_prepare_card_without_the_token_is_refused(server):
    srv, daemon = server
    called = []

    async def fake(fields):
        called.append(fields)
        return {"prompt": "x", "workflow": ""}, ""
    daemon.prepare_card_text = fake
    status, _ = await fetch(
        "/api/action",
        json.dumps({"action": "prepare_card", "summary": "s"}).encode())
    assert status == 403
    assert called == []


@pytest.mark.asyncio
async def test_prepare_card_with_an_empty_stored_token_is_refused(server):
    srv, daemon = server
    srv.token = ""
    called = []

    async def fake(fields):
        called.append(fields)
        return {"prompt": "x", "workflow": ""}, ""
    daemon.prepare_card_text = fake
    status, _ = await fetch(
        "/api/action",
        json.dumps({"action": "prepare_card", "summary": "s"}).encode(),
        {"X-Bob-Token": ""})
    assert status == 403
    assert called == []


@pytest.mark.asyncio
async def test_a_prepare_card_refusal_is_409_with_the_detail(server):
    srv, daemon = server

    async def fake(fields):
        return None, "Dark Army is not allowed to write card text (see the ⋯ menu)"
    daemon.prepare_card_text = fake
    status, body = await fetch(
        "/api/action",
        json.dumps({"action": "prepare_card", "summary": "s"}).encode(),
        {"X-Bob-Token": srv.token})
    assert status == 409
    payload = json.loads(body)
    assert payload["ok"] is False
    assert "not allowed" in payload["detail"]
    assert payload.get("prompt", "") == ""
    assert payload.get("workflow", "") == ""


@pytest.mark.asyncio
async def test_prepare_card_publishes_the_suggested_folder(server):
    srv, daemon = server

    async def fake(fields):
        return {"prompt": "Do the thing.", "workflow": "",
                "suggested_root": "/Users/x/Code/shop"}, ""
    daemon.prepare_card_text = fake

    status, body = await fetch(
        "/api/action",
        json.dumps({"action": "prepare_card", "title": "t", "summary": "s",
                    "tool": "claude", "project": "p", "root": "/tmp"}).encode(),
        {"X-Bob-Token": srv.token})
    assert status == 200, body
    assert json.loads(body)["suggested_root"] == "/Users/x/Code/shop"


@pytest.mark.asyncio
async def test_a_prepare_refusal_carries_no_suggested_folder(server):
    srv, daemon = server

    async def fake(fields):
        return None, "a card needs a description"
    daemon.prepare_card_text = fake

    status, body = await fetch(
        "/api/action",
        json.dumps({"action": "prepare_card", "summary": ""}).encode(),
        {"X-Bob-Token": srv.token})
    assert status == 409
    assert json.loads(body)["suggested_root"] == ""


@pytest.mark.asyncio
async def test_an_older_daemons_answer_reads_as_no_suggestion(server):
    """A result dict without the key is a daemon with no opinion, which must
    arrive as empty rather than as a missing field."""
    srv, daemon = server

    async def fake(fields):
        return {"prompt": "Do the thing.", "workflow": ""}, ""
    daemon.prepare_card_text = fake

    status, body = await fetch(
        "/api/action",
        json.dumps({"action": "prepare_card", "summary": "s"}).encode(),
        {"X-Bob-Token": srv.token})
    assert status == 200, body
    assert json.loads(body)["suggested_root"] == ""


def test_prepare_card_is_not_a_board_action():
    assert "prepare_card" not in ApiServer.BOARD_ACTIONS


def test_attachments_is_a_board_field():
    assert "attachments" in ApiServer._BOARD_FIELDS


def test_model_is_a_board_field():
    """A thing a person states, like `tool` beside it — so it passes the
    allow-list. The store is what refuses an off-list value."""
    assert "model" in ApiServer._BOARD_FIELDS


def test_a_board_update_carrying_a_model_survives_the_allow_list(server):
    srv, _ = server
    fields = srv._board_fields({"action": "board_update", "card_id": "c1",
                                "model": "opus"})
    assert fields == {"model": "opus"}


def test_priority_is_a_board_field():
    """`model`'s side of the line: a thing a person states about their own
    card, and the field Dark Army's own suggestion writes through the ordinary
    update."""
    assert "priority" in ApiServer._BOARD_FIELDS


def test_priority_is_in_the_stated_fields_a_refusal_reports():
    """So a conflicted phone sees the Mac's number beside its own."""
    from dark_army_daemon.daemon_board import BoardVerbsMixin
    assert "priority" in BoardVerbsMixin.STATED_FIELDS


def test_priority_supported_rides_the_pipeline_scalars():
    """A version marker on `start_when_planned_supported`'s argument: an older
    daemon simply sends no key, which decodes false, so the number box is
    drawn *absent* rather than present and 400ing inside
    `_board_named_fields`."""
    from dark_army_daemon.daemon_board import BoardVerbsMixin

    class Stub(BoardVerbsMixin):
        def _observers_implementing(self, name):
            return []

    assert Stub()._pipeline_writable()["priority_supported"] is True


def test_a_json_integer_priority_arrives_as_unscored(server):
    """The trap, written down rather than rediscovered: `_board_fields` does
    `str(payload.get(key) or "")`, so a client sending the JSON integer `0`
    stores `""` — unscored, not zero. Both clients therefore send strings."""
    srv, _ = server
    assert srv._board_fields({"action": "board_update", "card_id": "c1",
                              "priority": 0}) == {"priority": ""}
    assert srv._board_fields({"action": "board_update", "card_id": "c1",
                              "priority": "0"}) == {"priority": "0"}
    assert srv._board_fields({"action": "board_update", "card_id": "c1",
                              "priority": "90"}) == {"priority": "90"}


@pytest.mark.asyncio
async def test_prepare_card_forwards_the_attachments_string(server):
    srv, daemon = server

    async def fake(fields):
        fake.calls.append(fields)
        return {"prompt": "Do the thing.", "workflow": ""}, ""
    fake.calls = []
    daemon.prepare_card_text = fake

    status, body = await fetch(
        "/api/action",
        json.dumps({"action": "prepare_card", "title": "t", "summary": "s",
                    "tool": "claude", "project": "p", "root": "/tmp",
                    "attachments": "abcdefgh/shot.png"}).encode(),
        {"X-Bob-Token": srv.token})
    assert status == 200, body
    assert fake.calls and fake.calls[0]["attachments"] == "abcdefgh/shot.png"


@pytest.mark.asyncio
async def test_prepare_card_forwards_the_idea_and_answers_with_four_fields(
        server):
    srv, daemon = server

    async def fake(fields):
        fake.calls.append(fields)
        return ({"prompt": "Do the thing.", "workflow": "bc-planner",
                 "title": "A short name", "summary": "One sentence."}, "")
    fake.calls = []
    daemon.prepare_card_text = fake

    status, body = await fetch(
        "/api/action",
        json.dumps({"action": "prepare_card", "title": "", "summary": "",
                    "tool": "claude", "project": "p", "root": "/tmp",
                    "idea": "the whole thought"}).encode(),
        {"X-Bob-Token": srv.token})
    assert status == 200, body
    payload = json.loads(body)
    assert payload["title"] == "A short name"
    assert payload["summary"] == "One sentence."
    assert payload["prompt"] == "Do the thing."
    assert payload["workflow"] == "bc-planner"
    assert fake.calls and fake.calls[0]["idea"] == "the whole thought"


@pytest.mark.asyncio
async def test_a_legacy_prepare_payload_calls_and_answers_as_today(server):
    """An older client sends no `idea`: the daemon is called with an empty
    one, and the two new answer keys arrive empty rather than absent — which
    is what a newer client reads as \"leave that field alone\"."""
    srv, daemon = server

    async def fake(fields):
        fake.calls.append(fields)
        return {"prompt": "Do the thing.", "workflow": ""}, ""
    fake.calls = []
    daemon.prepare_card_text = fake

    status, body = await fetch(
        "/api/action",
        json.dumps({"action": "prepare_card", "title": "t", "summary": "s",
                    "tool": "claude", "project": "p",
                    "root": "/tmp"}).encode(),
        {"X-Bob-Token": srv.token})
    assert status == 200, body
    payload = json.loads(body)
    assert fake.calls[0]["idea"] == ""
    assert payload["prompt"] == "Do the thing."
    assert payload["title"] == ""
    assert payload["summary"] == ""


def test_the_prepare_verb_gained_no_route_and_no_new_list_entry():
    """The LAN and away lists are decisions somebody made; a new field on an
    existing verb must not quietly become a new one."""
    assert ApiServer.LAN_ACTIONS.count("prepare_card") == 1
    assert ApiServer.REMOTE_ACTIONS.count("prepare_card") == 1
    assert "prepare_card" not in ApiServer.BOARD_ACTIONS


# --- the refinement and the plan gate on the API surface ----------------------


def test_the_refine_fields_and_the_plan_path_are_not_api_writable():
    """The refine pair is `session_id`'s case exactly, and `plan_path` is the
    Prep column's exit condition — a surface that could write it could stamp a
    card "planned" and walk it past the plan gate with no plan behind it. The
    one writer is `BoardStore.attach_plan`."""
    assert "plan_path" not in ApiServer._BOARD_FIELDS
    assert "refine_session_id" not in ApiServer._BOARD_FIELDS
    assert "refine_state" not in ApiServer._BOARD_FIELDS
    assert "board_refine" in ApiServer.BOARD_ACTIONS
    assert "board_ask" in ApiServer.BOARD_ACTIONS
    assert "skip_plan_gate" in ApiServer._BOARD_ENVELOPE
    assert "own_terminal" in ApiServer._BOARD_ENVELOPE
    # The composer's one-press mark is envelope, never a card field: without
    # it in `_BOARD_ENVELOPE`, a write carrying only the flag would be judged
    # "named fields, all dropped" and 400.
    assert "refine" in ApiServer._BOARD_ENVELOPE
    assert "refine" not in ApiServer._BOARD_FIELDS
    assert "create_token" in ApiServer._BOARD_ENVELOPE
    assert "create_token" not in ApiServer._BOARD_FIELDS


@pytest.mark.asyncio
async def test_board_create_with_refine_calls_the_one_press_verb(board_server):
    """`refine: "true"` routes to `create_card_and_refine`; the flag itself
    never reaches the fields dict. Without it, plain `create_card` runs."""
    srv, daemon, _ = board_server
    seen = {}

    async def fake_both(fields):
        seen["fields"] = dict(fields)
        return {"id": "c1"}, "", True, "started"

    async def fake_create(fields):
        seen["plain"] = dict(fields)
        return {"id": "c2"}, ""

    daemon.create_card_and_refine = fake_both
    daemon.create_card = fake_create
    status, body = await board_fetch(
        "/api/action",
        json.dumps({"action": "board_create", "title": "x",
                    "refine": "true"}).encode(),
        {"X-Bob-Token": srv.token})
    assert status == 200, body
    out = json.loads(body)
    assert out["ok"] is True
    assert out["card_id"] == "c1"
    assert out["refine_ok"] is True
    assert out["refine_detail"] == "started"
    assert "refine" not in seen["fields"]
    assert seen["fields"]["title"] == "x"
    assert "plain" not in seen

    status, body = await board_fetch(
        "/api/action",
        json.dumps({"action": "board_create", "title": "y"}).encode(),
        {"X-Bob-Token": srv.token})
    assert status == 200, body
    assert json.loads(body)["card_id"] == "c2"
    assert json.loads(body)["refine_ok"] is False
    assert seen["plain"]["title"] == "y"


@pytest.mark.asyncio
async def test_board_create_refine_refusal_is_still_200(board_server):
    """Status follows the create alone: the card was written, so the answer
    is a success that says the second half was refused."""
    srv, daemon, _ = board_server

    async def fake_both(fields):
        return {"id": "c1"}, "", False, "nope"

    daemon.create_card_and_refine = fake_both
    status, body = await board_fetch(
        "/api/action",
        json.dumps({"action": "board_create", "title": "x",
                    "refine": "true"}).encode(),
        {"X-Bob-Token": srv.token})
    assert status == 200, body
    out = json.loads(body)
    assert out["ok"] is True
    assert out["refine_ok"] is False
    assert out["refine_detail"] == "nope"


@pytest.mark.asyncio
async def test_board_create_with_refine_flag_only_is_not_a_named_field(
        board_server):
    """A payload of nothing but the flag reaches the daemon (which refuses
    the empty title itself) rather than 400ing at the envelope gate."""
    srv, daemon, _ = board_server
    seen = {}

    async def fake_both(fields):
        seen["fields"] = dict(fields)
        return None, "a card needs a title", False, ""

    daemon.create_card_and_refine = fake_both
    status, _body = await board_fetch(
        "/api/action",
        json.dumps({"action": "board_create", "refine": "true"}).encode(),
        {"X-Bob-Token": srv.token})
    assert "fields" in seen
    assert "refine" not in seen["fields"]
    assert status == 409


_CREATE_TOKEN = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


@pytest.mark.asyncio
async def test_two_board_creates_with_the_same_token_are_one_card(board_server):
    srv, _, store = board_server
    headers = {"X-Bob-Token": srv.token}
    status, out1 = await board_fetch(
        "/api/action",
        json.dumps({"action": "board_create", "title": "first",
                    "create_token": _CREATE_TOKEN}).encode(),
        headers)
    assert status == 200, out1
    status, out2 = await board_fetch(
        "/api/action",
        json.dumps({"action": "board_create", "title": "second",
                    "create_token": _CREATE_TOKEN}).encode(),
        headers)
    assert status == 200, out2
    a, b = json.loads(out1), json.loads(out2)
    assert a["card_id"] == b["card_id"]
    assert a["card_id"]
    assert len(store.cards()) == 1
    assert store.cards()[0]["title"] == "first"


@pytest.mark.asyncio
async def test_board_create_without_a_create_token_still_creates(board_server):
    srv, _, store = board_server
    status, body = await board_fetch(
        "/api/action",
        json.dumps({"action": "board_create", "title": "plain"}).encode(),
        {"X-Bob-Token": srv.token})
    assert status == 200, body
    assert json.loads(body)["ok"] is True
    assert [c["title"] for c in store.cards()] == ["plain"]
    assert store.cards()[0]["create_token"] == ""


@pytest.mark.asyncio
async def test_board_create_refine_replay_does_not_call_refine_card(
        board_server):
    srv, daemon, store = board_server
    first, _ = store.create({"title": "x", "project": "bob",
                             "create_token": _CREATE_TOKEN})
    seen = []

    async def fake_refine(card_id):
        seen.append(card_id)
        return True, "started"

    daemon.refine_card = fake_refine
    status, body = await board_fetch(
        "/api/action",
        json.dumps({"action": "board_create", "title": "x",
                    "create_token": _CREATE_TOKEN,
                    "refine": "true"}).encode(),
        {"X-Bob-Token": srv.token})
    assert status == 200, body
    out = json.loads(body)
    assert out["card_id"] == first["id"]
    assert out["refine_ok"] is False
    assert seen == []
    assert len(store.cards()) == 1


@pytest.mark.asyncio
async def test_board_update_cannot_rewrite_create_token(board_server):
    srv, _, store = board_server
    card, _ = store.create({"title": "x", "project": "bob",
                            "create_token": _CREATE_TOKEN})
    status, body = await board_fetch(
        "/api/action",
        json.dumps({"action": "board_update", "card_id": card["id"],
                    "title": "y",
                    "create_token": "ffffffff-1111-4222-8333-444444444444"}
                   ).encode(),
        {"X-Bob-Token": srv.token})
    assert status == 200, body
    got = store.get(card["id"])
    assert got["title"] == "y"
    assert got["create_token"] == _CREATE_TOKEN


@pytest.mark.asyncio
async def test_board_refine_routes_to_the_daemon(board_server):
    srv, daemon, store = board_server
    card, _ = store.create({"title": "x", "project": "bob", "root": "/tmp"})
    seen = {}

    async def fake_refine(card_id):
        seen["card_id"] = card_id
        return True, "started"

    daemon.refine_card = fake_refine
    status, body = await board_fetch(
        "/api/action",
        json.dumps({"action": "board_refine", "card_id": card["id"]}).encode(),
        {"X-Bob-Token": srv.token})
    assert status == 200, body
    assert json.loads(body)["ok"] is True
    assert seen["card_id"] == card["id"]


@pytest.mark.asyncio
async def test_refine_from_the_api_is_refused_when_the_switch_is_off(board_server):
    """Refine is Dark Army starting a session, so it sits behind the same one
    preference dispatch does — a second toggle would be two copies of a gate."""
    srv, daemon, store = board_server
    card, _ = store.create({"title": "x", "project": "bob", "root": "/tmp"})
    daemon.board_dispatch_enabled = False
    status, body = await board_fetch(
        "/api/action",
        json.dumps({"action": "board_refine", "card_id": card["id"]}).encode(),
        {"X-Bob-Token": srv.token})
    assert status == 409
    assert "not allowed to start sessions" in json.loads(body)["detail"]


@pytest.mark.asyncio
async def test_an_unplanned_move_into_in_progress_is_refused_then_confirmed(
        board_server):
    """The plan gate over the API, both halves: the bare move 409s with the
    refusal in words, and the same move carrying `skip_plan_gate` lands."""
    srv, _, store = board_server
    card, _ = store.create({"title": "x", "project": "bob"})
    status, body = await board_fetch(
        "/api/action",
        json.dumps({"action": "board_update", "card_id": card["id"],
                    "column_name": "in_progress"}).encode(),
        {"X-Bob-Token": srv.token})
    assert status == 409
    assert "no plan yet" in json.loads(body)["detail"]
    assert store.get(card["id"])["column_name"] == "prep"

    status, body = await board_fetch(
        "/api/action",
        json.dumps({"action": "board_update", "card_id": card["id"],
                    "column_name": "in_progress",
                    "skip_plan_gate": "true"}).encode(),
        {"X-Bob-Token": srv.token})
    assert status == 200, body
    assert store.get(card["id"])["column_name"] == "in_progress"


@pytest.mark.asyncio
async def test_the_gate_guards_the_reorder_door_too(board_server):
    """`_after_board_write`'s lesson: two doors onto one column move, and a
    gate on only one of them works for half the drags."""
    srv, _, store = board_server
    card, _ = store.create({"title": "x", "project": "bob"})
    status, body = await board_fetch(
        "/api/action",
        json.dumps({"action": "board_reorder", "card_id": card["id"],
                    "column_name": "in_progress", "before_id": ""}).encode(),
        {"X-Bob-Token": srv.token})
    assert status == 409
    assert "no plan yet" in json.loads(body)["detail"]
    status, body = await board_fetch(
        "/api/action",
        json.dumps({"action": "board_reorder", "card_id": card["id"],
                    "column_name": "in_progress", "before_id": "",
                    "skip_plan_gate": "true"}).encode(),
        {"X-Bob-Token": srv.token})
    assert status == 200, body
    assert store.get(card["id"])["column_name"] == "in_progress"


@pytest.mark.asyncio
async def test_an_update_naming_only_the_skip_flag_still_400s(board_server):
    """`skip_plan_gate` is envelope, not a card field: it must not rescue an
    update that names nothing writable, and it must not count as a named
    field either — the 400 is about the *card* content."""
    srv, _, store = board_server
    card, _ = store.create({"title": "x", "project": "bob"})
    status, body = await board_fetch(
        "/api/action",
        json.dumps({"action": "board_update", "card_id": card["id"],
                    "skip_plan_gate": "true"}).encode(),
        {"X-Bob-Token": srv.token})
    assert status == 400
    assert "no fields to change" in json.loads(body)["detail"]


# --- the folders (initiatives) are gone from the API --------------------------


def test_no_folder_field_and_no_folder_verb_survives():
    """The folders were retired whole at schema 22: no membership field a
    surface could write, no authorship field, and none of the three verbs
    on the loopback table — a stale client sending one gets "unknown
    action", never a silent 200."""
    assert "initiative_id" not in ApiServer._BOARD_FIELDS
    assert "initiative_by" not in ApiServer._BOARD_FIELDS
    assert not hasattr(ApiServer, "_INITIATIVE_FIELDS")
    for action in ("board_initiative_create", "board_initiative_update",
                   "board_initiative_delete"):
        assert action not in ApiServer.BOARD_ACTIONS
        assert action not in ApiServer.LAN_ACTIONS
        assert action not in ApiServer.REMOTE_ACTIONS


@pytest.mark.asyncio
async def test_a_payload_naming_only_a_retired_folder_field_is_a_400(board_server):
    """Named fields, every one of them dropped, is a client error and never a
    silent 200 — the card must not change and the caller must not be told the
    write landed. A stale client still sending `initiative_id` is that case."""
    srv, _, store = board_server
    card, _ = store.create({"title": "x", "project": "bob",
                            "column_name": "backlog"})
    status, body = await board_fetch(
        "/api/action",
        json.dumps({"action": "board_update", "card_id": card["id"],
                    "initiative_id": "f1"}).encode(),
        {"X-Bob-Token": srv.token})
    assert status == 400, body
    assert "initiative_id" not in store.get(card["id"])


# --- the per-project work queue on the API surface ---------------------------


def test_the_queue_fields_are_not_api_writable_and_are_not_clock_fields():
    """`session_id`'s ring, with its own argument: a surface that could *set*
    a queue slot would be manufacturing a claim on Dark Army's future auto-start, or
    reordering somebody else's queue. Clearing is a verb of its own.

    And neither field may join `_CLOCK_FIELDS`: `queued_at` never moves without
    `queue_state` moving beside it, so it can never generate a frame on its own
    — but stripping it would hide a genuine re-queue, which is the stale-panel
    half of that set's stated trade.
    """
    assert "queue_state" not in ApiServer._BOARD_FIELDS
    assert "queued_at" not in ApiServer._BOARD_FIELDS
    assert "queue_rank" not in ApiServer._BOARD_FIELDS
    assert "board_unqueue" in ApiServer.BOARD_ACTIONS
    assert "board_queue_move" in ApiServer.BOARD_ACTIONS
    assert "queued_at" not in api_mod._CLOCK_FIELDS
    assert "queue_state" not in api_mod._CLOCK_FIELDS
    assert "queue_rank" not in api_mod._CLOCK_FIELDS


@pytest.mark.asyncio
async def test_a_board_update_naming_the_queue_fields_changes_nothing(board_server):
    """The allow-list is the gate, so a payload naming only excluded fields is
    a 400 rather than a quiet success — the shape `_board_fields` already
    settled on for `agent_trail`."""
    srv, daemon, store = board_server
    card, _ = store.create({"title": "x", "project": "bob", "root": "/tmp"})
    # The drain is switched off for this test on purpose: it is about the
    # API's allow-list, and a live drain would legitimately dequeue an
    # unplanned card between the two asserts.
    daemon.board_autostart_enabled = False
    store.update(card["id"], {"queue_state": "queued", "queued_at": 5.0})
    status, body = await board_fetch(
        "/api/action",
        json.dumps({"action": "board_update", "card_id": card["id"],
                    "queue_state": "", "queued_at": 0}).encode(),
        {"X-Bob-Token": srv.token})
    assert status == 400, body
    assert store.get(card["id"])["queue_state"] == "queued"
    status, body = await board_fetch(
        "/api/action",
        json.dumps({"action": "board_update", "card_id": card["id"],
                    "queue_rank": "1"}).encode(),
        {"X-Bob-Token": srv.token})
    assert status == 400, body
    assert store.get(card["id"])["queue_rank"] is None
    status, body = await board_fetch(
        "/api/action",
        json.dumps({"action": "board_update", "card_id": card["id"],
                    "queue_rank": "1", "queue_state": "",
                    "queued_at": "0"}).encode(),
        {"X-Bob-Token": srv.token})
    assert status == 400, body
    got = store.get(card["id"])
    assert got["queue_state"] == "queued" and got["queued_at"] == 5.0
    assert got["queue_rank"] is None


@pytest.mark.asyncio
async def test_board_unqueue_clears_the_slot(board_server):
    srv, daemon, store = board_server
    daemon.board_autostart_enabled = False
    card, _ = store.create({"title": "x", "project": "bob", "root": "/tmp"})
    store.update(card["id"], {"queue_state": "queued", "queued_at": 5.0})
    status, body = await board_fetch(
        "/api/action",
        json.dumps({"action": "board_unqueue", "card_id": card["id"]}).encode(),
        {"X-Bob-Token": srv.token})
    assert status == 200, body
    assert json.loads(body)["ok"] is True
    got = store.get(card["id"])
    assert got["queue_state"] == "" and got["queued_at"] is None
    # And a card that is not queued is a 409 with the reason, not a silent 200.
    status, body = await board_fetch(
        "/api/action",
        json.dumps({"action": "board_unqueue", "card_id": card["id"]}).encode(),
        {"X-Bob-Token": srv.token})
    assert status == 409
    assert "not queued" in json.loads(body)["detail"]


@pytest.mark.asyncio
async def test_board_queue_move_reaches_the_daemon_verb(board_server):
    srv, daemon, store = board_server
    daemon.board_autostart_enabled = False
    a, _ = store.create({"title": "a", "project": "bob", "root": "/tmp"})
    b, _ = store.create({"title": "b", "project": "bob", "root": "/tmp"})
    store.update(a["id"], {"queue_state": "queued", "queued_at": 10.0})
    store.update(b["id"], {"queue_state": "queued", "queued_at": 20.0})
    status, body = await board_fetch(
        "/api/action",
        json.dumps({"action": "board_queue_move", "card_id": b["id"],
                    "before_id": a["id"]}).encode(),
        {"X-Bob-Token": srv.token})
    assert status == 200, body
    assert json.loads(body)["ok"] is True
    assert [c["title"] for c in store.queued_cards("bob")] == ["b", "a"]
    idle, _ = store.create({"title": "idle", "project": "bob", "root": "/tmp"})
    status, body = await board_fetch(
        "/api/action",
        json.dumps({"action": "board_queue_move", "card_id": idle["id"],
                    "before_id": a["id"]}).encode(),
        {"X-Bob-Token": srv.token})
    assert status == 409
    assert "not queued" in json.loads(body)["detail"]


def test_manual_steps_is_not_api_writable():
    """`closed_by`'s side of the line: it is a statement the session that did
    the work made about its own work, so a surface that could set it could
    hang a chore on a card nobody flagged — and the badge's whole value is
    that it appears only where that session put it. Clearing is a verb."""
    assert "manual_steps" not in ApiServer._BOARD_FIELDS
    assert "board_manual_clear" in ApiServer.BOARD_ACTIONS


@pytest.mark.asyncio
async def test_a_board_update_naming_manual_steps_changes_nothing(board_server):
    srv, daemon, store = board_server
    card, _ = store.create({"title": "x", "project": "bob", "root": "/tmp"})
    store.bind_session(card["id"], "s1")
    store.flag_manual(card["id"], "s1", "1. look at it")
    status, body = await board_fetch(
        "/api/action",
        json.dumps({"action": "board_update", "card_id": card["id"],
                    "manual_steps": "go and do something else"}).encode(),
        {"X-Bob-Token": srv.token})
    assert status == 400, body
    assert store.get(card["id"])["manual_steps"] == "1. look at it"


@pytest.mark.asyncio
async def test_board_manual_clear_empties_the_field(board_server):
    srv, daemon, store = board_server
    card, _ = store.create({"title": "x", "project": "bob", "root": "/tmp"})
    store.bind_session(card["id"], "s1")
    store.flag_manual(card["id"], "s1", "1. look at it")
    status, body = await board_fetch(
        "/api/action",
        json.dumps({"action": "board_manual_clear",
                    "card_id": card["id"]}).encode(),
        {"X-Bob-Token": srv.token})
    assert status == 200, body
    assert json.loads(body)["ok"] is True
    assert store.get(card["id"])["manual_steps"] == ""
    # A second press, and an unknown card, are 409s with the reason rather
    # than silent 200s.
    status, body = await board_fetch(
        "/api/action",
        json.dumps({"action": "board_manual_clear",
                    "card_id": card["id"]}).encode(),
        {"X-Bob-Token": srv.token})
    assert status == 409
    assert "no manual check" in json.loads(body)["detail"]
    status, body = await board_fetch(
        "/api/action",
        json.dumps({"action": "board_manual_clear",
                    "card_id": "nope"}).encode(),
        {"X-Bob-Token": srv.token})
    assert status == 409


@pytest.mark.asyncio
async def test_board_manual_clear_honours_a_matching_echo_and_refuses_a_stale_one(
        board_server):
    """The phone's current-state echo on the loopback door. Absent means
    today's statement (the test above); a present `expected_manual_steps`
    is ANDed into the store's WHERE — a match clears, a stale one is 409 in
    `MANUAL_CHECK_CHANGED_REFUSAL`'s words with the field untouched."""
    from dark_army_daemon.board import MANUAL_CHECK_CHANGED_REFUSAL
    srv, daemon, store = board_server
    card, _ = store.create({"title": "x", "project": "bob", "root": "/tmp"})
    store.bind_session(card["id"], "s1")
    store.flag_manual(card["id"], "s1", "1. look at it")
    status, body = await board_fetch(
        "/api/action",
        json.dumps({"action": "board_manual_clear", "card_id": card["id"],
                    "expected_manual_steps": "1. yesterday's steps"}).encode(),
        {"X-Bob-Token": srv.token})
    assert status == 409, body
    assert json.loads(body)["detail"] == MANUAL_CHECK_CHANGED_REFUSAL
    assert store.get(card["id"])["manual_steps"] == "1. look at it"
    # Present but empty is a real, refusable echo — not "no guard".
    status, body = await board_fetch(
        "/api/action",
        json.dumps({"action": "board_manual_clear", "card_id": card["id"],
                    "expected_manual_steps": ""}).encode(),
        {"X-Bob-Token": srv.token})
    assert status == 409, body
    assert store.get(card["id"])["manual_steps"] == "1. look at it"
    status, body = await board_fetch(
        "/api/action",
        json.dumps({"action": "board_manual_clear", "card_id": card["id"],
                    "expected_manual_steps": "1. look at it"}).encode(),
        {"X-Bob-Token": srv.token})
    assert status == 200, body
    assert store.get(card["id"])["manual_steps"] == ""


def test_a_manual_flag_is_news_and_reaches_a_slim_client():
    """`?sections=changed` omits the board only while its clock-stripped JSON
    is unchanged. A card gaining a badge has to survive that comparison, or it
    would not appear until some unrelated card was edited."""
    before = {"board": {"generated_at": 1.0, "cards": [
        {"id": "a", "manual_steps": ""}]}}
    after = {"board": {"generated_at": 2.0, "cards": [
        {"id": "a", "manual_steps": "1. look"}]}}
    assert api_mod._news(before) != api_mod._news(after)


def test_a_queue_change_is_news_and_reaches_a_slim_client():
    """`?sections=changed` omits the board only while its clock-stripped JSON
    is unchanged. A card joining a queue has to survive that comparison, or the
    band would not appear until some unrelated card was edited."""
    before = {"board": {"generated_at": 1.0, "cards": [
        {"id": "a", "queue_state": "", "queued_at": None}]}}
    after = {"board": {"generated_at": 2.0, "cards": [
        {"id": "a", "queue_state": "queued", "queued_at": 9.0}]}}
    assert api_mod._news(before) != api_mod._news(after)
    # And a frame that differs only in the clock still is not news.
    same = {"board": {"generated_at": 3.0, "cards": [
        {"id": "a", "queue_state": "queued", "queued_at": 9.0}]}}
    assert api_mod._news(after) == api_mod._news(same)


# --- the review acknowledgement ----------------------------------------------


def test_reviewed_at_is_not_api_writable():
    """The field records a human's acknowledgement, but the card sheet saves
    several fields on every edit and a Save must never silently acknowledge a
    review the person did not make. The gesture arrives as the named verb
    `board_review` instead — the field is closed, the act is open."""
    assert "reviewed_at" not in ApiServer._BOARD_FIELDS
    assert "board_review" in ApiServer.BOARD_ACTIONS


@pytest.mark.asyncio
async def test_a_board_update_naming_reviewed_at_changes_nothing(board_server):
    srv, daemon, store = board_server
    card, _ = store.create({"title": "x", "project": "bob", "root": "/tmp",
                            "column_name": "in_progress"})
    store.bind_session(card["id"], "s1")
    store.declare_done(card["id"], "s1", "tests pass")
    status, body = await board_fetch(
        "/api/action",
        json.dumps({"action": "board_update", "card_id": card["id"],
                    "reviewed_at": "123.0"}).encode(),
        {"X-Bob-Token": srv.token})
    assert status == 400, body
    assert store.get(card["id"])["reviewed_at"] is None


@pytest.mark.asyncio
async def test_board_review_stamps_the_acknowledgement(board_server):
    srv, daemon, store = board_server
    card, _ = store.create({"title": "x", "project": "bob", "root": "/tmp",
                            "column_name": "in_progress"})
    store.bind_session(card["id"], "s1")
    store.declare_done(card["id"], "s1", "tests pass")
    status, body = await board_fetch(
        "/api/action",
        json.dumps({"action": "board_review",
                    "card_id": card["id"]}).encode(),
        {"X-Bob-Token": srv.token})
    assert status == 200, body
    assert json.loads(body)["ok"] is True
    got = store.get(card["id"])
    assert got["reviewed_at"] is not None
    # The signature survives the acknowledgement.
    assert got["closed_by"] == "s1" and got["close_note"] == "tests pass"
    # A second press, a hand-dragged Done card and an unknown card are 409s
    # with the store's refusal, never silent 200s.
    status, body = await board_fetch(
        "/api/action",
        json.dumps({"action": "board_review",
                    "card_id": card["id"]}).encode(),
        {"X-Bob-Token": srv.token})
    assert status == 409
    assert "already reviewed" in json.loads(body)["detail"]
    dragged, _ = store.create({"title": "dragged", "project": "bob",
                               "root": "/tmp", "column_name": "done"})
    status, body = await board_fetch(
        "/api/action",
        json.dumps({"action": "board_review",
                    "card_id": dragged["id"]}).encode(),
        {"X-Bob-Token": srv.token})
    assert status == 409
    assert "only an assistant's close" in json.loads(body)["detail"]
    status, body = await board_fetch(
        "/api/action",
        json.dumps({"action": "board_review", "card_id": "nope"}).encode(),
        {"X-Bob-Token": srv.token})
    assert status == 409


@pytest.mark.asyncio
async def test_board_review_honours_a_matching_echo_and_refuses_a_stale_one(
        board_server):
    """`board_manual_clear`'s twin on the loopback door: both halves of the
    close identity present and matching stamps the review; a stale pair is
    409 in `REVIEW_CHANGED_REFUSAL`'s words; half a pair is refused before
    any UPDATE in `REVIEW_HALF_ECHO_REFUSAL`'s. `reviewed_at` stays `None`
    on every refusal."""
    from dark_army_daemon.board import (REVIEW_CHANGED_REFUSAL,
                                            REVIEW_HALF_ECHO_REFUSAL)
    srv, daemon, store = board_server
    card, _ = store.create({"title": "x", "project": "bob", "root": "/tmp",
                            "column_name": "in_progress"})
    store.bind_session(card["id"], "s1")
    store.declare_done(card["id"], "s1", "tests pass")

    async def post(fields):
        return await board_fetch(
            "/api/action",
            json.dumps({"action": "board_review", "card_id": card["id"],
                        **fields}).encode(),
            {"X-Bob-Token": srv.token})

    status, body = await post({"expected_closed_by": "s1",
                               "expected_close_note": "yesterday's close"})
    assert status == 409, body
    assert json.loads(body)["detail"] == REVIEW_CHANGED_REFUSAL
    assert store.get(card["id"])["reviewed_at"] is None
    status, body = await post({"expected_closed_by": "s1"})
    assert status == 409, body
    assert json.loads(body)["detail"] == REVIEW_HALF_ECHO_REFUSAL
    assert store.get(card["id"])["reviewed_at"] is None
    status, body = await post({"expected_close_note": "tests pass"})
    assert status == 409, body
    assert json.loads(body)["detail"] == REVIEW_HALF_ECHO_REFUSAL
    status, body = await post({"expected_closed_by": "s1",
                               "expected_close_note": "tests pass"})
    assert status == 200, body
    got = store.get(card["id"])
    assert got["reviewed_at"] is not None
    assert got["closed_by"] == "s1" and got["close_note"] == "tests pass"


@pytest.mark.asyncio
async def test_board_review_requires_a_card_id(board_server):
    srv, _, _ = board_server
    status, body = await board_fetch(
        "/api/action",
        json.dumps({"action": "board_review"}).encode(),
        {"X-Bob-Token": srv.token})
    assert status == 400
    assert "card_id" in json.loads(body)["error"]


def test_a_review_is_news_and_reaches_a_slim_client():
    """`?sections=changed` omits the board only while its clock-stripped JSON
    is unchanged. A banner dropping has to survive that comparison, or the
    card would sit pinned until some unrelated card was edited."""
    before = {"board": {"generated_at": 1.0, "cards": [
        {"id": "a", "closed_by": "s1", "reviewed_at": None}]}}
    after = {"board": {"generated_at": 2.0, "cards": [
        {"id": "a", "closed_by": "s1", "reviewed_at": 9.0}]}}
    assert api_mod._news(before) != api_mod._news(after)


# --- enrolment ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_enroll_project_needs_the_token(server):
    body = json.dumps({"action": "enroll_project", "root": "/tmp"}).encode()
    assert (await fetch("/api/action", body))[0] == 403


@pytest.mark.asyncio
async def test_enroll_project_refuses_a_bearer_header(server):
    """The header is `X-Bob-Token`, not `Authorization: Bearer`. Reads are
    ungated, so the wrong header looks like it works and every write silently
    403s — which is why this is pinned per new write, not once."""
    srv, _ = server
    body = json.dumps({"action": "enroll_project", "root": "/tmp"}).encode()
    status, _ = await fetch("/api/action", body,
                            {"Authorization": f"Bearer {srv.token}"})
    assert status == 403


@pytest.mark.asyncio
async def test_enroll_and_unenroll_answer_with_the_modules_own_words(server,
                                                                     tmp_path,
                                                                     monkeypatch):
    from dark_army_daemon import enrollment, paths as paths_mod
    srv, _ = server
    monkeypatch.setattr(paths_mod, "ENROLLMENT_PATH",
                        tmp_path / "enrollment.json")
    monkeypatch.setattr(paths_mod, "STATE_DIR", tmp_path / "state")
    enrollment.invalidate()
    root = tmp_path / "proj"
    root.mkdir()

    body = json.dumps({"action": "enroll_project", "root": str(root)}).encode()
    status, out = await fetch("/api/action", body, {"X-Bob-Token": srv.token})
    assert status == 200 and json.loads(out)["ok"] is True
    assert (root / enrollment.KEY_RELATIVE).is_file()

    body = json.dumps({"action": "unenroll_project",
                       "root": str(root)}).encode()
    status, out = await fetch("/api/action", body, {"X-Bob-Token": srv.token})
    assert status == 200 and json.loads(out)["ok"] is True

    # And a second un-enrol is a 409 carrying the module's own refusal.
    status, out = await fetch("/api/action", body, {"X-Bob-Token": srv.token})
    assert status == 409 and "not enrolled" in json.loads(out)["detail"]
    enrollment.invalidate()


@pytest.mark.asyncio
async def test_the_state_carries_the_enrolment_section(server):
    """`available` is stated by the daemon rather than inferred from an empty
    list: an older daemon sends no section, and an empty list decoding as
    "nothing is enrolled" would draw the prompt over a working fleet."""
    _status, body = await fetch("/api/state")
    section = json.loads(body)["enrollment"]
    assert section["available"] is True
    assert isinstance(section["enrolled"], list)
    assert isinstance(section["pending"], list)


# --- the diary (`GET /api/log`) ------------------------------------------------


def _diary(daemon, tmp_path):
    from dark_army_daemon.event_log import EventLog
    log = EventLog(path=tmp_path / "event-log.jsonl")
    log.open()
    daemon._event_log = log
    return log


@pytest.mark.asyncio
async def test_log_is_served_newest_first(server, tmp_path):
    srv, daemon = server
    log = _diary(daemon, tmp_path)
    now = time.time()
    log.append("session_start", nickname="a", project="p", ts=now - 20)
    log.append("session_end", nickname="a", project="p", ts=now - 10,
               detail={"end_reason": "ended"})
    status, body = await fetch("/api/log")
    assert status == 200
    page = json.loads(body)
    assert page["available"] is True
    assert [e["kind"] for e in page["events"]] == ["session_end", "session_start"]
    assert page["events"][0]["text"] == "a finished in p"
    assert "generated_at" in page


@pytest.mark.asyncio
async def test_log_since_filters_strictly_and_limit_caps(server, tmp_path):
    srv, daemon = server
    log = _diary(daemon, tmp_path)
    now = time.time()
    for i in range(4):
        log.append("session_start", nickname=f"n{i}", ts=now - 40 + i)
    status, body = await fetch(f"/api/log?since={now - 40 + 1}")
    assert status == 200
    assert [e["nickname"] for e in json.loads(body)["events"]] == ["n3", "n2"]
    status, body = await fetch("/api/log?limit=1")
    assert [e["nickname"] for e in json.loads(body)["events"]] == ["n3"]


@pytest.mark.asyncio
async def test_log_rejects_a_bad_since_or_limit(server, tmp_path):
    srv, daemon = server
    _diary(daemon, tmp_path)
    for query in ("since=abc", "since=-1", "since=nan", "limit=0", "limit=x",
                  "limit=501"):
        status, _ = await fetch(f"/api/log?{query}")
        assert status == 400, query


@pytest.mark.asyncio
async def test_log_without_a_diary_says_so(server):
    srv, daemon = server
    assert daemon._event_log is None
    status, body = await fetch("/api/log")
    assert status == 200
    page = json.loads(body)
    assert page["available"] is False
    assert page["events"] == []


@pytest.mark.asyncio
async def test_state_carries_no_log_key(server, tmp_path):
    srv, daemon = server
    _diary(daemon, tmp_path).append("session_start", nickname="a")
    _status, body = await fetch("/api/state")
    assert "log" not in json.loads(body)


@pytest.mark.asyncio
@pytest.mark.parametrize('auth', ['token', 'missing', 'bearer', 'origin', 'host'])
async def test_refinement_close_action_is_authenticated_loopback_only(server, monkeypatch, auth):
    srv, daemon = server
    calls = []
    async def close(sid):
        calls.append(sid)
        return False, 'No completed plan attachment.'
    monkeypatch.setattr(daemon, 'close_refinement_terminal', close)
    headers = {'X-Bob-Token': srv.token}
    if auth == 'missing': headers = {}
    elif auth == 'bearer': headers = {'Authorization': 'Bearer ' + srv.token}
    elif auth == 'origin': headers['Origin'] = 'https://evil.example'
    elif auth == 'host': headers['Host'] = 'evil.example'
    status, body = await fetch('/api/action',
        json.dumps({'action': 'close_refinement_terminal', 'session_id': 'codex:one'}).encode(),
        headers=headers)
    assert status == (409 if auth == 'token' else 403)
    assert calls == (['codex:one'] if auth == 'token' else [])
    assert 'close_refinement_terminal' not in srv.LAN_ACTIONS
    assert 'close_refinement_terminal' not in srv.REMOTE_ACTIONS


@pytest.mark.asyncio
async def test_refinement_close_is_not_a_sealed_action(server, monkeypatch):
    srv, daemon = server
    async def forbidden(*args, **kwargs):
        pytest.fail('sealed door reached refinement close')
    monkeypatch.setattr(daemon, 'close_refinement_terminal', forbidden)
    for actions in (srv.LAN_ACTIONS, srv.REMOTE_ACTIONS):
        result = await srv._sealed_run('action', {'action': 'close_refinement_terminal',
                                      'session_id': 'codex:one'}, 'fixture', actions=actions, check_lease=False, record=False)
        assert result[0] == 404


@pytest.mark.asyncio
async def test_collaboration_section_slim_carry_and_present_empty_clear(server):
    from dark_army_daemon.collaboration import build
    srv, daemon = server
    daemon._collaboration = build({"running": [{"session_id": "root", "sent_to": {"missing": {"count": 2}}}]})
    reader, writer, _ = await _open_stream(query="?sections=changed")
    try:
        first = await _next_event(reader)
        assert first["collaboration"] == daemon._collaboration
        await _isolate_limiter(srv, reader)
        srv._broadcast()
        await _next_event(reader)
        srv.on_activity_change(9, 0, 0, 0)
        slim = await _next_event(reader)
        assert "collaboration" not in slim
        daemon._collaboration = build({})
        srv._broadcast()
        cleared = await _next_event(reader)
        assert cleared["collaboration"]["nodes"] == []
        assert cleared["collaboration"]["available"] is True
    finally:
        writer.close()


@pytest.mark.asyncio
async def test_collaboration_loopback_home_relay_digest_and_transport_size(server):
    from dark_army_daemon.collaboration import build, MAX_BYTES
    srv, daemon = server
    rows = [{"session_id": str(i), "nickname": "ą" * 800, "provider": "claude",
             "sent_to": {"peer": {"count": 1, "last": "invalid"}}} for i in range(260)]
    daemon._collaboration = build({"running": rows})
    status, raw = await fetch("/api/state")
    assert status == 200 and json.loads(raw)["collaboration"] == daemon._collaboration
    status, _, home = await srv._sealed_run("state", {}, "fixture",
        actions=srv.LAN_ACTIONS, check_lease=False, record=False)
    status2, _, away = await srv._remote_run("state", {}, "fixture")
    assert status == status2 == 200
    assert json.loads(home)["collaboration"] == json.loads(away)["collaboration"]
    assert len(json.dumps(daemon._collaboration, separators=(",", ":")).encode()) <= MAX_BYTES
    # Exercise the actual seal/open envelope, including JSON's escaped Unicode.
    from dark_army_daemon import relay
    assert len(home) < relay.RELAY_FRAME_MAX_BYTES
    for namespace in (relay.HOME, relay.RELAY):
        wire = relay.seal_frame(b"x" * 32, relay.DIR_MAC_TO_PHONE, 1, "state", json.loads(home), ns=namespace)
        assert wire and len(wire) <= relay.RELAY_FRAME_MAX_BYTES
        frame, refusal = relay.open_frame(b"x" * 32, relay.DIR_MAC_TO_PHONE, wire, 0, ns=namespace)
        assert not refusal and frame["body"]["collaboration"] == daemon._collaboration
    digest = json.loads(home)["state_digest"]
    unchanged = json.loads(srv._state_answer({"digest": digest})[2])
    assert unchanged["unchanged"] is True
    daemon._collaboration = build({})
    changed = json.loads(srv._state_answer({"digest": digest})[2])
    assert "unchanged" not in changed and changed["state_digest"] != digest


def test_area_is_supported_and_stated():
    from dark_army_daemon.daemon_board import BoardVerbsMixin
    class Stub(BoardVerbsMixin):
        def _observers_implementing(self, name): return []
    assert Stub()._pipeline_writable()["areas_supported"] is True
    assert "area" in BoardVerbsMixin.STATED_FIELDS
    assert "area" in ApiServer._BOARD_FIELDS


@pytest.mark.asyncio
@pytest.mark.parametrize("door", ["home", "relay"])
async def test_sealed_board_create_preserves_area_and_refuses_unknown(board_server, door):
    srv, _, store = board_server
    actions = srv.LAN_ACTIONS if door == "home" else srv.REMOTE_ACTIONS
    status, _, body = await srv._sealed_run(
        "action", {"action": "board_create", "title": "area card", "area": "pocket"},
        "fixture", actions=actions, check_lease=False, record=False)
    assert status == 200, body
    card_id = json.loads(body)["card_id"]
    assert store.get(card_id)["area"] == "pocket"
    status, _, body = await srv._sealed_run(
        "action", {"action": "board_create", "title": "invalid", "area": "unknown"},
        "fixture", actions=actions, check_lease=False, record=False)
    assert status == 409, body
    assert [card["id"] for card in store.cards()] == [card_id]


@pytest.mark.asyncio
async def test_area_marker_and_card_survive_loopback_home_and_relay_snapshots(board_server):
    from dark_army_daemon import relay
    srv, daemon, store = board_server
    card, detail = store.create({"title": "delivery", "area": "backbone"})
    assert card, detail
    daemon._refresh_board_state()
    status, body = await board_fetch("/api/state")
    assert status == 200
    snapshots = [json.loads(body)]
    for actions, namespace in ((srv.LAN_ACTIONS, relay.HOME),
                               (srv.REMOTE_ACTIONS, relay.RELAY)):
        status, _, body = await srv._sealed_run(
            "state", {}, "fixture", actions=actions, check_lease=False, record=False)
        assert status == 200
        wire = relay.seal_frame(b"a" * 32, relay.DIR_MAC_TO_PHONE, 1, "state",
                                json.loads(body), ns=namespace)
        opened, refusal = relay.open_frame(b"a" * 32, relay.DIR_MAC_TO_PHONE,
                                           wire, 0, ns=namespace)
        assert not refusal
        snapshots.append(opened["body"])
    for snapshot in snapshots:
        assert snapshot["board"]["areas_supported"] is True
        saved = next(row for row in snapshot["board"]["cards"] if row["id"] == card["id"])
        assert saved["area"] == "backbone"


@pytest.mark.asyncio
@pytest.mark.parametrize("door", ["loopback", "home", "relay"])
async def test_area_revision_conflict_returns_current_without_overwriting(board_server, door):
    srv, _, store = board_server
    card, detail = store.create({"title": "delivery", "area": "backbone"})
    assert card, detail
    current, detail = store.update(card["id"], {"area": "desk"})
    assert current, detail
    payload = {"action": "board_update", "card_id": card["id"], "area": "pocket",
               "expected_revision": card["revision"]}
    if door == "loopback":
        status, body = await board_fetch("/api/action", json.dumps(payload).encode(),
                                        {"X-Bob-Token": srv.token})
    else:
        actions = srv.LAN_ACTIONS if door == "home" else srv.REMOTE_ACTIONS
        status, _, body = await srv._sealed_run(
            "action", payload, "fixture", actions=actions, check_lease=False, record=False)
    assert status == 409, body
    returned = json.loads(body)["current"]
    assert returned["area"] == "desk"
    assert returned["revision"] == current["revision"]
    assert store.get(card["id"])["area"] == "desk"
    assert store.get(card["id"])["revision"] == current["revision"]


@pytest.mark.asyncio
async def test_a_state_read_asked_with_usage_carries_the_bars_beside_the_picture(server):
    """Proposal 1 of the relay-latency audit (21 Sep 2026): a phone that
    sends `with_usage` gets `_usage_report_for`'s body as a sibling key on
    the state answer — on the full picture and on the short `unchanged`
    line alike — and makes no second mailbox trip. Folded in after the
    digest, so moving bars never make a standing picture look changed;
    without the marker nothing is added."""
    srv, _daemon = server
    status, _, plain = await srv._remote_run("state", {}, "fixture")
    assert status == 200 and "usage" not in json.loads(plain)
    status, _, home = await srv._sealed_run(
        "state", {"with_usage": True}, "fixture",
        actions=srv.LAN_ACTIONS, check_lease=False, record=False)
    status2, _, away = await srv._remote_run("state", {"with_usage": True}, "fixture")
    assert status == status2 == 200
    for raw in (home, away):
        answer = json.loads(raw)
        assert "limits" in answer["usage"] and "bars" in answer["usage"]["limits"]
        # The same digest as the bare read: the bars are not in its input.
        assert answer["state_digest"] == json.loads(plain)["state_digest"]
    digest = json.loads(plain)["state_digest"]
    status, _, kept = await srv._remote_run(
        "state", {"digest": digest, "with_usage": True}, "fixture")
    answer = json.loads(kept)
    assert answer["unchanged"] is True and "limits" in answer["usage"]


@pytest.mark.asyncio
async def test_the_folded_usage_body_is_built_once_per_memo_window(server, monkeypatch):
    """The state body is memory; the usage body it rides beside is a 100 KB
    parse plus SQLite, rebuilt on every check-in (135-186 ms against 2 ms,
    21 Sep 2026). The fold believes one build for `USAGE_MEMO_SECONDS` per
    query string; the loopback `/api/usage` and the sealed `usage` read
    never look at the memo."""
    srv, _daemon = server
    calls = []
    real = srv._usage_report_for

    async def counted(query):
        calls.append(query)
        return await real(query)

    monkeypatch.setattr(srv, "_usage_report_for", counted)
    for _ in range(2):
        status, _, body = await srv._remote_run(
            "state", {"with_usage": True}, "fixture")
        assert status == 200 and "limits" in json.loads(body)["usage"]
    assert calls == [""]
    # The window lapses: the next check-in rebuilds.
    stamp, usage = srv._usage_memo[""]
    srv._usage_memo[""] = (stamp - api_mod.USAGE_MEMO_SECONDS - 1.0, usage)
    status, _, body = await srv._remote_run("state", {"with_usage": True}, "fixture")
    assert status == 200 and calls == ["", ""]
    # A different query string is its own entry, and the sealed `usage`
    # read is fresh every time.
    await srv._remote_run(
        "state", {"with_usage": True, "usage_query": "window=day"}, "fixture")
    assert calls == ["", "", "window=day"]
    await srv._remote_run("usage", {}, "fixture")
    await srv._remote_run("usage", {}, "fixture")
    assert calls == ["", "", "window=day", "", ""]


@pytest.mark.asyncio
async def test_the_card_read_carries_a_scouts_report_beside_the_plan(server, tmp_path):
    """A scout is an investigation ending in a report; until 21 Sep 2026
    the phone was handed `report_path` alone and drew it as a line of
    text. The on-open `card` read now carries `report` — `_card_plan`'s
    twin: the same containment (a Markdown file inside the card's own
    project), `available` stated, `reason` in words."""
    srv, daemon = server
    root = tmp_path / "proj"
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "r.md").write_text("# Found\n\nTwo things.\n")
    outside = tmp_path / "elsewhere.md"
    outside.write_text("# not yours\n")
    inside = srv._card_report({"root": str(root), "report_path": str(root / "docs" / "r.md")})
    assert inside["available"] is True and inside["text"].startswith("# Found")
    assert inside["path"] == str(root / "docs" / "r.md") and inside["reason"] == ""
    none = srv._card_report({"root": str(root), "report_path": ""})
    assert none["available"] is False and none["reason"] == "this card has no report"
    away = srv._card_report({"root": str(root), "report_path": str(outside)})
    assert away["available"] is False and "report" in away["reason"] and "plan" not in away["reason"]
    gone = srv._card_report({"root": str(root), "report_path": str(root / "docs" / "missing.md")})
    assert gone["available"] is False and gone["text"] == ""
    # It rides the same read as the plan.
    from dark_army_daemon.board import BoardStore
    store = BoardStore(tmp_path / "board.db")
    store.connect()
    daemon._board = store
    try:
        card, detail = store.create({
            "title": "Scout: why X", "project": "bob", "root": str(root),
            "prompt": "look into it", "tool": "claude", "kind": "scout"})
        assert card is not None, detail
        store.update(card["id"], {"column_name": "in_progress",
                                  "session_id": "s1"}, bump=False)
        attached, detail = store.attach_report(
            card["id"], str(root / "docs" / "r.md"), "s1")
        assert attached is not None, detail
        read = srv._card_collect(card["id"], with_plan=True)
        assert read["report"] == inside
        assert read["plan"]["available"] is False
        assert "report" not in srv._card_collect(card["id"], with_plan=False)
    finally:
        store.close()


# --- board_refine_batch: several Prep cards, one planning session -------------


@pytest.mark.asyncio
async def test_board_refine_batch_routes_card_ids_split_and_deduped(board_server):
    srv, daemon, store = board_server
    seen = {}

    async def fake_refine_cards(card_ids):
        seen["ids"] = card_ids
        return True, "started"

    daemon.refine_cards = fake_refine_cards
    status, body = await board_fetch(
        "/api/action",
        json.dumps({"action": "board_refine_batch",
                    "card_ids": " a1, b2 ,,a1,c3 "}).encode(),
        {"X-Bob-Token": srv.token})
    assert status == 200, body
    assert json.loads(body) == {"ok": True, "detail": "started"}
    assert seen["ids"] == ["a1", "b2", "c3"]


@pytest.mark.asyncio
async def test_board_refine_batch_refusal_is_409_in_the_daemons_words(board_server):
    srv, daemon, store = board_server

    async def refuse(card_ids):
        return False, "tick at least two Prep cards to refine them together"

    daemon.refine_cards = refuse
    status, body = await board_fetch(
        "/api/action",
        json.dumps({"action": "board_refine_batch", "card_ids": "a1"}).encode(),
        {"X-Bob-Token": srv.token})
    assert status == 409
    assert json.loads(body)["detail"].startswith("tick at least two")


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [None, "", " , ,", ["a1", "b2"], 7])
async def test_board_refine_batch_with_no_usable_card_ids_is_400(board_server, value):
    srv, daemon, store = board_server

    async def never(card_ids):
        raise AssertionError("a malformed batch reached the daemon")

    daemon.refine_cards = never
    payload = {"action": "board_refine_batch"}
    if value is not None:
        payload["card_ids"] = value
    status, body = await board_fetch(
        "/api/action", json.dumps(payload).encode(), {"X-Bob-Token": srv.token})
    assert status == 400, body
    assert json.loads(body)["error"] == "no card_ids"


def test_board_refine_batch_is_on_both_phone_tuples():
    """The phone's batch refine (25 Sep 2026): the verb is chosen on both
    phone tuples — each its own line, once — and in `_LAN_BOARD`, the away
    list still never exceeding the home one; `card_ids` is envelope, never a
    card field."""
    assert "board_refine_batch" in ApiServer.BOARD_ACTIONS
    assert ApiServer.LAN_ACTIONS.count("board_refine_batch") == 1
    assert ApiServer.REMOTE_ACTIONS.count("board_refine_batch") == 1
    assert "board_refine_batch" in ApiServer._LAN_BOARD
    assert set(ApiServer.REMOTE_ACTIONS) <= set(ApiServer.LAN_ACTIONS)
    assert "card_ids" in ApiServer._BOARD_ENVELOPE
    assert "card_ids" not in ApiServer._BOARD_FIELDS
    assert "batch_id" not in ApiServer._BOARD_FIELDS
    assert "batch_rank" not in ApiServer._BOARD_FIELDS


# --- board_start_batch: several Backlog cards, one implementation session -----


@pytest.mark.asyncio
async def test_board_start_batch_routes_card_ids_to_start_cards(board_server):
    srv, daemon, store = board_server
    seen = {}

    async def fake_start_cards(card_ids):
        seen["ids"] = card_ids
        return True, "card 1 started · 2 waiting"

    daemon.start_cards = fake_start_cards
    status, body = await board_fetch(
        "/api/action",
        json.dumps({"action": "board_start_batch",
                    "card_ids": " a1, b2 ,,a1,c3 "}).encode(),
        {"X-Bob-Token": srv.token})
    assert status == 200, body
    assert json.loads(body) == {"ok": True,
                                "detail": "card 1 started · 2 waiting"}
    assert seen["ids"] == ["a1", "b2", "c3"]


@pytest.mark.asyncio
async def test_board_start_batch_refusal_is_409_in_the_daemons_words(board_server):
    srv, daemon, store = board_server

    async def refuse(card_ids):
        return False, "tick at least two planned Backlog cards to start them together"

    daemon.start_cards = refuse
    status, body = await board_fetch(
        "/api/action",
        json.dumps({"action": "board_start_batch", "card_ids": "a1"}).encode(),
        {"X-Bob-Token": srv.token})
    assert status == 409
    assert json.loads(body)["detail"].startswith("tick at least two planned")


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [None, "", " , ,", ["a1", "b2"], 7])
async def test_board_start_batch_with_no_usable_card_ids_is_400(board_server, value):
    srv, daemon, store = board_server

    async def never(card_ids):
        raise AssertionError("a malformed batch reached the daemon")

    daemon.start_cards = never
    payload = {"action": "board_start_batch"}
    if value is not None:
        payload["card_ids"] = value
    status, body = await board_fetch(
        "/api/action", json.dumps(payload).encode(), {"X-Bob-Token": srv.token})
    assert status == 400, body
    assert json.loads(body)["error"] == "no card_ids"


def test_board_start_batch_is_on_both_phone_tuples():
    """The phone's batch Start (25 Sep 2026): the verb is chosen on both
    phone tuples — each its own line, once — and in `_LAN_BOARD`, the away
    list still never exceeding the home one."""
    assert "board_start_batch" in ApiServer.BOARD_ACTIONS
    assert ApiServer.LAN_ACTIONS.count("board_start_batch") == 1
    assert ApiServer.REMOTE_ACTIONS.count("board_start_batch") == 1
    assert "board_start_batch" in ApiServer._LAN_BOARD
    assert set(ApiServer.REMOTE_ACTIONS) <= set(ApiServer.LAN_ACTIONS)


@pytest.mark.asyncio
async def test_a_sealed_home_batch_press_reaches_refine_cards_with_the_split_list(
        board_server):
    """Success criterion — "press the batch button and confirm it; exactly
    one planning terminal opens": the phone's press at home reaches the one
    daemon verb that spawns once, with the list split, trimmed and deduped
    exactly as the loopback press is."""
    srv, daemon, store = board_server
    seen = []

    async def fake_refine_cards(card_ids):
        seen.append(card_ids)
        return True, ""

    daemon.refine_cards = fake_refine_cards
    status, _, body = await srv._sealed_run(
        "action", {"action": "board_refine_batch", "card_ids": "a1, b2,a1"},
        "fixture", actions=srv.LAN_ACTIONS, check_lease=False, record=False)
    assert status == 200, body
    assert json.loads(body)["ok"] is True
    assert seen == [["a1", "b2"]]


@pytest.mark.asyncio
async def test_a_sealed_away_batch_press_needs_the_lease_and_is_recorded(
        board_server, monkeypatch):
    """Away the batch rides the lease like every write: a lapsed window is
    a 403 in the relay's words and the verb is never called; a live one
    reaches it and the press is listed on the Mac as done remotely."""
    from dark_army_daemon import relay
    srv, daemon, store = board_server
    seen = []
    recorded = []

    async def fake_refine_cards(card_ids):
        seen.append(card_ids)
        return True, ""

    daemon.refine_cards = fake_refine_cards
    monkeypatch.setattr(daemon, "record_remote_action",
                        lambda device, action, ok: recorded.append((action, ok)))
    payload = {"action": "board_refine_batch", "card_ids": "a1,b2"}
    monkeypatch.setattr(relay, "lease_valid", lambda _device: False)
    status, _, body = await srv._sealed_run(
        "action", dict(payload), "fixture", actions=srv.REMOTE_ACTIONS,
        check_lease=True, record=True)
    assert status == 403
    assert json.loads(body)["detail"] == relay.LEASE_REFUSAL
    assert seen == [] and recorded == []
    monkeypatch.setattr(relay, "lease_valid", lambda _device: True)
    status, _, body = await srv._sealed_run(
        "action", dict(payload), "fixture", actions=srv.REMOTE_ACTIONS,
        check_lease=True, record=True)
    assert status == 200, body
    assert seen == [["a1", "b2"]]
    assert recorded == [("board_refine_batch", True)]


@pytest.mark.asyncio
@pytest.mark.parametrize("door", ["home", "relay"])
async def test_a_batch_press_with_no_card_ids_is_400_on_the_sealed_door(
        board_server, door):
    srv, daemon, store = board_server

    async def never(card_ids):
        raise AssertionError("a malformed batch reached the daemon")

    daemon.refine_cards = never
    actions = srv.LAN_ACTIONS if door == "home" else srv.REMOTE_ACTIONS
    for payload in ({"action": "board_refine_batch"},
                    {"action": "board_refine_batch", "card_ids": " , "}):
        status, _, body = await srv._sealed_run(
            "action", payload, "fixture", actions=actions,
            check_lease=False, record=False)
        assert status == 400, body
        assert json.loads(body)["error"] == "no card_ids"


@pytest.mark.asyncio
async def test_the_phones_sealed_state_carries_the_dependency_links_whole(
        board_server):
    """The phone's ✕ and Add… write the whole list back, so the list it
    reads must be the store's own: `blocked_by` rides the sealed `state`
    answer unchanged, beside the resolved `dependencies` — a field filter
    that dropped it would turn the next edit into an overwrite."""
    srv, daemon, store = board_server
    a, _ = store.create({"title": "a", "project": "bob", "column_name": "backlog"})
    b, _ = store.create({"title": "b", "project": "bob", "column_name": "backlog"})
    c, _ = store.create({"title": "c", "project": "bob", "column_name": "backlog"})
    store.update(a["id"], {"blocked_by": [b["id"], c["id"]]})
    daemon._refresh_board_state()
    for payload in ({}, {"done": "review"}):
        status, _, body = await srv._sealed_run(
            "state", payload, "fixture", actions=srv.LAN_ACTIONS,
            check_lease=False, record=False)
        assert status == 200
        cards = {x["id"]: x for x in json.loads(body)["board"]["cards"]}
        assert cards[a["id"]]["blocked_by"] == b["id"] + "\n" + c["id"]
        assert [e["id"] for e in cards[a["id"]]["dependencies"]] == [
            b["id"], c["id"]]
        assert cards[b["id"]]["dependents_line"] == 'Unblocks: "a"'
        assert json.loads(body)["board"]["dependencies_supported"] is True
