"""Dark Army's channel: the first thing in this app that talks *to* a session.

Three layers, tested apart: the JSON-RPC the channel server speaks to Claude
Code, the registry the daemon keeps of who is reachable, and the permission
relay that runs between them. The interesting cases are all about *not*
claiming reachability — a session that was not started with the channel is the
ordinary case, not a fault.
"""
import asyncio
import io
import json
from pathlib import Path

import os

import pytest

from dark_army_daemon import channel_server as cs
from dark_army_daemon import codex_rollouts
from dark_army_daemon.daemon import BobDaemon


# ── the wire, without a pty ──────────────────────────────────────────────────

def _run(server, messages):
    """Feed stdio messages in, get the written lines back as dicts."""
    out = io.StringIO()
    server._out = out
    for msg in messages:
        server.handle_stdio_message(msg)
    return [json.loads(line) for line in out.getvalue().splitlines() if line]


def _server(name=cs.CURRENT_NAME, active=True, host=cs.HOST_CLAUDE):
    return cs.ChannelServer(stdin=io.StringIO(), stdout=io.StringIO(),
                            name=name, active=active, host=host)


def test_the_handshake_declares_the_one_key_that_makes_it_a_channel():
    """`experimental['claude/channel']` is the whole declaration — its presence
    is what makes Claude Code register a listener. Without it this is an
    ordinary MCP server whose notifications are dropped in silence."""
    reply = _run(_server(), [{"jsonrpc": "2.0", "id": 1, "method": "initialize",
                              "params": {"protocolVersion": "2025-11-25"}}])[0]
    caps = reply["result"]["capabilities"]["experimental"]
    assert caps["claude/channel"] == {}
    assert caps["claude/channel/permission"] == {}
    # Echoed, never asserted: the client picks the protocol version.
    assert reply["result"]["protocolVersion"] == "2025-11-25"
    assert reply["result"]["serverInfo"]["name"] == "dark-army"
    legacy = _run(_server(name="bob"), [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-11-25"}}])[0]
    assert legacy["result"]["serverInfo"]["name"] == "bob"


def test_a_request_we_do_not_implement_is_still_answered():
    """An unanswered id is a client waiting forever."""
    reply = _run(_server(), [{"jsonrpc": "2.0", "id": 7, "method": "sampling/x"}])[0]
    assert reply["id"] == 7 and reply["error"]["code"] == -32601


def test_a_notification_we_do_not_implement_is_not_answered():
    """No id, no reply — answering a notification is a protocol error."""
    assert _run(_server(), [{"jsonrpc": "2.0", "method": "notifications/cancelled"}]) == []


def test_meta_keys_that_claude_code_would_drop_are_dropped_here():
    """Keys must be bare identifiers; anything else is discarded *silently* by
    the harness, so a hyphen costs an attribute and reports nothing. Filtered on
    our side because the daemon composes these from live data."""
    event = cs.channel_event("hi", {"ok_1": "a", "bad-key": "b", "9lives": "c"})
    assert event["params"]["meta"] == {"ok_1": "a"}
    assert event["method"] == "notifications/claude/channel"


def test_a_verdict_is_only_ever_allow_or_deny():
    assert cs.permission_verdict("abcde", "allow")["params"]["behavior"] == "allow"
    assert cs.permission_verdict("abcde", "nope")["params"]["behavior"] == "deny"
    # Anything unrecognised denies rather than allows: the failure mode of a
    # wrong guess here is running a command nobody approved.
    assert cs.permission_verdict("abcde", "")["params"]["behavior"] == "deny"


def test_a_permission_request_is_relayed_up_and_remembered():
    server = _server()
    sent = []
    server.notify_daemon = lambda msg: sent.append(msg) or True
    server.handle_stdio_message({
        "jsonrpc": "2.0",
        "method": "notifications/claude/channel/permission_request",
        "params": {"request_id": "abcde", "tool_name": "Bash",
                   "description": "Run shell command",
                   "input_preview": '{"command": "rm -rf /tmp/x"}'},
    })
    assert sent[0]["type"] == "channel_permission_request"
    assert sent[0]["tool_name"] == "Bash"
    assert "rm -rf" in sent[0]["input_preview"]
    assert "abcde" in server.pending


def test_the_daemon_can_push_an_event_and_answer_a_prompt():
    server = _server()
    server.secret = "sh4red"          # normally minted by listen()
    server.pending["abcde"] = {}
    out = io.StringIO()
    server._out = out
    server.handle_daemon_message(json.dumps(
        {"type": "event", "content": "5h window reset", "meta": {"kind": "usage"},
         "secret": "sh4red"}))
    server.handle_daemon_message(json.dumps(
        {"type": "permission_verdict", "request_id": "abcde", "behavior": "allow",
         "secret": "sh4red"}))
    lines = [json.loads(l) for l in out.getvalue().splitlines() if l]
    assert lines[0]["params"]["content"] == "5h window reset"
    assert lines[1]["method"] == "notifications/claude/channel/permission"
    # Answered once. The harness drops a second verdict for the same id anyway,
    # but silently, which is a worse thing to be relying on.
    assert "abcde" not in server.pending


# ── the daemon's registry ────────────────────────────────────────────────────

def _daemon():
    return BobDaemon(headless=True)


_NO_NAME = object()


def _attach(daemon, port=51000, pid=4242, is_channel=True, session_id="",
            secret="", host=cs.HOST_CLAUDE, cwd="/tmp", channel=_NO_NAME):
    """`channel` is the registered name the attach carries; left out, the
    message has no `channel` key at all, like a copy from before the window."""
    msg = {"type": "channel_attach", "port": port, "pid": pid, "cwd": cwd,
           "session_id": session_id, "is_channel": is_channel,
           "secret": secret, "host": host}
    if channel is not _NO_NAME:
        msg["channel"] = channel
    return daemon._handle_channel_message(msg)


def test_a_channel_is_resolved_to_its_session_by_pid_at_the_time_of_use():
    """Lazily, and this is the point: a channel starts *with* its session and
    routinely announces itself before the first hook event has told Dark Army the
    session exists."""
    daemon = _daemon()
    _attach(daemon, port=51000, pid=4242)
    assert daemon._channel_session(51000) is None      # session not known yet
    daemon._session_states["s1"] = {"pid": 4242, "last_event": 0}
    assert daemon._channel_session(51000) == "s1"


def test_a_session_without_a_channel_is_simply_unreachable():
    """The ordinary case: a channel only exists if the session was *started*
    with one, and most were not. Not an error, and not a log line per push."""
    daemon = _daemon()
    daemon._session_states["s1"] = {"pid": 4242, "last_event": 0}
    assert daemon._channel_for_session("s1") is None
    assert asyncio.run(daemon.push_channel_event("s1", "hello")) is False


def test_a_channel_that_stops_heartbeating_is_forgotten():
    daemon = _daemon()
    _attach(daemon, port=51000, pid=4242)
    assert 51000 in daemon._live_channels()
    daemon._channels[51000]["last_seen"] -= daemon.CHANNEL_STALE_SECONDS + 1
    assert daemon._live_channels() == {}


def test_reattaching_refreshes_rather_than_duplicates():
    daemon = _daemon()
    _attach(daemon, port=51000, pid=4242)
    daemon._channels[51000]["last_seen"] -= 60
    stale = daemon._channels[51000]["last_seen"]
    _attach(daemon, port=51000, pid=4242)
    assert list(daemon._channels) == [51000]
    assert daemon._channels[51000]["last_seen"] > stale


# ── the relay ────────────────────────────────────────────────────────────────

def _relay(daemon, request_id="abcde", port=51000):
    return daemon._handle_channel_message({
        "type": "channel_permission_request", "port": port,
        "pid": 4242, "request_id": request_id, "tool_name": "Bash",
        "description": "Delete the build directory",
        "input_preview": '{"command": "rm -rf .build"}'})


def test_a_relayed_prompt_names_its_session_and_its_command():
    daemon = _daemon()
    _attach(daemon)
    daemon._session_states["s1"] = {"pid": 4242, "last_event": 0}
    _relay(daemon)
    row = daemon._permission_snapshot()[0]
    assert row["session_id"] == "s1" and row["tool_name"] == "Bash"
    assert "rm -rf .build" in row["input_preview"]
    # The port is Dark Army's plumbing and no surface needs it.
    assert "port" not in row


def test_prompts_are_offered_in_the_order_they_blocked_in():
    daemon = _daemon()
    _attach(daemon)
    _relay(daemon, "aaaaa")
    _relay(daemon, "bbbbb")
    daemon._permission_requests["aaaaa"]["asked_at"] -= 10
    assert [r["request_id"] for r in daemon._permission_snapshot()] == ["aaaaa", "bbbbb"]


def test_the_oldest_prompt_per_session_is_the_one_offered_for_alerting():
    """`_prompts_by_session` feeds the alert policy. Oldest per session because
    that is the prompt the session is actually stopped on — the channel
    serialises them."""
    daemon = _daemon()
    _attach(daemon)
    daemon._session_states["s1"] = {"pid": 4242, "last_event": 0}
    _relay(daemon, "aaaaa")
    _relay(daemon, "bbbbb")
    daemon._permission_requests["aaaaa"]["asked_at"] -= 10
    prompts = daemon._prompts_by_session()
    assert list(prompts) == ["s1"]
    assert prompts["s1"]["request_id"] == "aaaaa"


def test_an_unresolved_prompt_is_not_offered_for_alerting():
    """A channel that announced itself before its session's first hook event
    has a prompt with no session. An alert that cannot name its agent cannot be
    muted, suppressed or revealed, so it waits until the pid joins up — and
    fires then, because nothing has marked its request id as fired."""
    daemon = _daemon()
    _attach(daemon)
    _relay(daemon)                       # no session_states entry for pid 4242
    assert daemon._prompts_by_session() == {}
    daemon._session_states["s1"] = {"pid": 4242, "last_event": 0}
    assert list(daemon._prompts_by_session()) == ["s1"]


def test_answering_a_prompt_that_is_already_gone_says_so():
    """The same prompt is live in the session's own terminal and either answer
    wins. "Too late" is a real outcome, and on a refusal nothing else on screen
    moves — so it is reported rather than swallowed."""
    ok, detail = asyncio.run(_daemon().answer_permission("nope", "allow"))
    assert ok is False and "already been answered" in detail


def test_an_unknown_verdict_is_refused_rather_than_guessed():
    daemon = _daemon()
    _attach(daemon)
    _relay(daemon)
    ok, detail = asyncio.run(daemon.answer_permission("abcde", "maybe"))
    assert ok is False and "maybe" in detail
    # And the prompt is still open — a rejected verdict must not consume it.
    assert "abcde" in daemon._permission_requests


def test_a_verdict_for_a_channel_that_has_gone_clears_the_prompt_anyway():
    """The channel dying means the session died with it. A prompt nobody can
    answer must not sit on the panel forever."""
    daemon = _daemon()
    import socket
    with socket.socket() as probe:       # a port nothing listens on, proven
        probe.bind(("127.0.0.1", 0))
        gone = probe.getsockname()[1]
    _attach(daemon, port=gone)
    _relay(daemon, port=gone)
    ok, detail = asyncio.run(daemon.answer_permission("abcde", "allow"))
    assert ok is False and "no longer listening" in detail
    assert daemon._permission_requests == {}


@pytest.mark.asyncio
async def test_a_verdict_reaches_a_channel_that_is_listening():
    """End to end over a real loopback socket: the daemon's side connects, and
    the channel server's own handler turns the line into the JSON-RPC verdict
    Claude Code is waiting for."""
    server = cs.ChannelServer(stdin=io.StringIO(), stdout=io.StringIO())
    port = server.listen()
    import threading
    threading.Thread(target=server.serve_daemon, daemon=True).start()
    server.pending["abcde"] = {}
    out = io.StringIO()
    server._out = out

    daemon = _daemon()
    _attach(daemon, port=port, secret=server.secret)
    _relay(daemon, port=port)
    ok, detail = await daemon.answer_permission("abcde", "deny")
    assert (ok, detail) == (True, "")

    for _ in range(50):
        if out.getvalue():
            break
        await asyncio.sleep(0.02)
    msg = json.loads(out.getvalue().splitlines()[0])
    assert msg["method"] == "notifications/claude/channel/permission"
    assert msg["params"] == {"request_id": "abcde", "behavior": "deny"}


@pytest.mark.asyncio
async def test_an_event_reaches_a_session_that_has_a_channel():
    server = cs.ChannelServer(stdin=io.StringIO(), stdout=io.StringIO())
    port = server.listen()
    import threading
    threading.Thread(target=server.serve_daemon, daemon=True).start()
    out = io.StringIO()
    server._out = out

    daemon = _daemon()
    _attach(daemon, port=port, secret=server.secret)
    daemon._session_states["s1"] = {"pid": 4242, "last_event": 0}
    assert await daemon.push_channel_event("s1", "Vex finished",
                                           {"kind": "fleet"}) is True
    for _ in range(50):
        if out.getvalue():
            break
        await asyncio.sleep(0.02)
    msg = json.loads(out.getvalue().splitlines()[0])
    assert msg["params"]["content"] == "Vex finished"
    assert msg["params"]["meta"] == {"kind": "fleet"}


# ── replying to a session ────────────────────────────────────────────────────

def test_a_reply_reaches_a_session_that_has_a_channel():
    """The wait that *ends a turn* — "Plan ready. accept?" — is answerable from
    Dark Army, because the session is idle rather than blocked and an arriving event
    wakes a turn. Nothing else in this app can put words in front of an agent."""
    server = cs.ChannelServer(stdin=io.StringIO(), stdout=io.StringIO())
    port = server.listen()
    import threading
    threading.Thread(target=server.serve_daemon, daemon=True).start()
    out = io.StringIO()
    server._out = out

    daemon = _daemon()
    _attach(daemon, port=port, secret=server.secret)
    daemon._session_states["s1"] = {"pid": 4242, "last_event": 0}
    ok, detail = asyncio.run(daemon.reply_to_session("s1", "  accept  "))
    assert (ok, detail) == (True, "")

    import time as _t
    for _ in range(50):
        if out.getvalue():
            break
        _t.sleep(0.02)
    msg = json.loads(out.getvalue().splitlines()[0])
    assert msg["params"]["content"] == "accept"          # trimmed
    # The kind is load-bearing: it is what tells the agent this is its own user
    # typing rather than Dark Army reporting something, and the channel's instructions
    # branch on it. Without it a reply gets acknowledged and the wait continues.
    assert msg["params"]["meta"] == {"kind": "user"}


def test_a_session_without_a_channel_refuses_in_words():
    """The failure that matters is the invisible one: a push at a session with
    no channel is dropped by the harness in silence, so a reply box that says
    nothing is worse than no reply box."""
    daemon = _daemon()
    daemon._session_states["s1"] = {"pid": 4242, "last_event": 0}
    ok, detail = asyncio.run(daemon.reply_to_session("s1", "accept"))
    assert ok is False and "not started with Dark Army's channel" in detail


def test_an_empty_reply_is_refused_before_anything_is_sent():
    daemon = _daemon()
    _attach(daemon)
    daemon._session_states["s1"] = {"pid": 4242, "last_event": 0}
    assert asyncio.run(daemon.reply_to_session("s1", "   "))[0] is False


def test_an_overlong_reply_is_refused():
    """A panel field is for the answers you give without leaving what you were
    doing; a prompt worth more than this is worth the terminal."""
    daemon = _daemon()
    _attach(daemon)
    daemon._session_states["s1"] = {"pid": 4242, "last_event": 0}
    ok, detail = asyncio.run(
        daemon.reply_to_session("s1", "x" * (daemon.MAX_REPLY_CHARS + 1)))
    assert ok is False and str(daemon.MAX_REPLY_CHARS) in detail


def test_the_instructions_tell_the_agent_a_user_reply_is_an_instruction():
    """The first version said events were "informational and one-way, nothing to
    reply to" — true while Dark Army could only announce things, and a bug the moment
    it could carry the user's own words: the agent would acknowledge a reply
    politely and carry on waiting."""
    assert 'kind="user"' in cs.INSTRUCTIONS
    assert "instruction" in cs.INSTRUCTIONS


def test_the_instructions_name_the_source_each_copy_is_registered_under():
    """The harness tags our events with the registered name, so the
    instructions have to say that name — `dark-army` for a new session,
    `bob` for one born before the rename."""
    assert 'source="dark-army"' in cs.instructions_for("dark-army")
    assert 'source="bob"' in cs.instructions_for("bob")
    assert 'source="bob"' not in cs.instructions_for("dark-army")
    assert cs.INSTRUCTIONS == cs.instructions_for(cs.CURRENT_NAME)


# ── is this actually a channel? ──────────────────────────────────────────────

def test_the_flag_is_what_makes_a_server_a_channel():
    """Registration is user-scope, so this server starts in *every* session.
    Only the ones launched with the flag can receive anything — and Claude Code
    hands both kinds the same environment and the same handshake, so the
    parent's command line is the only place the difference is written down."""
    flagged = ("claude --dangerously-load-development-channels server:bob")
    assert cs.is_channel(flagged, "bob") is True
    assert cs.is_channel("claude", "bob") is False
    assert cs.is_channel("claude --resume", "bob") is False
    new = "claude --dangerously-load-development-channels server:dark-army"
    assert cs.is_channel(new, "dark-army") is True
    assert cs.is_channel("claude", "dark-army") is False


def test_a_channel_flag_is_a_channel_only_for_the_name_it_names():
    """Cross-name is False both ways: a `server:bob` launch is the `bob`
    copy's channel, and the `dark-army` copy beside it must not claim it."""
    old = "claude --dangerously-load-development-channels server:bob"
    new = "claude --dangerously-load-development-channels server:dark-army"
    assert cs.is_channel(old, "dark-army") is False
    assert cs.is_channel(new, "bob") is False
    # An exact name, not a substring: `server:bobby` is somebody else's.
    assert cs.is_channel(
        "claude --dangerously-load-development-channels server:bobby",
        "bob") is False


def test_somebody_elses_channel_does_not_make_us_one():
    """A session loading a different development channel is not one Dark Army can
    reach, and the flag alone would have claimed it was."""
    assert cs.is_channel(
        "claude --dangerously-load-development-channels server:webhook",
        "dark-army") is False
    # A plugin entry naming us still counts — same server, different packaging.
    assert cs.is_channel("claude --channels plugin:bob@acme", "bob") is True
    assert cs.is_channel("claude --channels plugin:dark-army@acme",
                         "dark-army") is True


def test_an_ordinary_session_is_not_offered_as_reachable():
    """Observed live: two of the user's own sessions reported themselves
    reachable because the user-scope server had started in them, and the panel
    would have drawn a reply box that silently went nowhere."""
    daemon = _daemon()
    _attach(daemon, is_channel=False)
    daemon._session_states["s1"] = {"pid": 4242, "last_event": 0}
    assert daemon._channel_for_session("s1") is None
    assert asyncio.run(daemon.reply_to_session("s1", "accept"))[0] is False


def test_a_channel_names_its_own_session():
    """`CLAUDE_CODE_SESSION_ID` is in every MCP server's environment, so there
    is nothing to resolve — and nothing to be wrong about when two sessions
    share a pid namespace after a restart."""
    daemon = _daemon()
    _attach(daemon, pid=0, session_id="s9")
    daemon._session_states["s9"] = {"last_event": 0}
    assert daemon._channel_session(51000) == "s9"


def test_a_session_id_for_a_session_bob_does_not_have_resolves_to_nothing():
    daemon = _daemon()
    _attach(daemon, pid=0, session_id="ghost")
    assert daemon._channel_session(51000) is None
    # Board tools are the exception: a Prep card attributed to an unhooked
    # id starts nothing, and refusing it is how Grok's extra windows died.
    assert daemon._channel_session(51000, require_known=False) == "ghost"
    assert daemon._board_request_session(51000) == "ghost"


@pytest.mark.asyncio
async def test_a_grok_channel_files_a_card_before_hooks_know_the_session(tmp_path):
    """Measured live: GROK_SESSION_ID is unique per MCP child, the leader's
    pid is shared, and Dark Army's hook table often holds only the session that
    started the leader. Board verbs must still file onto the right project."""
    from dark_army_daemon.board import BoardStore
    daemon = _daemon()
    store = BoardStore(tmp_path / "board.db")
    store.connect()
    daemon._board = store
    root = tmp_path / "finance-demo"
    root.mkdir()
    try:
        _attach(daemon, port=51000, pid=1028, session_id="grok-stock",
                cwd=str(root))
        # The consulting session Dark Army *does* know, same leader pid.
        daemon._session_states["grok-consulting"] = {
            "pid": 1028, "last_event": 0, "project": "consulting"}
        daemon._agents_snapshot_cache = {"running": [{
            "session_id": "grok-consulting",
            "cwd": str(tmp_path / "consulting"),
            "project": "consulting",
        }]}
        daemon._known_project_roots = lambda: {os.path.realpath(root)}
        daemon._board_projects = lambda: [{
            "name": "finance-demo", "root": os.path.realpath(root),
        }]
        reply = await daemon._handle_board_card_request({
            "type": "board_card_request", "port": 51000,
            "title": "Remove the website product",
            "summary": "Keep the iPhone backend",
            "notes": "Plan: plans/x.md",
        })
        assert reply["ok"] is True
        card = store.cards()[0]
        assert card["author"] == "grok-stock"
        assert card["root"] == os.path.realpath(root)
        assert card["column_name"] == "prep"
        # Push still requires a hooked row. With only the consulting session
        # on the shared leader pid, the pid fallback would mis-attribute a
        # reply — which is why board tools do not use it.
        assert daemon._board_request_session(51000) == "grok-stock"
    finally:
        store.close()


# ── Grok reply, via the leader rather than the Claude channel ────────────────


class _FakeLeader:
    def __init__(self, result=(True, "")):
        self.result = result
        self.calls = []

    async def prompt(self, session_id, text):
        self.calls.append((session_id, text))
        return self.result


def _grok_daemon(resident=False, leader_up=False, leader=None):
    daemon = _daemon()
    daemon._session_states["g1"] = {
        "pid": 1, "last_event": 0, "provider": "grok",
    }
    daemon._grok_leader_up = leader_up
    daemon._grok_resident = frozenset({"g1"} if resident else ())
    daemon._leader = leader
    return daemon


def test_a_grok_session_without_a_leader_refuses_in_words():
    """The matching honesty rule: a box on a session the leader cannot
    reach is a button that does nothing."""
    daemon = _grok_daemon()
    pushed = []

    async def _push(*_a, **_k):
        pushed.append(True)
        return True

    daemon.push_channel_event = _push
    ok, detail = asyncio.run(daemon.reply_to_session("g1", "accept"))
    assert ok is False
    assert "Grok leader is not running" in detail
    assert pushed == []


def test_a_grok_session_not_attached_to_the_leader_refuses():
    daemon = _grok_daemon(leader_up=True)
    pushed = []

    async def _push(*_a, **_k):
        pushed.append(True)
        return True

    daemon.push_channel_event = _push
    ok, detail = asyncio.run(daemon.reply_to_session("g1", "accept"))
    assert ok is False
    assert "not attached to the Grok leader" in detail
    assert pushed == []


def test_a_resident_grok_session_is_replied_via_the_leader():
    fake = _FakeLeader((True, ""))
    daemon = _grok_daemon(resident=True, leader_up=True, leader=fake)
    pushed = []

    async def _push(*_a, **_k):
        pushed.append(True)
        return True

    daemon.push_channel_event = _push
    ok, detail = asyncio.run(daemon.reply_to_session("g1", "  accept  "))
    assert (ok, detail) == (True, "")
    assert fake.calls == [("g1", "accept")]
    assert pushed == []


def test_empty_and_overlong_refuse_before_the_grok_leader_is_called():
    fake = _FakeLeader((True, ""))
    daemon = _grok_daemon(resident=True, leader_up=True, leader=fake)
    assert asyncio.run(daemon.reply_to_session("g1", "   "))[0] is False
    assert fake.calls == []
    ok, _detail = asyncio.run(
        daemon.reply_to_session("g1", "x" * (daemon.MAX_REPLY_CHARS + 1)))
    assert ok is False
    assert fake.calls == []


def test_a_claude_session_in_the_grok_resident_set_still_uses_the_channel():
    """Provider fork, not id membership. A Claude id that happens to sit in
    the Grok resident set must not be sent through the leader."""
    daemon = _daemon()
    _attach(daemon)
    daemon._session_states["s1"] = {
        "pid": 4242, "last_event": 0, "provider": "claude",
    }
    daemon._grok_leader_up = True
    daemon._grok_resident = frozenset({"s1"})
    fake = _FakeLeader((True, ""))
    daemon._leader = fake
    called = []

    async def _push(sid, text, meta=None):
        called.append((sid, text, meta))
        return True

    daemon.push_channel_event = _push
    ok, detail = asyncio.run(daemon.reply_to_session("s1", "accept"))
    assert (ok, detail) == (True, "")
    assert called == [("s1", "accept", {"kind": "user"})]
    assert fake.calls == []


def test_a_grok_session_never_falls_through_to_the_claude_channel():
    """The inherited `bob` MCP already starts in Grok sessions; the handshake
    fails. A Grok `is_channel` true would be a box that silently goes nowhere."""
    daemon = _daemon()
    _attach(daemon)
    daemon._session_states["g1"] = {
        "pid": 4242, "last_event": 0, "provider": "grok",
    }
    daemon._grok_leader_up = False
    daemon._grok_resident = frozenset()
    assert daemon._session_reachable("g1") is False
    daemon._grok_leader_up = True
    daemon._grok_resident = frozenset({"g1"})
    assert daemon._session_reachable("g1") is True
    # A live Claude channel on the same pid still must not count.
    daemon._grok_resident = frozenset()
    assert daemon._session_reachable("g1") is False


# ── the port is a lock, not a hiding place ───────────────────────────────────

def test_a_push_without_the_password_is_ignored():
    """The whole point of the secret. An ephemeral port is in `lsof` and 64k
    blind connects take a second, so anything on this machine can reach a
    channel's socket — and a `kind=user` push is treated by the session as if
    the person had typed it. Reaching the socket must not be enough."""
    server = _server()
    server.secret = "sh4red"
    server.pending["abcde"] = {}
    out = io.StringIO()
    server._out = out

    server.handle_daemon_message(json.dumps(
        {"type": "event", "content": "rm -rf ~", "meta": {"kind": "user"}}))
    server.handle_daemon_message(json.dumps(
        {"type": "event", "content": "rm -rf ~", "secret": "guessed"}))
    server.handle_daemon_message(json.dumps(
        {"type": "permission_verdict", "request_id": "abcde", "behavior": "allow"}))

    assert out.getvalue() == ""
    # And the prompt is still open: an unauthenticated verdict must not even
    # count as having answered it.
    assert "abcde" in server.pending


def test_a_server_that_never_minted_a_secret_refuses_everything():
    """Fail closed. An empty password that compares equal to an absent header
    would open the socket to exactly what the password is here to keep out."""
    server = _server()
    assert server.secret == ""
    out = io.StringIO()
    server._out = out
    server.handle_daemon_message(json.dumps({"type": "event", "content": "hi"}))
    server.handle_daemon_message(json.dumps({"type": "event", "content": "hi",
                                             "secret": ""}))
    assert out.getvalue() == ""


def test_listen_mints_a_fresh_secret_each_time():
    a, b = _server(), _server()
    try:
        a.listen()
        b.listen()
        assert a.secret and b.secret and a.secret != b.secret
        assert len(a.secret) >= 32
        # It is announced to the daemon, and that is the only place it travels.
        msg = cs.attach_message(a.port, 1, "/tmp", "s1", True, secret=a.secret)
        assert msg["secret"] == a.secret
    finally:
        for s in (a, b):
            if s._sock:
                s._sock.close()


def test_the_secret_never_reaches_the_read_surface():
    """`/api/state` is deliberately ungated — `curl :19874/api/state | jq` is a
    supported thing to do. Anything the registry grows has to be checked against
    that, and a password published there would be worse than no password."""
    daemon = _daemon()
    _attach(daemon, port=51000, pid=4242, session_id="s1", secret="TOPSECRET")
    daemon._session_states["s1"] = {"pid": 4242, "last_event": 0, "state": "idle"}
    daemon._handle_channel_message(
        {"type": "channel_permission_request", "port": 51000, "request_id": "r1",
         "tool_name": "Bash", "description": "d", "input_preview": "p"})

    published = json.dumps(daemon.detailed_snapshot(), default=str)
    published += json.dumps(daemon._permission_snapshot(), default=str)
    assert "TOPSECRET" not in published


# ── a session's channel cannot be taken over ─────────────────────────────────

def test_a_second_channel_cannot_displace_a_live_one():
    """The hook socket authenticates nothing, so an attach is only a *claim*.
    A process that claimed a session someone else already owns would be handed
    that session's replies — everything typed into Dark Army's panel for it."""
    daemon = _daemon()
    _attach(daemon, port=51000, pid=4242, session_id="s1", secret="real")
    _attach(daemon, port=51999, pid=4242, session_id="s1", secret="stolen")

    assert 51999 not in daemon._channels
    assert daemon._channels[51000]["secret"] == "real"


def test_a_claim_lapses_when_the_holder_stops_heartbeating():
    """The honest restart case. A channel that dies stops announcing itself, and
    its hold on the session has to expire or the session is unreachable until
    the staleness sweep — a permanent refusal to fix a temporary one."""
    daemon = _daemon()
    _attach(daemon, port=51000, pid=4242, session_id="s1", secret="old")
    daemon._channels[51000]["last_seen"] -= daemon.CHANNEL_CLAIM_SECONDS + 1

    _attach(daemon, port=51999, pid=4242, session_id="s1", secret="new")
    assert 51999 in daemon._channels


def test_an_unrelated_session_is_not_refused():
    """The guard is per session, not a global one-channel-at-a-time lock."""
    daemon = _daemon()
    _attach(daemon, port=51000, pid=4242, session_id="s1")
    _attach(daemon, port=51001, pid=4343, session_id="s2")
    assert {51000, 51001} <= set(daemon._channels)


def test_a_heartbeat_from_the_holder_is_not_treated_as_a_takeover():
    """It re-announces the same port every 30s for the life of the session."""
    daemon = _daemon()
    _attach(daemon, port=51000, pid=4242, session_id="s1", secret="real")
    _attach(daemon, port=51000, pid=4242, session_id="s1", secret="real")
    assert daemon._channels[51000]["secret"] == "real"


# ── the one tool: a card on Dark Army's board ──────────────────────────────────────
#
# The channel stays one-way for *conversation* — there is still no reply tool,
# and the person is reading the terminal. What it gained is one outbound verb
# that writes something a human has to act on.


def test_the_handshake_now_advertises_tools():
    reply = _run(_server(), [{"jsonrpc": "2.0", "id": 1, "method": "initialize",
                              "params": {"protocolVersion": "2025-11-25"}}])[0]
    caps = reply["result"]["capabilities"]
    assert "tools" in caps
    # ...without giving up the declaration that makes it a channel at all.
    assert "claude/channel" in caps["experimental"]


def test_tools_list_names_exactly_the_ten_verbs():
    """Ten — six board verbs, the two knowledge ones, Mission Control's ask
    to start a card and a batch session's move to its next card — and the
    list is exhaustive on purpose: every name here is something an agent can
    do to Dark Army, and an eleventh arriving unnoticed is the thing this
    assertion exists to prevent."""
    reply = _run(_server(), [{"jsonrpc": "2.0", "id": 2,
                              "method": "tools/list"}])[0]
    tools = reply["result"]["tools"]
    assert [t["name"] for t in tools] == [
        "dark_army_add_card", "dark_army_close_card", "dark_army_attach_plan",
        "dark_army_attach_report", "dark_army_needs_manual_check", "dark_army_answer_card",
        "dark_army_knowledge_read", "dark_army_knowledge_write",
        "dark_army_request_start", "dark_army_next_card"]


def test_host_argument_defaults_to_claude_and_accepts_codex():
    assert cs.host_from_argv([]) == cs.HOST_CLAUDE
    assert cs.host_from_argv(["--host=codex"]) == cs.HOST_CODEX
    assert cs.host_from_argv(["--host=unknown"]) == cs.HOST_CLAUDE


def test_codex_lists_only_the_board_verbs():
    """Add, close, attach-plan, attach-report, manual-check and next-card —
    the verbs that write a board row and type nothing. Reply, answer and the
    knowledge notes stay off the list, and `call_tool` enforces the same
    one."""
    server = cs.ChannelServer(stdin=io.StringIO(), stdout=io.StringIO(),
                              host=cs.HOST_CODEX)
    reply = _run(server, [{"jsonrpc": "2.0", "id": 2,
                           "method": "tools/list"}])[0]
    assert [tool["name"] for tool in reply["result"]["tools"]] == [
        "dark_army_add_card", "dark_army_close_card", "dark_army_attach_plan",
        "dark_army_attach_report", "dark_army_needs_manual_check",
        "dark_army_next_card"]
    assert [tool["name"] for tool in cs.tools_for_host(cs.HOST_CODEX)] == [
        "dark_army_add_card", "dark_army_close_card", "dark_army_attach_plan",
        "dark_army_attach_report", "dark_army_needs_manual_check",
        "dark_army_next_card"]


def test_codex_may_flag_a_manual_check_on_its_card(monkeypatch):
    """A board row, typed at nobody — the close verb's argument. Without it a
    Codex run whose only open item was a hand-check could neither close its
    card nor release its place, and the project's queue waited on it."""
    server = cs.ChannelServer(host=cs.HOST_CODEX)
    sent = []
    monkeypatch.setattr(server, "call_daemon", lambda msg: sent.append(msg) or {
        "ok": True, "detail": "manual check flagged", "card_id": "c1",
        "column": "in_progress", "title": "t"})
    result = server.call_tool({"name": "dark_army_needs_manual_check",
                               "arguments": {"steps": "1. look"}})
    assert result.get("isError") is not True, result
    assert len(sent) == 1 and "card_id" not in sent[0]


def test_codex_cannot_call_the_hidden_answer_tool(monkeypatch):
    server = cs.ChannelServer(host=cs.HOST_CODEX)
    monkeypatch.setattr(server, "call_daemon", lambda msg: pytest.fail(
        "a forbidden tool must never reach the daemon"))
    result = server.call_tool({"name": "dark_army_answer_card",
                               "arguments": {"answer": "yes"}})
    assert result["isError"] is True
    assert "unavailable for codex" in result["content"][0]["text"]


def test_codex_may_close_the_card_it_was_started_for(monkeypatch):
    """The one widening: a board row, typed at nobody. Codex could open a card
    and attach a plan but never say it had finished one, so every card a Codex
    session worked stayed in In progress for ever."""
    server = cs.ChannelServer(host=cs.HOST_CODEX)
    sent = []
    monkeypatch.setattr(server, "call_daemon",
                        lambda msg: sent.append(msg) or {"ok": True,
                                                         "detail": "moved to Done",
                                                         "title": "a card"})
    result = server.call_tool({"name": "dark_army_close_card",
                               "arguments": {"note": "done", "summary": "s"}})
    assert result.get("isError") is not True
    assert sent and "card_id" not in sent[0]


def test_codex_initialize_declares_no_claude_channel_capabilities():
    server = cs.ChannelServer(host=cs.HOST_CODEX)
    reply = _run(server, [{"jsonrpc": "2.0", "id": 1, "method": "initialize",
                           "params": {"protocolVersion": "2025-11-25"}}])[0]
    result = reply["result"]
    assert "experimental" not in result["capabilities"]
    assert "instructions" not in result


def test_attach_propagates_its_validated_host():
    msg = cs.attach_message(51000, 4242, "/tmp", host=cs.HOST_CODEX)
    assert msg["host"] == cs.HOST_CODEX
    assert cs.attach_message(51000, 4242, "/tmp", host="other")["host"] \
        == cs.HOST_CLAUDE


# ── the dual-name window: two registrations, one script, one voice ───────────

_PLAIN = "claude"
_RESUME = "claude --resume"
_OLD = "claude --dangerously-load-development-channels server:bob"
_NEW = "claude --dangerously-load-development-channels server:dark-army"
_OTHER = "claude --channels server:other"
_PLUGIN_OLD = "claude --channels plugin:bob@market"


def test_name_from_argv_defaults_to_the_legacy_name():
    """The build before registered the script with no flag, and that
    registration *is* the `bob` one — so absent or unknown means `bob`."""
    assert cs.name_from_argv([]) == "bob"
    assert cs.name_from_argv(["--name=dark-army"]) == "dark-army"
    assert cs.name_from_argv(["--name=bob"]) == "bob"
    assert cs.name_from_argv(["--name=x"]) == "bob"
    assert cs.name_from_argv(["--host=codex", "--name=dark-army"]) == "dark-army"
    assert cs.host_from_argv(["--host=codex", "--name=dark-army"]) == cs.HOST_CODEX


@pytest.mark.parametrize("command,names", [
    (_PLAIN, frozenset()),
    (_RESUME, frozenset()),
    (_OLD, frozenset({"bob"})),
    (_NEW, frozenset({"dark-army"})),
    (_OTHER, frozenset({"other"})),
    (_PLUGIN_OLD, frozenset({"bob"})),
    # Without a channel flag nothing on the line is a channel registration.
    ("claude --mcp-config server:bob", frozenset()),
])
def test_launch_names_reads_only_a_flagged_line(command, names):
    assert cs.launch_names(command) == names


@pytest.mark.parametrize("command,current_active,legacy_active", [
    (_PLAIN, True, False),
    (_RESUME, True, False),
    (_OLD, False, True),
    (_NEW, True, False),
    (_OTHER, True, False),
    (_PLUGIN_OLD, False, True),
])
def test_is_active_picks_exactly_one_copy_per_session(command, current_active,
                                                      legacy_active):
    """The flag names the active copy; naming neither of ours, the current
    name speaks. Never both and never neither, for every line we serve."""
    assert cs.is_active(command, "dark-army") is current_active
    assert cs.is_active(command, "bob") is legacy_active
    assert current_active != legacy_active


def test_name_flag_present_tells_a_flagless_bob_from_an_explicit_one():
    assert cs.name_flag_present([]) is False
    assert cs.name_flag_present(["--host=codex"]) is False
    assert cs.name_flag_present(["--name=bob"]) is True
    assert cs.name_flag_present(["--name=dark-army"]) is True
    # The plan's own assertion still holds: absent reads as `bob`.
    assert cs.name_from_argv([]) == "bob"


@pytest.mark.parametrize("command,active", [
    (_PLAIN, True),
    (_RESUME, True),
    (_OLD, True),
    (_OTHER, True),
    (_NEW, False),
])
def test_a_flagless_bob_speaks_unless_the_launch_names_dark_army(command,
                                                                 active):
    """The build before's lone registration has no `dark-army` sibling: a
    session opened before the install whose helper restarts, or one opened
    between the script write and the `dark-army` add, would otherwise be
    left with no board tools at all."""
    assert cs.is_active(command, "bob", flagged=False) is active


def test_an_explicit_legacy_copy_keeps_the_sibling_rule():
    """`--name=bob` has a `dark-army` sibling, so a plain session stays on
    the current name — unchanged by the flagless rule."""
    assert cs.is_active(_PLAIN, "bob", flagged=True) is False
    assert cs.is_active(_OLD, "bob", flagged=True) is True


def test_main_passes_flag_presence_to_the_active_decision(monkeypatch):
    seen = []
    monkeypatch.setattr(cs, "parent_command", lambda pid: _PLAIN)
    monkeypatch.setattr(cs.ChannelServer, "run",
                        lambda self: seen.append((self.name, self.active)))
    cs.main([])
    cs.main(["--name=bob"])
    cs.main(["--name=dark-army"])
    assert seen == [("bob", True), ("bob", False), ("dark-army", True)]


@pytest.mark.parametrize("command,names", [
    # A prompt after the flag's own argument is never a channel entry.
    ('claude --dangerously-load-development-channels server:dark-army '
     '"fix server:bob now"', frozenset({"dark-army"})),
    # `ps` flattens the quotes away; the prompt's first word still ends the
    # flag's arguments.
    ("claude --dangerously-load-development-channels server:dark-army "
     "fix server:bob now", frozenset({"dark-army"})),
    ("claude --channels server:dark-army 'ask x:bob@host'",
     frozenset({"dark-army"})),
    # A quote in `ps` output is prompt text, not shell quoting: the split is
    # whitespace only, so an apostrophe never hides the flag behind it.
    ("claude --channels server:dark-army don't use server:bob",
     frozenset({"dark-army"})),
    ("claude what's up --channels server:dark-army it's",
     frozenset({"dark-army"})),
    # The `=` form and comma-separated entries.
    ("claude --channels=server:bob", frozenset({"bob"})),
    ("claude --channels server:bob,server:other", frozenset({"bob", "other"})),
    ("claude --channels server:bob server:other", frozenset({"bob", "other"})),
    # A name in some other option's value is not a channel entry.
    ("claude --mcp-config server:bob --channels server:dark-army",
     frozenset({"dark-army"})),
    # The flag's name inside another token is not the flag.
    ("claude --no-channels server:bob", frozenset()),
])
def test_launch_names_reads_only_the_flags_own_arguments(command, names):
    assert cs.launch_names(command) == names


def test_a_prompt_naming_the_other_server_wakes_only_the_flagged_copy():
    line = ('claude --dangerously-load-development-channels server:dark-army '
            '"fix server:bob now"')
    assert cs.is_active(line, "dark-army") is True
    assert cs.is_active(line, "bob") is False
    assert cs.is_channel(line, "dark-army") is True
    assert cs.is_channel(line, "bob") is False


def test_a_line_naming_both_names_makes_both_active():
    """The one shape the window does not serve, pinned so nobody mistakes it
    for a supported case (see the module docstring)."""
    both = ("claude --dangerously-load-development-channels "
            "server:bob server:dark-army")
    assert cs.is_active(both, "bob") and cs.is_active(both, "dark-army")


@pytest.mark.parametrize("name,prefix", [("dark-army", "dark_army_"),
                                         ("bob", "bob_")])
def test_tools_for_host_spells_every_verb_with_its_own_prefix(name, prefix):
    claude = [t["name"] for t in cs.tools_for_host(cs.HOST_CLAUDE, name)]
    assert claude == [prefix + verb for verb in cs.VERBS]
    codex = [t["name"] for t in cs.tools_for_host(cs.HOST_CODEX, name)]
    assert codex == [prefix + verb for verb in
                     ("add_card", "close_card", "attach_plan", "attach_report",
                      "needs_manual_check", "next_card")]
    # The descriptions name siblings in the same spelling, never the other.
    other = "bob_" if prefix == "dark_army_" else "dark_army_"
    for tool in cs.tools_for_host(cs.HOST_CLAUDE, name):
        assert other not in tool["description"]


def test_tools_for_host_returns_fresh_dicts():
    """A caller editing its list must not edit the template another reads."""
    first = cs.tools_for_host(cs.HOST_CLAUDE, "bob")
    first[0]["name"] = "mangled"
    first[0]["inputSchema"]["properties"].clear()
    again = cs.tools_for_host(cs.HOST_CLAUDE, "bob")
    assert again[0]["name"] == "bob_add_card"
    assert again[0]["inputSchema"]["properties"]


def test_tool_name_falls_back_to_the_legacy_prefix():
    assert cs.tool_name("answer_card", "dark-army") == "dark_army_answer_card"
    assert cs.tool_name("answer_card", "bob") == "bob_answer_card"
    assert cs.tool_name("answer_card", None) == "bob_answer_card"


def test_initialize_active_and_passive():
    active = cs.initialize_result("2025-06-18", cs.HOST_CLAUDE, "dark-army",
                                  active=True)
    assert "claude/channel" in active["capabilities"]["experimental"]
    assert 'source="dark-army"' in active["instructions"]
    assert active["serverInfo"]["name"] == "dark-army"
    passive = cs.initialize_result("2025-06-18", cs.HOST_CLAUDE, "bob",
                                   active=False)
    assert passive["capabilities"] == {"tools": {"listChanged": False}}
    assert "instructions" not in passive
    assert passive["serverInfo"]["name"] == "bob"


def test_a_passive_copy_answers_the_handshake_and_offers_nothing(monkeypatch):
    server = _server(name="bob", active=False)
    monkeypatch.setattr(server, "call_daemon", lambda msg: pytest.fail(
        "a passive copy never reaches the daemon"))
    replies = _run(server, [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-11-25"}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "bob_add_card", "arguments": {"title": "x"}}},
    ])
    init, listed, called = replies
    assert "experimental" not in init["result"]["capabilities"]
    assert "instructions" not in init["result"]
    assert listed["result"]["tools"] == []
    assert called["result"]["isError"] is True
    assert "passive copy of bob" in called["result"]["content"][0]["text"]


def test_a_passive_copy_never_talks_to_the_daemon(monkeypatch):
    """`run()` over a whole handshake: no listener, no heartbeat, no attach —
    the active sibling holds this session, and `_attach_is_displaced` would
    refuse a second port anyway."""
    lines = "\n".join(json.dumps(m) for m in (
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-11-25"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "method":
         "notifications/claude/channel/permission_request",
         "params": {"request_id": "abcde", "tool_name": "Bash"}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    )) + "\n"
    out = io.StringIO()
    server = cs.ChannelServer(stdin=io.StringIO(lines), stdout=out,
                              name="bob", active=False, parent=_PLAIN)

    def boom(*a, **k):
        raise AssertionError("a passive copy must not reach the daemon")

    monkeypatch.setattr(server, "listen", boom)
    monkeypatch.setattr(server, "notify_daemon", boom)
    monkeypatch.setattr(server, "call_daemon", boom)
    server.run()
    replies = [json.loads(line) for line in out.getvalue().splitlines() if line]
    assert [r["id"] for r in replies] == [1, 2]
    assert replies[1]["result"]["tools"] == []
    assert server.pending == {}


def test_an_active_copy_attaches_under_its_own_name(monkeypatch):
    server = _server(name="bob")
    server._parent = _OLD
    sent = []
    monkeypatch.setattr(server, "notify_daemon",
                        lambda msg: sent.append(msg) or True)
    server.handle_stdio_message({"jsonrpc": "2.0",
                                 "method": "notifications/initialized"})
    assert sent[0]["channel"] == "bob"
    assert sent[0]["is_channel"] is True
    assert cs.attach_message(1, 2, "/tmp", name="dark-army")["channel"] \
        == "dark-army"
    assert cs.attach_message(1, 2, "/tmp", name="zzz")["channel"] == "bob"


@pytest.mark.parametrize("name,foreign", [("dark-army", "bob_add_card"),
                                          ("bob", "dark_army_add_card")])
def test_a_copy_refuses_the_other_names_spelling_as_unknown(monkeypatch, name,
                                                            foreign):
    server = _server(name=name)
    monkeypatch.setattr(server, "call_daemon", lambda msg: pytest.fail(
        "another name's spelling must never reach the daemon"))
    result = server.call_tool({"name": foreign, "arguments": {"title": "x"}})
    assert result["isError"] is True
    assert "unknown tool" in result["content"][0]["text"]


def test_a_legacy_copy_still_runs_its_bob_tools(monkeypatch):
    """A session born before the rename keeps answering to `bob_*`."""
    server = _server(name="bob")
    sent = []
    monkeypatch.setattr(server, "call_daemon",
                        lambda msg: sent.append(msg) or {"ok": True,
                                                         "project": "p",
                                                         "plan_attached": True})
    result = server.call_tool({"name": "bob_add_card",
                               "arguments": {"title": "legacy works",
                                             "plan": "plans/x.md"}})
    assert result["isError"] is False
    assert sent[0]["type"] == "board_card_request"
    # Its reply names the legacy spelling of the sibling verb.
    assert "bob_attach_plan" in result["content"][0]["text"]


def test_main_makes_codex_active_and_reads_the_parent_once(monkeypatch):
    seen = []
    reads = []
    monkeypatch.setattr(cs, "parent_command",
                        lambda pid: reads.append(pid) or _OLD)
    monkeypatch.setattr(cs.ChannelServer, "run",
                        lambda self: seen.append((self.host, self.name,
                                                  self.active, self._parent)))
    cs.main(["--name=dark-army"])
    cs.main(["--name=bob"])
    cs.main(["--host=codex", "--name=dark-army"])
    assert seen == [
        (cs.HOST_CLAUDE, "dark-army", False, _OLD),
        (cs.HOST_CLAUDE, "bob", True, _OLD),
        (cs.HOST_CODEX, "dark-army", True, ""),
    ]
    assert len(reads) == 2      # one `ps` per Claude process, none for Codex


# ── the daemon keeps the name, and asks with it ──────────────────────────────

@pytest.mark.parametrize("sent,kept", [("bob", "bob"),
                                       ("dark-army", "dark-army"),
                                       ("zzz", "bob")])
def test_the_registry_keeps_the_attach_name(sent, kept):
    daemon = _daemon()
    _attach(daemon, channel=sent)
    assert daemon._channels[51000]["name"] == kept


def test_an_attach_with_no_channel_key_is_the_legacy_name():
    """A copy from before the window sends nothing: it was born `bob`."""
    daemon = _daemon()
    _attach(daemon)
    assert daemon._channels[51000]["name"] == "bob"


@pytest.mark.parametrize("sent,tool", [("bob", "bob_answer_card"),
                                       ("dark-army", "dark_army_answer_card"),
                                       (_NO_NAME, "bob_answer_card")])
def test_a_question_names_the_tool_the_session_was_born_with(sent, tool):
    """`ask_card`'s direct route reads the name off the entry
    `_channel_for_session` returned, so a legacy session is told to call
    `bob_answer_card` and a new one `dark_army_answer_card`."""
    from dark_army_daemon import daemon_board
    daemon = _daemon()
    _attach(daemon, channel=sent)
    daemon._session_states["s1"] = {"pid": 4242, "last_event": 0}
    entry = daemon._channel_for_session("s1")
    assert entry is not None
    tail = daemon_board.ask_direct_tail(entry.get("name"))
    assert tool in tail
    other = "dark_army_answer_card" if tool == "bob_answer_card" \
        else "bob_answer_card"
    assert other not in tail


def _codex_record(thread, cwd, pid=None, parent=""):
    return codex_rollouts.CodexRecord(
        session_id=f"codex:{thread}", thread_id=thread,
        path=Path(f"/tmp/{thread}.jsonl"), cwd=str(cwd), pid=pid,
        parent_thread_id=parent,
    )


def test_codex_channel_resolves_an_exact_monitored_parent_pid():
    daemon = _daemon()
    _attach(daemon, host=cs.HOST_CODEX, pid=4242)
    daemon._codex_records = {
        "codex:thread": _codex_record("thread", "/tmp/project", pid=4242),
    }
    assert daemon._codex_channel_session(51000) == "codex:thread"


def test_codex_channel_falls_back_to_one_normalised_cwd(tmp_path):
    daemon = _daemon()
    _attach(daemon, host=cs.HOST_CODEX, pid=9999,
            cwd=str(tmp_path / "."))
    daemon._codex_records = {
        "codex:thread": _codex_record("thread", tmp_path),
    }
    assert daemon._codex_channel_session(51000) == "codex:thread"


def test_codex_channel_refuses_an_ambiguous_cwd(tmp_path):
    daemon = _daemon()
    _attach(daemon, host=cs.HOST_CODEX, pid=9999, cwd=str(tmp_path))
    daemon._codex_records = {
        "codex:a": _codex_record("a", tmp_path),
        "codex:b": _codex_record("b", tmp_path),
    }
    assert daemon._codex_channel_session(51000) is None


def test_codex_channel_tells_two_threads_in_one_folder_apart(tmp_path):
    """The cwd fallback cannot, and does not have to: a project worked in
    twice inside the live window has two root rollouts in one folder, and
    the pid the start-time pairing attaches is what names the caller."""
    daemon = _daemon()
    _attach(daemon, host=cs.HOST_CODEX, pid=4242, cwd=str(tmp_path))
    daemon._codex_records = {
        "codex:a": _codex_record("a", tmp_path, pid=4141),
        "codex:b": _codex_record("b", tmp_path, pid=4242),
    }
    assert daemon._codex_channel_session(51000) == "codex:b"


def test_codex_channel_never_attributes_to_a_child_record(tmp_path):
    daemon = _daemon()
    _attach(daemon, host=cs.HOST_CODEX, pid=4242, cwd=str(tmp_path))
    daemon._codex_records = {
        "codex:child": _codex_record("child", tmp_path, pid=4242,
                                      parent="parent"),
    }
    assert daemon._codex_channel_session(51000) is None


def test_codex_is_never_a_push_or_permission_channel(tmp_path):
    daemon = _daemon()
    _attach(daemon, host=cs.HOST_CODEX, pid=4242, is_channel=True,
            cwd=str(tmp_path))
    daemon._codex_records = {
        "codex:thread": _codex_record("thread", tmp_path, pid=4242),
    }
    _relay(daemon)
    assert daemon._permission_requests == {}
    assert daemon._channel_for_session("codex:thread") is None


@pytest.mark.asyncio
async def test_codex_add_then_attach_makes_one_planned_backlog_card(tmp_path):
    daemon, store = _board_daemon(tmp_path)
    root = tmp_path / "project"
    plan = root / "plans" / "work.md"
    plan.parent.mkdir(parents=True)
    plan.write_text("# Work\n")
    try:
        _attach(daemon, host=cs.HOST_CODEX, pid=4242, cwd=str(root))
        daemon._codex_records = {
            "codex:thread": _codex_record("thread", root, pid=4242),
        }
        daemon._agents_snapshot_cache = {"waiting": [{
            "session_id": "codex:thread", "cwd": str(root),
            "project": "project", "provider": "codex",
        }]}
        daemon._known_project_roots = lambda: {os.path.realpath(root)}
        daemon._board_projects = lambda: [{
            "name": "project", "root": os.path.realpath(root),
        }]
        added = await daemon._handle_board_card_request({
            "type": "board_card_request", "port": 51000,
            "title": "Ship the plan", "summary": "Codex can file its plan",
            "notes": "Plan: plans/work.md", "tool": "codex",
        })
        assert added["ok"] is True
        attached = await daemon._handle_board_attach_request({
            "type": "board_attach_request", "port": 51000,
            "path": "plans/work.md",
        })
        assert attached["ok"] is True
        cards = store.cards()
        assert len(cards) == 1
        assert cards[0]["column_name"] == "backlog"
        assert cards[0]["author"] == "codex:thread"
        assert cards[0]["plan_path"] == str(plan)
    finally:
        store.close()


def _plan_filing_daemon(tmp_path):
    daemon, store = _board_daemon(tmp_path)
    root = tmp_path / "project"
    (root / "plans").mkdir(parents=True)
    _attach(daemon, host=cs.HOST_CODEX, pid=4242, cwd=str(root))
    daemon._codex_records = {
        "codex:thread": _codex_record("thread", root, pid=4242),
    }
    daemon._agents_snapshot_cache = {"waiting": [{
        "session_id": "codex:thread", "cwd": str(root),
        "project": "project", "provider": "codex",
    }]}
    daemon._known_project_roots = lambda: {os.path.realpath(root)}
    daemon._board_projects = lambda: [{
        "name": "project", "root": os.path.realpath(root),
    }]
    return daemon, store, root


@pytest.mark.asyncio
async def test_one_session_files_several_plans_and_each_lands_planned(tmp_path):
    """The fit-app stall of 23 Sep 2026: a planning session filed a follow-up
    note, and from then on every plan it attached met "matches more than one
    Prep card" — so it named the plans in the notes, the cards stayed in
    Prep, and Start opened planning runs nothing could finish. `plan` on
    add_card attaches to the card that call creates, ladder or no ladder."""
    daemon, store, root = _plan_filing_daemon(tmp_path)
    try:
        note = await daemon._handle_board_card_request({
            "type": "board_card_request", "port": 51000,
            "title": "A follow-up with no plan", "tool": "codex",
        })
        assert note["ok"] is True and note["plan_attached"] is False
        for name in ("b1", "b2", "b3"):
            (root / "plans" / f"{name}.md").write_text(f"# {name}\n")
            added = await daemon._handle_board_card_request({
                "type": "board_card_request", "port": 51000,
                "title": f"Build {name}", "tool": "codex",
                "plan": f"plans/{name}.md",
            })
            assert added["ok"] is True, added
            assert added["plan_attached"] is True
            assert added["column"] == "backlog"
        by_title = {c["title"]: c for c in store.cards()}
        assert by_title["A follow-up with no plan"]["column_name"] == "prep"
        for name in ("b1", "b2", "b3"):
            card = by_title[f"Build {name}"]
            assert card["column_name"] == "backlog"
            assert card["plan_path"] == os.path.realpath(
                root / "plans" / f"{name}.md")
    finally:
        store.close()


@pytest.mark.asyncio
async def test_a_plan_outside_the_project_files_no_card(tmp_path):
    daemon, store, root = _plan_filing_daemon(tmp_path)
    outside = tmp_path / "elsewhere.md"
    outside.write_text("# not yours\n")
    try:
        for bad in (str(outside), "plans/missing.md"):
            reply = await daemon._handle_board_card_request({
                "type": "board_card_request", "port": 51000,
                "title": "Should not land", "tool": "codex", "plan": bad,
            })
            assert reply["ok"] is False
        assert store.cards() == []
    finally:
        store.close()


def test_add_card_forwards_the_plan_and_says_backlog(monkeypatch):
    server = _server()
    sent = []
    monkeypatch.setattr(server, "call_daemon",
                        lambda msg: sent.append(msg) or {
                            "ok": True, "project": "p", "plan_attached": True})
    result = server.call_tool({"name": "dark_army_add_card",
                               "arguments": {"title": "t",
                                             "plan": " plans/x.md "}})
    assert sent[0]["plan"] == "plans/x.md"
    assert result["isError"] is False
    assert "Backlog" in result["content"][0]["text"]
    assert "plan" in cs.CARD_TOOL["inputSchema"]["properties"]


def test_the_attach_tool_takes_a_path_and_nothing_else():
    """No `card_id` — the omission is the security property, `CLOSE_TOOL`'s
    argument in this verb's terms: the daemon resolves the card solely from
    the calling session, so a forger can only ever attach a file inside that
    card's own project to the card its session is already recorded on."""
    schema = cs.ATTACH_TOOL["inputSchema"]
    assert set(schema["properties"]) == {"path"}
    assert schema["required"] == ["path"]


def test_the_manual_check_tool_takes_steps_and_nothing_else():
    """No `card_id` — `CLOSE_TOOL`'s omission, made in this verb's terms and
    buying back less because it spends less: the daemon resolves the card
    solely from the calling session, so a forger can only hang a badge on the
    card that session is already executing, and a person clears it with one
    press."""
    schema = cs.MANUAL_TOOL["inputSchema"]
    assert set(schema["properties"]) == {"steps", "path"}
    assert schema["required"] == ["steps"]
    assert "session_id" not in schema["properties"]
    assert "card_id" not in schema["properties"]
    # And no way to *clear* it: unflagging your own work is a person's press.
    assert "clear" not in cs.MANUAL_TOOL["name"]


def test_the_manual_check_tool_says_flag_then_close():
    """A card with an open check goes to Done: the description asks for the
    file, the checker, the flag and *then* the close — never "instead of"."""
    text = cs.MANUAL_TOOL["description"]
    assert "instead of" not in text
    assert "manual-check/<YYYY-MM-DD>-<slug>/check.md" in text
    assert "manual_check.py" in text
    assert "then call dark_army_close_card" in text


def test_the_check_path_is_forwarded_and_an_empty_one_sends_none(monkeypatch):
    server = _server()
    server.port = 51000
    sent = []
    monkeypatch.setattr(server, "call_daemon", lambda msg: sent.append(msg) or {
        "ok": True, "title": "t"})
    result = server.call_tool({"name": "dark_army_needs_manual_check",
                               "arguments": {"steps": "1. look",
                                             "path": "  /p/manual-check/a/check.md "}})
    assert result["isError"] is False
    assert sent[-1]["path"] == "/p/manual-check/a/check.md"
    assert "dark_army_close_card" in result["content"][0]["text"]
    server.call_tool({"name": "dark_army_needs_manual_check",
                      "arguments": {"steps": "1. look", "path": "  "}})
    assert "path" not in sent[-1]
    assert "card_id" not in sent[-1]


def test_the_manual_check_tool_asks_for_steps_not_a_hint():
    """The wording is the convention travelling, so it is pinned: numbered,
    what to press, what they should see, and the required reason line."""
    text = cs.MANUAL_TOOL["description"] + \
        cs.MANUAL_TOOL["inputSchema"]["properties"]["steps"]["description"]
    lowered = text.lower()
    assert "numbered" in lowered
    assert "press" in lowered
    assert "Why not automated:" in text
    # And the minimisation: automate it instead where you can.
    assert "in code instead" in lowered


def test_an_empty_manual_check_never_reaches_the_daemon(monkeypatch):
    server = _server()
    monkeypatch.setattr(server, "call_daemon", lambda msg: pytest.fail(
        "an empty flag must be refused locally"))
    result = server.call_tool({"name": "dark_army_needs_manual_check",
                               "arguments": {"steps": "   "}})
    assert result["isError"] is True
    assert "its steps" in result["content"][0]["text"]


def test_the_card_tool_cannot_name_a_column_at_all():
    """Arriving in In progress is what a *dispatch* means, and there is no
    dispatch tool. An agent must not be able to route around that by naming the
    column — so the column is not on offer: every agent-written card lands in
    Backlog."""
    schema = cs.CARD_TOOL["inputSchema"]["properties"]
    assert "column" not in schema
    assert "root" not in schema        # no naming a folder Dark Army would launch into
    assert "session_id" not in schema  # the daemon resolves the caller, not this
    # The plain-language line is asked for, because it is the half of the card a
    # person actually reads.
    assert "summary" in schema


def test_a_tool_call_returns_the_daemons_words(monkeypatch):
    server = _server()
    server.port = 51000
    monkeypatch.setattr(server, "call_daemon", lambda msg: {
        "ok": True, "detail": "added", "column": "prep", "project": "bob"})
    result = server.call_tool({"name": "dark_army_add_card",
                               "arguments": {"title": "fix the thing"}})
    assert result["isError"] is False
    assert "Dark Army's Prep column" in result["content"][0]["text"]


def test_a_tool_call_with_no_title_is_an_error_not_a_hang():
    result = _server().call_tool({"name": "dark_army_add_card", "arguments": {}})
    assert result["isError"] is True


def test_an_unknown_tool_name_is_answered(monkeypatch):
    result = _server().call_tool({"name": "bob_start_everything"})
    assert result["isError"] is True
    assert "unknown tool" in result["content"][0]["text"]


def test_a_daemon_that_never_answers_ends_in_is_error(monkeypatch):
    """An unanswered `tools/call` id is a session waiting forever, which is the
    one outcome this must never produce."""
    server = _server()
    monkeypatch.setattr(server, "call_daemon", lambda msg: None)
    result = server.call_tool({"name": "dark_army_add_card",
                               "arguments": {"title": "x"}})
    assert result["isError"] is True
    assert "did not answer" in result["content"][0]["text"]


def test_a_tools_call_over_stdio_always_answers_its_id(monkeypatch):
    server = _server()
    monkeypatch.setattr(server, "call_daemon", lambda msg: None)
    reply = _run(server, [{"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                           "params": {"name": "dark_army_add_card",
                                      "arguments": {"title": "x"}}}])[0]
    assert reply["id"] == 7
    assert reply["result"]["isError"] is True


@pytest.mark.asyncio
async def test_a_card_request_bob_cannot_attribute_is_refused(tmp_path):
    """An unattributable card is the forgery case. The hook socket authenticates
    nothing, so the port, pid and session id in the message are merely claimed —
    and a card with no author is exactly what somebody forging one would want to
    leave behind."""
    from dark_army_daemon.board import BoardStore
    daemon = _daemon()
    store = BoardStore(tmp_path / "board.db")
    store.connect()
    daemon._board = store
    try:
        reply = await daemon._handle_board_card_request(
            {"type": "board_card_request", "port": 51000, "title": "sneak"})
        assert reply["ok"] is False
        assert store.total() == 0
    finally:
        store.close()


@pytest.mark.asyncio
async def test_a_card_from_a_resolvable_session_is_attributed_to_it(tmp_path):
    from dark_army_daemon.board import BoardStore
    daemon = _daemon()
    store = BoardStore(tmp_path / "board.db")
    store.connect()
    daemon._board = store
    try:
        _attach(daemon, port=51000, pid=4242, session_id="s1")
        daemon._session_states["s1"] = {"pid": 4242, "last_event": 0,
                                        "project": "bob"}
        # The folder comes off the **agents snapshot**, not out of the hook
        # state map — which has no `cwd` key at all, so reading it there filed
        # every agent-written card with an empty root and made every one of
        # them unstartable.
        daemon._agents_snapshot_cache = {"running": [
            {"session_id": "s1", "cwd": "/tmp", "project": "bob"}]}
        reply = await daemon._handle_board_card_request({
            "type": "board_card_request", "port": 51000,
            "title": "while you are in there", "notes": "also this",
            "summary": "the tests are flaky and people stop trusting them",
            "column": "in_progress"})
        assert reply["ok"] is True
        card = store.cards()[0]
        assert card["author"] == "s1"
        assert card["title"] == "while you are in there"
        # And it carries the folder, which is what makes it startable at all.
        assert card["root"] == os.path.realpath("/tmp")
        assert card["summary"].startswith("the tests are flaky")
        # A column named on the socket is ignored outright, not validated: the
        # only place an agent's card may land is Prep.
        assert card["column_name"] == "prep"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_explicit_channel_stages_win_over_workflow_fallbacks(tmp_path):
    from dark_army_daemon.board import BoardStore

    daemon = _daemon()
    store = BoardStore(tmp_path / "board.db")
    store.connect()
    daemon._board = store
    try:
        _attach(daemon, port=51000, pid=4242, session_id="s1")
        daemon._session_states["s1"] = {"pid": 4242, "last_event": 0}
        daemon._agents_snapshot_cache = {"running": [{
            "session_id": "s1", "cwd": "/tmp", "project": "bob",
        }]}
        reply = await daemon._handle_board_card_request({
            "type": "board_card_request",
            "port": 51000,
            "title": "explicit pipeline",
            "notes": "/ship implement plans/work.md",
            "stages": ["custom-first", "custom-last"],
        })
        assert reply["ok"] is True
        card = store.cards()[0]
        assert card["workflow"] == "custom-first\ncustom-last"
        assert card["agent_trail"] == ""
    finally:
        store.close()


@pytest.mark.asyncio
async def test_a_project_only_row_does_not_shadow_the_row_with_the_folder(tmp_path):
    """Measured on a live fleet: a session holds more than one row, a `finished`
    tombstone carries `cwd: None`, and several live rows carry `cwd: ""` with the
    workspace resolved. Returning on the first row that had *either* field filed
    twelve agent-written cards with an empty root — right column, right heading,
    and refused by `dispatch.guard` the moment somebody pressed Start."""
    from dark_army_daemon.board import BoardStore
    daemon = _daemon()
    store = BoardStore(tmp_path / "board.db")
    store.connect()
    daemon._board = store
    try:
        _attach(daemon, port=51000, pid=4242, session_id="s1")
        daemon._session_states["s1"] = {"pid": 4242, "last_event": 0}
        daemon._agents_snapshot_cache = {
            "finished": [{"session_id": "s1", "cwd": None, "project": "bob"}],
            "waiting": [{"session_id": "s1", "cwd": "", "project": "bob"}],
            "running": [{"session_id": "s1", "cwd": "/tmp", "project": "bob"}],
        }
        reply = await daemon._handle_board_card_request({
            "type": "board_card_request", "port": 51000, "title": "startable"})
        assert reply["ok"] is True
        assert store.cards()[0]["root"] == os.path.realpath("/tmp")
    finally:
        store.close()


@pytest.mark.asyncio
async def test_a_card_from_a_session_with_no_cwd_still_finds_its_folder(tmp_path):
    """The name is evidence too. When no row carries a cwd, the project Dark Army
    itself resolved is matched against the same visible-projects list a
    caller-named project is matched against — so the card keeps the folder Dark Army
    already knows the session is in, rather than becoming unstartable."""
    from dark_army_daemon.board import BoardStore
    daemon = _daemon()
    store = BoardStore(tmp_path / "board.db")
    store.connect()
    daemon._board = store
    root = os.path.realpath(str(tmp_path))
    try:
        _attach(daemon, port=51000, pid=4242, session_id="s1")
        daemon._session_states["s1"] = {"pid": 4242, "last_event": 0}
        # No cwd anywhere on the row, and the channel record's own cwd is not a
        # project either — the name is all that is left.
        daemon._agents_snapshot_cache = {"waiting": [
            {"session_id": "s1", "cwd": "", "project": "arpg-web"}]}
        daemon._channels[51000]["cwd"] = ""
        monkey = {"arpg-web": root}
        daemon._board_projects = lambda: [
            {"name": name, "root": path} for name, path in monkey.items()]
        daemon._known_project_roots = lambda: {root}
        reply = await daemon._handle_board_card_request({
            "type": "board_card_request", "port": 51000, "title": "S1 — skills"})
        assert reply["ok"] is True
        card = store.cards()[0]
        assert card["project"] == "arpg-web"
        assert card["root"] == root
    finally:
        store.close()


@pytest.mark.asyncio
async def test_a_card_request_cannot_ask_for_in_progress(tmp_path):
    """Not via the schema, which is a description, but at the daemon — the thing
    an attacker reaches is the socket, not the schema."""
    from dark_army_daemon.board import BoardStore
    daemon = _daemon()
    store = BoardStore(tmp_path / "board.db")
    store.connect()
    daemon._board = store
    try:
        _attach(daemon, port=51000, pid=4242, session_id="s1")
        daemon._session_states["s1"] = {"pid": 4242, "last_event": 0,
                                        "project": "bob"}
        # The folder comes off the **agents snapshot**, not out of the hook
        # state map — which has no `cwd` key at all, so reading it there filed
        # every agent-written card with an empty root and made every one of
        # them unstartable.
        daemon._agents_snapshot_cache = {"running": [
            {"session_id": "s1", "cwd": "/tmp", "project": "bob"}]}
        await daemon._handle_board_card_request({
            "type": "board_card_request", "port": 51000, "title": "start me",
            "column": "in_progress"})
        assert store.cards()[0]["column_name"] == "prep"
    finally:
        store.close()


def test_the_secret_never_reaches_the_board_snapshot(tmp_path):
    """The board rides in `/api/state` alongside everything else that is
    deliberately ungated, so it joins the surfaces the password must be absent
    from."""
    from dark_army_daemon.board import BoardStore
    daemon = _daemon()
    store = BoardStore(tmp_path / "board.db")
    store.connect()
    daemon._board = store
    try:
        _attach(daemon, port=51000, pid=4242, session_id="s1", secret="TOPSECRET")
        store.create({"title": "a card", "project": "bob"})
        published = json.dumps(daemon._build_board_state(), default=str)
        assert "TOPSECRET" not in published
    finally:
        store.close()


# ── whose session is this, really ─────────────────────────────────────────────

def test_a_per_session_id_outranks_one_inherited_from_a_shared_parent():
    """The Grok case, measured live before it was coded for.

    `grok agent leader` is one long-lived process that spawns a channel per
    session and outlives all of them. It carries whatever
    `CLAUDE_CODE_SESSION_ID` was in the environment of the Claude session that
    first started it, and every channel inherits that — so three channels for
    three different projects all announced one dead session id. The real id was
    in `GROK_SESSION_ID` the whole time, unread.
    """
    env = {"GROK_SESSION_ID": "grok-1",
           "CLAUDE_CODE_SESSION_ID": "a-dead-claude-session"}
    assert cs.session_env_id(env) == "grok-1"


def test_an_inherited_id_is_still_used_when_nothing_more_specific_says():
    """Claude Code's own variable is exactly right in a Claude session. The
    ordering only demotes it where something per-session disagrees."""
    assert cs.session_env_id(
        {"CLAUDE_CODE_SESSION_ID": "s1"}) == "s1"


def test_saying_nothing_is_a_real_answer():
    """"" falls the registry back to the pid, which is what it did before any of
    these variables were read. Announcing somebody else's id would be a claim on
    their session, and the registry cannot tell a false claim from a true one."""
    assert cs.session_env_id({}) == ""
    assert cs.session_env_id({"GROK_SESSION_ID": "  "}) == ""


def test_grok_session_cwd_unquotes_the_parent_folder(tmp_path):
    """The Grok MCP child's getcwd() is the leader's project. The on-disk
    session folder is the one the user actually opened."""
    from urllib.parse import quote
    real = "/Users/me/finance-demo"
    (tmp_path / "sessions" / quote(real, safe="") / "grok-1").mkdir(parents=True)
    assert cs.grok_session_cwd("grok-1", grok_home=tmp_path) == real
    assert cs.grok_session_cwd("", grok_home=tmp_path) == ""
    assert cs.grok_session_cwd("missing", grok_home=tmp_path) == ""


def test_session_cwd_prefers_the_grok_folder_over_getcwd(tmp_path, monkeypatch):
    from urllib.parse import quote
    real = "/Users/me/finance-demo"
    grok_home = tmp_path / "grok-home"
    (grok_home / "sessions" / quote(real, safe="") / "grok-1").mkdir(parents=True)
    monkeypatch.setenv("GROK_SESSION_ID", "grok-1")
    monkeypatch.setenv("HOME", str(tmp_path / "not-used"))
    monkeypatch.setattr(cs, "grok_session_cwd",
                        lambda sid, grok_home=None: real if sid == "grok-1" else "")
    monkeypatch.setattr(cs.os, "getcwd", lambda: "/Users/me/consulting")
    assert cs.session_cwd("grok-1", env={"GROK_SESSION_ID": "grok-1"}) == real
    assert cs.session_cwd("s1", env={"CLAUDE_CODE_SESSION_ID": "s1"}) == "/Users/me/consulting"


def test_two_sessions_behind_one_shared_parent_each_keep_their_own_channel():
    """What the stale id actually cost: `_attach_is_displaced` saw a second port
    claiming a session the first already held and refused it — every 30s, for
    the life of the leader — so every Grok session but one had no channel, and
    the one that won resolved to a session the daemon had already evicted."""
    daemon = _daemon()
    daemon._session_states["grok-a"] = {"pid": 2946, "last_event": 0}
    daemon._session_states["grok-b"] = {"pid": 2946, "last_event": 0}
    _attach(daemon, port=51000, pid=2946, session_id="grok-a")
    _attach(daemon, port=51001, pid=2946, session_id="grok-b")
    assert sorted(daemon._channels) == [51000, 51001]
    assert daemon._channel_session(51000) == "grok-a"
    assert daemon._channel_session(51001) == "grok-b"


def test_a_pid_naming_more_than_one_session_resolves_to_none():
    """Fail closed. The pid fallback is for a harness that announces no id at
    all, and a shared parent makes it ambiguous — resolving to the first match
    would hand one session's card, or one session's reply, to whichever row
    happened to be earliest in a dict."""
    daemon = _daemon()
    _attach(daemon, port=51000, pid=2946, session_id="")
    daemon._session_states["grok-a"] = {"pid": 2946, "last_event": 0}
    assert daemon._channel_session(51000) == "grok-a"   # one match still works
    daemon._session_states["grok-b"] = {"pid": 2946, "last_event": 0}
    assert daemon._channel_session(51000) is None


def test_the_card_tool_can_name_the_stages_and_nothing_more():
    """`stages` is pure annotation: it decides which markers the card draws as
    still-to-come and grants no capability at all. That is what makes it safe on
    a socket that authenticates nothing — the worst a forged value achieves is a
    card promising specialists that never run, read by the person who has to
    move it into In progress anyway."""
    schema = cs.CARD_TOOL["inputSchema"]["properties"]
    assert schema["stages"]["type"] == "array"
    assert schema["stages"]["maxItems"] == 12
    assert "stages" not in cs.CARD_TOOL["inputSchema"]["required"]
    # Still nothing that could start work or aim it somewhere new.
    assert "column" not in schema and "root" not in schema


def test_the_stages_reach_the_daemon_clamped(monkeypatch):
    """Clamped at the channel as well as at the store. A tool call arrives as
    whatever JSON the model emitted, so this only has to keep the payload
    bounded — `board.parse_stages` does the normalising."""
    server = _server()
    server.port = 51000
    sent = {}
    monkeypatch.setattr(server, "call_daemon", lambda msg: sent.update(msg) or {
        "ok": True, "detail": "added", "column": "backlog", "project": "bob"})
    server.call_tool({"name": "dark_army_add_card", "arguments": {
        "title": "fix the thing",
        "stages": ["bc-planner", "  ", "x" * 200] + [f"s{i}" for i in range(30)],
    }})
    assert sent["stages"][0] == "bc-planner"
    assert len(sent["stages"]) <= 12
    assert all(len(s) <= 48 for s in sent["stages"])


def test_a_card_with_no_stages_sends_nothing_for_them(monkeypatch):
    server = _server()
    server.port = 51000
    sent = {}
    monkeypatch.setattr(server, "call_daemon", lambda msg: sent.update(msg) or {
        "ok": True, "detail": "added", "column": "backlog", "project": "bob"})
    server.call_tool({"name": "dark_army_add_card", "arguments": {"title": "x"}})
    assert not sent["stages"]


# ── closing a card the session is already doing ──────────────────────────────


def test_the_close_tool_takes_no_card_id_at_all():
    """**The security property, and the reason a close verb is tolerable on a
    socket that authenticates nothing.** The daemon resolves the card from the
    calling session, so the only card a caller can reach is one it is already
    the running author of. A `card_id` property here would turn this into a way
    to mark arbitrary work finished, and would delete the whole justification —
    which is why the absence is pinned by a test rather than by a comment."""
    schema = cs.CLOSE_TOOL["inputSchema"]
    assert set(schema["properties"]) == {"note"}
    assert schema["required"] == ["note"]
    assert cs.CLOSE_TOOL_NAME == "dark_army_close_card"


def test_a_close_with_no_note_never_reaches_the_socket(monkeypatch):
    """Refused locally as well as at the store: a model with nothing to say is
    told immediately rather than after a round trip that ends the same way."""
    server = _server()
    monkeypatch.setattr(server, "call_daemon", lambda msg: pytest.fail(
        "the socket must not be touched"))
    result = server.call_tool({"name": "dark_army_close_card",
                               "arguments": {"note": "   "}})
    assert result["isError"] is True
    assert "a reason" in result["content"][0]["text"]


def test_a_close_call_carries_the_port_and_no_card(monkeypatch):
    server = _server()
    server.port = 51000
    sent = {}
    monkeypatch.setattr(server, "call_daemon", lambda msg: sent.update(msg) or {
        "ok": True, "detail": "moved to Done", "title": "do the thing"})
    result = server.call_tool({"name": "dark_army_close_card",
                               "arguments": {"note": "tests pass"}})
    assert result["isError"] is False
    assert "Done" in result["content"][0]["text"]
    assert sent["type"] == "board_close_request"
    assert sent["port"] == 51000
    assert sent["note"] == "tests pass"
    assert "card_id" not in sent


def test_a_refused_close_comes_back_in_the_daemons_words(monkeypatch):
    server = _server()
    monkeypatch.setattr(server, "call_daemon", lambda msg: {
        "ok": False, "detail": "no card on Dark Army's board names this session"})
    result = server.call_tool({"name": "dark_army_close_card",
                               "arguments": {"note": "done"}})
    assert result["isError"] is True
    assert "no card" in result["content"][0]["text"]


def test_the_answer_tool_takes_no_card_id_at_all():
    schema = cs.ANSWER_TOOL["inputSchema"]
    assert set(schema["properties"]) == {"answer"}
    assert schema["required"] == ["answer"]
    assert cs.ANSWER_TOOL_NAME == "dark_army_answer_card"
    dumped = __import__("json").dumps(cs.ANSWER_TOOL)
    assert "card_id" not in dumped and "session_id" not in dumped


def test_an_empty_answer_never_reaches_the_socket(monkeypatch):
    server = _server()
    monkeypatch.setattr(server, "call_daemon", lambda msg: pytest.fail(
        "the socket must not be touched"))
    result = server.call_tool({"name": "dark_army_answer_card",
                               "arguments": {"answer": "   "}})
    assert result["isError"] is True
    assert "answer needs some text" in result["content"][0]["text"]


def test_an_answer_call_carries_the_port_and_no_card(monkeypatch):
    server = _server()
    server.port = 51000
    sent = {}
    monkeypatch.setattr(server, "call_daemon", lambda msg: sent.update(msg) or {
        "ok": True, "detail": "answered"})
    result = server.call_tool({"name": "dark_army_answer_card",
                               "arguments": {"answer": "yes it is"}})
    assert result["isError"] is False
    assert sent["type"] == "board_answer_request"
    assert sent["port"] == 51000
    assert sent["pid"]
    assert sent["answer"] == "yes it is"
    assert "card_id" not in sent
    assert "session_id" not in sent


def test_codex_cannot_call_the_hidden_answer_tool(monkeypatch):
    server = cs.ChannelServer(host=cs.HOST_CODEX)
    monkeypatch.setattr(server, "call_daemon", lambda msg: pytest.fail(
        "a forbidden tool must never reach the daemon"))
    result = server.call_tool({"name": "dark_army_answer_card",
                               "arguments": {"answer": "nope"}})
    assert result["isError"] is True
    assert "unavailable for codex" in result["content"][0]["text"]


def test_a_daemon_that_never_answers_a_close_ends_in_is_error(monkeypatch):
    """Same contract as the card tool: an unanswered `tools/call` id is a
    session waiting forever."""
    server = _server()
    monkeypatch.setattr(server, "call_daemon", lambda msg: None)
    result = server.call_tool({"name": "dark_army_close_card",
                               "arguments": {"note": "done"}})
    assert result["isError"] is True
    assert "did not answer" in result["content"][0]["text"]


def _board_daemon(tmp_path):
    from dark_army_daemon.board import BoardStore
    daemon = _daemon()
    store = BoardStore(tmp_path / "board.db")
    store.connect()
    daemon._board = store
    return daemon, store


@pytest.mark.asyncio
async def test_a_close_bob_cannot_attribute_is_refused(tmp_path):
    """The forgery case. Nothing on the board moves, and nothing is written."""
    daemon, store = _board_daemon(tmp_path)
    try:
        card, _ = store.create({"title": "a card", "project": "bob"})
        store.bind_session(card["id"], "s1")
        reply = await daemon._handle_board_close_request(
            {"type": "board_close_request", "port": 51000, "note": "done"})
        assert reply["ok"] is False
        assert store.get(card["id"])["column_name"] == "in_progress"
        assert store.get(card["id"])["closed_by"] == ""
    finally:
        store.close()


@pytest.mark.asyncio
async def test_a_session_closes_the_one_card_it_is_bound_to(tmp_path):
    daemon, store = _board_daemon(tmp_path)
    try:
        _attach(daemon, port=51000, pid=4242, session_id="s1")
        daemon._session_states["s1"] = {"pid": 4242, "last_event": 0}
        mine, _ = store.create({"title": "mine", "project": "bob"})
        theirs, _ = store.create({"title": "theirs", "project": "bob"})
        store.bind_session(mine["id"], "s1")
        store.bind_session(theirs["id"], "s2")
        reply = await daemon._handle_board_close_request({
            "type": "board_close_request", "port": 51000,
            "note": "verifier came back PASS"})
        assert reply["ok"] is True
        assert reply["card_id"] == mine["id"]
        closed = store.get(mine["id"])
        assert closed["column_name"] == "done"
        assert closed["closed_by"] == "s1"
        assert closed["close_note"] == "verifier came back PASS"
        # And it reached nothing else: the scope is the whole argument.
        assert store.get(theirs["id"])["column_name"] == "in_progress"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_a_session_on_no_card_is_refused_in_words(tmp_path):
    daemon, store = _board_daemon(tmp_path)
    try:
        _attach(daemon, port=51000, pid=4242, session_id="s1")
        daemon._session_states["s1"] = {"pid": 4242, "last_event": 0}
        # An unbound card on the board, which is what `by_session("")` would
        # have matched without its guard.
        store.create({"title": "nobody's", "project": "bob"})
        reply = await daemon._handle_board_close_request({
            "type": "board_close_request", "port": 51000, "note": "done"})
        assert reply["ok"] is False
        assert "no card" in reply["detail"]
        assert store.cards()[0]["column_name"] == "prep"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_a_session_on_two_live_cards_fails_closed(tmp_path):
    """Ambiguity is refused rather than guessed at, exactly as
    `_channel_session`'s own pid-to-session rule does. Closing the wrong card
    is silent and has no undo but a human noticing."""
    daemon, store = _board_daemon(tmp_path)
    try:
        _attach(daemon, port=51000, pid=4242, session_id="s1")
        daemon._session_states["s1"] = {"pid": 4242, "last_event": 0}
        a, _ = store.create({"title": "one", "project": "bob"})
        b, _ = store.create({"title": "two", "project": "bob"})
        store.bind_session(a["id"], "s1")
        store.bind_session(b["id"], "s1")
        reply = await daemon._handle_board_close_request({
            "type": "board_close_request", "port": 51000, "note": "done"})
        assert reply["ok"] is False
        assert "more than one" in reply["detail"]
        assert store.get(a["id"])["column_name"] == "in_progress"
        assert store.get(b["id"])["column_name"] == "in_progress"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_a_card_already_closed_does_not_make_the_session_ambiguous(tmp_path):
    """Done cards are not counted when looking for the session's card, so a
    session that closed one and is still bound to it is not called ambiguous
    the next time it is asked about."""
    daemon, store = _board_daemon(tmp_path)
    try:
        _attach(daemon, port=51000, pid=4242, session_id="s1")
        daemon._session_states["s1"] = {"pid": 4242, "last_event": 0}
        first, _ = store.create({"title": "one", "project": "bob"})
        second, _ = store.create({"title": "two", "project": "bob"})
        store.bind_session(first["id"], "s1")
        store.declare_done(first["id"], "s1", "already finished")
        store.bind_session(second["id"], "s1")
        reply = await daemon._handle_board_close_request({
            "type": "board_close_request", "port": 51000, "note": "and this"})
        assert reply["ok"] is True
        assert reply["card_id"] == second["id"]
    finally:
        store.close()


@pytest.mark.asyncio
async def test_a_second_close_reads_as_idempotent_not_as_a_broken_binding(tmp_path):
    """A `/ship` retry, or an agent re-confirming, must not be told that no
    card names it. Done cards are filtered out before counting, so the second
    call matches nothing — and "no card on Dark Army's board names this session"
    reads as a scope or attribution failure, which invites the model to report
    a broken binding when the truth is that its card is already closed."""
    daemon, store = _board_daemon(tmp_path)
    try:
        _attach(daemon, port=51000, pid=4242, session_id="s1")
        daemon._session_states["s1"] = {"pid": 4242, "last_event": 0}
        card, _ = store.create({"title": "mine", "project": "bob"})
        store.bind_session(card["id"], "s1")
        first = await daemon._handle_board_close_request({
            "type": "board_close_request", "port": 51000, "note": "PASS"})
        assert first["ok"] is True
        second = await daemon._handle_board_close_request({
            "type": "board_close_request", "port": 51000, "note": "PASS"})
        assert second["ok"] is False
        assert "already done" in second["detail"]
        assert "no card" not in second["detail"]
        # And the first close is untouched by the second attempt.
        assert store.get(card["id"])["close_note"] == "PASS"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_the_close_route_is_reachable_from_the_hook_socket(tmp_path):
    """`_handle_message` answers it, because a `tools/call` id left unanswered
    is a session waiting forever."""
    daemon, store = _board_daemon(tmp_path)
    try:
        reply = await daemon._handle_message(
            {"type": "board_close_request", "port": 0, "note": "done"})
        assert reply["ok"] is False
    finally:
        store.close()


def test_a_close_over_stdio_always_answers_its_id(monkeypatch):
    """The restructure of `call_tool` into per-name branches must not lose the
    contract the single-name version had: never raises, never hangs, always a
    result — an unanswered `tools/call` id is a session waiting forever."""
    server = _server()
    monkeypatch.setattr(server, "call_daemon", lambda msg: None)
    reply = _run(server, [{"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                           "params": {"name": "dark_army_close_card",
                                      "arguments": {"note": "done"}}}])[0]
    assert reply["id"] == 9
    assert reply["result"]["isError"] is True


