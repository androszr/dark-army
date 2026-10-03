"""Per-session statistics parsed from a Claude Code transcript.

Claude Code writes one JSONL transcript per session (the same file
``ai_title.py`` reads). Each assistant line carries ``message.model`` and a
``message.usage`` block (input / output / cache tokens), every line carries a
``timestamp``, and assistant ``message.content`` lists ``tool_use`` items. This
module rolls those up into a :class:`SessionStats` for the menu-bar agent view.

Pure and I/O-light: it streams the file once, tolerates malformed lines, and
returns raw numbers — formatting and cost live elsewhere. Callers that poll
should cache by transcript mtime (see the daemon snapshot layer).
"""

from __future__ import annotations

import json
import os
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Where Claude Code stores per-session transcripts:
#   ~/.claude/projects/<encoded-cwd>/<session-id>.jsonl
CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"

# Models that appear in transcripts but aren't real inference (e.g. local
# synthetic completions); excluded from the reported model list.
_SYNTHETIC_MODELS = {"<synthetic>"}

# Tool-use input keys that name an edited/written file, for the files-touched count.
_FILE_INPUT_KEYS = ("file_path", "path", "notebook_path")
_EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}

# Tools that spawn a subagent. Their tool_result carries the agent_id the
# SubagentStart/Stop hooks report, letting us name a running subagent.
_SPAWN_TOOLS = {"Agent", "Task"}

# Agent-to-agent messaging. `SendMessage({to: "<name>", …})` addresses another
# session by the name in Claude Code's own registry — the one `session_registry`
# reads — so the recipient is a *name*, resolvable to a session or not. Dark Army
# cannot send one of these (the inbox socket is not a public API), but who is
# talking to whom is already written down in the transcript this module streams
# anyway, and a mesh nobody can see is discovered by trying.
_MESSAGE_TOOLS = {"SendMessage"}
_RECIPIENT_KEYS = ("to", "agent", "recipient", "name")
# One entry per recipient, not per message: a session that messages a partner
# forty times is one edge with a count, and the dict is bounded by the number of
# agents that exist rather than by how chatty they are.
MAX_MESH_EDGES = 32

# A question the agent asked its human. `AskUserQuestion` blocks the session on
# terminal input, and — unlike a permission prompt — there is **no relay
# capability for it**: an event pushed through Dark Army's channel is queued behind
# the very prompt it would answer, so it arrives only after somebody has already
# typed at the terminal. That rules out the channel, and for a while it was read
# as ruling out answering at all — wrongly, because Dark Army has a second route it
# already uses for `/clear` and `/compact`: `session_io.send_text`, which
# puts keystrokes on the session's own input line, *in front of* the dialog
# rather than behind it. The dialog is a keyboard select (a digit moves the
# highlight to that absolute index, Enter picks the highlighted one — read out
# of the client, not guessed), so `BobDaemon.answer_question` can choose an
# option exactly, and this module's job widens accordingly: the text is still
# the caption, and the options are now also the labels of buttons. Reach is
# `can_type`, the same reach wrap-up has — a VS Code window with the 0.1.6+
# extension — never `channel`.
# Claude's transcript name and Grok's. The hook path already aliases both
# (`protocol.ASK_USER_QUESTION_TOOLS`); the transcript walker has to list
# them too or a Grok `ask_user_question` that made it into a Claude-shaped
# jsonl would be counted as an ordinary tool and never become buttons.
_QUESTION_TOOLS = {"AskUserQuestion", "ask_user_question"}
# 600, up from a 160 justified by a panel that no longer exists in that shape.
# "One line for it" was true of the old row caption; both surfaces that draw a
# question today wrap it — `MultiQuestionAnswer` renders the text through
# `MarkdownText` with `fixedSize(vertical: true)`, and the phone's Needs-you
# pane does the same. So the cap stopped protecting a layout and started
# eating the question.
#
# It ate real ones. Measured on a live `/ship` interview: a 249-character
# question arrived on the phone as "… they show the fund's price (SPY is
# 765.16, not the" — cut mid-clause, three words before the choice it was
# actually posing — and a 164-character sibling died at "Which do you w". The
# options underneath were intact, so the pane showed four answerable buttons
# under a sentence that never finished asking anything, which is worse than
# showing nothing: the reader cannot tell they are missing the question.
#
# 600 fits a two-to-three sentence question whole, which is what the tool is
# for — a question that needs framing before it can be answered. The bytes are
# cheap for the same reason `MAX_LAST_TEXT_CHARS` gives (the payload's
# frequency is floored in `api_server._broadcast`), and this rides at most
# `MAX_QUESTIONS` times per dialog, only while a session is actually blocked.
MAX_QUESTION_CHARS = 600
# A label is a button now, and a truncated button is a choice nobody can make:
# at 40 two real options died at the same "Zawsze działa (nowa gałąź…" and were
# indistinguishable. 80 fits a full-width panel row on two wrapped lines.
MAX_OPTION_CHARS = 80
# Grok's options carry the real choice in `description` (Claude's `preview`
# is a different layout and stays off this field). 200 fits the one-or-two
# sentence reason both surfaces draw under the label; the hook still drops
# the unclipped original so a paragraph does not cross the socket.
MAX_DETAIL_CHARS = 200
MAX_OPTIONS = 4
# The tool itself takes one to four questions per call, so the list is capped
# where the tool is, not below it: a fifth question is not a real shape, and a
# list this size rides in every agents push for every waiting session.
MAX_QUESTIONS = 4

# The last thing the agent said. Not every wait is a question with a tool call
# behind it: the commonest one by far is a turn that simply ends — "Plan ready.
# accept?" — where the session is idle rather than blocked, and the row says
# "Waiting for input" while the *what for* sits in a terminal on another Space.
# The tail of the transcript is the only place that answer exists.
# Two lines on the row, the rest behind a chevron — so the cap is what the
# *expanded* view needs, not what the caption shows. Still a cap: this rides in
# every agents push, several times a minute, for every waiting session.
#
# 4000, up from a cautious 1200 that was never measured against a real turn.
# 1200 lost the *head* of an ordinary reply — a message of ~1900 characters
# opened at "… Poprawka", with the paragraph that said what was wrong cut off
# — and the head is where a turn explains itself. What the tail rule buys is
# the closing ask, which survives either way; what the small cap cost was the
# reason for it. At 4000 a normal turn fits whole and the cut is back to being
# what it was meant for: the genuinely long report. The extra bytes are cheap
# where it was feared they were not — the payload's frequency is floored by
# `api_server._broadcast` (200ms, 1s quiet), so a bigger row is a bigger frame,
# not more frames.
MAX_LAST_TEXT_CHARS = 4000

# The completion report `WORK_REPORT_HINT` asks for (`## Work done`, then
# Asked / Changed / Verified / Unchecked). `last_text` is the *latest* message
# and nothing else, so a one-line follow-up — "that was the background job,
# nothing new" — replaced a finished piece of work's whole report with a
# sentence, and the pane then had no way back to it: the transcript is not on
# the wire. So the report is kept beside the tail rather than instead of it,
# survives every later message, and is cleared by the person's next prompt,
# which is what actually supersedes it. Cut from the *head* like `last_text`,
# because the report's last section (`Unchecked:`) is the one somebody has to
# act on. The report is capped at about 25 lines by the hint itself, so 4000
# fits a whole one and the frequency argument above is unchanged.
MAX_LAST_REPORT_CHARS = 4000

# How long a live session must be quiet before it reads as finished rather than
# merely sleeping. Must stay well under the staleness timeout (300s) or a
# session would be evicted before it ever crossed the grace, and the live
# half of the section would never appear. Here rather than in `daemon.py`
# (which re-exports it) so `alerts.py` can read the same number without
# importing the daemon: a work report is fresh news only inside this grace.
FINISHED_IDLE_GRACE_SECONDS = 120.0
#: The heading, on its own line, exactly as the hint dictates it.
_WORK_REPORT_RE = re.compile(r"^##[ \t]+Work done[ \t]*$", re.M)

#: The line `/ship`'s close-out helper prints when it leaves the terminal
#: open (`.claude/skills/ship/close-out.sh`), which a plan or scout run ends
#: its last message with. It is the second way a turn says it is waiting on
#: **nobody**: the run is finished and the tab stays only to be read. The
#: script's `echo` and this constant are one contract, pinned by
#: `test_ship_close_out.test_default_line_is_the_one_the_daemon_reads`; the
#: `close-out:` prefix is also what the Grok demote keys on.
CLOSE_OUT_LEFT_OPEN = "close-out: terminal left open"
_LEFT_OPEN_RE = re.compile(r"^[ \t>]*" + re.escape(CLOSE_OUT_LEFT_OPEN) + r"\b", re.M)


