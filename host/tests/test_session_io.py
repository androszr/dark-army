# host/tests/test_session_io.py
"""The router's fork, and that every former call site goes through it.

`session_io` asks `ptyhost.owns` first and falls through to the
identically-named `vscode_reveal` function otherwise, forwarding the
caller's own argument shape. The grep-shaped case at the end is the whole
feature's safety net: a call site left on `vscode_reveal` makes a Dark Army-run
session lose typing, closing, `/compact` and question answering silently.
"""

from __future__ import annotations

import asyncio
import pathlib
import re

import pytest

from dark_army_daemon import ptyhost, session_io, vscode_reveal

DAEMON_DIR = pathlib.Path(__file__).resolve().parents[1] / "dark_army_daemon"


# --- the fork -----------------------------------------------------------------


def test_can_send_text_is_true_for_a_pty_without_asking_vscode(monkeypatch):
    asked = []
    monkeypatch.setattr(ptyhost, "owns", lambda pid: "pty-1" if pid == 42 else None)
    monkeypatch.setattr(vscode_reveal, "can_send_text",
                        lambda pid, tty="": asked.append(pid) or False)
    assert session_io.can_send_text(42) is True
    assert asked == []
    assert session_io.can_send_text(43) is False
    assert asked == [43]


def test_can_send_text_forwards_the_callers_shape(monkeypatch):
    """Tests elsewhere stub `can_send_text` as a one-argument lambda."""
    monkeypatch.setattr(ptyhost, "owns", lambda pid: None)
    monkeypatch.setattr(vscode_reveal, "can_send_text", lambda pid: pid == 7)
    assert session_io.can_send_text(7) is True
    seen = []
    monkeypatch.setattr(vscode_reveal, "can_send_text",
                        lambda pid, tty="": seen.append((pid, tty)) or True)
    session_io.can_send_text(7, "ttys001")
    assert seen == [(7, "ttys001")]


def test_can_close_terminal_forks_the_same_way(monkeypatch):
    monkeypatch.setattr(ptyhost, "owns", lambda pid: "pty-1" if pid == 42 else None)
    monkeypatch.setattr(vscode_reveal, "can_close_terminal", lambda pid: pid == 8)
    assert session_io.can_close_terminal(42) is True
    assert session_io.can_close_terminal(8) is True
    assert session_io.can_close_terminal(9) is False


def test_send_text_types_into_the_pty_when_owned(monkeypatch):
    typed = []
    monkeypatch.setattr(ptyhost, "owns", lambda pid: "pty-1" if pid == 42 else None)
    monkeypatch.setattr(ptyhost, "send",
                        lambda handle, text, newline=True: typed.append((handle, text, newline))
                        or {"matched": True, "sent": True, "terminalName": "cat", "matchedBy": "pty"})

    async def boom(*a, **k):
        raise AssertionError("vscode was asked for a pty session")
    monkeypatch.setattr(vscode_reveal, "send_text", boom)
    result = asyncio.run(session_io.send_text(42, "", "hello"))
    assert result["sent"] is True and result["matchedBy"] == "pty"
    assert typed == [("pty-1", "hello", True)]
    asyncio.run(session_io.send_text(42, "", "1", newline=False))
    assert typed[-1] == ("pty-1", "1", False)


def test_send_text_falls_through_with_the_callers_positional_shape(monkeypatch):
    """The stubs every other suite uses: `async def _send(pid, tty, text,
    newline=True)`. `newline=` is passed only when it is not the default."""
    monkeypatch.setattr(ptyhost, "owns", lambda pid: None)
    calls = []

    async def _send(pid, tty, text, newline=True):
        calls.append((pid, tty, text, newline))
        return {"sent": True, "matched": True}
    monkeypatch.setattr(vscode_reveal, "send_text", _send)
    assert asyncio.run(session_io.send_text(5, "", "x"))["sent"] is True
    assert asyncio.run(session_io.send_text(5, "t", "y", newline=False))["sent"] is True
    assert calls == [(5, "", "x", True), (5, "t", "y", False)]

    async def _three(pid, tty, text):
        calls.append(("three", pid))
        return None
    monkeypatch.setattr(vscode_reveal, "send_text", _three)
    assert asyncio.run(session_io.send_text(6, "", "z")) is None
    assert calls[-1] == ("three", 6)


def test_send_text_with_nothing_to_type_is_none_and_asks_nobody(monkeypatch):
    monkeypatch.setattr(ptyhost, "owns", lambda pid: (_ for _ in ()).throw(AssertionError))
    assert asyncio.run(session_io.send_text(5, "", "")) is None


def test_close_terminal_forks(monkeypatch):
    closed = []

    async def _pty_close(handle):
        closed.append(handle)
        return {"matched": True, "terminalName": "cat", "matchedBy": "pty"}

    async def _vs_close(pid, tty):
        closed.append(("vscode", pid, tty))
        return {"matched": True, "terminalName": "zsh"}
    monkeypatch.setattr(ptyhost, "owns", lambda pid: "pty-1" if pid == 42 else None)
    monkeypatch.setattr(ptyhost, "close", _pty_close)
    monkeypatch.setattr(vscode_reveal, "close_terminal", _vs_close)
    assert asyncio.run(session_io.close_terminal(42, ""))["matchedBy"] == "pty"
    assert asyncio.run(session_io.close_terminal(43, "ttys1"))["terminalName"] == "zsh"
    assert closed == ["pty-1", ("vscode", 43, "ttys1")]


