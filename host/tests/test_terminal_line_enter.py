"""A line typed into a hosted terminal from Comm or the phone is the words,
then — after a pause — Enter as its own write.

18 Oct 2026: one write of ``text + "\\r"`` reaches Claude Code as a single
burst, which it reads as a paste; from about 140 characters the Enter lands as
a line break in the input box and nothing is submitted, while the phone is
told ok. Measured on a real Claude Code 2.1.302 on a pty: 138, 226 and 402
characters in one write were never submitted; the same 402 with the Enter
0.05, 0.15 or 0.3 s later all were. `session_io._type_into_pty` split the
same burst on 3 Oct."""
import asyncio

from dark_army_daemon import session_io
from dark_army_daemon.daemon import BobDaemon


class FakePty:
    def __init__(self):
        self.events = []
        self.alive = True

    def write(self, handle, text="", data=None):
        self.events.append(("write", text))
        return self.alive

    async def drain(self, handle, timeout):
        self.events.append(("drain",))
        return True


def _daemon(tmp_path, monkeypatch):
    d = BobDaemon(sessions_path=tmp_path / "sessions.json")
    d._pty = FakePty()
    slept = []

    async def sleep(seconds):
        slept.append(seconds)
        d._pty.events.append(("sleep", seconds))

    monkeypatch.setattr("dark_army_daemon.daemon.asyncio.sleep", sleep)
    return d, slept


def test_the_words_and_the_enter_are_two_writes_with_a_pause(tmp_path, monkeypatch):
    d, _ = _daemon(tmp_path, monkeypatch)
    line = "x" * 300
    ok, detail = asyncio.run(d._terminal_line_input("h-1", line))
    assert (ok, detail) == (True, "")
    assert d._pty.events == [
        ("write", line), ("drain",),
        ("sleep", session_io.PTY_ENTER_GAP_SECONDS),
        ("write", "\r"), ("drain",)]


def test_no_write_ever_carries_the_words_and_the_enter_together(tmp_path, monkeypatch):
    d, _ = _daemon(tmp_path, monkeypatch)
    asyncio.run(d._terminal_line_input("h-1", "hello there\n"))
    writes = [e[1] for e in d._pty.events if e[0] == "write"]
    assert writes == ["hello there", "\r"]


def test_a_terminal_gone_before_the_enter_is_not_reported_typed(tmp_path, monkeypatch):
    d, _ = _daemon(tmp_path, monkeypatch)
    real_write = d._pty.write

    def write(handle, text="", data=None):
        if text == "\r":
            d._pty.alive = False
        return real_write(handle, text, data)

    d._pty.write = write
    ok, _ = asyncio.run(d._terminal_line_input("h-1", "hello"))
    assert ok is False


def test_a_prompt_that_appears_in_the_pause_gets_no_enter(tmp_path, monkeypatch):
    """The prompt check runs again just before the Enter: a dialog that came
    up while the words drained must not be confirmed by it."""
    from dark_army_daemon import daemon as dm
    d, _ = _daemon(tmp_path, monkeypatch)
    prompts = {}
    monkeypatch.setattr(d, "_prompts_by_session", lambda: prompts)

    async def sleep(seconds):
        prompts["s-1"] = object()

    monkeypatch.setattr("dark_army_daemon.daemon.asyncio.sleep", sleep)
    ok, detail = asyncio.run(d._terminal_line_input("h-1", "hello", "s-1"))
    assert (ok, detail) == (False, dm.TERMINAL_TYPED_NOT_SENT_REFUSAL)
    assert "not sent" in detail
    assert [e[1] for e in d._pty.events if e[0] == "write"] == ["hello"]