def _ends_on_left_open(text: str) -> bool:
    """Whether the helper's "left open" line is the message's last non-blank
    line: quoted mid-message and followed by a question, it ends nothing."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return bool(lines) and bool(_LEFT_OPEN_RE.match(lines[-1]))

# The agent's own TL;DR, when it left one. The transcript tail above is
# developer prose aimed at a terminal — paths, pids, backticks, code
# identifiers — and what a status row needs is "what do you want from me" in
# one plain sentence. The instruction to write one rides in with the
# SessionStart hook (see NOTIFY_SCRIPT in dark_army_menubar/hooks.py), and
# the agent answers behind an HTML comment: `<!-- bob-tldr: … -->`. A comment
# because every other shape fails a test this one passes: a bare `TL;DR:`
# prefix occurs in real prose and models re-spell it (`**TL;DR** —`); an
# invented element (`<tldr>`) is literal angle-bracket noise wherever the
# message is rendered; a fenced block draws a box around metadata, which is
# the opposite of its weight. A namespaced comment never occurs by accident,
# vanishes in any real markdown renderer, and still reads as an annotation if
# a terminal shows it raw. Cut from the *head*, unlike last_text: a summary
# leads with its point, and anything past the cap is the one-sentence
# instruction being ignored, not content worth chasing.
MAX_SUMMARY_CHARS = 200
_TLDR_RE = re.compile(r"<!--\s*bob-tldr:\s*(.*?)\s*-->", re.IGNORECASE | re.DOTALL)

# The choices the agent is actually offering, when it is offering any:
# `<!-- bob-actions: Accept | Iterate | Ship it -->`. Same shape as the summary
# above and for the same reasons, but answering a different question — the
# summary says *what* is wanted, this says *what the answers are*.
#
# It exists because the panel used to draw an **Accept** button on every
# reachable row that had stopped, whether or not anything had been proposed.
# That is a button inventing a question: a turn that ended with a status report
# offered the same "Accept" as one that ended with a plan, and pressing it sent
# the word "accept" at an agent that had asked nothing. Guessing from the prose
# was the alternative and it is worse — the failure mode of a heuristic here is
# a confident button for a choice that does not exist, which is exactly the bug.
# So the agent declares them or there are none, and "none" is a perfectly good
# answer: the reply field and a Send button can say anything a button can.
MAX_ACTIONS = 3           # a row is 460pt wide, and a fourth choice is a menu
MAX_ACTION_CHARS = 24     # a button label, not a sentence
_ACTIONS_RE = re.compile(r"<!--\s*bob-actions:\s*(.*?)\s*-->",
                         re.IGNORECASE | re.DOTALL)


# The next-step marker (`docs/session-state-contract.md`): one line beside the
# `## Work done` report saying the finished work needs something done to Dark
# Army itself to take effect. A comment for the two existing markers' reasons,
# `dark-army-` and not `bob-` because the `bob` list is closed, and a closed
# vocabulary because both clients switch on the value and it is never free text.
# Parsed here by the daemon, never by the hook handler.
_NEXT_RE = re.compile(r"<!--\s*dark-army-next:\s*([a-z-]+)\s*-->", re.IGNORECASE)
NEXT_STEPS = ("rebuild",)


def _parse_actions(raw: str) -> list[str]:
    """The declared choices, in the order they were offered.

    Deliberately strict about shape and completely incurious about meaning: a
    label is sent back verbatim as the reply, so what "Iterate" means is the
    agent's business, exactly as it would be if the word had been typed in the
    terminal. Empties and duplicates go, everything else is trusted to be a
    word the agent will recognise — it wrote it.

    An over-long label is **refused, not trimmed**. The cap used to be a slice,
    which is the one thing that cannot be done to a string that is also the
    reply: `Commit only price-history` became the button `Commit only
    price-histor`, and pressing it typed that fragment at the agent — a word it
    never wrote, which is precisely what "sent back verbatim" is supposed to
    rule out. Observed live as the option `Add the plan and build i`.

    And a refusal takes the **whole declaration** with it, rather than dropping
    the offending label and drawing the rest. Three choices rendered as two is
    not a smaller menu, it is a wrong one: the reader presses the best of what
    is shown without knowing an option was withheld. With no buttons the row
    falls back to the reply field, which can say anything a button can — the
    same "none is a good answer" the marker was built on.
    """
    out: list[str] = []
    for part in raw.split("|"):
        label = " ".join(part.split())
        if not label:
            continue
        if len(label) > MAX_ACTION_CHARS:
            return []
        if label.lower() not in {o.lower() for o in out}:
            out.append(label)
    return out[:MAX_ACTIONS]


# Grok cannot receive the SessionStart stdout hint that teaches Claude to
# emit `bob-actions`, and its `ask_user_question` tool is sometimes
# advertised then refused (`unknown_tool`). The fallback the model actually
# writes is a short numbered list. Guessing buttons from prose is the thing
# this module refuses for Claude; Grok is the exception because without this
# the pane has no buttons at all for a question the agent did ask.
_NUMBERED_ITEM_RE = re.compile(
    r"(?m)^[ \t]*(\d+)[.)][ \t]+(?:\*\*|__)?([^*\n_]+?)(?:\*\*|__)?(?:[ \t]*[—–-].*)?[ \t]*$"
)
_WORK_DONE_RE = re.compile(r"(?m)^## Work done\b")


def _parse_numbered_actions(text: str) -> list[str]:
    """Short labels from a 2–3 item numbered list, or `[]`.

    Consecutive `1.` / `2.` (or `1)`) starting at 1, each label at most
    `MAX_ACTION_CHARS`, and only when the message is actually a question
    (`?` somewhere in it). A Work-done report, a single item, a fourth
    item, or any over-long label refuses the whole list — same "none is
    better than a wrong menu" rule as `_parse_actions`.
    """
    if not text or _WORK_DONE_RE.search(text) or "?" not in text:
        return []
    items: list[tuple[int, str]] = []
    for match in _NUMBERED_ITEM_RE.finditer(text):
        label = " ".join(match.group(2).split()).rstrip(".:")
        if not label:
            return []
        items.append((int(match.group(1)), label))
    if not (2 <= len(items) <= MAX_ACTIONS):
        return []
    if [n for n, _ in items] != list(range(1, len(items) + 1)):
        return []
    out: list[str] = []
    for _, label in items:
        if len(label) > MAX_ACTION_CHARS:
            return []
        if label.lower() in {o.lower() for o in out}:
            return []
        out.append(label)
    return out


def _multi_select_flag(raw: dict) -> bool:
    """Claude writes `multiSelect`; Grok writes `multi_select`. Either
    spelling is a real bool or the question is pick-one — never a truthy
    string, because that would type the multi-select key burst at a
    pick-one widget."""
    return raw.get("multiSelect") is True or raw.get("multi_select") is True


def _has_preview_flag(raw: dict) -> bool:
    """Whether any option carries a non-empty `preview` string.

    The terminal draws a pick-one question with previews in a different
    layout — option list beside the focused option's preview — where a digit
    only moves focus and Enter selects. The daemon's burst follows the digit
    with an Enter on exactly these questions (`PREVIEW_PICK_ONE_SUBMIT_KEYS`),
    so the flag is a strict bool, computed over every option the tool sent
    rather than the clipped few, because the client's own test is
    `options.some(o => o.preview !== undefined)`."""
    for option in (raw.get("options") or []):
        if isinstance(option, dict):
            preview = option.get("preview")
            if isinstance(preview, str) and preview.strip():
                return True
    return False


# Where the tail cut lands, and how it says so. `text[-1200:]` on a long turn
# slices the message at whatever character happens to sit 1200 back, so the
# unfolded row opened mid-word — observed as "era, a każdy punkt…", the back
# half of a word, which reads as a corrupted payload rather than as a long
# message with its head left off. Nothing downstream can repair that: the panel
# is handed a string and draws it.
#
# So the cut walks forward to the nearest structural boundary — a paragraph
# break first (the message's own units, kept by _collapse_prose), then a line
# break, then the end of a sentence, then at worst a space — and the result
# opens with an ellipsis, so a shortened message is legible *as* shortened. The
# search is confined to the first third of the tail: past that, honouring a
# boundary costs more content than the ragged edge was worth, and the space
# fallback is always available inside it.
_ELLIPSIS = "…"
_TAIL_WINDOW_FRACTION = 3
_SENTENCE_END_RE = re.compile(r"[.!?…:;]\s")


def _tail_prose(text: str, limit: int = MAX_LAST_TEXT_CHARS) -> str:
    """The last ``limit`` characters, cut at a boundary and marked as cut."""
    if len(text) <= limit:
        return text
    # Room for the marker, so the payload cap still holds.
    tail = text[-(limit - len(_ELLIPSIS) - 1):]
    window = tail[: max(1, len(tail) // _TAIL_WINDOW_FRACTION)]
    cut = -1
    for sep in ("\n\n", "\n"):
        found = window.find(sep)
        if found != -1:
            cut = found + len(sep)
            break
    if cut == -1:
        match = _SENTENCE_END_RE.search(window)
        if match:
            cut = match.end()
    if cut == -1:
        found = window.find(" ")
        if found != -1:
            cut = found + 1
    if cut != -1:
        tail = tail[cut:]
    return _ELLIPSIS + " " + tail.lstrip()


def _work_report(text: str) -> str:
    """The `## Work done` report inside this message, or "".

    From the heading to the end of the message: the hint puts the report last,
    and everything after the heading is the report. Head-cut and marked with
    the same ellipsis `_tail_prose` uses, so a long report is legible *as*
    shortened rather than as a corrupted payload.
    """
    match = None
    for match in _WORK_REPORT_RE.finditer(text):
        pass
    if match is None:
        return ""
    return _tail_prose(text[match.start():].strip(), MAX_LAST_REPORT_CHARS)


def _collapse_prose(text: str) -> str:
    """Squeeze the whitespace *inside* each line, keep the lines.

    This used to be `" ".join(text.split())`, which flattens the message to a
    single run of words — and the panel then rendered that run verbatim. A turn
    that closes with a numbered list and a two-item caveat came out as one
    unbroken paragraph with `- **`pnpm gate`...` sitting mid-sentence: every
    structural cue the agent wrote was thrown away here, before any surface
    could decide what to do with it. The panel's caption is two lines and wants
    one flat run, but that is the caption's business — it flattens what it
    shows; the unfolded message keeps the paragraphs.

    Runs of blank lines collapse to one — the payload rides in every agents
    push, and three blank lines look the same as one when rendered.

    **A fenced code block is kept as written**, trailing spaces aside. Its
    spacing is its meaning: the crew's ASCII banners are boxes whose right
    edge lines up only when every run of spaces survives, and squeezing them
    here drew every box with a ragged right side on the panel and the phone.
    A fence opens on a line starting with three backticks or tildes and
    closes on the next such line; an unclosed fence runs to the end.
    """
    out: list[str] = []
    fence = ""
    for raw in text.splitlines():
        marker = raw.lstrip()[:3]
        if fence:
            out.append(raw.rstrip())
            if marker == fence:
                fence = ""
            continue
        if marker in ("```", "~~~"):
            fence = marker
            out.append(raw.strip())
            continue
        line = " ".join(raw.split())
        if not line and (not out or not out[-1]):
            continue
        out.append(line)
    return "\n".join(out).strip()


@dataclass
class AgentInfo:
    """What a spawned subagent is doing, recovered from the parent transcript.

    Claude Code writes the spawn record (``toolUseResult.agentId`` +
    ``description``) at *launch*, ~40-70ms before the SubagentStart hook reaches
    the daemon — so a running subagent is labellable, not just a finished one."""

    agent_id: str = ""
    description: str = ""          # e.g. "Verify LVGL clone and version"
    subagent_type: str = ""        # e.g. "Explore", "general-purpose"
    model: str = ""
    parent_agent_id: str = ""      # set only for nested spawns (see load_subagent_meta)
    # Runtime state when a provider publishes it. Claude's transcript metadata
    # predates this field and leaves it empty; Codex collaboration items report
    # pendingInit/running/completed/etc explicitly.
    activity: str = ""


@dataclass
class SessionStats:
    """Rolled-up usage for one session's transcript."""

    model: Optional[str] = None            # primary (most-used real) model
    models: list[str] = field(default_factory=list)  # all real models, most-used first
    effort: str = ""                       # reasoning effort of the latest turn
    fast: bool = False                     # latest turn ran in fast mode
    input_tokens: int = 0                  # fresh (uncached) input tokens
    output_tokens: int = 0
    cache_read_tokens: int = 0             # context re-read from cache (large)
    cache_creation_tokens: int = 0         # tokens written to cache
    first_ts: Optional[datetime] = None
    last_ts: Optional[datetime] = None
    assistant_messages: int = 0            # ≈ number of model turns
    user_prompts: int = 0                  # user turns that aren't tool results
    tool_counts: dict[str, int] = field(default_factory=dict)
    files_touched: int = 0                 # distinct files edited/written
    agents: dict = field(default_factory=dict)  # agent_id -> AgentInfo
    # recipient name -> {"count": int, "last": iso-timestamp}. Who this session
    # has messaged, in the address vocabulary another agent would type. Bounded
    # at MAX_MESH_EDGES; resolving a name to a session is the daemon's job, not
    # this module's, because it needs the live registry.
    sent_to: dict = field(default_factory=dict)
    sent_to_partial: bool = False
    # The question this session is *currently* stopped on, or {}. Shape:
    # {"text": str, "options": [str], "header": str, "id": str}. Cleared the
    # moment the transcript carries its answer, so a row never shows a question
    # that has already been answered — which would be worse than showing none,
    # since it would send you to a terminal with nothing to do. The id is the
    # tool_use id, published so a verdict aimed at a question that has since
    # been answered can *miss* rather than land on its successor — the same
    # argument `answer_permission` makes for `request_id`.
    question: dict = field(default_factory=dict)
    # Every question of that same dialog, in order — the flat dict above is its
    # first element for every surface from before the list existed. Elements
    # add an `index` (0-based position in the dialog) and share the one
    # tool_use id, because the dialog is one tool call. Written and cleared
    # together with `question`, never separately.
    questions: list = field(default_factory=list)
    # Internal bounded outcomes, consumed only by durable decision capture.
    question_results: list = field(default_factory=list)
    # The tail of the agent's most recent message, trimmed to a caption. What
    # you are being asked to accept, in the agent's own words, for the very
    # common wait that has no tool call behind it at all.
    last_text: str = ""
    # The agent's own plain-language summary of that message, parsed from the
    # `bob-tldr` marker — or "" when the latest message carries none. Cleared
    # rather than carried forward: a summary describes the message beside it,
    # and an old one standing under new words would send someone to a terminal
    # expecting a different question.
    last_summary: str = ""
    # The last completion report this session wrote (`## Work done` onwards),
    # kept across later messages and cleared by the next human prompt. See
    # `MAX_LAST_REPORT_CHARS` for why it is not simply `last_text`.
    last_report: str = ""
    # The choices that message offered, from the `bob-actions` marker — the
    # labels a surface may draw as buttons, each sent back verbatim as the
    # reply. Empty for nearly every turn, which is the point: no declaration,
    # no buttons, just the reply field.
    last_actions: list = field(default_factory=list)
    # The transcript's own clocks for the two markers above (epoch seconds,
    # `0.0` where the line carried no timestamp): `marker_at` is when the
    # standing `bob-tldr` / `bob-actions` were written, `prompt_at` when the
    # person last sent a genuine prompt. `alerts.marker_current` compares
    # them, so choices offered before the person's last prompt are never read
    # as this turn's question even if a clearing below were missed (S5,
    # the false-buzz investigation of 20 Sep 2026).
    marker_at: float = 0.0
    prompt_at: float = 0.0
    # When the report that carried `dark-army-next: rebuild` was written (epoch
    # seconds, 0.0 for none). Deliberately **not** `marker_at`: that one makes
    # a row "waiting on somebody"; this one only offers a button. Belongs to
    # the report — it survives chatter, and the person's next prompt clears it
    # where `last_report` clears.
    rebuild_marker_at: float = 0.0
    # Where the work is happening. Both are stamped on most transcript lines by
    # Claude Code itself, so they are known for any session with a transcript —
    # unlike the statusline's copies, which arrive only after the first tick.
    # Latest line wins: a `git checkout` mid-session should move the branch a row
    # displays. It must not move the *nickname*, which is why identity is keyed
    # on the directory alone (see identity.py).
    git_branch: str = ""
    cwd: str = ""

    @property
    def duration_seconds(self) -> float:
        """Wall-clock span of the transcript (first → last event)."""
        if self.first_ts and self.last_ts:
            return max(0.0, (self.last_ts - self.first_ts).total_seconds())
        return 0.0

    @property
    def total_input_tokens(self) -> int:
        """Fresh input plus tokens written to cache — the non-cache-read input."""
        return self.input_tokens + self.cache_creation_tokens

    @property
    def total_tool_calls(self) -> int:
        return sum(self.tool_counts.values())

    @property
    def output_tokens_per_sec(self) -> float:
        d = self.duration_seconds
        return (self.output_tokens / d) if d > 0 else 0.0


