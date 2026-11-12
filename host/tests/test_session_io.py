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


@pytest.fixture(autouse=True)
def _no_host_installed(monkeypatch):
    """`ptyhost.current()` is whichever daemon another test built last; pin it
    so these cases never depend on that. Cases that want a host set their own."""
    monkeypatch.setattr(ptyhost, "current", lambda: None)


class _FakeHost:
    """Records its drains into the shared `events` list; answers `ok`."""

    def __init__(self, events, ok=True):
        self.events, self.ok, self.calls = events, ok, []

    async def drain(self, handle, timeout):
        self.calls.append((handle, timeout))
        self.events.append(("drain", handle, timeout))
        return self.ok


def _pty_events(monkeypatch, host_ok=True):
    """Own pid 42 as "pty-1"; record words, drain, sleep and enter in order."""
    events = []
    monkeypatch.setattr(ptyhost, "owns", lambda pid: "pty-1" if pid == 42 else None)

    def send(handle, text, newline=True):
        events.append(("enter",) if text == "\r" else ("words", text))
        return {"matched": True, "sent": True, "matchedBy": "pty"}
    monkeypatch.setattr(ptyhost, "send", send)
    host = _FakeHost(events, host_ok)
    monkeypatch.setattr(ptyhost, "current", lambda: host)

    async def nap(seconds):
        events.append(("sleep", seconds))
    monkeypatch.setattr(session_io.asyncio, "sleep", nap)
    return events, host


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
    slept = []

    async def nap(seconds):
        slept.append(seconds)
    monkeypatch.setattr(session_io.asyncio, "sleep", nap)
    result = asyncio.run(session_io.send_text(42, "", "hello"))
    assert result["sent"] is True and result["matchedBy"] == "pty"
    # The words and the Enter are two writes with a pause between them: one
    # burst of "hello\r" reads as a paste and is never submitted.
    assert typed == [("pty-1", "hello", False), ("pty-1", "\r", False)]
    assert slept == [session_io.PTY_ENTER_GAP_SECONDS]
    asyncio.run(session_io.send_text(42, "", "1", newline=False))
    assert typed[-1] == ("pty-1", "1", False)
    assert len(slept) == 1, "a single keystroke needs no pause"


def test_a_pty_that_takes_no_words_gets_no_enter(monkeypatch):
    typed = []
    monkeypatch.setattr(ptyhost, "owns", lambda pid: "pty-1")
    monkeypatch.setattr(ptyhost, "send",
                        lambda handle, text, newline=True: typed.append(text) and None)
    assert asyncio.run(session_io.send_text(42, "", "hello")) is None
    assert typed == ["hello"], "an Enter after unsent words would submit a stale line"


def test_an_enter_that_does_not_land_is_not_sent(monkeypatch):
    """The pty went away in the gap: the words were typed, the line was
    never submitted, and the reply must not say `sent`."""
    replies = iter([{"sent": True, "matchedBy": "pty"}, None])
    monkeypatch.setattr(ptyhost, "owns", lambda pid: "pty-1")
    monkeypatch.setattr(ptyhost, "send", lambda handle, text, newline=True: next(replies))
    monkeypatch.setattr(session_io, "PTY_ENTER_GAP_SECONDS", 0.0)
    assert asyncio.run(session_io.send_text(42, "", "hello")) is None


def test_two_lines_to_one_pty_never_interleave(monkeypatch):
    """The gap hands the loop away; a second line in that window waits for
    the first line's Enter instead of landing between its words and it."""
    typed = []
    monkeypatch.setattr(ptyhost, "owns", lambda pid: "pty-1")
    monkeypatch.setattr(ptyhost, "send", lambda handle, text, newline=True:
                        typed.append(text) or {"sent": True})
    monkeypatch.setattr(session_io, "PTY_ENTER_GAP_SECONDS", 0.01)

    async def both():
        await asyncio.gather(session_io.send_text(42, "", "one"),
                             session_io.send_text(42, "", "two"))
    asyncio.run(both())
    assert typed == ["one", "\r", "two", "\r"]


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
    # The verbs' bodies run in the session's steering lane (`_steer`), so the
    # typing call sits in each `_now` body (`docs/steering-queue.md`).
    for fn in (BobDaemon._wrap_up_session_now,
               BobDaemon._low_priority_session_now,
               BobDaemon._type_answer_burst, BobDaemon._auto_compact_now,
               BobDaemon._message_card_now):
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


# --- every pty line waits for its words before its Enter -----------------------

def test_send_text_drains_the_words_before_the_enter(monkeypatch):
    events, _ = _pty_events(monkeypatch)
    result = asyncio.run(session_io.send_text(42, "", "hello"))
    assert events == [("words", "hello"),
                      ("drain", "pty-1", session_io.PTY_DRAIN_SECONDS),
                      ("sleep", session_io.PTY_ENTER_GAP_SECONDS),
                      ("enter",)]
    assert result["sent"] is True


def test_send_text_sends_no_enter_after_words_that_did_not_drain(monkeypatch):
    events, _ = _pty_events(monkeypatch, host_ok=False)
    result = asyncio.run(session_io.send_text(42, "", "hello"))
    assert result["sent"] is False and result["settled"] is False
    assert result["error"] == session_io.PTY_BUSY_REFUSAL
    assert [e for e in events if e[0] == "words"] == [("words", "hello")]
    assert ("enter",) not in events and not [e for e in events if e[0] == "sleep"]