def test_an_over_long_note_is_clamped_here_rather_than_refused(monkeypatch):
    """The schema is a description, not a gate, so the bound is applied to
    whatever the model actually emitted — and clamped rather than refused,
    which is what the store does too. A refusal on length would cost somebody
    a close over a field that is only Dark Army's record of their sentence."""
    from dark_army_daemon.board import MAX_CLOSE_NOTE_CHARS
    assert cs.CLOSE_TOOL["inputSchema"]["properties"]["note"]["maxLength"] \
        == MAX_CLOSE_NOTE_CHARS
    server = _server()
    sent = {}
    monkeypatch.setattr(server, "call_daemon",
                        lambda msg: sent.update(msg) or {"ok": True})
    result = server.call_tool({"name": "dark_army_close_card",
                               "arguments": {"note": "x" * 5000}})
    assert result["isError"] is False
    assert len(sent["note"]) == MAX_CLOSE_NOTE_CHARS


# ── the third verb: dark_army_attach_plan ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_an_attach_bob_cannot_attribute_is_refused(tmp_path):
    """The forgery case, `dark_army_close_card`'s test in this verb's terms: an
    unresolvable port attaches nothing and moves nothing."""
    daemon, store = _board_daemon(tmp_path)
    try:
        card, _ = store.create({"title": "a card", "project": "bob"})
        reply = await daemon._handle_board_attach_request({
            "type": "board_attach_request", "port": 51000,
            "path": "plans/x.md"})
        assert reply["ok"] is False
        assert "which session" in reply["detail"]
        got = store.get(card["id"])
        assert got["column_name"] == "prep"
        assert got["plan_path"] == ""
    finally:
        store.close()