def _parse_ts(value) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        # Transcripts use e.g. "2026-07-25T21:58:09.887Z"; normalise the Z.
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _epoch(ts: Optional[datetime]) -> float:
    """A parsed transcript timestamp as epoch seconds, `0.0` for none."""
    if ts is None:
        return 0.0
    try:
        return float(ts.timestamp())
    except (OverflowError, OSError, ValueError):
        return 0.0


#: What a `user` record opens with when Claude Code wrote it rather than the
#: person asking the agent for something: a local command's caveat and
#: output, a `!` shell-mode input and its output, a background task's
#: resumption, an injected reminder.
_NOT_A_PROMPT_PREFIXES = (
    "<task-notification>",
    "<local-command-caveat>",
    "<local-command-stdout>",
    "<local-command-stderr>",
    "<system-reminder>",
    "<user-prompt-submit-hook>",
    "<bash-input>",
    "<bash-stdout>",
    "<bash-stderr>",
)
#: The opening tag of the person's own words arriving through Dark Army's
#: channel: `reply_to_session` pushes `kind="user"`, and Claude Code writes it
#: as an `isMeta` `user` record with `origin.kind == "channel"`. The source
#: name is matched as any non-empty name, never pinned: sessions born before
#: the rename say the old name for their whole life, the alias-window plan
#: greps the old `source=` literal under this package to zero, and
#: `test_product_name` refuses the bare old name here. Both names are pinned
#: in the tests instead.
_CHANNEL_USER_RE = re.compile(r'<channel source="[^"\s]+" kind="user">')


