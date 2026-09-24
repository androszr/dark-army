# host/tests/test_unchanged_state.py
"""The Mac answers "nothing changed" in one line.

A phone's sealed `state` read may quote the digest of the picture it already
holds; when the Mac's own picture still fingerprints the same, the answer is a
short `{"unchanged": true, ...}` line instead of the whole fleet and board.
One seam (`ApiServer._state_answer`), both doors — `/api/home` at home and
`_remote_run` away.

The fixtures are `test_lan_access.py`'s, imported rather than copied so the two
files cannot drift about what a sealed home frame looks like.
"""

from __future__ import annotations

import json

import pytest

from dark_army_daemon import api_server as api_mod
from dark_army_daemon.api_server import ApiServer

from .test_lan_access import (  # noqa: F401  (fixtures are used by name)
    _home_post,
    _no_fleet_snapshot,
    _pair_plain,
    fetch,
    ledger,
    machine,
    ports,
    server,
    token_path,
)


async def _paired(server):
    """LAN on, one paired phone. Returns `(srv, daemon, lan_port, key)`."""
    srv, daemon, (_loop_port, lan_port) = server
    daemon.lan_access_enabled = True
    await srv.start_lan()
    paired = await _pair_plain(lan_port)
    return srv, daemon, lan_port, paired["key"]


# --- the home door ------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_state_read_without_a_digest_carries_one(server):
    _srv, _daemon, lan_port, key = await _paired(server)
    status, body = await _home_post(lan_port, key, "state", {}, 1)
    assert status == 200
    payload = json.loads(body)
    assert "agents" in payload and "board" in payload
    digest = payload["state_digest"]
    assert len(digest) == 64
    assert all(c in "0123456789abcdef" for c in digest)


@pytest.mark.asyncio
async def test_a_quiet_second_poll_is_answered_short(server):
    srv, _daemon, lan_port, key = await _paired(server)
    _status, body = await _home_post(lan_port, key, "state", {}, 1)
    digest = json.loads(body)["state_digest"]

    status, second = await _home_post(
        lan_port, key, "state", {"digest": digest}, 2)
    assert status == 200
    payload = json.loads(second)
    assert payload["unchanged"] is True
    assert payload["state_digest"] == digest
    # Not an omission: `unchanged` is a present key, because the phone's
    # `Snapshot` would decode an omission-marked body into a blank fleet.
    assert "agents" not in payload
    assert "board" not in payload
    assert "devices" not in payload
    # The regression test for a field that moves because the poll happened.
    assert len(second.encode()) < 200
    # An empty test fleet's full body is under a kilobyte; on a real Mac it
    # is ~80 KB. The absolute cap above is the load-bearing half.
    assert len(second.encode()) < len(body.encode())
    assert isinstance(srv, ApiServer)


@pytest.mark.asyncio
async def test_a_change_brings_the_whole_picture_back(server):
    srv, _daemon, lan_port, key = await _paired(server)
    _status, body = await _home_post(lan_port, key, "state", {}, 1)
    digest = json.loads(body)["state_digest"]

    srv.on_agents_change({"running": [{"session_id": "s1", "title": "one"}]})

    status, second = await _home_post(
        lan_port, key, "state", {"digest": digest}, 2)
    assert status == 200
    payload = json.loads(second)
    assert "unchanged" not in payload
    assert "agents" in payload
    assert payload["state_digest"] != digest


@pytest.mark.asyncio
async def test_a_board_change_brings_the_whole_picture_back(server):
    srv, _daemon, lan_port, key = await _paired(server)
    _status, body = await _home_post(lan_port, key, "state", {}, 1)
    digest = json.loads(body)["state_digest"]

    srv.on_board_change({"available": True, "cards": [{"id": "c1"}]})

    status, second = await _home_post(
        lan_port, key, "state", {"digest": digest}, 2)
    payload = json.loads(second)
    assert "unchanged" not in payload
    assert payload["state_digest"] != digest