def test_attach_plan_over_stdio_carries_the_port_and_the_path(monkeypatch):
    server = _server()
    server.port = 51000
    sent = {}
    monkeypatch.setattr(server, "call_daemon",
                        lambda msg: sent.update(msg) or {
                            "ok": True, "column": "backlog", "title": "t"})
    result = server.call_tool({"name": "dark_army_attach_plan",
                               "arguments": {"path": "plans/x.md"}})
    assert result["isError"] is False
    assert sent["type"] == "board_attach_request"
    assert sent["port"] == 51000
    assert sent["path"] == "plans/x.md"
    assert "card_id" not in sent
    assert "moved to Backlog" in result["content"][0]["text"]


def test_attach_plan_with_no_path_is_an_error_not_a_hang():
    result = _server().call_tool({"name": "dark_army_attach_plan", "arguments": {}})
    assert result["isError"] is True


def test_a_daemon_that_never_answers_an_attach_ends_in_is_error(monkeypatch):
    server = _server()
    monkeypatch.setattr(server, "call_daemon", lambda msg: None)
    result = server.call_tool({"name": "dark_army_attach_plan",
                               "arguments": {"path": "plans/x.md"}})
    assert result["isError"] is True
    assert "did not answer" in result["content"][0]["text"]


# ── the project's enrolment key ───────────────────────────────────────────────