def _is_channel_user_message(obj: dict, lead: str) -> bool:
    """Whether a `user` record is the person replying through Dark Army's
    channel (panel or phone): the harness's `origin.kind == "channel"` stamp
    **and** ``lead`` (the record's text, left-stripped) opening with the
    `kind="user"` tag. Neither alone: skill expansions and another session's
    hand-back quote the tag without that origin, and a `kind="fleet"` event
    carries the origin without the tag."""
    origin = obj.get("origin")
    return (
        isinstance(origin, dict)
        and origin.get("kind") == "channel"
        and _CHANNEL_USER_RE.match(lead) is not None
    )


#: A slash command's echo opens with one of these. It is decided by the
#: command's *name*, never by the tag: a built-in in `_LOCAL_COMMANDS` sends
#: the agent nothing, while a skill or custom command (`/ship implement …`,
#: `/review`) is the person's request — its expansion follows as an `isMeta`
#: record, so the echo is the only record that can say so.
#: `ai_title._SYNTHETIC_PROMPT_PREFIXES` is covered by these two tuples
#: together (pinned by `test_session_stats.py`; not imported, because
#: `ai_title` → `transcript_scan` → this module is a cycle).
_COMMAND_ECHO_PREFIXES = ("<command-name>", "<command-message>", "<command-args>")
#: Claude Code's built-in commands that run locally and send the agent no
#: request of their own. `/compact` is here: Dark Army's own `autocompact.py`
#: types it at sessions parked on a question.
_LOCAL_COMMANDS = frozenset({
    "compact", "context", "cost", "clear", "model", "status", "help",
    "config", "settings", "usage", "login", "logout", "doctor", "memory",
    "permissions", "allowed-tools", "hooks", "mcp", "agents", "resume",
    "exit", "quit", "statusline", "terminal-setup", "vim", "theme",
    "export", "ide", "add-dir", "bug", "feedback", "release-notes",
    "upgrade", "privacy-settings", "output-style", "rewind", "todos",
    "fast", "effort", "plugin", "plugins", "bashes", "tasks", "stats",
    "rename", "sandbox", "install-github-app", "migrate-installer",
})
_COMMAND_NAME_RE = re.compile(r"<command-name>\s*/?([^<\s]+)\s*</command-name>")


def _is_person_prompt(obj: dict, content) -> bool:
    """Whether a non-tool-result `user` record is the person sending a new
    request — what may clear last turn's markers and stamp `prompt_at`.

    Not a prompt: a record Claude Code flags `isMeta` (its own caveats, a
    skill's expanded text) or `isCompactSummary` (the summary a `/compact`
    writes); one whose text opens with `_NOT_A_PROMPT_PREFIXES`; and a
    command echo naming a built-in in `_LOCAL_COMMANDS`. A `/compact` writes
    four records, none a tool result and none the person asking anything, and
    reading them as a prompt wiped the question a session was parked on. Any
    other command echo — a skill, a custom command, or one whose name cannot
    be read — is a prompt. Content may be a string or a list of blocks; the
    first text block decides."""
    if obj.get("isMeta") or obj.get("isCompactSummary"):
        return False
    text = ""
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text = str(item.get("text") or "")
                break
    lead = text.lstrip()
    if lead.startswith(_NOT_A_PROMPT_PREFIXES):
        return False
    if lead.startswith(_COMMAND_ECHO_PREFIXES):
        name = _COMMAND_NAME_RE.search(lead)
        return not (name and name.group(1).lower() in _LOCAL_COMMANDS)
    return True


def _is_tool_result(content) -> bool:
    if isinstance(content, list):
        return any(
            isinstance(it, dict) and it.get("type") == "tool_result" for it in content
        )
    return False


def _collect_spawn_types(content, out: dict) -> None:
    """Record ``tool_use id -> subagent_type`` for any Agent/Task call in an
    assistant message. The spawn's own record carries only a description, so
    this is the sole source of the agent's type."""
    if not isinstance(content, list):
        return
    for item in content:
        if not isinstance(item, dict) or item.get("type") != "tool_use":
            continue
        if item.get("name") not in _SPAWN_TOOLS or not item.get("id"):
            continue
        tin = item.get("input")
        if isinstance(tin, dict) and tin.get("subagent_type"):
            out[str(item["id"])] = str(tin["subagent_type"])


def _tool_use_id_of(content) -> str:
    """The tool_use_id carried by a tool_result message, or "" if absent.

    Links a subagent spawn record back to the Agent/Task call that made it, which
    is where subagent_type lives (the spawn record itself has only description)."""
    if isinstance(content, list):
        for it in content:
            if isinstance(it, dict) and it.get("type") == "tool_result":
                return str(it.get("tool_use_id") or "")
    return ""


@dataclass
class _Accum:
    """Resumable state for an incrementally-parsed transcript.

    Everything :func:`parse_transcript` used to keep in locals lives here, so a
    transcript that grew by three lines costs three lines instead of its whole
    size. ``offset`` is a byte position, and it only ever advances past a
    complete line — a live session is appending while we read, and resuming from
    the middle of a half-written record would corrupt every count after it."""

    stats: SessionStats = field(default_factory=SessionStats)
    model_counts: dict[str, int] = field(default_factory=dict)
    files: set[str] = field(default_factory=set)
    # tool_use id -> subagent_type, so a spawn record can recover the agent type.
    # The spawn's tool_use row always precedes its tool_result row, but the two
    # may now land in different chunks — hence carrying this across reads.
    spawn_types: dict[str, str] = field(default_factory=dict)
    # The tool_use id of the question the session is stopped on. Held here
    # rather than in the stats because it is bookkeeping: the *answer* arrives
    # as a tool_result carrying this id, and matching on the id is the only way
    # to tell "answered" from "asked again".
    question_id: str = ""
    offset: int = 0