@pytest.mark.parametrize("bad", [
    "",
    "A" * 64,
    "a" * 63,
    "z" * 64,
    17,
    ["a" * 64],
    None,
    {"digest": "a" * 64},
])
@pytest.mark.asyncio
async def test_a_digest_that_is_not_one_gets_the_whole_picture(server, bad):
    _srv, _daemon, lan_port, key = await _paired(server)
    status, body = await _home_post(
        lan_port, key, "state", {"digest": bad}, 1)
    # Never a 400: a bad digest is a cache validator to ignore, not a
    # destructive write to refuse.
    assert status == 200
    payload = json.loads(body)
    assert "unchanged" not in payload
    assert "agents" in payload


# --- the away door ------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_away_door_answers_short_too(server):
    """The one `last_frame_at` / `relay_health` would break."""
    srv, _daemon, _lan_port, _key = await _paired(server)
    status, _ctype, body = await srv._remote_run("state", {}, "dev-1")
    assert status == 200
    digest = json.loads(body)["state_digest"]

    status, _ctype, second = await srv._remote_run(
        "state", {"digest": digest}, "dev-1")
    assert status == 200
    payload = json.loads(second)
    assert payload["unchanged"] is True
    assert payload["state_digest"] == digest


# --- the strip sets -----------------------------------------------------------


def test_poll_echo_fields_are_the_three_named_ones():
    assert api_mod._POLL_ECHO_FIELDS == frozenset(
        {"last_frame_at", "last_ok_at", "failing_for"})
    assert not (api_mod._POLL_ECHO_FIELDS & api_mod._CLOCK_FIELDS)


def test_the_link_timing_echo_rides_the_envelope_not_the_state():
    """`timing` — the Mac's legs on every sealed reply — is neither a poll
    echo field nor a clock field, because it never enters the state body:
    it sits beside `body` on the reply envelope (`_lan_home`,
    `relay_client._execute`), so the digest never sees it."""
    assert "timing" not in api_mod._POLL_ECHO_FIELDS
    assert "timing" not in api_mod._CLOCK_FIELDS
    state = {"agents": {}, "devices": {"devices": []}}
    with_echo = dict(state)
    assert api_mod._state_digest(with_echo) == api_mod._state_digest(state)
    assert "timing" not in json.dumps(api_mod._news(state))


def test_the_broadcast_path_never_strips_a_poll_echo_field():
    state = {"devices": {"devices": [{"id": "d", "last_frame_at": 12.0}]},
             "relay_health": {"last_ok_at": 3.0, "failing_for": 1.0},
             "generated_at": 9.0}
    news = api_mod._news(state)
    assert news["devices"]["devices"][0]["last_frame_at"] == 12.0
    assert news["relay_health"] == {"last_ok_at": 3.0, "failing_for": 1.0}
    assert "generated_at" not in news
    # The phone's digest strips all three; the panel's news strips none.
    stripped = api_mod._news(
        state, api_mod._CLOCK_FIELDS | api_mod._POLL_ECHO_FIELDS)
    assert stripped["devices"]["devices"] == [{"id": "d"}]
    assert stripped["relay_health"] == {}


def test_a_poll_echo_field_moving_does_not_move_the_digest():
    before = {"agents": {}, "devices": {"devices": [
        {"id": "d", "last_frame_at": 1.0}]}}
    after = {"agents": {}, "devices": {"devices": [
        {"id": "d", "last_frame_at": 2.0}]}}
    assert api_mod._state_digest(before) == api_mod._state_digest(after)
    moved = {"agents": {"running": [1]}, "devices": after["devices"]}
    assert api_mod._state_digest(moved) != api_mod._state_digest(before)


def test_valid_state_digest_is_a_shape_check_and_never_raises():
    good = "a" * 64
    assert api_mod._valid_state_digest(good) == good
    for bad in ("", "A" * 64, "a" * 63, "g" * 64, 5, None, [], {}):
        assert api_mod._valid_state_digest(bad) == ""


# --- loopback and SSE purity --------------------------------------------------


@pytest.mark.asyncio
async def test_the_loopback_state_carries_no_digest(server):
    srv, _daemon, (loop_port, _lan_port) = server
    status, body = await fetch("/api/state", port=loop_port)
    assert status == 200
    assert "state_digest" not in json.loads(body)
    assert "state_digest" not in srv.state()