def test_the_module_looks_vscode_reveal_up_at_call_time():
    """A `from .vscode_reveal import send_text` would freeze the function
    before any test could stub it — and before `_invalidate_can_send_cache`
    could matter."""
    src = (DAEMON_DIR / "session_io.py").read_text()
    assert "from .vscode_reveal import" not in src
    assert "from .ptyhost import" not in src
    assert src.count("def send_text") == 1
    assert src.count("def close_terminal") == 1
    assert src.count("def can_send_text") == 1
    assert src.count("def can_close_terminal") == 1


# --- every former call site goes through the router ---------------------------


NAMES = ("send_text", "can_send_text", "close_terminal", "can_close_terminal")


def test_no_module_outside_the_router_reaches_vscode_reveal_for_the_four_names():
    """Comments and docstrings included: the acceptance grep counts prose
    too, and a stale sentence naming the old route is how the next reader
    puts a call back."""
    pattern = re.compile(r"vscode_reveal\.(" + "|".join(NAMES) + r")\b")
    offenders = []
    for path in sorted(DAEMON_DIR.glob("*.py")):
        if path.name in ("vscode_reveal.py", "session_io.py"):
            continue
        for n, line in enumerate(path.read_text().splitlines(), 1):
            if "close_refinement" in line:
                continue
            if pattern.search(line):
                offenders.append(f"{path.name}:{n}: {line.strip()}")
    assert offenders == [], "\n".join(offenders)


def test_the_typing_sites_name_the_router():
    """Each function the plan named still says `send_text` (its own source
    pin elsewhere) and says it through `session_io`."""
    import inspect
    from dark_army_daemon.daemon import BobDaemon
    for fn in (BobDaemon.wrap_up_session, BobDaemon.low_priority_session,
               BobDaemon._type_answer_burst, BobDaemon._flush_auto_compacts,
               BobDaemon.message_card):
        src = inspect.getsource(fn)
        assert "session_io.send_text(" in src, fn.__name__
    for fn in (BobDaemon._close_session_terminal, BobDaemon._close_terminal_by_pid):
        assert "session_io.close_terminal(" in inspect.getsource(fn), fn.__name__
    src = inspect.getsource(BobDaemon._enrich_agent_stubs)
    assert src.count("session_io.can_send_text(") >= 2
    assert "session_io.can_close_terminal(" in src
    assert "session_io.can_send_text(" in inspect.getsource(BobDaemon._decide_auto_compacts)


def test_the_refinement_close_stays_on_vscode_reveal():
    """Out of scope by decision: the Codex receipt machinery is not moved."""
    src = (DAEMON_DIR / "daemon.py").read_text()
    assert "vscode_reveal.close_refinement_terminal(" in src
    assert "close_refinement_terminal(" not in (DAEMON_DIR / "session_io.py").read_text()


# --- the capability gates through the daemon ----------------------------------


def _enrich(monkeypatch, stub, *, owned):
    from dark_army_daemon.daemon import BobDaemon
    monkeypatch.setattr(ptyhost, "owns", lambda pid: "pty-1" if owned else None)
    monkeypatch.setattr(vscode_reveal, "can_send_text", lambda pid: False)
    monkeypatch.setattr(vscode_reveal, "can_close_terminal", lambda pid, tty="": False)
    daemon = BobDaemon(headless=True)
    out = daemon._enrich_agent_stubs([dict(stub)])
    rows = [r for bucket in out.values() for r in bucket
            if r.get("session_id") == stub["session_id"]]
    assert len(rows) == 1
    return rows[0]


WAITING = {"session_id": "s1", "pid": 4242, "_category": "waiting", "cwd": ""}


def test_a_pty_session_publishes_can_type_and_can_close_without_vscode(monkeypatch):
    row = _enrich(monkeypatch, WAITING, owned=True)
    assert row["can_type"] is True
    assert row["can_close"] is True


def test_a_session_nobody_owns_stays_false(monkeypatch):
    row = _enrich(monkeypatch, WAITING, owned=False)
    assert row["can_type"] is False
    assert row["can_close"] is False
    assert row["own_terminal"] is False
    assert row["can_terminal_input"] is False


@pytest.mark.asyncio
async def test_the_answer_burst_lands_on_a_pty(monkeypatch):
    from dark_army_daemon.daemon import BobDaemon
    from tests.test_multi_question import _ask_msg
    typed = []
    monkeypatch.setattr(ptyhost, "owns", lambda pid: "pty-1" if pid == 4242 else None)
    monkeypatch.setattr(ptyhost, "send",
                        lambda handle, text, newline=True: typed.append(text)
                        or {"matched": True, "sent": True, "terminalName": "cat"})
    monkeypatch.setattr(vscode_reveal, "can_send_text", lambda pid: False)
    monkeypatch.setattr("dark_army_daemon.daemon.QUESTION_KEY_GAP_SECONDS", 0.0)
    daemon = BobDaemon(headless=True)
    daemon._session_states["s1"] = {"pid": 4242, "last_event": 0, "state": "waiting",
                                    "provider": "claude"}
    daemon._track_pending_question("tool_use", "s1", _ask_msg(1))
    held = daemon._pending_questions["s1"]
    # The live reading `answer_question` consults, seeded as the other
    # suites seed it, from the same tracked dialog.
    daemon._questions["s1"] = dict(held)
    monkeypatch.setattr(daemon, "_ensure_session_pid", lambda sid, st: 4242)
    ok, detail = await daemon.answer_question("s1", held["id"], 1)
    assert ok, detail
    assert typed[0] == "2"