def _fold_line(line: str, acc: _Accum) -> None:
    """Fold one transcript line into ``acc``. Malformed lines are skipped."""
    line = line.strip()
    if not line:
        return
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return

    stats = acc.stats
    model_counts = acc.model_counts
    files = acc.files
    spawn_types = acc.spawn_types

    ts = _parse_ts(obj.get("timestamp"))
    if ts is not None:
        if stats.first_ts is None or ts < stats.first_ts:
            stats.first_ts = ts
        if stats.last_ts is None or ts > stats.last_ts:
            stats.last_ts = ts

    otype = obj.get("type")
    msg = obj.get("message")
    msg = msg if isinstance(msg, dict) else {}

    # Read before the synthetic-model gate below, and outside the `assistant`
    # branch entirely: these are line-level facts about the session, not part of
    # token accounting, and the first lines of a transcript carry neither a
    # message nor a model.
    branch = obj.get("gitBranch")
    if isinstance(branch, str) and branch:
        stats.git_branch = branch
    cwd = obj.get("cwd")
    if isinstance(cwd, str) and cwd:
        stats.cwd = cwd

    # Subagent spawn types, harvested BEFORE the synthetic-model gate
    # below. That gate exists for token accounting; letting it also
    # decide whether we learn a subagent's type would silently drop
    # labels for reasons that have nothing to do with labelling.
    if otype == "assistant":
        _collect_spawn_types(msg.get("content"), spawn_types)

    if otype == "assistant":
        model = msg.get("model")
        # Skip synthetic completions (local/non-inference) entirely —
        # they aren't real turns and carry no billable usage.
        if not isinstance(model, str) or model in _SYNTHETIC_MODELS:
            return
        stats.assistant_messages += 1
        model_counts[model] = model_counts.get(model, 0) + 1

        # Effort and fast mode are per-turn and switchable mid-session
        # (/fast, effort picker), so the *latest* turn wins rather than
        # the most common one — the menu answers "what is it running
        # on now", not "what did it mostly run on".
        effort = obj.get("effort")
        if isinstance(effort, str) and effort:
            stats.effort = effort

        usage = msg.get("usage")
        if isinstance(usage, dict):
            stats.fast = usage.get("speed") == "fast"
            stats.input_tokens += int(usage.get("input_tokens", 0) or 0)
            stats.output_tokens += int(usage.get("output_tokens", 0) or 0)
            stats.cache_read_tokens += int(
                usage.get("cache_read_input_tokens", 0) or 0
            )
            stats.cache_creation_tokens += int(
                usage.get("cache_creation_input_tokens", 0) or 0
            )

        content = msg.get("content")
        if isinstance(content, list):
            # The agent's own words, latest wins. Taken from the *end* of the
            # message rather than the start: a turn opens with what it did and
            # closes with what it wants, and the closing line is the one a row
            # is being asked to summarise.
            summary = None
            actions = None
            spoke = False
            next_token = ""
            reported = False
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    raw = str(item.get("text") or "")
                    marks = _TLDR_RE.findall(raw)
                    if marks:
                        # Last marker in the message wins, same as the text —
                        # and the marker is metadata, not prose, so it is cut
                        # out of the tail the chevron will show in full.
                        summary = " ".join(marks[-1].split())[:MAX_SUMMARY_CHARS]
                        raw = _TLDR_RE.sub(" ", raw)
                    nexts = _NEXT_RE.findall(raw)
                    if nexts:
                        next_token = nexts[-1].lower()
                        raw = _NEXT_RE.sub(" ", raw)
                    offers = _ACTIONS_RE.findall(raw)
                    if offers:
                        actions = _parse_actions(offers[-1])
                        raw = _ACTIONS_RE.sub(" ", raw)
                    text = _collapse_prose(raw)
                    if text:
                        stats.last_text = _tail_prose(text)
                        spoke = True
                        report = _work_report(text)
                        if report:
                            stats.last_report = report
                            reported = True
            # A marker with no report in the same message is noise: an agent
            # waiting on the person must not light a button. A fresh report
            # without the marker withdraws an older offer.
            if reported:
                stats.rebuild_marker_at = (
                    _epoch(ts) if next_token in NEXT_STEPS else 0.0)
            # The summary is a caption *for* last_text, so the two move
            # together: a marker sets it, a new message without one clears it,
            # and a tool-only turn — which does not move last_text either —
            # leaves both alone. Parsed here, inside the assistant branch, so a
            # marker pasted into a *user* message is never read at all.
            if summary is not None:
                stats.last_summary = summary
            elif spoke:
                stats.last_summary = ""
            # The offered choices move with the message that offered them, and
            # are cleared by any later message that offers none — a button for
            # a decision two turns old is worse than no button, because it
            # still sends the word.
            if actions is not None:
                stats.last_actions = actions
            elif spoke:
                stats.last_actions = []
            # When the standing markers were written: stamped by the message
            # that carried either, reset with them by a message that spoke
            # without one.
            if summary is not None or actions is not None:
                stats.marker_at = _epoch(ts)
            elif spoke:
                stats.marker_at = 0.0
            for item in content:
                if not isinstance(item, dict) or item.get("type") != "tool_use":
                    continue
                name = item.get("name") or "?"
                stats.tool_counts[name] = stats.tool_counts.get(name, 0) + 1
                if name in _EDIT_TOOLS:
                    tin = item.get("input")
                    if isinstance(tin, dict):
                        for key in _FILE_INPUT_KEYS:
                            if tin.get(key):
                                files.add(str(tin[key]))
                                break
                elif name in _MESSAGE_TOOLS:
                    _record_message(stats, item.get("input"),
                                    obj.get("timestamp"))
                elif name in _QUESTION_TOOLS:
                    _record_question(acc, item)

    elif otype == "user":
        # Count genuine prompts, not tool-result carrier messages.
        if not _is_tool_result(msg.get("content")):
            stats.user_prompts += 1
            # A new request supersedes the last one's report. Only a genuine
            # prompt clears it — a tool result is the same turn still running.
            stats.last_report = ""
            stats.rebuild_marker_at = 0.0
        # And last turn's caption and offered choices: they answered the
        # previous request, and standing under this one they read as a
        # question nobody is asking (RC2, 22 Sep 2026). Blank until the agent
        # writes again; `prompt_at` is the clock the markers are checked
        # against (`alerts.marker_current`). Only the person's own prompt
        # does this — never a slash command's records or a meta turn
        # (`_is_person_prompt`), or `/compact` at a question would erase it.
        if (not _is_tool_result(msg.get("content"))
                and _is_person_prompt(obj, msg.get("content"))):
            stats.last_summary = ""
            stats.last_actions = []
            stats.marker_at = 0.0
            stats.prompt_at = _epoch(ts)

        # The answer to a pending question arrives as its tool_result. Matched
        # on the id rather than on "any tool_result": a session asks a question
        # and then keeps working, and every Read in between would otherwise
        # clear a question nobody has answered.
        if acc.question_id and _answers_question(msg.get("content"),
                                                 acc.question_id):
            for result in msg.get("content") or []:
                if (isinstance(result, dict) and result.get("type") == "tool_result"
                        and str(result.get("tool_use_id") or "") == acc.question_id
                        and result.get("is_error") is not True):
                    content = result.get("content")
                    if isinstance(content, list):
                        content = "\n".join(str(part.get("text") or "") for part in content
                                            if isinstance(part, dict) and part.get("type") == "text")
                    if isinstance(content, str) and content:
                        stats.question_results.append({"source_id": acc.question_id,
                            "outcome": content[:2000], "truncated": len(content) > 2000})
                        del stats.question_results[:-32]
            acc.question_id = ""
            stats.question = {}
            stats.questions = []

        # A subagent spawn lands here as the Agent/Task tool_result.
        # Written at launch, so this names *running* subagents too.
        tur = obj.get("toolUseResult")
        if isinstance(tur, dict) and tur.get("agentId"):
            aid = str(tur["agentId"])
            stats.agents[aid] = AgentInfo(
                agent_id=aid,
                description=str(tur.get("description") or ""),
                subagent_type=spawn_types.get(
                    _tool_use_id_of(msg.get("content")), ""
                ),
                model=str(tur.get("resolvedModel") or ""),
            )


def _record_message(stats: SessionStats, tool_input, timestamp) -> None:
    """Note that this session addressed another agent.

    The recipient is taken from the tool input under whichever key names it, and
    kept **as typed**: it is an address in Claude Code's own registry, and
    normalising it here would break the only join that can resolve it back to a
    session. The message body is deliberately never read — who is talking to
    whom is a fact about the fleet, what they said is the user's.
    """
    if not isinstance(tool_input, dict):
        return
    to = ""
    for key in _RECIPIENT_KEYS:
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            to = value
            break
    if not to:
        return
    edge = stats.sent_to.get(to)
    if edge is None:
        if len(stats.sent_to) >= MAX_MESH_EDGES:
            stats.sent_to_partial = True
            return
        edge = stats.sent_to[to] = {"count": 0, "last": ""}
    edge["count"] += 1
    if isinstance(timestamp, str) and timestamp:
        edge["last"] = timestamp


def clip(text: str, cap: int) -> str:
    """`text` at most `cap` characters, ending in `…` when anything was lost.

    A bare `text[:cap]` is a silent cut, and a silently cut question reads as a
    finished sentence that happens to be strange — the reader blames the agent
    for asking badly instead of scrolling to the terminal for the rest. One
    character of ellipsis says "there was more" and costs nothing.

    The result is never longer than `cap`, unconditionally — including the
    degenerate caps no caller passes today — so `cap` stays the honest bound
    it claims to be rather than one with an exception in it.
    """
    if cap <= 0:
        return ""
    if len(text) <= cap:
        return text
    return text[:cap - 1] + "…"


