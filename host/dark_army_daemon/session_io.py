"""The one place that decides where a session's terminal lives.

Dark Army can type into, and close, two kinds of terminal: the VS Code one the
extension opened for a hand-started session (`vscode_reveal.py`), and the
pty Dark Army itself opened for a board-dispatched card (`ptyhost.py`). Every
route that reaches an input line — the answer burst, `/clear`, `/compact`,
`/low-priority`, a card's message — and every capability flag that says it
could (`can_type`, `can_close`, `can_low_priority`, `can_message`) asks
**here**, and nowhere asks `vscode_reveal` for these four names directly.
`test_session_io.py` greps for that, because the failure mode is silent: a
missed call site leaves a Dark Army-run session publishing `can_type: false` and
refusing in words that read like a bug in the feature.

Four functions, four identical signatures, four identical reply shapes. The
pty is asked first (`ptyhost.owns`, the exact-pid or descent answer), and
everything else falls through to the extension **with the caller's own
argument shape** — tests stub `vscode_reveal.send_text` as
`async def _send(pid, tty, text, newline=True)` and `can_send_text` as a
one-argument lambda, so the fall-through forwards positionally and passes
`newline=` only when it is not the default. Every `vscode_reveal.<name>` is
looked up at call time so a monkeypatch on that module still bites.

The refinement close (`close_refinement_terminal`) is deliberately **not**
here: it is Codex's receipt path through the editor bridge, and a refinement
that ran on Dark Army's own pty is simply left open with its reason.

Every line typed into a pty waits for its words to drain before its Enter
(`_pty_settle`): a terminal takes about 1 KB at a time, and an Enter queued
behind half-taken words reaches the CLI as part of a paste.
"""
from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Optional

from . import ptyhost, vscode_reveal

#: The pause between a line's words and its Enter on Dark Army's own pty —
#: the question burst's `QUESTION_KEY_GAP_SECONDS`, for the same reason.
PTY_ENTER_GAP_SECONDS = 0.12

#: How long a line's words get to drain into the pty before its Enter is
#: withheld. The one bound for the typed line, replies and card messages
#: (`daemon.TERMINAL_DRAIN_SECONDS` aliases it).
PTY_DRAIN_SECONDS = 3.0

#: The pty took only part of the line within `PTY_DRAIN_SECONDS`. Its
#: own sentence and never an "exited" one: Darwin's pty input queue is ~1 KB,
#: so a long line to a CLI mid-redraw is a wait, not a death.
PTY_BUSY_REFUSAL = ("The terminal is not taking input right now — part of "
                    "that line may have landed, unsent; check the screen "
                    "before typing it again.")

#: The words went in but their Enter was held back: a permission prompt or a
#: question came up after the caller's last check. The words sit in the input
#: box unsent, so a blind retry would submit them twice.
PTY_TYPED_NOT_SENT_REFUSAL = ("A permission prompt came up while typing — "
                              "the words are in the terminal's input box "
                              "but were not sent. Answer the prompt, then "
                              "press Enter there or clear the line.")

#: One lock per (event loop, pty handle): the pause above hands the loop away
#: between a line's words and its Enter, and a second line typed into the same
#: pty in that window would read `text1text2\r\r` — one garbled submit. Keyed
#: by the loop too, because an `asyncio.Lock` belongs to the loop that first
#: waits on it.
_PTY_LOCKS: dict = {}


def _pty_lock(handle) -> asyncio.Lock:
    key = (id(asyncio.get_running_loop()), handle)
    lock = _PTY_LOCKS.get(key)
    if lock is None:
        lock = _PTY_LOCKS[key] = asyncio.Lock()
    return lock


def _pty_settle(handle) -> Callable[[], Awaitable[bool]]:
    """An `async () -> bool` that says whether `handle`'s words have all gone
    into the pty. The host and the bound are read at call time, so a
    monkeypatch on either bites; no host installed counts as drained."""
    async def settle() -> bool:
        host = ptyhost.current()
        if host is None:
            return True
        return bool(await host.drain(handle, PTY_DRAIN_SECONDS))
    return settle


