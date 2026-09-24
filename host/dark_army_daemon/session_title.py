"""The name a session wears when Claude Code no longer writes one.

`ai_title.py` reads the `ai-title` entry Claude Code puts in the transcript —
its own one-line name for the session, and by far the best thing a row can be
called. It stopped appearing, and the cause is ours: `terminal_title` writes
`CLAUDE_CODE_DISABLE_TERMINAL_TITLE=1` into `~/.claude/settings.json`, and in
2.1.233 that flag does not only silence the OSC title, it gates the title
*generation*. In the REPL component the flag is read once
(`X = useMemo(() => CLAUDE_CODE_DISABLE_TERMINAL_TITLE)`) and the call that
mints the name sits behind it (`if (!X && … ) ui.generateSessionTitle(…)`, whose
result is what gets appended as `ai-title`). Measured, not guessed: every
session started on this machine up to the minute that key was written carries a
title, and none after it does.

So this is the same debt `terminal_title.py` already describes, one surface
along. We took Claude Code's pen away from the tab and gave the tab a
replacement; the row's name was the second thing on the end of that pen, and it
went unreplaced for a day. **Dark Army names the session itself.**

**One `claude -p` per session, and four flags earn their place:**

* ``--no-session-persistence`` — no transcript is written, so Dark Army's own
  transcript scan, its history database and its agents snapshot never see the
  helper. Without it every title leaves a session-shaped file behind, in a
  project folder chosen by whatever cwd we happened to use.
* ``--setting-sources ""`` — no user/project settings, therefore **no hooks**.
  Verified the expensive way: a plain `claude -p` fires SessionStart/Stop
  through the installed handler, and Dark Army raised a banner about its own helper
  ("Cipher-d374 needs you"). Silencing it by pointing the hook door
  (`DARK_ARMY_HOOK_SOCKET`, or the bridge `BOB_COMPANION_PORT`) at a dead
  address would have hidden our own hooks and still run everyone else's.
* ``--strict-mcp-config`` — with no `--mcp-config` beside it, no MCP servers at
  all. The channel is registered user-scope (`claude mcp add -s user`), so
  without this every title spawns a `dark-army-channel` that announces
  itself to the daemon that started it.
* ``--model`` — Haiku. This is a naming task on ~600 characters; the session
  being named is paying for it out of the same account.

The instruction rides **in the prompt**, not in `--system-prompt`: with the
instruction there the model answered the request instead of naming it — 73
seconds and a paragraph of advice, against 10 seconds and four words.

**One attempt per session, ever.** A miss is remembered exactly like a hit,
because the alternative is a subprocess every few seconds for the life of a row
that will never be named. Titles are persisted so a daemon restart does not
re-spend on sessions it has already paid for.

Pure, like `autocompact.py` and `alerts.py` next door: the policy, the command
line and the cleaning are here and testable with a dict; the subprocess belongs
to the daemon, which owns the event loop.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger("dark-army")

ASK, PASS = "ask", "pass"

#: Cheapest model that can name a paragraph. Spelled as an alias rather than a
#: dated id so a Haiku release does not strand this on an old one.
MODEL = "haiku"

#: How much of the opening request the namer is shown. A title is decided by the
#: first sentence or two; the rest is pasted logs as often as it is context.
REQUEST_CHARS = 600

#: What a title may be. The ceiling is a *rejection* threshold, not a truncation
#: point — a model that answers a naming task with a paragraph has not produced
#: a long title, it has misunderstood the job, and the opening prompt we already
#: have is a better row than the first sixty characters of an essay.
MAX_CHARS = 60
MAX_WORDS = 8

#: How long the helper gets. Startup dominates (the CLI boots in ~5s); a title
#: that takes longer than this has hit something other than the model.
TIMEOUT_SECONDS = 45.0

#: Titles kept on disk. One line each, and a session id is never reused.
MAX_TITLES = 512

PROMPT_HEAD = (
    "Name this work session. Answer with a title of at most six words, in the "
    "language of the request, no quotes, no trailing period, nothing else. "
    "Do not use any tools. Do not answer the request itself.\n\nREQUEST:\n"
)


def prompt_for(request: str) -> str:
    """The whole `-p` argument: the instruction, then the person's own opening
    words, cut to `REQUEST_CHARS`."""
    text = " ".join((request or "").split())[:REQUEST_CHARS]
    return PROMPT_HEAD + text


def argv(claude_bin: str, request: str) -> list[str]:
    """The command line, in one place so the test and the daemon cannot drift.

    Each flag is argued for in the module docstring; none of them is optional
    politeness, and dropping any one turns a naming call into a visible session
    on somebody's screen."""
    return [
        claude_bin,
        "-p", prompt_for(request),
        "--model", MODEL,
        "--no-session-persistence",
        "--strict-mcp-config",
        "--setting-sources", "",
    ]