def questions_from_tool_input(tool_input, question_id: str = "") -> list:
    """Every question of an `AskUserQuestion`, built from the tool's own input.

    Two callers read the same objects out of two different places, so the shape
    is written once. This module finds the tool call in the **transcript**; the
    hook path (`protocol.hook_payload_to_daemon_message`) finds the identical
    `tool_input` on **PreToolUse** — which is the only place it exists while the
    dialog is actually up. Claude Code does not write the assistant turn that
    asks until the answer arrives: measured on a live session, the transcript's
    last record was the tool_result *before* the question, unchanged for the six
    minutes the dialog stood, with the whole asking turn — thinking, prose and
    the `tool_use` — flushed only alongside its `tool_result`. So a reader of
    the transcript alone learns what was asked exactly once it no longer
    matters, which is why the panel showed a bare "Waiting" on the one row that
    had something to say.

    The tool takes one to four questions per call, each with its own header
    and options, and the dialog answers them in order — so the whole list is
    carried, capped at `MAX_QUESTIONS`, each element
    `{text, options, details, header, id, index, multi_select, has_preview}`
    with `index` the question's 0-based position in the dialog, `details` the
    option `description`s in the same order as `options` (empty strings
    where the tool sent none; never Claude's `preview`), `multi_select` the
    tool's own `multiSelect` flag as a strict bool — the terminal draws a
    flagged question with a different widget (tick boxes and a Submit
    button), so the answer verbs type a different burst for it — and
    `has_preview` whether any option carries a preview, which picks a third
    layout where a digit only focuses. Every question shares the one
    tool_use id, because
    the dialog is one tool call. Returns `[]` for anything that is not a
    dialog we can draw — an empty list is the "no questions" value everywhere
    downstream.
    """
    if not isinstance(tool_input, dict):
        return []
    raw_questions = tool_input.get("questions")
    if not isinstance(raw_questions, list) or not raw_questions:
        return []
    out = []
    for position, raw in enumerate(raw_questions[:MAX_QUESTIONS]):
        if not isinstance(raw, dict):
            continue
        text = str(raw.get("question") or "").strip()
        if not text:
            continue
        options = []
        details = []
        for option in (raw.get("options") or [])[:MAX_OPTIONS]:
            if isinstance(option, dict):
                label = str(option.get("label") or "").strip()
                if label:
                    options.append(clip(label, MAX_OPTION_CHARS))
                    desc = str(option.get("description") or "").strip()
                    details.append(clip(desc, MAX_DETAIL_CHARS) if desc else "")
        out.append({
            "text": clip(text, MAX_QUESTION_CHARS),
            "options": options,
            "details": details,
            "header": clip(str(raw.get("header") or "").strip(), MAX_OPTION_CHARS),
            # Published, not just book-kept: `answer_question` matches on it,
            # so an answer aimed at a question the terminal already dealt with
            # misses instead of choosing an option of whatever was asked next.
            # Empty is tolerated at both ends — a hook payload may carry no
            # tool_use id.
            "id": str(question_id or ""),
            "index": position,
            "multi_select": _multi_select_flag(raw),
            "has_preview": _has_preview_flag(raw),
        })
    return out


def question_from_tool_input(tool_input, question_id: str = "") -> dict:
    """The first question of the dialog, in the flat wire shape.

    The flat `question` key predates the `questions` list and is what every
    older panel and phone decodes, so it is never renamed and never becomes a
    list — this wrapper keeps it exactly as it always was: the first question,
    keys `{text, options, details, header, id, multi_select, has_preview}`,
    `{}` where there is no
    dialog. New
    surfaces read the list; old ones read this and are refused in words when
    a one-question answer would leave the rest of the dialog standing.
    """
    every = questions_from_tool_input(tool_input, question_id)
    if not every:
        return {}
    return {k: v for k, v in every[0].items() if k != "index"}


def clamp_question(raw) -> dict:
    """Re-apply the caps to a question that arrived over the wire.

    The hook handler trims before it sends, but it is a *file on disk* outside
    the bundle and a version behind whenever the app has not reinstalled it yet
    — so what arrives is shaped by whichever copy is installed, not by this
    one. The caps are cheap and the payload rides in every agents push; a
    question is re-cut here rather than trusted, which also keeps an
    `AskUserQuestion` with a paragraph-long option preview from ever reaching
    the panel through the shorter route.

    `multi_select` and `has_preview` ride through as strict bools and
    default to False, so an older installed hook (which never sends the keys)
    and an older `sessions.json` envelope both read as a plain pick-one —
    nothing breaks, the question is merely drawn without its tick boxes, or
    answered with a digit alone, until the hook is reinstalled.
    """
    if not isinstance(raw, dict):
        return {}
    text = str(raw.get("text") or "").strip()
    if not text:
        return {}
    options = []
    for option in (raw.get("options") or [])[:MAX_OPTIONS]:
        label = str(option or "").strip()
        if label:
            options.append(clip(label, MAX_OPTION_CHARS))
    details = []
    raw_details = raw.get("details")
    if not isinstance(raw_details, list):
        raw_details = []
    for i, _label in enumerate(options):
        if i < len(raw_details):
            desc = str(raw_details[i] or "").strip()
            details.append(clip(desc, MAX_DETAIL_CHARS) if desc else "")
        else:
            details.append("")
    return {
        "text": clip(text, MAX_QUESTION_CHARS),
        "options": options,
        "details": details,
        "header": clip(str(raw.get("header") or "").strip(), MAX_OPTION_CHARS),
        "id": str(raw.get("id") or ""),
        "multi_select": raw.get("multi_select") is True,
        "has_preview": raw.get("has_preview") is True,
    }


def clamp_questions(raw) -> list:
    """Re-apply the caps to a whole dialog that arrived over the wire.

    `clamp_question`'s sibling, and the tolerance seam for an older installed
    hook: the message it re-cuts may carry the full `questions` list (a current
    handler), only the flat `question` (an installed copy from before the list
    existed), or be the bare flat dict itself (a persisted envelope value).
    A flat-only reading wraps into a one-element list — first-question-only
    until the app reinstalls the hook, which it does on every launch — and
    garbage of any shape is `[]`, never a raise. `index` is re-stamped from
    the element's own claim where it is a sane int, else from its position,
    so a hole left by an unreadable question does not renumber its neighbours.
    """
    if not isinstance(raw, dict):
        return []
    listed = raw.get("questions")
    if isinstance(listed, list) and listed:
        out = []
        for position, item in enumerate(listed[:MAX_QUESTIONS]):
            if not isinstance(item, dict):
                continue
            cut = clamp_question(item)
            if not cut:
                continue
            claimed = item.get("index")
            cut["index"] = (claimed if isinstance(claimed, int)
                            and not isinstance(claimed, bool)
                            and 0 <= claimed < MAX_QUESTIONS else position)
            out.append(cut)
        return out
    flat = raw.get("question")
    if isinstance(flat, dict):
        cut = clamp_question(flat)
        return [dict(cut, index=0)] if cut else []
    cut = clamp_question(raw)
    return [dict(cut, index=0)] if cut else []


def _record_question(acc: _Accum, item: dict) -> None:
    """Note the question this session has just stopped on.

    Deliberately unlike `_record_message`, which keeps the recipient and throws
    the body away: here the text *is* the whole value, and it is the agent's
    words to its own user rather than anything the user wrote. Only the latest
    question is kept — a session is stopped on one at a time, and the ones
    before it are answered history. One *dialog* at a time, that is: both the
    flat first question and the full list are written together and cleared
    together, so the two fields can never describe different dialogs.
    """
    every = questions_from_tool_input(item.get("input"),
                                      str(item.get("id") or ""))
    if not every:
        return
    acc.stats.question = {k: v for k, v in every[0].items() if k != "index"}
    acc.stats.questions = every
    acc.question_id = str(item.get("id") or "")


def _answers_question(content, question_id: str) -> bool:
    if not isinstance(content, list):
        return False
    return any(
        isinstance(it, dict) and it.get("type") == "tool_result"
        and str(it.get("tool_use_id") or "") == question_id
        for it in content
    )


def _finalize(acc: _Accum) -> SessionStats:
    """Fill in the fields derived from the whole run and return the stats."""
    stats = acc.stats
    stats.models = sorted(
        acc.model_counts, key=lambda m: acc.model_counts[m], reverse=True
    )
    stats.model = stats.models[0] if stats.models else None
    stats.files_touched = len(acc.files)
    return stats


def _tail_is_complete(tail: bytes) -> bool:
    """Whether trailing bytes with no newline are nonetheless a whole record.

    JSONL is one object per line, so a tail that already parses as JSON is a
    finished record still waiting for its newline — safe to consume. A tail
    caught mid-write is almost never valid JSON, so it fails here and is held
    back. Guessing by newline alone would drop the last record of any file that
    simply does not end in one."""
    tail = tail.strip()
    if not tail:
        return False
    try:
        json.loads(tail)
    except (ValueError, UnicodeDecodeError):
        return False
    return True


def _read_new(acc: _Accum, p: Path, hold_partial: bool = True) -> None:
    """Fold everything appended since ``acc.offset`` into ``acc``.

    With ``hold_partial`` an unterminated trailing record is left unconsumed
    unless it already parses (see :func:`_tail_is_complete`), so a live session
    appending as we read resumes cleanly instead of splitting a record in two.
    The one-shot :func:`parse_transcript` clears the flag and takes everything."""
    with p.open("rb") as f:
        f.seek(acc.offset)
        chunk = f.read()
    if not chunk:
        return
    if hold_partial:
        nl = chunk.rfind(b"\n")
        tail = chunk[nl + 1:]
        if tail and not _tail_is_complete(tail):
            if nl < 0:
                return
            chunk = chunk[: nl + 1]
    acc.offset += len(chunk)
    for line in chunk.decode("utf-8", errors="replace").splitlines():
        _fold_line(line, acc)


#: How much of a transcript's end `finished_quietly` reads: the closing
#: message of a turn is the last `assistant` record carrying text, and a
#: 25-line report plus the records around it fits in a fraction of this.
CLOSING_TAIL_BYTES = 256 * 1024


