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
"""
from __future__ import annotations

import asyncio
from typing import Optional

from . import ptyhost, vscode_reveal

#: The pause between a line's words and its Enter on Dark Army's own pty —
#: the question burst's `QUESTION_KEY_GAP_SECONDS`, for the same reason.
PTY_ENTER_GAP_SECONDS = 0.12

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


async def send_text(pid, tty: str, text: str, newline: bool = True) -> Optional[dict]:
    """Type `text` at the terminal owning `pid`. The reply carries `sent`
    (what every caller tests) and `terminalName` / `matchedBy` (the log
    line), or None when nothing could type."""
    if not text:
        return None
    handle = ptyhost.owns(pid)
    if handle is not None:
        async with _pty_lock(handle):
            return await _type_into_pty(handle, text, newline)
    if newline:
        return await vscode_reveal.send_text(pid, tty, text)
    return await vscode_reveal.send_text(pid, tty, text, newline=False)


async def _type_into_pty(handle, text: str, newline: bool) -> Optional[dict]:
    """`send_text`'s pty half, run under the handle's lock."""
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
    await asyncio.sleep(PTY_ENTER_GAP_SECONDS)
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