def _plant_key(root, value, folder=".dark-army"):
    (root / folder).mkdir(parents=True, exist_ok=True)
    (root / folder / "key").write_text(value)


def test_attach_message_carries_the_project_key(tmp_path):
    _plant_key(tmp_path / "proj", "proj-key", folder=".dark-army")
    msg = cs.attach_message(port=1, pid=2, cwd=str(tmp_path / "proj"))
    assert msg["key"] == "proj-key"


def test_attach_message_carries_an_old_name_key(tmp_path):
    """A project holding only `.bob-companion/key` keeps its channel keyed for
    the read window."""
    _plant_key(tmp_path / "proj", "old-proj-key", folder=".bob-companion")
    msg = cs.attach_message(port=1, pid=2, cwd=str(tmp_path / "proj" / "sub"))
    assert msg["key"] == "old-proj-key"


def test_the_channel_key_prefers_the_new_name_when_both_exist(tmp_path):
    root = tmp_path / "proj"
    _plant_key(root, "new-key", folder=".dark-army")
    _plant_key(root, "old-key", folder=".bob-companion")
    assert cs.project_key(str(root)) == "new-key"


def test_the_key_walk_never_reads_the_home_state_directory(tmp_path,
                                                           monkeypatch):
    """`~/.dark-army` is Dark Army's own state directory and `~/.bob-companion`
    a link to it; both share a name with a project's key folder, so without
    the skip any session under $HOME would enrol the whole home directory.
    Planted under both names."""
    home = tmp_path / "home"
    _plant_key(home, "bobs-own-state", folder=".dark-army")
    _plant_key(home, "bobs-old-state", folder=".bob-companion")
    nested = home / "code" / "a-clone"
    nested.mkdir(parents=True)
    monkeypatch.setattr(cs.Path, "home", classmethod(lambda cls: home))
    assert cs.project_key(str(nested)) == ""


