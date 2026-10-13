"""The helper bot's presses carry a one-time mark.

`relay_bot._marked` mints one mark per action, outside every retry, so a
resend of the same press is answered once by the Mac's receipt ledger.
"""
import json
import re

import pytest

from dark_army_daemon import relay_bot
from dark_army_daemon import rebuild_state
from dark_army_daemon.api_server import ApiServer
from dark_army_daemon.command_receipts import CommandReceipts

HEX16 = re.compile(r"^[0-9a-f]{16}$")
OK_FRAME = {"kind": "res", "body": {"status": 200, "body": "{}"}}


class _FakeLine:
    def __init__(self):
        self.sent = []

    async def request(self, kind, body):
        self.sent.append((kind, body))
        return OK_FRAME


class _StubDaemon:
    _agents_poller = None

    def __init__(self):
        self.calls = 0

    def request_rebuild(self, token=""):
        self.calls += 1
        return True, rebuild_state.REBUILD_STARTED

    def rebuild_snapshot(self):
        return {"available": True, "label": "Rebuild & Reload",
                "rebuilding": False}


def _away(monkeypatch):
    from dark_army_daemon import devices, relay
    monkeypatch.setattr(relay, "lease_valid", lambda _d: False)
    monkeypatch.setattr(devices, "is_bot", lambda _d: True)
    monkeypatch.setattr(relay, "materialise_bot_grants", lambda _d: None,
                        raising=False)
    monkeypatch.setattr(relay, "bot_grant_valid", lambda _d, _side: True)


def _server(daemon):
    server = ApiServer(daemon)
    server.token = "desk-secret"
    server.session_token = "session-secret"
    return server


@pytest.mark.asyncio
async def test_every_bot_action_carries_a_fresh_mark():
    line = _FakeLine()
    args = {"action": "dismiss", "id": "x"}
    await relay_bot._call_tool(line, "act", args)
    await relay_bot._call_tool(line, "act", args)
    marks = [body["command_token"] for _kind, body in line.sent]
    for mark in marks:
        assert CommandReceipts.usable(mark) == mark
        assert HEX16.match(mark)
    assert marks[0] != marks[1]
    assert "command_token" not in args


@pytest.mark.asyncio
async def test_a_callers_mark_is_kept_and_a_bad_one_replaced():
    line = _FakeLine()
    for given in ("0123456789abcdef", "x", 123):
        await relay_bot._call_tool(
            line, "act", {"action": "dismiss", "command_token": given})
    kept, short, number = (b["command_token"] for _k, b in line.sent)
    assert kept == "0123456789abcdef"
    assert HEX16.match(short) and HEX16.match(number)


class _FlakySession:
    """First request: recorded, then the reply is lost."""

    def __init__(self, bodies, run=None):
        self.bodies = bodies
        self.run = run

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def request(self, kind, body):
        self.bodies.append(body)
        answer = None
        if self.run is not None:
            answer = await self.run(kind, dict(body))
        if len(self.bodies) == 1:
            raise relay_bot.BotError("the Mac did not answer")
        if answer is not None:
            return answer
        return OK_FRAME


@pytest.mark.asyncio
async def test_the_lines_retry_reuses_the_mark(monkeypatch):
    bodies = []
    monkeypatch.setattr(relay_bot, "BotSession",
                        lambda: _FlakySession(bodies))
    await relay_bot._call_tool(relay_bot._Line(), "act",
                               {"action": "rebuild_app"})
    assert len(bodies) == 2
    assert bodies[0]["command_token"] == bodies[1]["command_token"]
    assert HEX16.match(bodies[0]["command_token"])


def test_cmd_action_marks_its_press(monkeypatch):
    seen = []

    async def fake(kind, body):
        seen.append((kind, body))
        return OK_FRAME

    monkeypatch.setattr(relay_bot, "_one_request", fake)
    assert relay_bot.cmd_action('{"action": "rebuild_app"}') == 0
    assert seen[0][0] == "action"
    assert HEX16.match(seen[0][1]["command_token"])


@pytest.mark.asyncio
async def test_a_read_carries_no_mark():
    line = _FakeLine()
    await relay_bot._call_tool(line, "picture", {})
    kind, body = line.sent[0]
    assert kind == "state" and "command_token" not in body


async def _two_sends(monkeypatch):
    _away(monkeypatch)
    daemon = _StubDaemon()
    server = _server(daemon)
    answers = []

    async def run(kind, body):
        status, _ctype, text = await server._remote_run(kind, body, "bot-1")
        answers.append((status, text))
        shown = text.decode() if isinstance(text, bytes) else text
        return {"kind": "res", "body": {"status": status, "body": shown}}

    bodies = []
    monkeypatch.setattr(relay_bot, "BotSession",
                        lambda: _FlakySession(bodies, run))
    await relay_bot._call_tool(relay_bot._Line(), "act",
                               {"action": "rebuild_app"})
    return daemon, answers


@pytest.mark.asyncio
async def test_two_sends_of_one_bot_press_rebuild_once(monkeypatch):
    daemon, answers = await _two_sends(monkeypatch)
    assert len(answers) == 2
    assert daemon.calls == 1
    assert answers[0] == answers[1]
    assert json.loads(answers[0][1])["detail"] == rebuild_state.REBUILD_STARTED


@pytest.mark.asyncio
async def test_without_a_mark_two_sends_rebuild_twice(monkeypatch):
    monkeypatch.setattr(relay_bot, "_marked", lambda body: dict(body))
    daemon, _answers = await _two_sends(monkeypatch)
    assert daemon.calls == 2