def test_state_is_on_neither_action_tuple():
    assert "state" not in ApiServer.LAN_ACTIONS
    assert "state" not in ApiServer.REMOTE_ACTIONS
    # Away may never exceed home — the standing pin, restated here because
    # this change touches the shared run.
    assert set(ApiServer.REMOTE_ACTIONS) <= set(ApiServer.LAN_ACTIONS)


# --- the answer is partitioned by section -------------------------------------
#
# Every full answer carries `section_digests`; a phone quoting them back as
# `sections` gets only the sections that moved, and the ones it may keep are
# named in `sections_unchanged` — a present list, never an omission.

_ADDED_KEYS = {"state_digest", "section_digests", "sections_unchanged"}


def _is_hex64(value) -> bool:
    return (isinstance(value, str) and len(value) == 64
            and all(c in "0123456789abcdef" for c in value))


@pytest.mark.asyncio
async def test_a_full_answer_names_a_digest_per_section(server):
    _srv, _daemon, lan_port, key = await _paired(server)
    status, body = await _home_post(lan_port, key, "state", {}, 1)
    assert status == 200
    payload = json.loads(body)
    digests = payload["section_digests"]
    present = [k for k in api_mod._OMITTABLE_SECTIONS if k in payload]
    assert present, "the fixture state has sections"
    assert set(digests) == set(present)
    assert all(_is_hex64(v) for v in digests.values())
    assert "sections_unchanged" not in payload
    # None of the three added keys is its own input: recomputing over the
    # answer without them reproduces both the whole and the per-section
    # digests.
    base = {k: v for k, v in payload.items() if k not in _ADDED_KEYS}
    whole, sections = api_mod._state_digests(base)
    assert whole == payload["state_digest"]
    assert sections == digests


@pytest.mark.asyncio
async def test_only_the_sections_that_moved_come_back(server):
    """The success criterion: a check-in during which only the agents moved
    comes back without the board, and one during which nothing moved is
    still the short line."""
    srv, _daemon, lan_port, key = await _paired(server)
    srv.on_board_change({"available": True, "cards": [
        {"id": f"c{i}", "title": f"card {i}", "description": "d" * 400}
        for i in range(10)]})
    _status, first_raw = await _home_post(lan_port, key, "state", {}, 1)
    first = json.loads(first_raw)

    srv.on_agents_change({"running": [{"session_id": "s1", "title": "one"}]})

    status, second_raw = await _home_post(lan_port, key, "state", {
        "digest": first["state_digest"],
        "sections": first["section_digests"]}, 2)
    assert status == 200
    second = json.loads(second_raw)
    assert "unchanged" not in second
    assert "agents" in second
    assert "board" not in second
    assert "board" in second["sections_unchanged"]
    assert "agents" not in second["sections_unchanged"]
    assert second["section_digests"]["board"] == first["section_digests"]["board"]
    assert second["section_digests"]["agents"] != first["section_digests"]["agents"]
    # Every section left out is named; nothing is omitted silently.
    for k in api_mod._OMITTABLE_SECTIONS:
        if k in first:
            assert (k in second) != (k in second["sections_unchanged"]), k
    assert len(second_raw) < len(first_raw) - len(json.dumps(first["board"]))

    status, third_raw = await _home_post(lan_port, key, "state", {
        "digest": second["state_digest"],
        "sections": second["section_digests"]}, 3)
    third = json.loads(third_raw)
    assert third["unchanged"] is True
    assert third["state_digest"] == second["state_digest"]
    assert set(third) == {"unchanged", "state_digest", "generated_at"}
    assert len(third_raw.encode()) < 200


@pytest.mark.asyncio
async def test_a_board_change_brings_the_board_and_leaves_the_fleet(server):
    srv, _daemon, lan_port, key = await _paired(server)
    srv.on_agents_change({"running": [{"session_id": "s1", "title": "one"}]})
    _status, first_raw = await _home_post(lan_port, key, "state", {}, 1)
    first = json.loads(first_raw)

    srv.on_board_change({"available": True, "cards": [{"id": "c1"}]})

    _status, second_raw = await _home_post(lan_port, key, "state", {
        "digest": first["state_digest"],
        "sections": first["section_digests"]}, 2)
    second = json.loads(second_raw)
    assert "board" in second
    assert "agents" not in second
    assert "agents" in second["sections_unchanged"]
    assert "board" not in second["sections_unchanged"]
    assert second["section_digests"]["agents"] == first["section_digests"]["agents"]