def test_notify_and_call_stamp_the_key_on_every_payload(monkeypatch):
    """One place, so no message shape can be forgotten — and on every message
    rather than once per session, because un-enrolment has to bite next."""
    server = cs.ChannelServer(stdin=io.StringIO(), stdout=io.StringIO())
    server._project_key = "the-key"
    sent = []
    # Both doors refuse: neither the network port nor the real private
    # socket in this account's home is ever dialled from a test.
    monkeypatch.setattr(cs.socket, "create_connection",
                        lambda *a, **kw: (_ for _ in ()).throw(OSError()))
    monkeypatch.setattr(server, "_connect_daemon",
                        lambda timeout: (_ for _ in ()).throw(OSError()))
    monkeypatch.setattr(server, "_keyed",
                        lambda msg: sent.append(msg) or {**msg, "key": "the-key"})
    server.notify_daemon({"type": "x"})
    server.call_daemon({"type": "y"})
    assert [m["type"] for m in sent] == ["x", "y"]
    assert cs.ChannelServer._keyed(server, {"type": "z"})["key"] == "the-key"


@pytest.mark.asyncio
async def test_an_unkeyed_attach_enters_no_registry(enforce_enrolment,
                                                    tmp_path):
    from dark_army_daemon import enrollment
    root = tmp_path / "proj"
    root.mkdir()
    assert enrollment.enroll(str(root))[0]
    key = (root / enrollment.KEY_RELATIVE).read_text().strip()
    daemon = _daemon()
    await daemon._handle_message(
        {"type": "channel_attach", "port": 52000, "pid": os.getpid(),
         "session_id": "s1", "cwd": str(root)})
    assert 52000 not in daemon._channels
    await daemon._handle_message(
        {"type": "channel_attach", "port": 52001, "pid": os.getpid(),
         "session_id": "s1", "cwd": str(root), "key": enrollment.mint()})
    assert 52001 not in daemon._channels
    await daemon._handle_message(
        {"type": "channel_attach", "port": 52002, "pid": os.getpid(),
         "session_id": "s1", "cwd": str(root), "key": key})
    assert 52002 in daemon._channels