def test_a_single_keystroke_is_never_drained(monkeypatch):
    events, host = _pty_events(monkeypatch)
    asyncio.run(session_io.send_text(42, "", "1", newline=False))
    assert host.calls == [] and events == [("words", "1")]


def test_the_editor_route_is_never_drained(monkeypatch):
    events, host = _pty_events(monkeypatch)
    monkeypatch.setattr(ptyhost, "owns", lambda pid: None)
    reply = {"matched": True, "sent": True, "terminalName": "zsh"}

    async def editor(pid, tty, text, newline=True):
        return reply
    monkeypatch.setattr(vscode_reveal, "send_text", editor)
    assert asyncio.run(session_io.send_text(42, "", "hello")) is reply
    assert host.calls == []


def test_with_no_host_installed_the_words_count_as_drained(monkeypatch):
    events, _ = _pty_events(monkeypatch)
    monkeypatch.setattr(ptyhost, "current", lambda: None)
    assert asyncio.run(session_io.send_text(42, "", "hello"))["sent"] is True
    assert events[-1] == ("enter",)


def test_the_drain_bound_is_read_at_call_time(monkeypatch):
    _, host = _pty_events(monkeypatch)
    monkeypatch.setattr(session_io, "PTY_DRAIN_SECONDS", 0.5)
    asyncio.run(session_io.send_text(42, "", "hello"))
    assert host.calls == [("pty-1", 0.5)]


@pytest.mark.asyncio
async def test_a_long_message_to_a_full_pty_gets_no_enter(tmp_path, monkeypatch):
    """Real pipe nobody reads: the pty cannot take the words, so the drain runs
    out and no carriage return is ever queued behind them."""
    import os
    from dark_army_daemon import daemon as dm
    from dark_army_daemon.ptyhost import PtyTerminal
    from dark_army_daemon.vtgrid import Screen
    d = dm.BobDaemon(headless=True)
    r, w = os.pipe()
    os.set_blocking(w, False)
    host = d._pty
    host._loop = asyncio.get_running_loop()
    term = PtyTerminal(handle="pty-pipe", name="pipe", root=str(tmp_path), pid=0,
                       master=w, proc=None, screen=Screen(80, 24), cols=80, rows=24)
    host._terms[term.handle] = term
    monkeypatch.setattr(ptyhost, "current", lambda: host)
    monkeypatch.setattr(ptyhost, "owns", lambda pid: term.handle)
    monkeypatch.setattr(session_io, "PTY_DRAIN_SECONDS", 0.1)
    try:
        assert host.write(term.handle, "y" * 300_000)
        assert host.pending(term.handle) > 0
        result = await session_io.send_text(42, "", "x" * 1500)
        assert result["sent"] is False
        assert result["error"] == session_io.PTY_BUSY_REFUSAL
        os.set_blocking(r, False)
        got = bytearray()
        for _ in range(600):
            try:
                got += os.read(r, 65536)
            except BlockingIOError:
                pass
            await asyncio.sleep(0.005)
            if host.pending(term.handle) == 0:
                break
        try:
            got += os.read(r, 65536)
        except BlockingIOError:
            pass
        assert bytes(got).endswith(b"x" * 1500)
        assert b"\r" not in got
    finally:
        os.close(r)
        host._terms.pop(term.handle, None)
        os.close(w)


def test_the_bound_and_the_sentence_are_one_definition():
    from dark_army_daemon import daemon as dm
    assert dm.TERMINAL_DRAIN_SECONDS == session_io.PTY_DRAIN_SECONDS
    assert dm.TERMINAL_BUSY_REFUSAL is session_io.PTY_BUSY_REFUSAL


# --- the caller's "still clear" check, right before the Enter ------------------

def test_a_failed_still_clear_sends_no_enter(monkeypatch):
    events, _ = _pty_events(monkeypatch)
    result = asyncio.run(session_io.send_text(
        42, "", "hello", still_clear=lambda: events.append(("asked",)) or False))
    assert result["sent"] is False
    assert result["error"] == session_io.PTY_TYPED_NOT_SENT_REFUSAL
    # asked after the drain and the gap, never before
    assert events == [("words", "hello"),
                      ("drain", "pty-1", session_io.PTY_DRAIN_SECONDS),
                      ("sleep", session_io.PTY_ENTER_GAP_SECONDS),
                      ("asked",)]


def test_a_passing_still_clear_lets_the_enter_go(monkeypatch):
    events, _ = _pty_events(monkeypatch)
    assert asyncio.run(session_io.send_text(
        42, "", "hello", still_clear=lambda: True))["sent"] is True
    assert events[-1] == ("enter",)


def test_still_clear_is_not_asked_on_the_editor_route_or_for_a_keystroke(monkeypatch):
    events, host = _pty_events(monkeypatch)

    def boom():
        raise AssertionError("asked")
    asyncio.run(session_io.send_text(42, "", "1", newline=False, still_clear=boom))
    monkeypatch.setattr(ptyhost, "owns", lambda pid: None)

    async def editor(pid, tty, text, newline=True):
        return {"matched": True, "sent": True}
    monkeypatch.setattr(vscode_reveal, "send_text", editor)
    assert asyncio.run(session_io.send_text(42, "", "hi", still_clear=boom))["sent"]


def test_the_typed_not_sent_sentence_is_one_definition():
    from dark_army_daemon import daemon as dm
    assert dm.TERMINAL_TYPED_NOT_SENT_REFUSAL is session_io.PTY_TYPED_NOT_SENT_REFUSAL