@pytest.mark.asyncio
async def test_the_board_share_leaves_the_delta_under_a_third(server):
    """The measurement: on a fifty-card board the fleet-only delta is at most
    35 % of the whole picture."""
    srv, _daemon, lan_port, key = await _paired(server)
    srv.on_board_change({"available": True, "cards": [
        {"id": f"c{i}", "title": f"card {i}", "column": "backlog",
         "description": "x" * 2000}
        for i in range(50)]})
    srv.on_agents_change({"running": [{"session_id": "s0", "title": "zero"}]})
    _status, full_raw = await _home_post(lan_port, key, "state", {}, 1)
    full = json.loads(full_raw)
    assert len(json.dumps(full["board"])) >= 0.70 * len(full_raw)

    srv.on_agents_change({"running": [{"session_id": "s1", "title": "one"}]})

    _status, delta_raw = await _home_post(lan_port, key, "state", {
        "digest": full["state_digest"],
        "sections": full["section_digests"]}, 2)
    delta = json.loads(delta_raw)
    assert "board" not in delta and "board" in delta["sections_unchanged"]
    assert len(delta_raw) <= 0.35 * len(full_raw), (len(delta_raw), len(full_raw))


@pytest.mark.asyncio
async def test_an_older_phone_quoting_no_sections_gets_the_whole_picture(server):
    srv, _daemon, lan_port, key = await _paired(server)
    _status, first_raw = await _home_post(lan_port, key, "state", {}, 1)
    first = json.loads(first_raw)

    srv.on_agents_change({"running": [{"session_id": "s1", "title": "one"}]})

    _status, second_raw = await _home_post(
        lan_port, key, "state", {"digest": first["state_digest"]}, 2)
    second = json.loads(second_raw)
    assert "sections_unchanged" not in second
    for k in api_mod._OMITTABLE_SECTIONS:
        if k in srv.state():
            assert k in second, k
    today = set(srv.state()) | {"state_digest"}
    assert set(second) == today | {"section_digests"}


@pytest.mark.parametrize("sections", [
    "not a dict",
    ["board"],
    17,
    None,
    {"board": "A" * 64},
    {"board": "a" * 63},
    {"board": 5},
    {"not_a_section": "a" * 64},
    {"board": "a" * 64},            # well formed, stale
])
@pytest.mark.asyncio
async def test_a_section_quote_that_is_wrong_or_unknown_is_ignored(server, sections):
    srv, _daemon, lan_port, key = await _paired(server)
    _status, first_raw = await _home_post(lan_port, key, "state", {}, 1)
    first = json.loads(first_raw)
    srv.on_agents_change({"running": [{"session_id": "s1", "title": "one"}]})
    status, body = await _home_post(lan_port, key, "state", {
        "digest": first["state_digest"], "sections": sections}, 2)
    # Never a 400: a section quote is a cache validator to ignore.
    assert status == 200
    payload = json.loads(body)
    assert "board" in payload and "agents" in payload
    assert "sections_unchanged" not in payload
    assert "not_a_section" not in payload


@pytest.mark.asyncio
async def test_the_away_door_answers_deltas_too(server):
    srv, _daemon, _lan_port, _key = await _paired(server)
    status, _ctype, first_raw = await srv._remote_run("state", {}, "dev-1")
    assert status == 200
    first = json.loads(first_raw)

    srv.on_agents_change({"running": [{"session_id": "s1", "title": "one"}]})

    status, _ctype, second_raw = await srv._remote_run("state", {
        "digest": first["state_digest"],
        "sections": first["section_digests"]}, "dev-1")
    assert status == 200
    second = json.loads(second_raw)
    assert "agents" in second
    assert "board" not in second
    assert "board" in second["sections_unchanged"]