def test_no_project_key_reaches_a_published_surface(enforce_enrolment,
                                                    tmp_path):
    """`test_the_secret_never_reaches_the_read_surface`'s shape, for the other
    secret: `/api/state` is ungated, so a key published there would be worse
    than no key at all."""
    from dark_army_daemon import enrollment
    root = tmp_path / "proj"
    root.mkdir()
    assert enrollment.enroll(str(root))[0]
    key = (root / enrollment.KEY_RELATIVE).read_text().strip()
    daemon = _daemon()
    daemon._session_states["s1"] = {"pid": 4242, "last_event": 0,
                                    "state": "idle", "key": key}
    published = json.dumps(daemon.detailed_snapshot(), default=str)
    published += json.dumps(daemon._permission_snapshot(), default=str)
    published += json.dumps(daemon.enrollment_snapshot(), default=str)
    published += json.dumps(daemon._build_board_state(), default=str)
    assert key not in published
    assert enrollment.digest(key) not in published


# ── one bad message never ends a loop ────────────────────────────────────────

def test_a_non_dict_daemon_line_is_dropped_before_it_can_raise():
    """`[1]` parses as JSON and then dies on `.get` — before the guard that
    AttributeError killed the listener thread and Dark Army could never push into
    the session again."""
    server = _server()
    server.secret = "s3cret"
    out = io.StringIO()
    server._out = out
    for line in ("[1]", "null", '"hi"', "3"):
        server.handle_daemon_message(line)  # must not raise
    assert out.getvalue() == ""