def can_send_text(pid, tty: str = "") -> bool:
    """Could Dark Army type into this process's terminal?"""
    if ptyhost.owns(pid) is not None:
        return True
    if tty:
        return bool(vscode_reveal.can_send_text(pid, tty))
    return bool(vscode_reveal.can_send_text(pid))


def can_close_terminal(pid, tty: str = "") -> bool:
    """Could Dark Army dispose this process's terminal?"""
    if ptyhost.owns(pid) is not None:
        return True
    if tty:
        return bool(vscode_reveal.can_close_terminal(pid, tty))
    return bool(vscode_reveal.can_close_terminal(pid))


async def send_text(pid, tty: str, text: str, newline: bool = True, *,
                    still_clear: Optional[Callable[[], bool]] = None
                    ) -> Optional[dict]:
    """Type `text` at the terminal owning `pid`. The reply carries `sent`
    (what every caller tests) and `terminalName` / `matchedBy` (the log
    line), or None when nothing could type.

    `still_clear`, for a session-aware caller, is asked on the pty route only,
    after the drain and the gap and right before the Enter; a False answer
    sends no Enter and replies `sent: False` with `PTY_TYPED_NOT_SENT_REFUSAL`.
    The editor route and `newline=False` keystrokes never ask it."""
    if not text:
        return None
    handle = ptyhost.owns(pid)
    if handle is not None:
        async with _pty_lock(handle):
            reply = await _type_into_pty(
                handle, text, newline, settle=_pty_settle(handle),
                still_clear=still_clear)
            if isinstance(reply, dict) and reply.get("settled") is False:
                # `error` is what `_typed_nothing_refusal` hands the person,
                # instead of "no VS Code window owns this terminal".
                reply = {**reply, "error": PTY_BUSY_REFUSAL}
            return reply
    if newline:
        return await vscode_reveal.send_text(pid, tty, text)
    return await vscode_reveal.send_text(pid, tty, text, newline=False)


async def _type_into_pty(handle, text: str, newline: bool,
                         settle: Optional[Callable[[], Awaitable[bool]]] = None
                         , still_clear: Optional[Callable[[], bool]] = None
                         ) -> Optional[dict]:
    """`send_text`'s pty half, run under the handle's lock. `settle`, when
    given, is awaited after the words land and before the gap; a False
    answer sends no Enter and replies `settled: False`. `still_clear`, when
    given, is asked after the gap and right before the Enter."""
    if not newline:
        return ptyhost.send(handle, text, False)
    # The words, a pause, then Enter as its own write. One write of
    # `text + "\r"` reaches the TUI as a single burst, which Claude Code
    # reads as a paste: the Enter lands as a line break in the input box
    # and nothing is submitted (3 Oct 2026 — four phone messages to a
    # card agent shown "delivered", none ever submitted). The question
    # burst already spaces its keys by the same gap.
    typed = ptyhost.send(handle, text, False)
    if not (typed and typed.get("sent")):
        return typed
    if settle is not None and not await settle():
        return {"matched": True, "sent": False, "settled": False}
    await asyncio.sleep(PTY_ENTER_GAP_SECONDS)
    if still_clear is not None and not still_clear():
        # A dialog that opened since the caller's check would read this Enter
        # as "confirm the highlighted choice"; the words stay unsent.
        return {"matched": True, "sent": False, "typedNotSent": True,
                "error": PTY_TYPED_NOT_SENT_REFUSAL}
    # The Enter's own answer, never the words': a pty that went away in the
    # gap left the line unsubmitted, and saying `sent` would be the very
    # "delivered, never submitted" this split exists to end.
    return ptyhost.send(handle, "\r", False)


async def close_terminal(pid, tty: str) -> Optional[dict]:
    """Dispose the terminal owning `pid`. The reply carries `matched` (what
    every caller tests) or None."""
    handle = ptyhost.owns(pid)
    if handle is not None:
        return await ptyhost.close(handle)
    return await vscode_reveal.close_terminal(pid, tty)


__all__ = ["can_send_text", "can_close_terminal", "send_text", "close_terminal"]