@pytest.mark.asyncio
async def test_the_usage_fold_rides_a_delta_answer(server):
    srv, _daemon, _lan_port, _key = await _paired(server)
    _status, _ctype, first_raw = await srv._remote_run("state", {}, "dev-1")
    first = json.loads(first_raw)
    srv.on_agents_change({"running": [{"session_id": "s1", "title": "one"}]})

    _status, _ctype, bare_raw = await srv._remote_run("state", {
        "digest": first["state_digest"],
        "sections": first["section_digests"]}, "dev-1")
    status, _ctype, folded_raw = await srv._remote_run("state", {
        "digest": first["state_digest"],
        "sections": first["section_digests"],
        "with_usage": True}, "dev-1")
    assert status == 200
    bare, folded = json.loads(bare_raw), json.loads(folded_raw)
    assert "usage" in folded
    assert "board" in folded["sections_unchanged"]
    # The bars are folded in after hashing: neither digest moves for them.
    assert folded["state_digest"] == bare["state_digest"]
    assert folded["section_digests"] == bare["section_digests"]
    assert folded["sections_unchanged"] == bare["sections_unchanged"]


@pytest.mark.asyncio
async def test_the_review_board_digest_is_the_one_sent(server):
    from dark_army_daemon import board as board_mod
    srv, _daemon, lan_port, key = await _paired(server)
    whole_board = {"available": True, "cards": [
        {"id": "keep", "column": "backlog"},
        {"id": "gone", "column": "done", "done_preview": True}]}
    srv.on_board_change(whole_board)
    _status, body = await _home_post(
        lan_port, key, "state", {"done": "review"}, 1)
    payload = json.loads(body)
    assert [c["id"] for c in payload["board"]["cards"]] == ["keep"]

    def section_digest(section):
        stripped = api_mod._news(
            section, api_mod._CLOCK_FIELDS | api_mod._POLL_ECHO_FIELDS)
        return api_mod.hashlib.sha256(
            json.dumps(stripped, sort_keys=True).encode()).hexdigest()

    sent = payload["section_digests"]["board"]
    assert sent == section_digest(board_mod.review_only(whole_board))
    assert sent != section_digest(whole_board)


@pytest.mark.asyncio
async def test_the_loopback_read_and_sse_carry_no_section_digests(server):
    import asyncio
    srv, _daemon, (loop_port, _lan_port) = server
    status, body = await fetch("/api/state", port=loop_port)
    assert status == 200
    loopback = json.loads(body)
    assert "section_digests" not in loopback
    assert "sections_unchanged" not in loopback
    assert "section_digests" not in srv.state()

    # One SSE frame, whole and slim, on the registered-queue seam.
    for slim in (False, True):
        queue: asyncio.Queue = asyncio.Queue(
            maxsize=api_mod.SSE_CLIENT_QUEUE_MAX)
        srv._clients[queue] = slim
        srv._counts = {"working": 7 + slim, "idle": 0, "attention": 0,
                       "subagents": 0}
        srv._flush()
        frame = json.loads(queue.get_nowait())
        del srv._clients[queue]
        assert "section_digests" not in frame
        assert "sections_unchanged" not in frame
        assert "state_digest" not in frame


def test_valid_section_quotes_is_a_shape_check_and_never_raises():
    good = "a" * 64
    assert api_mod._valid_section_quotes({"board": good, "agents": good}) == \
        {"board": good, "agents": good}
    assert api_mod._valid_section_quotes(
        {"board": good, "nope": good, "agents": "A" * 64}) == {"board": good}
    for bad in (None, "", 5, [], ["board"], {"board": None}, {1: good},
                {"board": ["a" * 64]}):
        assert api_mod._valid_section_quotes(bad) == {}
    # A section name is only ever one of the partition's.
    quoted = {k: good for k in api_mod._OMITTABLE_SECTIONS}
    quoted["state_digest"] = good
    assert set(api_mod._valid_section_quotes(quoted)) == \
        set(api_mod._OMITTABLE_SECTIONS)


# --- a row's clock stamp is news only when its source moves ------------------


def _stubs_by_id(daemon):
    return {st["session_id"]: st for st in daemon._collect_agent_stubs()}