def test_a_bad_line_on_the_daemon_socket_does_not_kill_the_listener():
    """Over the real socket: garbage first, then an authentic event — the
    listener must survive the first to deliver the second."""
    import socket
    import threading
    import time as _t
    server = _server()
    port = server.listen()
    threading.Thread(target=server.serve_daemon, daemon=True).start()
    out = io.StringIO()
    server._out = out
    for line in ("[1]\n", "null\n"):
        with socket.create_connection(("127.0.0.1", port)) as sock:
            sock.sendall(line.encode())
    with socket.create_connection(("127.0.0.1", port)) as sock:
        sock.sendall((json.dumps(
            {"type": "event", "content": "still here",
             "secret": server.secret}) + "\n").encode())
    for _ in range(150):
        if out.getvalue():
            break
        _t.sleep(0.02)
    msg = json.loads(out.getvalue().splitlines()[0])
    assert msg["params"]["content"] == "still here"


def test_a_non_dict_stdio_message_is_answered_not_fatal():
    reply = _run(_server(), [[1]])[0]
    assert reply["error"]["code"] == -32600
    assert reply["id"] is None


def test_list_params_on_tools_call_refuse_rather_than_crash():
    reply = _run(_server(), [{"jsonrpc": "2.0", "id": 3,
                              "method": "tools/call", "params": [1]}])[0]
    assert reply["error"]["code"] == -32602
    assert reply["id"] == 3


def test_list_params_on_a_notification_are_dropped_in_silence():
    """A notification has no id, so there is nobody to answer — but the
    permission-request branch does `params.get` and must not die on a list."""
    out = _run(_server(), [{
        "jsonrpc": "2.0",
        "method": "notifications/claude/channel/permission_request",
        "params": [1]}])
    assert out == []


def test_a_crashing_handler_does_not_end_the_stdio_loop():
    """Belt beside the braces: even a handler bug on one message must not end
    `run()` — exiting silently severs the session's only channel."""
    lines = (json.dumps({"jsonrpc": "2.0", "id": 1, "method": "boom"}) + "\n"
             + json.dumps({"jsonrpc": "2.0", "id": 2,
                           "method": "tools/list"}) + "\n")
    server = cs.ChannelServer(stdin=io.StringIO(lines), stdout=io.StringIO())
    server.listen = lambda: 0
    server.serve_daemon = lambda: None
    server.heartbeat = lambda once=False: None
    out = io.StringIO()
    server._out = out
    real = server.handle_stdio_message
    calls = {"n": 0}

    def flaky(msg):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("handler bug")
        real(msg)

    server.handle_stdio_message = flaky
    server.run()  # returns at EOF; must not raise on the way
    replies = [json.loads(line) for line in out.getvalue().splitlines()]
    assert any(r.get("id") == 2 and "result" in r for r in replies)


@pytest.mark.asyncio
async def test_codex_two_exact_parents_add_and_attach_their_own_cards(tmp_path, monkeypatch):
    from tests.test_agents_poll import _navigation_daemon
    daemon, roots, processes = _navigation_daemon(tmp_path, monkeypatch)
    from dark_army_daemon.board import BoardStore
    store = BoardStore(tmp_path / "nav-board.db")
    store.connect()
    daemon._board = store
    plan = tmp_path / "work.md"
    plan.write_text("# Work")
    # Use the real test project as both roots' cwd, then republish exact proof.
    from types import MappingProxyType
    for r, p in zip(roots, processes):
        r.cwd = str(tmp_path)
        p.info["cwd"] = p.fresh["cwd"] = str(tmp_path)
    daemon._codex_navigation = MappingProxyType(codex_rollouts.resolve_navigation_proofs(
        codex_rollouts.project_title_roots(roots)))
    daemon._known_project_roots = lambda: {os.path.realpath(tmp_path)}
    daemon._board_projects = lambda: [{"name": "project", "root": os.path.realpath(tmp_path)}]
    daemon._agents_snapshot_cache = {"waiting": [
        {"session_id": r.session_id, "cwd": str(tmp_path), "project": "project", "provider": "codex"}
        for r in roots]}
    try:
        for i, (root, process) in enumerate(zip(roots, processes)):
            port = 51000 + i
            daemon._channels[port] = {"host": cs.HOST_CODEX, "pid": process.pid, "cwd": str(tmp_path)}
            added = await daemon._handle_board_card_request({"port": port, "title": "Own plan", "tool": "codex"})
            assert added["ok"], added
            assert store.get(added["card_id"])["author"] == root.session_id
            attached = await daemon._handle_board_attach_request({"port": port, "path": str(plan)})
            assert attached["ok"], attached
            assert attached["card_id"] == added["card_id"]
        assert all(card["column_name"] == "backlog" for card in store.cards())
        assert daemon._channel_for_session(roots[0].session_id) is None
    finally:
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["wrong_parent", "pid_reused", "child", "ambiguous", "registry", "root_replaced", "timeout"])
async def test_codex_board_exact_binding_refuses_stale_or_ambiguous_parent(tmp_path, monkeypatch, fault):
    from tests.test_agents_poll import _navigation_daemon
    from dataclasses import replace
    daemon, roots, processes = _navigation_daemon(tmp_path, monkeypatch)
    daemon._channels[51000] = {"host": cs.HOST_CODEX, "pid": 701, "cwd": roots[0].cwd}
    if fault == "wrong_parent":
        daemon._channels[51000]["pid"] = 601
    elif fault == "pid_reused":
        processes[0].fresh["create_time"] += 100
    elif fault == "child":
        roots[0].parent_thread_id = "parent"
    elif fault == "ambiguous":
        processes[1]._open_file_reads = [(roots[0].path, roots[1].path)]
    elif fault == "timeout":
        async def timeout(_captured, **_kwargs):
            raise asyncio.TimeoutError
        monkeypatch.setattr(daemon, "_fresh_navigation_targets", timeout)
    else:
        original = codex_rollouts.navigation_targets
        def changed(*args):
            found = original(*args)
            if fault == "registry":
                daemon._channels[51000] = dict(daemon._channels[51000])
            else:
                daemon._codex_records[roots[0].session_id] = replace(roots[0])
            return found
        monkeypatch.setattr(codex_rollouts, "navigation_targets", changed)
    assert await daemon._board_request_session_fresh(51000) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("verb", ["add", "attach"])
async def test_codex_board_guard_survives_downstream_awaits(tmp_path, monkeypatch, verb):
    from tests.test_agents_poll import _navigation_daemon
    from dark_army_daemon.board import BoardStore
    daemon, roots, _processes = _navigation_daemon(tmp_path, monkeypatch)
    store = BoardStore(tmp_path / "guard.db")
    store.connect()
    daemon._board = store
    daemon._channels[51000] = {"host": cs.HOST_CODEX, "pid": 701, "cwd": roots[0].cwd}
    guard = daemon._board_author_guard(51000)
    def swap(*args):
        daemon._channels[51000] = dict(daemon._channels[51000])
        return set()
    try:
        if verb == "add":
            monkeypatch.setattr(daemon, "_known_project_roots", swap)
            card, detail = await daemon.create_card({"title": "No write"}, _author_guard=guard)
            assert card is None and "which session" in detail
            assert store.cards() == []
        else:
            card, _ = store.create({"title": "Plan", "author": roots[0].session_id, "root": str(tmp_path)})
            def changed_path(*args):
                swap()
                return str(tmp_path / "plan.md"), ""
            monkeypatch.setattr(daemon, "_plan_path_refusal", changed_path)
            attached, detail = await daemon.attach_plan_by_session(roots[0].session_id, "plan.md", _author_guard=guard)
            assert attached is None and "which session" in detail
            assert store.get(card["id"])["column_name"] == "prep"
    finally:
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("parent", [601, 701, 702])
async def test_codex_board_cold_cache_requires_the_exact_native_parent(tmp_path, monkeypatch, parent):
    from tests.test_agents_poll import _navigation_daemon
    from types import MappingProxyType
    daemon, roots, _processes = _navigation_daemon(tmp_path, monkeypatch)
    if parent == 601:
        daemon._codex_records = {roots[0].session_id: roots[0]}
    daemon._codex_navigation = MappingProxyType({})
    daemon._channels[51000] = {"host": cs.HOST_CODEX, "pid": parent, "cwd": roots[0].cwd}
    result = await daemon._board_request_session_fresh(51000)
    assert result == (None if parent == 601 else roots[parent - 701].session_id)
    assert daemon._codex_navigation == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["duplicate_holders", "failed_scan", "denied_files", "denied_identity", "conflicting_resume", "healthy_absence", "healthy_empty"])
async def test_codex_board_cold_cache_keeps_conflict_distinct_from_absence(tmp_path, monkeypatch, fault):
    from tests.test_agents_poll import _navigation_daemon
    from tests.test_codex_rollouts import _native_holder
    from types import MappingProxyType
    daemon, roots, processes = _navigation_daemon(tmp_path, monkeypatch)
    root = roots[0]
    daemon._codex_records = {root.session_id: root}
    daemon._codex_navigation = MappingProxyType({})
    daemon._channels[51000] = {"host": cs.HOST_CODEX, "pid": 601, "cwd": root.cwd}
    if fault == "duplicate_holders":
        processes[1]._open_file_reads = [(root.path,)]
    elif fault == "failed_scan":
        monkeypatch.setattr(codex_rollouts, "_process_snapshot", lambda: None)
    elif fault == "denied_files":
        processes[0]._open_file_reads = [PermissionError("denied")]
    elif fault == "denied_identity":
        processes.append(_native_holder(799, root.path, "ttys099", denied=("create_time",)))
    elif fault == "conflicting_resume":
        processes[0].info["cmdline"] = processes[0].fresh["cmdline"] = ["codex", "resume", "someone-else"]
    elif fault == "healthy_absence":
        processes[0]._open_file_reads = [()]
    else:
        processes.clear()
    expected = root.session_id if fault.startswith("healthy_") else None
    assert await daemon._board_request_session_fresh(51000) == expected
    assert daemon._codex_navigation == {}