#: Wrappers a model reaches for when it has been told not to use any.
_STRIP = " \t\r\n\"'`*_.…"


def clean(raw: str) -> str:
    """The title inside whatever came back, or "" if that is not a title.

    The **last** non-empty line, because a preamble comes before the answer and
    never after it. Empty is a real answer here: it means the row keeps the
    opening prompt it was already showing, which is a worse name than a good
    title and a much better one than a bad title.
    """
    lines = [ln.strip() for ln in (raw or "").splitlines()]
    line = next((ln for ln in reversed(lines) if ln), "")
    line = " ".join(line.strip(_STRIP).split())
    if not line or len(line) > MAX_CHARS or len(line.split()) > MAX_WORDS:
        return ""
    return line


@dataclass
class TitleShop:
    """What Dark Army has named, what it is naming, and what it will not name twice."""

    enabled: bool = True
    path: Optional[Path] = None
    max_titles: int = MAX_TITLES
    #: session_id -> title, where "" is "asked, and there is no title". Ordered
    #: so the cap drops the oldest rather than an arbitrary key.
    _titles: "OrderedDict[str, str]" = field(default_factory=OrderedDict)

    def title_for(self, session_id: str) -> str:
        return self._titles.get(session_id, "")

    def consider(self, session_id: str, request: str) -> str:
        """`ASK` to spend a subprocess on this session, `PASS` to leave the row
        with the opening prompt it is already showing.

        Marks the session as asked *here*, on the way out, rather than when the
        answer lands: the caller runs on the snapshot thread every few seconds
        and the helper takes ten of those, so anything that waits for a result
        before remembering the question asks it half a dozen times.
        """
        if not self.enabled or not session_id or not request.strip():
            return PASS
        if session_id in self._titles:
            return PASS
        self._titles[session_id] = ""
        return ASK

    def note(self, session_id: str, raw: str) -> str:
        """Record what came back. Returns the title actually stored ("" if the
        answer was not one), so the caller can log the difference."""
        title = clean(raw)
        if not session_id:
            return ""
        self._titles[session_id] = title
        self._titles.move_to_end(session_id)
        self._trim()
        if title:
            self.save()
        return title

    def note_failed(self, session_id: str) -> None:
        """The helper did not run, or did not finish. Remembered exactly like an
        empty answer: the failure is nearly always structural (no CLI on PATH,
        no credentials), and retrying it per snapshot would be a subprocess
        every few seconds for as long as the row exists."""
        if session_id:
            self._titles.setdefault(session_id, "")

    def _trim(self) -> None:
        while len(self._titles) > self.max_titles:
            self._titles.popitem(last=False)

    # --- persistence -----------------------------------------------------
    # Only the hits are written. A miss is worth remembering for this daemon's
    # life — it stops the retry storm — but not across a restart, where the
    # cause may well have been fixed in between.

    def load(self) -> None:
        if self.path is None or not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.debug("session titles unreadable", exc_info=True)
            return
        if not isinstance(data, dict):
            return
        for sid, title in list(data.items())[-self.max_titles:]:
            if isinstance(sid, str) and isinstance(title, str) and title:
                self._titles[sid] = title

    def save(self) -> None:
        if self.path is None:
            return
        payload = {sid: t for sid, t in self._titles.items() if t}
        tmp_path = ""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f)
            os.replace(tmp_path, str(self.path))
        except OSError:
            logger.debug("session titles unwritable", exc_info=True)
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass


__all__ = ["TitleShop", "ASK", "PASS", "MODEL", "MAX_CHARS", "MAX_WORDS",
           "REQUEST_CHARS", "TIMEOUT_SECONDS", "PROMPT_HEAD", "argv",
           "prompt_for", "clean"]