def _read_tail_bytes(path: str, limit: int) -> Optional[bytes]:
    """The last ``limit`` bytes of a regular file, or None.

    `background_watch._read_tail`'s rule: opened `O_RDONLY | O_NONBLOCK`
    and typed on the descriptor, so a FIFO at that name cannot block the
    loop and a device never seeks."""
    import stat as stat_mod
    fd = None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        st = os.fstat(fd)
        if not stat_mod.S_ISREG(st.st_mode):
            return None
        if st.st_size > limit:
            os.lseek(fd, -limit, os.SEEK_END)
        chunks = []
        remaining = limit
        while remaining > 0:
            block = os.read(fd, remaining)
            if not block:
                break
            chunks.append(block)
            remaining -= len(block)
        return b"".join(chunks)
    except (OSError, ValueError):
        return None
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


def closing_words(transcript_path: str) -> str:
    """The text of the last assistant message in the transcript, or "".

    The closing message is the last `assistant` record whose content carries
    a text block; a trailing tool-only record is the same turn still
    running and is skipped over. Reads a bounded tail once, on the caller's
    thread, and never raises."""
    if not transcript_path:
        return ""
    tail = _read_tail_bytes(transcript_path, CLOSING_TAIL_BYTES)
    if not tail:
        return ""
    for line in reversed(tail.decode("utf-8", errors="replace").splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if not isinstance(obj, dict) or obj.get("type") != "assistant":
            continue
        msg = obj.get("message")
        content = msg.get("content") if isinstance(msg, dict) else None
        if not isinstance(content, list):
            continue
        texts = [str(item.get("text") or "") for item in content
                 if isinstance(item, dict) and item.get("type") == "text"]
        text = "\n".join(t for t in texts if t)
        if text:
            return text
    return ""


#: The four-letter words a finished row may wear, and nothing else: the
#: STATE column on the Mac and the phone is four characters wide.
FINISH_WORDS = ("done", "cut", "lost", "end")


def finish_word(state: Optional[str], end_reason: Optional[str],
                last_report: Optional[str]) -> str:
    """The honest word for a row on the finished list.

    "done" was the one word every finished row wore, and it lied: two
    sessions cut off mid-build when their editor windows went away
    (SessionEnd `other`, hook state still `working`) read "done" beside
    cards nobody had accepted, and the person waited for a completion
    message that was never coming. Four words now, decided from what the
    daemon already holds:

    - ``done``: the session left a completion report (`last_report`).
    - ``lost``: the daemon lost sight of it (`evicted`, `no process`) with
      no report — whatever its last hook state said. Read before ``cut``:
      a row evicted for silence mid-build has a process still alive under
      it, and "cut" would say the terminal went away when nothing did.
    - ``cut``: it was working or thinking when it ended — the terminal or
      the editor went away under it, or Stop killed it.
    - ``end``: it ended on its own with nothing to report — a planning
      session closing out, a `/clear`, an idle tab closed.
    """
    if (last_report or "").strip():
        return "done"
    if end_reason in ("evicted", "no process"):
        return "lost"
    if state in ("working", "thinking"):
        return "cut"
    return "end"


def finished_quietly(transcript_path: str) -> bool:
    """Whether the turn that just ended is waiting on **nobody**.

    The two standing hints (`hooks.NOTIFY_SCRIPT`) give a turn two ways to
    say who it is waiting on: `<!-- bob-tldr -->` / `<!-- bob-actions -->`
    mark a turn waiting on somebody, and a `## Work done` report marks a
    turn waiting on nobody. A closing message that carries the report and
    neither marker is finished work, and a Stop for it raises no card: the
    row goes to sleep instead of Needs you. So is a closing message carrying
    the close-out helper's "left open" line (`CLOSE_OUT_LEFT_OPEN`) and
    neither marker: a plan or scout run that ends on it is done, its tab
    kept only to be read. Anything else — a report or the line *with* a
    summary, a plain answer, neither — is left exactly as it was.
    """
    text = closing_words(transcript_path)
    if not text:
        return False
    if not (_WORK_REPORT_RE.search(text) or _ends_on_left_open(text)):
        return False
    if _TLDR_RE.search(text) or _ACTIONS_RE.search(text):
        return False
    return True


def quiet_close_out(text: str) -> bool:
    """Whether *text* ends a run on the close-out helper's "left open" line
    with neither `bob-tldr` nor `bob-actions` — finished, waiting on nobody.

    Pure, over one message's text. `finished_quietly` reads the same line off
    a Claude transcript; Codex's rollout reader asks this of the last
    assistant message (`codex_rollouts.parse_rollout`), and only this — a
    Codex `## Work done` report alone still asks, because Needs you is the
    only place an unchecked Codex build is flagged.
    """
    if not text or not _ends_on_left_open(text):
        return False
    if _WORK_REPORT_RE.search(text):
        return False
    return not (_TLDR_RE.search(text) or _ACTIONS_RE.search(text))


def parse_transcript(transcript_path: str) -> SessionStats:
    """Stream a transcript file and return its :class:`SessionStats`.

    Never raises for I/O or parse problems — a missing or unreadable file yields
    an empty (zeroed) SessionStats, and malformed lines are skipped."""
    acc = _Accum()
    if not transcript_path:
        return _finalize(acc)
    p = Path(transcript_path)
    if not p.exists():
        return _finalize(acc)
    try:
        _read_new(acc, p, hold_partial=False)
    except OSError:
        pass
    return _finalize(acc)


# Spawn metadata for a subagent, written by Claude Code next to the subagent's
# own transcript:
#   ~/.claude/projects/<encoded-cwd>/<session-id>/subagents/agent-<agent-id>.meta.json
# Cached by agent id, which is stable and whose record never changes once
# complete.
#
# This used to say "only live subagents are ever looked up, so this stays tiny",
# which bounds the set that is *read* and says nothing about the set that is
# *kept*: nothing dropped an entry once its subagent finished, and agent ids are
# never reused. Agent-heavy days add entries by the hundred, so it is an LRU now.
_SUBAGENT_META_CACHE: "OrderedDict[str, AgentInfo]" = OrderedDict()
_SUBAGENT_META_MAX = 1024


def _subagents_dir(transcript_path: str) -> Optional[Path]:
    """The subagent directory that sits beside a session transcript."""
    if not transcript_path:
        return None
    p = Path(transcript_path)
    return p.parent / p.stem / "subagents"


def _first_model(path: Path) -> str:
    """The model of the first assistant turn in a transcript, or "".

    The meta file records what an agent *is* but not what it runs on, and the
    model is the field the menu leads with. An agent's own transcript names it
    on its first turn — a few lines in, so this stops there rather than
    streaming the file."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or '"assistant"' not in line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") != "assistant":
                    continue
                msg = obj.get("message")
                model = msg.get("model") if isinstance(msg, dict) else None
                if isinstance(model, str) and model and model not in _SYNTHETIC_MODELS:
                    return model
    except OSError:
        pass
    return ""


def load_subagent_meta(transcript_path: str, agent_id: str) -> Optional[AgentInfo]:
    """Recover a subagent's identity from the meta file written at its spawn.

    :func:`parse_transcript` can only see subagents the *session* spawned: their
    ``toolUseResult`` lands in the session transcript. A subagent spawned by
    another subagent leaves its record in that subagent's transcript instead, so
    to the session it is nameless — which is why nested agents used to render as
    a bare hex id. The meta file is the one place every spawn is recorded,
    whatever its depth, and it also names the parent, so the menu can nest them.

    Returns None when there is no meta file (an older Claude Code, or an agent
    with no record at all); callers degrade to the short id."""
    if not agent_id:
        return None
    hit = _SUBAGENT_META_CACHE.get(agent_id)
    if hit is not None:
        _SUBAGENT_META_CACHE.move_to_end(agent_id)
        return hit
    directory = _subagents_dir(transcript_path)
    if directory is None:
        return None
    try:
        raw = (directory / f"agent-{agent_id}.meta.json").read_text(
            encoding="utf-8", errors="replace"
        )
        meta = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(meta, dict):
        return None

    info = AgentInfo(
        agent_id=agent_id,
        description=str(meta.get("description") or ""),
        subagent_type=str(meta.get("agentType") or ""),
        model=_first_model(directory / f"agent-{agent_id}.jsonl"),
        parent_agent_id=str(meta.get("parentAgentId") or ""),
    )
    # Cache only once the record is complete. The meta file exists from the
    # moment the agent is spawned, but the model appears with its first turn —
    # caching before then would freeze a modelless label for the agent's life.
    if info.model:
        _SUBAGENT_META_CACHE[agent_id] = info
        _SUBAGENT_META_CACHE.move_to_end(agent_id)
        while len(_SUBAGENT_META_CACHE) > _SUBAGENT_META_MAX:
            _SUBAGENT_META_CACHE.popitem(last=False)
    return info


# session_id -> resolved path. A transcript never moves, so once found it stays
# found; the glob below walks every project directory, and the snapshot resolves
# every session on every refresh. Without this, watching 8 sessions on a 10s
# refresh means 48 directory walks a minute for an answer that cannot change.
#
# Bounded because this daemon is meant to run for weeks: the key is a session id,
# so an unbounded dict here gains an entry for every session the machine ever
# runs and gives none back.
_TRANSCRIPT_PATHS: "OrderedDict[str, str]" = OrderedDict()

# Sessions we looked for and did not find, with the time we gave up. Misses are
# the common case — background agents and finished tombstones never have a
# transcript — and each one used to re-walk every project directory on every
# refresh. They expire rather than stick, because the other kind of miss is a
# brand-new session whose transcript appears a moment later, and that one has to
# be discovered; the TTL is the delay we are willing to accept for it.
#
# Expiry alone does not bound this: an entry is only removed when that same
# session is asked about again, and the sessions that never come back are exactly
# the ones that never had a transcript. Hence the sweep in _record_miss.
_TRANSCRIPT_MISSES: dict[str, float] = {}
_MISS_TTL_SECONDS = 30.0
_TRANSCRIPT_CACHE_MAX = 2048


def _record_miss(session_id: str) -> None:
    """Remember that ``session_id`` has no transcript, without growing forever."""
    now = time.monotonic()
    if len(_TRANSCRIPT_MISSES) >= _TRANSCRIPT_CACHE_MAX:
        # Anything past its TTL is dead weight; that is normally the whole excess.
        for key, seen in list(_TRANSCRIPT_MISSES.items()):
            if (now - seen) >= _MISS_TTL_SECONDS:
                del _TRANSCRIPT_MISSES[key]
        while len(_TRANSCRIPT_MISSES) >= _TRANSCRIPT_CACHE_MAX:
            del _TRANSCRIPT_MISSES[min(_TRANSCRIPT_MISSES,
                                       key=_TRANSCRIPT_MISSES.get)]
    _TRANSCRIPT_MISSES[session_id] = now


def resolve_transcript(session_id: str, projects_dir: Path = CLAUDE_PROJECTS_DIR) -> str:
    """Locate a session's transcript by id (globbing across project dirs, since
    the daemon doesn't always receive the transcript_path). Returns the newest
    match, or "" if none. Session ids are UUIDs, so collisions are unrealistic.

    Memoised on the resolved path. A hit is re-checked with one stat: a transcript
    the user deleted must not keep being reported as present. A miss is memoised
    for :data:`_MISS_TTL_SECONDS`."""
    if not session_id:
        return ""
    cached = _TRANSCRIPT_PATHS.get(session_id)
    if cached is not None:
        if os.path.isfile(cached):
            _TRANSCRIPT_PATHS.move_to_end(session_id)
            return cached
        del _TRANSCRIPT_PATHS[session_id]

    missed_at = _TRANSCRIPT_MISSES.get(session_id)
    if missed_at is not None:
        if (time.monotonic() - missed_at) < _MISS_TTL_SECONDS:
            return ""
        del _TRANSCRIPT_MISSES[session_id]

    try:
        matches = list(projects_dir.glob(f"*/{session_id}.jsonl"))
    except OSError:
        return ""
    if not matches:
        _record_miss(session_id)
        return ""
    try:
        found = str(max(matches, key=lambda p: p.stat().st_mtime))
    except OSError:
        found = str(matches[0])
    _TRANSCRIPT_PATHS[session_id] = found
    _TRANSCRIPT_PATHS.move_to_end(session_id)
    while len(_TRANSCRIPT_PATHS) > _TRANSCRIPT_CACHE_MAX:
        _TRANSCRIPT_PATHS.popitem(last=False)
    return found


class StatsCache:
    """Memoises transcript parsing by mtime, and re-reads only what was appended.

    The mtime gate alone spares the *idle* sessions but not the busy ones: an
    active session appends on every turn, so its mtime moves on every snapshot
    tick and the old cache re-read the file from byte 0 — up to tens of MB every
    ten seconds for the one session most likely to be large. Keeping the
    accumulator (see :class:`_Accum`) makes a tick cost the delta instead.

    Bounded LRU, because the daemon sees every transcript that ever scrolls past
    and an unbounded dict here grew one entry per session forever."""

    def __init__(self, max_entries: int = 512) -> None:
        self._cache: "OrderedDict[str, tuple[float, _Accum]]" = OrderedDict()
        self._max_entries = max_entries

    def get(self, transcript_path: str) -> SessionStats:
        if not transcript_path:
            return SessionStats()
        try:
            st = os.stat(transcript_path)
        except OSError:
            return SessionStats()

        acc: Optional[_Accum] = None
        hit = self._cache.get(transcript_path)
        if hit is not None:
            cached_mtime, cached_acc = hit
            if cached_mtime == st.st_mtime:
                self._cache.move_to_end(transcript_path)
                return _finalize(cached_acc)
            # Shrunk means truncated or replaced by a different file, so the
            # offset no longer points at what we think it does — start over.
            acc = _Accum() if st.st_size < cached_acc.offset else cached_acc
        if acc is None:
            acc = _Accum()

        try:
            _read_new(acc, Path(transcript_path))
        except OSError:
            return _finalize(acc)

        self._cache[transcript_path] = (st.st_mtime, acc)
        self._cache.move_to_end(transcript_path)
        while len(self._cache) > self._max_entries:
            self._cache.popitem(last=False)
        return _finalize(acc)


#: How long a session must have been quiet before "waiting" is believed.
#: A busy subagent fleet delivers events every second or so, and each one used
#: to flip its parent in and out of the waiting bucket — which the panel draws
#: as the row hopping between "Needs you" and its project, destroying the view
#: mid-click. Anything that happened this recently says the session is
#: demonstrably active, so a wait younger than the window reads as `running`
#: and surfaces on its own once it has stood still (the daemon arms a re-push
#: for exactly that moment — see BobDaemon._arm_hysteresis_repush).
WAITING_HYSTERESIS_SECONDS = 3.0


def categorize(
    state_name: Optional[str], subagents_count: int, has_notification: bool,
    *, seconds_since_event: Optional[float] = None,
) -> str:
    """Bucket a session for the menu: waiting (needs the human) > running
    (actively working) > sleeping (idle).

    `seconds_since_event` is the caller's clock reading — this function stays
    pure. When provided and fresher than WAITING_HYSTERESIS_SECONDS it vetoes
    `waiting` to `running`; the None default leaves every other caller exactly
    as it was.

    `confused` counts as waiting only beside a notification card (22 Sep
    2026, RC3 in the false-buzz investigation of 20 Sep 2026):
    a failed tool call or an idle reminder whose card was dismissed is a snag,
    not a person being asked for anything, and banking it under waiting put it
    in every buzz's badge and fired the 30-minute blocked signal for it. With
    no card it falls through to sleeping; `error` (the model service's own
    failure) still waits."""
    if has_notification or state_name in ("waiting", "error"):
        if (seconds_since_event is not None
                and seconds_since_event < WAITING_HYSTERESIS_SECONDS):
            return "running"
        return "waiting"
    if state_name in ("working", "thinking") or subagents_count > 0:
        return "running"
    return "sleeping"


#: `spawn_counts` keeps at most this many subagent types, most frequent
#: first. A transcript that spawned forty distinct types is not a row a
#: surface needs the whole list of.
MAX_SPAWN_TYPES = 16


def spawn_counts(s: SessionStats) -> dict:
    """`{subagent_type: n}` over every spawned agent — **one entry per spawn**,
    which is what `subagents_seen` (deduped by type at the stamp) and the
    card's `agent_trail` (merged by name) cannot say. The daemon's run-health
    line reads a second `bc-implementer` spawn off this and nowhere else.
    An empty type is skipped; bounded at `MAX_SPAWN_TYPES` keys."""
    counts: dict[str, int] = {}
    for info in s.agents.values():
        name = str(getattr(info, "subagent_type", "") or "")
        if not name:
            continue
        counts[name] = counts.get(name, 0) + 1
    if len(counts) > MAX_SPAWN_TYPES:
        top = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        counts = dict(top[:MAX_SPAWN_TYPES])
    return counts


def stats_to_dict(s: SessionStats) -> dict:
    """Flatten SessionStats (including computed properties) to a plain dict for
    the menu-bar layer. datetimes are dropped in favour of duration_seconds."""
    return {
        "model": s.model,
        "models": list(s.models),
        "effort": s.effort,
        "fast": s.fast,
        "input_tokens": s.input_tokens,
        "output_tokens": s.output_tokens,
        "cache_read_tokens": s.cache_read_tokens,
        "cache_creation_tokens": s.cache_creation_tokens,
        "total_input_tokens": s.total_input_tokens,
        "duration_seconds": s.duration_seconds,
        "assistant_messages": s.assistant_messages,
        "user_prompts": s.user_prompts,
        "marker_at": s.marker_at,
        "prompt_at": s.prompt_at,
        "tool_counts": dict(s.tool_counts),
        "total_tool_calls": s.total_tool_calls,
        "files_touched": s.files_touched,
        "output_tokens_per_sec": s.output_tokens_per_sec,
        "git_branch": s.git_branch,
        "cwd": s.cwd,
        "spawn_counts": spawn_counts(s),
    }