@pytest.mark.asyncio
async def test_codex_close_binds_to_its_own_card_by_fresh_proof(tmp_path, monkeypatch):
    """Codex may now say a card is finished, and the claim is attributed the
    same way `dark_army_add_card` and `dark_army_attach_plan` are: the fresh exact native
    root-journal holder. The weaker cwd fallback would let the stronger claim
    rest on the thinner evidence."""
    from tests.test_agents_poll import _navigation_daemon
    from dark_army_daemon.board import BoardStore
    from types import MappingProxyType
    daemon, roots, processes = _navigation_daemon(tmp_path, monkeypatch)
    store = BoardStore(tmp_path / "close-board.db")
    store.connect()
    daemon._board = store
    for r, p in zip(roots, processes):
        r.cwd = str(tmp_path)
        p.info["cwd"] = p.fresh["cwd"] = str(tmp_path)
    daemon._codex_navigation = MappingProxyType(codex_rollouts.resolve_navigation_proofs(
        codex_rollouts.project_title_roots(roots)))
    try:
        card, detail = store.create({"title": "Codex work",
                                     "root": str(tmp_path), "tool": "codex"})
        assert card is not None, detail
        store.update(card["id"], {"column_name": "in_progress",
                                  "session_id": roots[0].session_id,
                                  "link_state": "live"})
        # The wrong parent pid cannot close it.
        daemon._channels[51000] = {"host": cs.HOST_CODEX, "pid": 601,
                                   "cwd": roots[0].cwd}
        refused = await daemon._handle_board_close_request(
            {"port": 51000, "note": "done"})
        assert refused["ok"] is False
        assert store.get(card["id"])["column_name"] == "in_progress"
        # The exact holder can.
        daemon._channels[51000] = {"host": cs.HOST_CODEX, "pid": 701,
                                   "cwd": roots[0].cwd}
        closed = await daemon._handle_board_close_request(
            {"port": 51000, "note": "all of it"})
        assert closed["ok"], closed
        moved = store.get(card["id"])
        assert moved["column_name"] == "done"
        assert moved["closed_by"] == roots[0].session_id
    finally:
        store.close()


@pytest.mark.asyncio
async def test_codex_close_asks_the_fresh_resolver_not_the_fallback(tmp_path, monkeypatch):
    """The line the widening is paid for, pinned on its own.

    `test_codex_close_binds_to_its_own_card_by_fresh_proof` above proves the
    right session closes the card, but the weaker `_board_request_session`
    would often answer the same way — so a revert of that one line would leave
    it green. Here the two resolvers deliberately disagree: the fresh one names
    the session the card is bound to, the fallback names a stranger.
    """
    from tests.test_agents_poll import _navigation_daemon
    from dark_army_daemon.board import BoardStore
    daemon, _roots, _processes = _navigation_daemon(tmp_path, monkeypatch)
    store = BoardStore(tmp_path / "resolver-board.db")
    store.connect()
    daemon._board = store
    try:
        card, detail = store.create({"title": "Codex work",
                                     "root": str(tmp_path), "tool": "codex"})
        assert card is not None, detail
        store.update(card["id"], {"column_name": "in_progress",
                                  "session_id": "fresh-holder",
                                  "link_state": "live"})
        asked = []

        async def fresh(port):
            asked.append(port)
            return "fresh-holder"

        def fallback(port):
            asked.append("fallback")
            return "cwd-guess"

        monkeypatch.setattr(daemon, "_board_request_session_fresh", fresh)
        monkeypatch.setattr(daemon, "_board_request_session", fallback)
        daemon._channels[51000] = {"host": cs.HOST_CODEX, "pid": 701,
                                   "cwd": str(tmp_path)}
        closed = await daemon._handle_board_close_request(
            {"port": 51000, "note": "done"})
        assert closed["ok"], closed
        assert asked == [51000], "the close must spend the fresh resolver alone"
        assert store.get(card["id"])["closed_by"] == "fresh-holder"
    finally:
        store.close()


# --- the hook door: the private socket, by the one address rule -------------

class _DoorStandIn:
    """A one-line-per-connection stand-in for the daemon's hook door, on a
    Unix path or a loopback port, answering `reply` (None: no line)."""

    def __init__(self, *, path=None, reply=None):
        import socket
        import threading
        if path is not None:
            self._srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._srv.bind(path)
        else:
            self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._srv.bind(("127.0.0.1", 0))
            self.port = self._srv.getsockname()[1]
        self._srv.listen(4)
        self._srv.settimeout(0.2)
        self.reply = reply
        self.received = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join(timeout=5.0)
        self._srv.close()

    def _serve(self):
        import socket
        while not self._stop.is_set():
            try:
                conn, _ = self._srv.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with conn:
                conn.settimeout(3.0)
                line = conn.makefile("r", encoding="utf-8").readline()
                if line.strip():
                    self.received.append(json.loads(line))
                if self.reply is not None:
                    conn.sendall(json.dumps(self.reply).encode("utf-8") + b"\n")


@pytest.fixture
def short_dir():
    # AF_UNIX paths are capped near 104 bytes; tmp_path is longer.
    import shutil
    import tempfile
    path = tempfile.mkdtemp(prefix="ch-", dir="/tmp")
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _door_server(**kw):
    server = cs.ChannelServer(stdin=io.StringIO(), stdout=io.StringIO(), **kw)
    server._project_key = "the-key"
    return server


def test_notify_daemon_delivers_over_the_private_socket(short_dir):
    path = f"{short_dir}/hook.sock"
    with _DoorStandIn(path=path) as door:
        server = _door_server(daemon_sock=path, daemon_port=0)
        assert server.notify_daemon({"type": "channel_attach"}) is True
        for _ in range(100):
            if door.received:
                break
            import time
            time.sleep(0.01)
    assert door.received == [{"type": "channel_attach", "key": "the-key"}]


def test_call_daemon_reads_its_reply_over_the_private_socket(short_dir):
    path = f"{short_dir}/hook.sock"
    with _DoorStandIn(path=path, reply={"ok": True, "card": "c1"}) as door:
        server = _door_server(daemon_sock=path, daemon_port=0)
        assert server.call_daemon({"type": "board_add"}) == {"ok": True, "card": "c1"}
    assert door.received[0]["type"] == "board_add"


def test_an_explicit_port_keeps_the_channel_on_tcp(short_dir, monkeypatch):
    """A channel whose environment names a port (a session from before the
    socket) stays on the bridge, and never tries the socket as well."""
    monkeypatch.setattr(cs, "default_hook_socket",
                        lambda: pytest.fail("the default socket was dialled"))
    with _DoorStandIn(reply={"ok": True}) as door:
        server = _door_server(daemon_sock="", daemon_port=door.port)
        assert server.call_daemon({"type": "board_add"}) == {"ok": True}
    assert [m["type"] for m in door.received] == ["board_add"]


def test_with_nothing_named_the_channel_dials_the_default_socket(short_dir, monkeypatch):
    path = f"{short_dir}/hook.sock"
    monkeypatch.setattr(cs, "default_hook_socket", lambda: path)
    with _DoorStandIn(path=path, reply={"ok": True}):
        server = _door_server(daemon_sock="", daemon_port=0)
        assert server.call_daemon({"type": "board_add"}) == {"ok": True}


def test_the_quiet_socket_reaches_nobody_and_raises_nothing(monkeypatch):
    monkeypatch.setattr(cs, "default_hook_socket",
                        lambda: pytest.fail("the default socket was dialled"))
    server = _door_server(daemon_sock="/dev/null", daemon_port=1)
    assert server.notify_daemon({"type": "x"}) is False
    assert server.call_daemon({"type": "y"}) is None


def test_the_address_rule_is_exclusive():
    assert cs.daemon_address("/a/hook.sock", 19873) == ("unix", "/a/hook.sock")
    assert cs.daemon_address("", 4242) == ("tcp", 4242)
    assert cs.daemon_address("", 0) == ("unix", cs.default_hook_socket())
    assert cs.default_hook_socket().endswith("/.dark-army/hook.sock")


def test_the_attach_tool_says_how_a_batch_plan_names_its_card():
    """Several cards in one session: the plan's own `- **Card:**` header says
    which card, and the schema still takes a path and nothing else."""
    assert set(cs.ATTACH_TOOL["inputSchema"]["properties"]) == {"path"}
    text = cs.ATTACH_TOOL["description"]
    assert "several cards at once" in text
    assert "`- **Card:** <id>`" in text


# --- `depends_on`: the cards a card filed without a plan waits on -----------


def test_the_card_tool_can_name_what_it_depends_on_and_nothing_more():
    schema = cs.CARD_TOOL["inputSchema"]["properties"]
    assert schema["depends_on"]["type"] == "array"
    assert schema["depends_on"]["maxItems"] == 8
    assert schema["depends_on"]["items"]["maxLength"] == 200
    assert "same project" in schema["depends_on"]["description"]
    assert "depends_on" not in cs.CARD_TOOL["inputSchema"]["required"]


def test_depends_on_reaches_the_daemon_clamped(monkeypatch):
    server = _server()
    server.port = 51000
    sent = []
    monkeypatch.setattr(server, "call_daemon",
                        lambda msg: sent.append(msg) or {"ok": True, "project": "p"})
    refs = [f"card {i}" for i in range(12)] + ["", "x" * 500]
    server.call_tool({"name": "dark_army_add_card",
                      "arguments": {"title": "t", "depends_on": refs}})
    assert sent[0]["depends_on"] == [f"card {i}" for i in range(8)]
    sent.clear()
    server.call_tool({"name": "dark_army_add_card",
                      "arguments": {"title": "t", "depends_on": "x" * 500}})
    assert sent[0]["depends_on"] == ["x" * 200]
    sent.clear()
    server.call_tool({"name": "dark_army_add_card", "arguments": {"title": "t"}})
    assert sent[0]["depends_on"] == []


def test_a_list_the_daemon_could_not_save_is_said_after_the_card_landed(
        monkeypatch):
    server = _server()
    monkeypatch.setattr(server, "call_daemon", lambda msg: {
        "ok": True, "project": "p",
        "dependencies_detail": 'no card in this project is called "ghost"'})
    result = server.call_tool({"name": "dark_army_add_card",
                               "arguments": {"title": "t",
                                             "depends_on": ["ghost"]}})
    assert result["isError"] is False
    text = result["content"][0]["text"]
    assert "Prep column" in text
    assert 'not saved: no card in this project is called "ghost"' in text


@pytest.mark.asyncio
async def test_add_card_links_the_cards_it_names_by_id_and_by_title(tmp_path):
    daemon, store, root = _plan_filing_daemon(tmp_path)
    real = os.path.realpath(root)
    try:
        first, _ = store.create({"title": "The Foundation", "project": "project",
                                 "root": real, "column_name": "backlog"})
        second, _ = store.create({"title": "finished work", "project": "project",
                                  "root": real, "column_name": "done"})
        reply = await daemon._handle_board_card_request({
            "type": "board_card_request", "port": 51000,
            "title": "A follow-up", "tool": "codex",
            "depends_on": ["  the foundation ", second["id"]],
        })
        assert reply["ok"] is True, reply
        assert reply["dependencies_detail"] == ""
        card = store.get(reply["card_id"])
        from dark_army_daemon.board import parse_ids
        assert parse_ids(card["blocked_by"]) == [first["id"], second["id"]]
        assert card["column_name"] == "prep"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_add_card_files_the_card_and_says_why_a_link_was_refused(tmp_path):
    """A title naming nothing, two cards, or a card in another project
    refuses the whole list — never a partial one — and the card is filed
    regardless, with the trouble in `dependencies_detail`."""
    daemon, store, root = _plan_filing_daemon(tmp_path)
    real = os.path.realpath(root)
    try:
        store.create({"title": "twin", "project": "project", "root": real})
        store.create({"title": "twin", "project": "project", "root": real})
        store.create({"title": "mine", "project": "project", "root": real})
        foreign, _ = store.create({"title": "foreign", "project": "other",
                                   "root": str(tmp_path / "other")})
        cases = [
            (["mine", "ghost"], 'no card in this project is called "ghost"'),
            (["twin"], '"twin" names more than one card in this project'),
            ([foreign["id"]], f'no card in this project is called "{foreign["id"]}"'),
            (["foreign"], 'no card in this project is called "foreign"'),
        ]
        for refs, words in cases:
            before = len(store.cards())
            reply = await daemon._handle_board_card_request({
                "type": "board_card_request", "port": 51000,
                "title": f"filed with {refs}", "tool": "codex",
                "depends_on": refs,
            })
            assert reply["ok"] is True, reply
            assert reply["dependencies_detail"].startswith(words), reply
            assert len(store.cards()) == before + 1
            assert store.get(reply["card_id"])["blocked_by"] == ""
    finally:
        store.close()


# --- dark_army_next_card: a batch session moving on ---------------------------


def test_the_next_tool_takes_no_properties_at_all():
    """`CLOSE_TOOL`'s security property in this verb's terms: the port is the
    addressing and the scope is the defence. A `card_id` here would turn the
    socket into a way to bind any session to any card."""
    assert cs.NEXT_TOOL["inputSchema"]["properties"] == {}
    assert "required" not in cs.NEXT_TOOL["inputSchema"]
    assert cs.NEXT_TOOL_NAME == "dark_army_next_card"
    # And the close schema is still the note alone.
    assert set(cs.CLOSE_TOOL["inputSchema"]["properties"]) == {"note"}
    assert "dark_army_next_card" in cs.CLOSE_TOOL["description"]


def test_the_next_call_carries_the_port_and_never_a_card(monkeypatch):
    server = _server()
    server.port = 51000
    sent = []
    monkeypatch.setattr(server, "call_daemon", lambda msg: sent.append(msg) or {
        "ok": True, "detail": "now on card 2 of 3", "card_id": "c2",
        "title": "Second", "plan_path": "/p/plans/2.md", "rank": 2,
        "size": 3})
    result = server.call_tool({"name": "dark_army_next_card",
                               "arguments": {"card_id": "someone-else"}})
    assert result.get("isError") is not True, result
    assert len(sent) == 1
    assert sent[0]["type"] == "board_next_request"
    assert sent[0]["port"] == 51000
    assert "card_id" not in sent[0]
    text = result["content"][0]["text"]
    assert text == "Now on card 2 of 3: Second — Plan: /p/plans/2.md"


def test_the_next_call_says_when_the_batch_is_finished(monkeypatch):
    server = _server()
    monkeypatch.setattr(server, "call_daemon", lambda msg: {
        "ok": True, "detail": "the batch is finished — no card left",
        "remaining": 0})
    result = server.call_tool({"name": "dark_army_next_card", "arguments": {}})
    assert result.get("isError") is not True
    assert result["content"][0]["text"] == \
        "No card left; the batch is finished."


def test_a_next_refusal_comes_back_in_the_daemons_words(monkeypatch):
    server = _server()
    monkeypatch.setattr(server, "call_daemon", lambda msg: {
        "ok": False, "detail": "this session is not working a batch card"})
    result = server.call_tool({"name": "dark_army_next_card", "arguments": {}})
    assert result["isError"] is True
    assert result["content"][0]["text"] == \
        "this session is not working a batch card"


def test_codex_may_move_on_to_its_next_batch_card(monkeypatch):
    """A board row, typed at nobody — the close verb's argument."""
    server = cs.ChannelServer(host=cs.HOST_CODEX)
    sent = []
    monkeypatch.setattr(server, "call_daemon", lambda msg: sent.append(msg) or {
        "ok": True, "detail": "done", "remaining": 0})
    result = server.call_tool({"name": "dark_army_next_card", "arguments": {}})
    assert result.get("isError") is not True, result
    assert len(sent) == 1 and "card_id" not in sent[0]
    names = [t["name"] for t in cs.tools_for_host(cs.HOST_CODEX)]
    assert "dark_army_next_card" in names