def test_a_stampless_row_carries_no_quiet_since_and_makes_no_news_across_frames(monkeypatch):
    """`quiet_since` is the moment the row went quiet, stamped from the
    row's own source and 0.0 where there is none — a reconciler row without
    `started_at`, a Grok row without `opened_at`. Stamping `now - idle`
    wrote `now` on those rows every frame, `_news` fired every frame, and
    SSE coalescing and the phone's conditional read never settled. The
    stamp is 0.0 on those rows whether or not the key is stripped from
    news; the strip is the next test's business."""
    from dark_army_daemon.agents_poll import AgentRecord
    from dark_army_daemon.daemon import BobDaemon
    from dark_army_daemon.grok_roster import GrokRecord
    d = BobDaemon()
    d._agent_records = {"bg1": AgentRecord("bg1", kind="background",
                                           activity="blocked", name="forgotten")}
    d._grok_records = {"g1": GrokRecord(session_id="g1", pid=None, cwd="",
                                        opened_at=None)}
    clock = [1_000.0]
    monkeypatch.setattr("dark_army_daemon.daemon.time.time", lambda: clock[0])
    first = _stubs_by_id(d)
    assert first["bg1"]["quiet_since"] == 0.0 and first["g1"]["quiet_since"] == 0.0
    clock[0] += 7.0
    second = _stubs_by_id(d)
    assert api_mod._news(first) == api_mod._news(second)
    assert first["bg1"]["quiet_since"] == second["bg1"]["quiet_since"] == 0.0


def test_a_hook_rows_quiet_since_is_a_clock_field_so_a_same_tool_event_is_no_news(monkeypatch):
    """A hook row stamps `quiet_since` from `last_event`, which moves on
    every hook event. So it sits in `_CLOCK_FIELDS` beside `last_event`
    (it is that stamp rounded): two stubs differing only in `last_event`
    — a working agent's second `Read` after its first — make no news, so
    a working row costs no SSE frame per tool call and the phone's digest
    settles. The row's move to waiting is still news, through `state`."""
    from dark_army_daemon.daemon import BobDaemon
    d = BobDaemon()
    d._session_states["s1"] = {"state": "working", "last_event": 1_000.0,
                               "tool_name": "Read"}
    monkeypatch.setattr("dark_army_daemon.daemon.time.time", lambda: 1_010.0)
    first = _stubs_by_id(d)
    d._session_states["s1"]["last_event"] = 1_005.0
    second = _stubs_by_id(d)
    assert first["s1"]["quiet_since"] == 1_000.0
    assert second["s1"]["quiet_since"] == 1_005.0
    assert "quiet_since" in api_mod._CLOCK_FIELDS
    assert api_mod._news(first) == api_mod._news(second)
    assert api_mod._state_digest({"agents": first}) == \
        api_mod._state_digest({"agents": second})
    # The full answer still carries the stamp: stripped from the digest and
    # the news compare only, never from the body.
    assert "quiet_since" in json.dumps(second)
    # And a change of state on the same row is news.
    d._session_states["s1"]["state"] = "idle"
    third = _stubs_by_id(d)
    assert api_mod._news(second) != api_mod._news(third)


def test_each_source_stamps_quiet_since_from_its_own_clock(monkeypatch):
    from dark_army_daemon.agents_poll import AgentRecord
    from dark_army_daemon.daemon import BobDaemon
    from dark_army_daemon.grok_roster import GrokRecord
    d = BobDaemon()
    d._session_states["s1"] = {"state": "idle", "last_event": 1_000.4}
    d._agent_records = {"bg1": AgentRecord("bg1", kind="background",
                                           activity="busy", started_at=900.0)}
    d._grok_records = {"g1": GrokRecord(session_id="g1", pid=None, cwd="",
                                        opened_at=800.6)}
    monkeypatch.setattr("dark_army_daemon.daemon.time.time", lambda: 1_090.0)
    stubs = _stubs_by_id(d)
    assert stubs["s1"]["quiet_since"] == 1_000.0
    assert stubs["bg1"]["quiet_since"] == 900.0
    assert stubs["g1"]["quiet_since"] == 801.0
    # The stamp holds while the age grows.
    monkeypatch.setattr("dark_army_daemon.daemon.time.time", lambda: 1_190.0)
    again = _stubs_by_id(d)
    assert {k: v["quiet_since"] for k, v in again.items()} == \
        {k: v["quiet_since"] for k, v in stubs.items()}
    assert again["s1"]["idle_seconds"] == pytest.approx(189.6)
