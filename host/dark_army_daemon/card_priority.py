"""How important one card is, 0..100, asked once and never argued about.

The third instance of the `session_title.py` pattern, modelled on it line for
line: a pure module whose command line, prompt and refusals are testable with
dicts and strings, while the subprocess belongs to the daemon, which owns the
event loop. Unlike `card_prepare.py`, it takes neither of that module's
departures — a scorer
reads a title and a plain summary and answers with a number, so it runs in
`STATE_DIR` (it has no business loading somebody's `CLAUDE.md`) and it writes
nothing a person typed.

**Scores are absolute, not relative.** The helper never sees the project's
other cards or their numbers, so a whole board can drift a band high or low
and two cards a person would rank equal can differ by ten or fifteen points.
That cost is stated here rather than discovered, and two things buy it back.
It is what makes "once ever" honest — a score that depended on the board's
contents would be wrong the moment the next card arrived, and re-scoring the
board on every arrival is a subprocess per card per arrival. And the
correction is one number away on either surface.

**One attempt per card, ever.** A miss is remembered exactly like a hit for
this daemon's life (the failure is nearly always structural — no CLI, no
credentials — and retrying per snapshot is a subprocess every few seconds),
and only hits are persisted: a restart may find the cause fixed. Unscored is
a legitimate resting state, not an error.

**`''` is not `'0'`.** An empty answer means nobody has scored the card and
draws nothing on its face; `'0'` is a real score and draws `P 0`. The two are
deliberately indistinguishable in the sort — see `board.CARD_ORDER_SQL`.

Four flags on the command line, each for the reason `session_title.py`'s
docstring gives: no transcript, no hooks, no MCP servers, cheap model. The
instruction rides **in the prompt**, never in `--system-prompt` — that
module's measured finding, borrowed whole.
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

from . import board

logger = logging.getLogger("dark-army")

ASK, PASS = "ask", "pass"

#: Cheapest model that can read two lines and produce one number. An alias
#: rather than a dated id so a Haiku release does not strand this on an old one.
MODEL = "haiku"

#: How long the helper gets. Startup dominates; a one-number answer that takes
#: longer than this has hit something other than the model.
TIMEOUT_SECONDS = 45.0

#: How much of the card the scorer is shown. The store's own ceilings — longer
#: values were refused at `create` anyway.
MAX_TITLE_INPUT = board.MAX_TITLE_CHARS
MAX_SUMMARY_INPUT = board.MAX_SUMMARY_CHARS

#: Scores kept on disk. One line each, and a card id is never reused.
MAX_SCORES = 512

PROMPT_HEAD = (
    "You are scoring one piece of work on a project's board, so that the "
    "most important work sits at the top. Answer with a single whole number "
    "from 0 to 100 and nothing else.\n\n"
    "The number is an absolute judgement of importance, not a comparison "
    "with any other card, and it means:\n\n"
    "- 90-100 - something is broken or unsafe for the people using this "
    "right now: data lost, a crash, a security hole, work that cannot "
    "proceed at all until this is done.\n"
    "- 70-89 - the product already promises this and is not keeping the "
    "promise: a workflow that is blocked, wrong or has only a bad "
    "workaround, or work that several other pieces of work are waiting on.\n"
    "- 40-69 - an ordinary improvement: a feature people want, a rough edge "
    "that has a workaround, a cleanup that pays for itself soon.\n"
    "- 15-39 - polish, tidying, convenience. Nothing is worse for waiting.\n"
    "- 0-14 - speculative, exploratory, or a someday idea.\n\n"
    "Judge only what the card says. Do not inflate a small change because it "
    "sounds urgent, and do not deflate a real failure because it is "
    "described calmly. Answer with the number alone - no words, no "
    "punctuation, no explanation. Do not use any tools. Do not do the "
    "work.\n\n"
)


def prompt_for(*, title: str, summary: str, project: str) -> str:
    """The whole `-p` argument: the rubric, then the card's two lines and the
    project's name.

    **Nothing else** — no other card, no other card's score, no folder list.
    That is what makes the judgement absolute and the "once ever" honest.
    """
    title_text = " ".join((title or "").split())[:MAX_TITLE_INPUT]
    summary_text = " ".join((summary or "").split())[:MAX_SUMMARY_INPUT]
    return (
        f"{PROMPT_HEAD}"
        f"TITLE: {title_text}\n"
        f"SUMMARY: {summary_text}\n"
        f"PROJECT: {project or ''}\n"
    )


def argv(claude_bin: str, **fields) -> list[str]:
    """The command line, in one place so the test and the daemon cannot drift.

    Each flag is argued for in `session_title.py`'s module docstring; none is
    optional politeness, and dropping any one turns a scoring call into a
    visible session on somebody's screen.
    """
    return [
        claude_bin,
        "-p", prompt_for(
            title=str(fields.get("title") or ""),
            summary=str(fields.get("summary") or ""),
            project=str(fields.get("project") or ""),
        ),
        "--model", MODEL,
        "--no-session-persistence",
        "--strict-mcp-config",
        "--setting-sources", "",
    ]


#: Wrappers a model reaches for when it has been told not to use any.
_STRIP = " \t\r\n\"'`*_.…#"


def clean(raw: str) -> str:
    """The number inside whatever came back, or `""` if that is not one.

    The **last** non-empty line, because a preamble comes before the answer
    and never after it. Everything that is not a bare ASCII decimal in
    `0..board.MAX_PRIORITY` is **rejected rather than clamped or truncated**:
    a model that answers "somewhere around 80, because…" has misunderstood the
    job, and unscored is a better resting state than the first two characters
    of an essay. `NONE` is an explicit "cannot tell" and returns `""` too.

    The canonical decimal form is returned (`"07"` -> `"7"`), so a value never
    differs from what `board.normalise_priority` would store.

    `isascii()` is checked as well as `isdigit()` — Arabic-Indic digits pass
    `isdigit()` and `int()` and would store a string no client's `Int(...)`
    parses the same way.
    """
    lines = [ln.strip() for ln in (raw or "").splitlines()]
    line = next((ln for ln in reversed(lines) if ln), "")
    line = line.strip(_STRIP).strip()
    if not line or line.upper() == "NONE":
        return ""
    if not (1 <= len(line) <= 3):
        return ""
    if not (line.isascii() and line.isdigit()):
        return ""
    number = int(line)
    if number > board.MAX_PRIORITY:
        return ""
    return str(number)


@dataclass
class PriorityShop:
    """What Dark Army has scored, what it is scoring, and what it will not score twice."""

    enabled: bool = True
    path: Optional[Path] = None
    max_cards: int = MAX_SCORES
    #: card_id -> score string, where "" is "asked, and there is no answer".
    #: Ordered so the cap drops the oldest rather than an arbitrary key.
    _scores: "OrderedDict[str, str]" = field(default_factory=OrderedDict)

    def consider(self, card_id: str) -> str:
        """`ASK` to spend a subprocess on this card, `PASS` to leave it unscored.

        Marks the card as asked *here*, on the way out, rather than when the
        answer lands — `TitleShop.consider`'s stated reason: the caller runs on
        the snapshot thread every few seconds and the helper takes ten of
        those, so anything that waits for a result before remembering the
        question asks it half a dozen times.
        """
        if not self.enabled or not card_id:
            return PASS
        if card_id in self._scores:
            return PASS
        self._scores[card_id] = ""
        self._trim()
        return ASK

    def note(self, card_id: str, raw: str) -> str:
        """Record what came back. Returns the score actually stored (`""` if the
        answer was not a number), so the caller can log the difference."""
        score = clean(raw)
        if not card_id:
            return ""
        self._scores[card_id] = score
        self._scores.move_to_end(card_id)
        self._trim()
        if score:
            self.save()
        return score

    def learn(self, card_id: str, score: str) -> None:
        """Remember a number that is *already on the card*, whoever wrote it.

        The skip half of `consider`: a card that arrives carrying a score has
        an answer, and the shop has to hold it durably — otherwise a number
        somebody typed by hand is never learned, and clearing it later puts
        the card straight back in front of the helper, which would have Dark Army
        re-score a card somebody deliberately emptied.

        **Idempotent and silent when nothing changed**, which is what makes it
        safe on the reconcile's own path: the caller runs every few seconds
        over every card, and `note`'s unconditional `save()` there would be a
        file rewrite per scored card per pass.
        """
        if not card_id or not score:
            return
        if self._scores.get(card_id) == score:
            return
        self._scores[card_id] = score
        self._scores.move_to_end(card_id)
        self._trim()
        self.save()

    def note_failed(self, card_id: str) -> None:
        """The helper did not run, or did not finish. Remembered exactly like
        an empty answer — the failure is nearly always structural, and retrying
        per snapshot would be a subprocess every few seconds for as long as the
        card sits on the board."""
        if card_id:
            self._scores.setdefault(card_id, "")
            self._trim()

    # Called from every path that grows the dict, the miss paths included:
    # a Mac with no `claude` binary marks every card it ever sees and never
    # writes a hit, so trimming only on success would grow unbounded.
    def _trim(self) -> None:
        while len(self._scores) > self.max_cards:
            self._scores.popitem(last=False)

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
            logger.debug("card priorities unreadable", exc_info=True)
            return
        if not isinstance(data, dict):
            return
        for cid, score in list(data.items())[-self.max_cards:]:
            if isinstance(cid, str) and isinstance(score, str) and score:
                self._scores[cid] = score

    def save(self) -> None:
        if self.path is None:
            return
        # `list(...)` first: `learn` writes from the executor while `note`
        # writes from the loop, and iterating the live dict could raise
        # "dictionary changed size during iteration" between the two.
        payload = {cid: s for cid, s in list(self._scores.items()) if s}
        tmp_path = ""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(dir=str(self.path.parent),
                                            suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f)
            os.replace(tmp_path, str(self.path))
        except OSError:
            logger.debug("card priorities unwritable", exc_info=True)
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass


__all__ = ["PriorityShop", "ASK", "PASS", "MODEL", "TIMEOUT_SECONDS",
           "MAX_SCORES", "MAX_TITLE_INPUT", "MAX_SUMMARY_INPUT",
           "PROMPT_HEAD", "prompt_for", "argv", "clean"]
