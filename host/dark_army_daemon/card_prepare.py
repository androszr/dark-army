"""Write a new card's fields from plain language.

The precedent is `session_title.py`: a pure module whose command line, prompt
and refusals are testable with strings, while the subprocess belongs to the
daemon. Two named departures, so they are decisions rather than copy-paste:

* **cwd is the card's project root**, not `STATE_DIR`. The namer runs in the
  state dir precisely so it has no business loading somebody's `CLAUDE.md`;
  Prepare runs in the project so it *should* see that project's conventions.
  The daemon still refuses a root that is not a known project directory.
* **Two modes, and which one runs is decided by one field.** The *legacy*
  mode — no ``idea`` — writes ``prompt`` and ``workflow`` only: the person's
  title and description are input, and are never rewritten. The *idea* mode
  — one box holding the whole thought — drafts all four of ``title``,
  ``summary``, ``prompt`` and ``workflow`` from it. Nothing either mode
  writes is ever saved by itself: the answer lands in the composer as
  editable text that a person reads, corrects and then saves. The two
  prompts and the two parsers are separate on purpose, so a chatty legacy
  answer that happens to echo ``SUMMARY:`` cannot change legacy parsing.

Four flags, each for the reason `session_title.py` gives: no transcript, no
hooks, no MCP servers, cheap model. The *data* half — which labels to fill,
the roster, the fields — rides in the prompt, not in `--system-prompt`. The
brief is the named agent ``bc-card-preparer`` (project copy, then user copy,
then the shipped ``card_preparer_brief.BRIEF``) and each CLI is handed it
the way that CLI accepts an agent: claude ``--agents`` / ``--agent`` (inline,
because ``--setting-sources ""`` hides ``.claude/agents/``), grok
``--agent <file>`` (a definition Dark Army writes under the state directory; a
project's ``.grok/`` is ignored and the daemon must never write into a
checkout), Codex the brief at the top of the prompt (no ``--agent`` flag).
A project may steer its own cards by carrying a copy; that is the same trust
``read_roster`` and ``cwd=root`` already extend. A copy that no longer asks
for the labelled answer (``brief_ok``) is set aside for the bundled one
rather than breaking the press. The roster skips the preparer: it is a
helper Dark Army runs, not a stage of the work.

**The helper is the card's own assistant, on that assistant's cheap model.**
A card that names codex is prepared by ``codex exec`` on ``gpt-6-luna``; a
card that names grok by ``grok -p`` on ``grok-4.5``; anything else — claude,
or a name Dark Army does not know — by ``claude -p`` on Haiku (``HELPER_MODELS``,
``helper_tool``). The two other CLIs get the same four properties by their
own flags: codex runs ``--ephemeral`` (no journal for `codex_rollouts` to
find), ``--ignore-user-config`` (no MCP servers, so no ``dark-army-board``
copy is spawned) and a ``read-only`` sandbox; grok runs ``--no-subagents``,
``--disable-web-search`` and ``--no-plan``. Neither has a switch that turns
Dark Army's own hooks off, so ``env_for`` points ``BOB_COMPANION_PORT`` at a port
nothing listens on — the notify script exits 0 and drops the event, exactly
its no-daemon behaviour — and the helper never shows up as a row. Only
claude gets a per-file ``--allowed-tools`` grant for attachments. Codex and
grok cannot: the files live under ``~/.dark-army/attachments/``, outside
codex's ``read-only`` sandbox (cwd is the project) and grok has no scoped
Read grant. Prepare refuses attachments on those two in words rather than
succeeding with instructions that never saw the photos.

Nothing here observes a session. What Dark Army saw run is recorded elsewhere;
this writes only what a card *declares*.

When the card carries attachments, this is a deliberate widening of the
no-tools helper: the "Do not use any tools." sentence is replaced, an
``ATTACHMENTS:`` section is appended, and ``--allowed-tools`` grants a
scoped ``Read(//<absolute path>)`` rule per attached copy. The helper
reads those files and is told **not** to list their paths: a stored
prompt never carries attachment links at all, and the daemon strips any
line the helper writes anyway (``attachments.strip_path_lines``, applied
before ``refusal`` judges the text). The list is handed to the assistant
once, at the moment it is started — both Start and Refine append
``attachments.prompt_block*`` — so nothing depends on the saved
instructions naming the files, and removing an attachment later can
never leave a stale link behind. The helper can be prompt-injected *by attachment content*
into writing strange instructions — but its only tool reaches only the
attached copies, it has no network and no write, its output is bounded
by ``MAX_PROMPT_OUT`` and ``refusal``, and the result lands in a local
card a person reads before anything runs. ``--setting-sources ""``
stays, so no user settings can widen the grant. With no attachments
the ``--allowed-tools`` tail is absent; the brief still rides the
agent route.
"""
from __future__ import annotations

from . import areas

import json
import logging
import os
import re
from typing import NamedTuple, Optional

from . import board
from . import board_outcomes
from . import card_preparer_brief
from . import dispatch

logger = logging.getLogger("dark-army")

#: Cheapest model that can turn a sentence into instructions. Spelled as an
#: alias rather than a dated id so a Haiku release does not strand this.
MODEL = "haiku"

#: The cheap model per assistant, keyed by the card's `tool`. Read off each
#: CLI on this machine: `grok models` (2026-09-05) lists `grok-4.6 (default)`
#: and `grok-4.5`; `codex debug models` (codex-cli 0.156.1, 2026-09-23) lists
#: `gpt-6-luna` as its efficient model for focused, repeatable work. Every
#: cell is on `dispatch.MODELS`. A tool missing here is prepared by claude on
#: `MODEL`.
HELPER_MODELS = {
    "claude": MODEL,
    "codex": "gpt-6-luna",
    "grok": "grok-4.5",
}

#: The cheap *worker* per assistant — the model the shunt skill's two
#: wrappers (`.claude/skills/shunt/bulk_read.py`, `code_write.py`) run a
#: bulk read or a boilerplate write on. Kept apart from `HELPER_MODELS`
#: because the two lists are not the same fact: the preparer's model is what
#: turns a sentence into a card, this one is what a shunt helper runs on. They
#: name the same Codex model today and may part again. Every cell here is
#: on `dispatch.MODELS`, so the `worker` slot of Dark Army's Agent models
#: setting validates against the ordinary allowlist (`agent_models`).
#: `gpt-6-luna` is the efficient focused-work model of that listing. Also the
#: shipped contents of the pack's `workers.json`
#: (`pack_render.pin_worker_models`).
WORKER_MODELS = {
    "claude": MODEL,
    "codex": "gpt-6-luna",
    "grok": "grok-4.5",
}

#: A loopback port nothing listens on. Codex and grok have no flag that turns
#: Dark Army's own hooks off, so the helper is pointed here: the notify script's
#: connect is refused, it exits 0 and drops the event — the same thing it does
#: when no daemon is running — and the helper never becomes a row.
QUIET_HOOK_PORT = "1"

#: The same silence for the private hook socket. `connect()` on `/dev/null`
#: fails at once (`ENOTSOCK`), the path exists on every Mac and nothing is
#: read; under the one address rule an explicit socket never falls through to
#: the port. Both are set: this one for a notify script from after the
#: private socket, the port for an installed one from before it.
QUIET_HOOK_SOCKET = "/dev/null"


def helper_tool(tool) -> str:
    """Which CLI prepares a card for this assistant: the assistant itself
    where Dark Army knows how to run it headless, else claude."""
    chosen = str(tool or "").strip().lower()
    return chosen if chosen in HELPER_MODELS else "claude"


def env_for(tool, base=None):
    """The helper's environment. Never inherit the daemon's raw env.

    The frozen app's PYTHONHOME would make a child interpreter boot against
    the bundle. Claude does not need the quiet hook port (`--setting-sources
    ""` already leaves it hookless); the other two get `DARK_ARMY_HOOK_SOCKET`
    pointed at `QUIET_HOOK_SOCKET` and `BOB_COMPANION_PORT` at
    `QUIET_HOOK_PORT`, so a hook that fires anyway — from a notify script of
    either age — has nobody to talk to.
    """
    from . import subprocess_env

    env = subprocess_env.clean_env(os.environ if base is None else base)
    if helper_tool(tool) != "claude":
        env["DARK_ARMY_HOOK_SOCKET"] = QUIET_HOOK_SOCKET
        env["BOB_COMPANION_PORT"] = QUIET_HOOK_PORT
        env.pop("CLAWD_TANK_PORT", None)
    return env

#: How long the helper gets. Startup dominates; an answer that takes longer
#: than this has hit something other than the model.
TIMEOUT_SECONDS = 60.0

#: Opening attached files is real extra work; a screenshot run that finishes
#: at 61s is not a hang. Used only when the call carries attachments.
TIMEOUT_WITH_ATTACHMENTS_SECONDS = 120.0

#: How much of the person's description the helper is shown. The store's own
#: ceiling (now 2000 — a paragraph of plain words is legitimate); a longer
#: summary would be refused at `create` anyway.
MAX_SUMMARY_INPUT = board.MAX_SUMMARY_CHARS

#: What a generated prompt may be. A *rejection* threshold, not a truncation
#: point: silently keeping the first N characters of generated instructions
#: is exactly the failure `promptTruncated` exists to prevent.
MAX_PROMPT_OUT = 4000

#: How many agent files the roster will list. A prompt naming hundreds of
#: helpers stops being a menu; files beyond the bound are ignored.
MAX_ROSTER_AGENTS = 30

#: Per-entry description ceiling. Enough to tell two similar helpers apart,
#: small enough that the worst-case roster stays a few KB in the argv.
MAX_ROSTER_DESC_CHARS = 200

#: How much of each agent file is read. Frontmatter lives at the top; never
#: read a 2 GB `.md` whole.
ROSTER_READ_BYTES = 8192

#: How many project folders the FOLDER menu will list. A closed set that runs
#: to hundreds of paths stops being a menu and starts being a haystack; roots
#: beyond the bound are simply not offered.
MAX_PROJECT_CHOICES = 40

#: How many of the project's unfinished cards the DEPENDS ON menu will list,
#: and how much of each title is shown *and* matched (the clip happens before
#: both, so what the helper copies is what the reader compares).
MAX_DEPENDENCY_CHOICES = 40
MAX_DEPENDENCY_TITLE_CHARS = 120

#: How much of the one-box idea the helper is shown. The same ceiling the
#: description already has: the idea is what the description would have been.
MAX_IDEA_INPUT = board.MAX_SUMMARY_CHARS

#: What a generated *title* may be. `session_title.py`'s rule, one notch
#: wider because a card names a piece of work rather than a conversation:
#: a rejection threshold, never a truncation point. A paragraph clipped to
#: 80 characters no longer names the work — it only looks as if it does.
MAX_TITLE_OUT = 80
MAX_TITLE_WORDS = 14

#: The data half of the prompt: which labels to fill, in order. The rules
#: live in the named agent (`card_preparer_brief.BRIEF`); this string only
#: names the sections the parser will read.
MODE_HEAD = (
    "Answer with these labelled sections and nothing else, in this "
    "order:\nINSTRUCTIONS:\nSPECIALISTS:\n\n"
)
#: Idea mode asks for seven. The three objective labels sit between
#: SUMMARY and INSTRUCTIONS so INSTRUCTIONS is still immediately followed by
#: SPECIALISTS, and the trailing FOLDER: answer still lands inside the
#: specialists block where `_clean_specialists` drops it.
MODE_HEAD_IDEA = (
    "Answer with these labelled sections and nothing else, in this "
    "order:\nTITLE:\nSUMMARY:\nBENEFICIARY:\nBENEFIT:\nCRITERION:\n"
    "INSTRUCTIONS:\nSPECIALISTS:\n\n"
)

_NO_TOOLS = "Do not use any tools."
_WITH_ATTACHMENTS = (
    "Use the Read tool to open each file listed under ATTACHMENTS before "
    "writing, and use no other tool. Do not read any other file. "
    "Do not list the attachment paths in INSTRUCTIONS."
)

#: The agent Dark Army runs on Prepare. A vendored project's copy is
#: ``{prefix}-card-preparer``; both match ``is_preparer_name``.
PREPARER_ROLE = "card-preparer"

#: How much of a preparer agent file is read. The brief is short; never
#: read a 2 GB `.md` whole.
BRIEF_READ_BYTES = 32768

#: Grok has no inline-agent switch that survives ``-p``. Dark Army writes this
#: one file under the state directory on each grok Prepare, overwritten,
#: serialised by `_prepare_lock`.
GROK_AGENT_FILENAME = "card-preparer.grok.md"


class Brief(NamedTuple):
    """The preparer agent Prepare will actually run."""

    name: str
    description: str
    body: str
    source: str


def _mode_head(idea: bool = False) -> str:
    """The mode's label list. Rules live in the brief, not here."""
    return MODE_HEAD_IDEA if idea else MODE_HEAD


def _brief(body: str, has_attachments: bool) -> str:
    """The brief, or the one-sentence swap for attachments.

    ``replace(..., 1)`` is silent when `_NO_TOOLS` is missing, which is why
    ``brief_ok`` demands the sentence exactly once: a project override that
    rewords it would drop the Read grant's instructions in silence.
    """
    text = body or ""
    if not has_attachments:
        return text
    return text.replace(_NO_TOOLS, _WITH_ATTACHMENTS, 1)


def is_preparer_name(name) -> bool:
    """True for ``card-preparer`` and any ``*-card-preparer``, case-insensitive."""
    key = str(name or "").strip().lower()
    return key == PREPARER_ROLE or key.endswith("-" + PREPARER_ROLE)


def brief_ok(body: str) -> bool:
    """Whether this brief still asks for the labelled answer Dark Army parses."""
    text = body or ""
    if text.count(_NO_TOOLS) != 1:
        return False
    return all(
        label in text
        for label in (
            "TITLE:", "SUMMARY:", "INSTRUCTIONS:", "SPECIALISTS:", "FOLDER:",
        )
    )


def _body_after_frontmatter(text: str) -> str:
    """Everything after the closing ``---``, or the whole string."""
    raw = (text or "").lstrip("\ufeff")
    parts = raw.split("---", 2)
    if len(parts) < 3:
        return raw
    return parts[2].lstrip("\n")


def _bundled_brief() -> Brief:
    return Brief(
        card_preparer_brief.NAME,
        card_preparer_brief.DESCRIPTION,
        card_preparer_brief.BRIEF,
        "bundled",
    )


def read_brief(root: str, user_dir: Optional[str] = None) -> Brief:
    """The preparer agent to run: project copy, then user copy, then bundled.

    Walks the same two directories as ``read_roster``, in the same order.
    The first file whose frontmatter name satisfies ``is_preparer_name``
    wins; if its body fails ``brief_ok``, the bundled brief is returned
    and one log line names the rejected file. Never raises.
    """
    directories = (
        (os.path.join(root, ".claude", "agents"), "project"),
        (user_dir or os.path.expanduser("~/.claude/agents"), "user"),
    )
    for directory, source in directories:
        try:
            entries = sorted(os.listdir(directory))
        except OSError:
            continue
        for entry in entries:
            if not entry.endswith(".md"):
                continue
            path = os.path.join(directory, entry)
            if not os.path.isfile(path):
                continue
            try:
                with open(path, "rb") as handle:
                    raw = handle.read(BRIEF_READ_BYTES)
            except OSError:
                continue
            text = raw.decode("utf-8", "replace")
            parsed = parse_frontmatter(text)
            if parsed is None:
                continue
            name, description = parsed
            if not is_preparer_name(name):
                continue
            body = _body_after_frontmatter(text)
            if brief_ok(body):
                return Brief(name, description, body, source)
            logger.info("rejected preparer agent %s", path)
            return _bundled_brief()
    return _bundled_brief()


def agents_json(name: str, description: str, body: str) -> str:
    """Claude's inline ``--agents`` value: one named agent, sorted keys."""
    return json.dumps(
        {name: {"description": description, "prompt": body}},
        sort_keys=True,
    )


def grok_agent_file(name: str, description: str, body: str) -> str:
    """The definition file grok's ``--agent`` flag accepts.

    Measured: ``prompt_mode: full`` plus the body is what steers ``-p``;
    ``--agents`` JSON, ``--rules`` and ``--system-prompt-override`` did not.
    Description quotes are neutralised so a folded YAML line cannot break
    the frontmatter.
    """
    desc = " ".join(str(description or "").split()).replace('"', "'")
    return (
        f"---\n"
        f"name: {name}\n"
        f'description: "{desc}"\n'
        f"prompt_mode: full\n"
        f"permission_mode: plan\n"
        f"agents_md: false\n"
        f"---\n\n"
        f"{body}"
    )

_SECTION = re.compile(r"(INSTRUCTIONS|SPECIALISTS):")
#: Its own regex, deliberately not a widening of `_SECTION`. A legacy answer
#: that chattily writes "SUMMARY:" inside its instructions must go on parsing
#: exactly as it does today.
#: Two forms of a label, both tolerant of markdown decoration the helper
#: intermittently adds against the brief's word: a *heading* form (`## TITLE`,
#: colon optional, the label alone on its line, the `#` marker required) and
#: an *inline* form (`TITLE:`, `**TITLE:**`, `` `TITLE:` `` — the colon
#: required). The colon stays required off a heading line so a chatty
#: INSTRUCTIONS body mentioning "the SUMMARY of the work" mid-sentence goes
#: on parsing exactly as today.
_IDEA_LABELS = "TITLE|SUMMARY|BENEFICIARY|BENEFIT|CRITERION|INSTRUCTIONS|SPECIALISTS"
_SECTION_IDEA = re.compile(
    r"^[ \t]*#{1,6}[ \t]*[*_`]*(" + _IDEA_LABELS + r")[*_`]*:?[*_`]*[ \t]*$"
    r"|[*_`]*(" + _IDEA_LABELS + r")[*_`]*:(?:[*_`]*(?=[ \t]|$))?",
    re.MULTILINE)
#: A wrapper glued to the colon is consumed only when whitespace or the end
#: of the line follows it (`**TITLE:**Foo` → body `**Foo`); otherwise it is
#: the answer's own opening span (`TITLE:*Foo*` → body `*Foo*` → `Foo`).
#: The same two-form rule for the answer's final AREA section.
_AREA_LABEL = re.compile(
    r"^[ \t]*#{1,6}[ \t]*[*_`]*AREA[*_`]*:?[*_`]*[ \t]*$"
    r"|[*_`]*AREA[*_`]*:(?:[*_`]*(?=[ \t]|$))?",
    re.MULTILINE)
#: The idea-mode answer's optional DEPENDS ON section, the `_AREA_LABEL`
#: shape over its own words. Not a widening of `_SECTION_IDEA`.
_DEPENDS_LABEL = re.compile(
    r"^[ \t]*#{1,6}[ \t]*[*_`]*DEPENDS[ \t]+ON[*_`]*:?[*_`]*[ \t]*$"
    r"|[*_`]*DEPENDS[ \t]+ON[*_`]*:(?:[*_`]*(?=[ \t]|$))?",
    re.MULTILINE)
#: Where an area answer's tacked-on description begins ("Pocket — phones &
#: widgets", "Pocket (phones)", "Pocket: phones"); the left piece is matched.
_AREA_TRAIL = re.compile(r" — | – | - |\(|:")

#: The three objective answers idea mode asks for, label -> card field. Read
#: by `parse_objective`; `parse_idea` keeps its four-tuple and never sees
#: them. `outcome_check_on` is deliberately not here: Prepare drafts words,
#: never a date.
OBJECTIVE_LABELS = (
    ("BENEFICIARY", "beneficiary"),
    ("BENEFIT", "intended_benefit"),
    ("CRITERION", "success_criterion"),
)
_LIST_MARKERS = ("- ", "* ", "1. ")
_TRAILING_PARENTHETICAL = re.compile(r"\s*\([^)]*\)\s*$")
_HEADING = re.compile(r"^#{1,6}[ \t]+")
#: Matching pairs stripped off both ends of a scalar; longest first so
#: `**Foo**` loses the bold before `*` could take one star of it.
_WRAPS = ("**", "__", "`", "*", "_")
#: How much of the helper's answer a refusal log line keeps.
LOG_EXCERPT_CHARS = 600


def _undecorate(line: str) -> str:
    """Markdown decoration off a scalar line: one heading marker, then every
    matching wrapper pair (`**Foo**`, `` `Foo` ``, ``**`Foo`**``) off both
    ends. Pairs only — `**Foo` alone is left as it is, an unmatched wrapper
    being a typo the refusals then judge, not decoration; stripping one side
    would silently rewrite a title. A pair whose inner text carries the same
    token again is two spans, not one wrapper (`` `a` and `b` ``,
    `*Fix* the *strip*`), and the line is left whole.
    """
    line = _HEADING.sub("", line.strip(), count=1).strip()
    while True:
        for wrap in _WRAPS:
            n = len(wrap)
            if (len(line) > 2 * n and line.startswith(wrap)
                    and line.endswith(wrap)):
                inner = line[n:-n]
                if wrap in inner:
                    continue
                line = inner.strip()
                break
        else:
            return line


def log_excerpt(raw, limit: int = LOG_EXCERPT_CHARS) -> str:
    """The helper's answer as one log-safe line: newlines and tabs collapsed
    to single spaces, cut at `limit` characters with `…` when longer."""
    text = " ".join(str(raw or "").split())
    if len(text) > limit:
        return text[:limit] + "…"
    return text


def _unquote(value: str) -> str:
    """One layer of matching YAML quotes off a scalar.

    A hand-quoted `name: "bc-planner"` would otherwise enter the roster
    wearing its quotes, so the prompt offers one spelling and the closed-set
    filter demands another — the helper is listed and then silently dropped.
    """
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("\"", "'"):
        return value[1:-1].strip()
    return value


def parse_frontmatter(text: str) -> Optional[tuple[str, str]]:
    """`(name, description)` from an agent file's YAML frontmatter, or None.

    Hand-rolled and tolerant on purpose — `host/` carries no YAML dependency,
    and a malformed agent file must never turn Prepare into a refusal. The
    shape read is the one Claude Code writes: a `---` fence on the first
    line, `name:` at column 0 taking the rest of its line, `description:` at
    column 0 taking the rest of its line plus every immediately following
    line that starts with whitespace (the folded block), a closing `---`.
    Anything else is None and the caller skips the file silently.
    """
    # A BOM before the opening fence is an editor's doing, not a malformed
    # file — without stripping it the first line never equals `---` and a
    # perfectly good agent file drops out of the roster in silence.
    lines = (text or "").lstrip("\ufeff").splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return None
    name = ""
    desc_parts: list[str] = []
    i = 1
    while i < end:
        line = lines[i]
        if line.startswith("name:"):
            name = line[len("name:"):].strip()
        elif line.startswith("description:"):
            desc_parts.append(line[len("description:"):].strip())
            while i + 1 < end and lines[i + 1][:1] in (" ", "\t"):
                i += 1
                desc_parts.append(lines[i].strip())
        i += 1
    name = _unquote(name)
    if not name:
        return None
    description = _unquote(" ".join(part for part in desc_parts if part))
    return name, description


def read_roster(root: str, user_dir: Optional[str] = None) -> list[tuple[str, str]]:
    """The project's real subagent roster: `[(name, description), ...]`.

    Reads `<root>/.claude/agents/*.md` then the user-level directory —
    project first, so a case-insensitive name collision resolves the way
    Claude Code's own project-over-user precedence does, and the roster Dark Army
    shows is the roster the dispatched session will actually resolve. Only
    regular `*.md` files directly inside each directory, sorted per
    directory for determinism, at most `ROSTER_READ_BYTES` per file. Never
    raises: an unreadable file, a decode failure, a malformed frontmatter or
    a name too long to store as a stage is a skipped file, and a missing
    directory is simply absent from the roster.

    `user_dir` exists so tests point at a tmp_path instead of the real home.
    """
    directories = (
        os.path.join(root, ".claude", "agents"),
        user_dir or os.path.expanduser("~/.claude/agents"),
    )
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for directory in directories:
        try:
            entries = sorted(os.listdir(directory))
        except OSError:
            continue
        for entry in entries:
            if len(out) >= MAX_ROSTER_AGENTS:
                return out
            if not entry.endswith(".md"):
                continue
            path = os.path.join(directory, entry)
            if not os.path.isfile(path):
                continue
            try:
                with open(path, "rb") as handle:
                    raw = handle.read(ROSTER_READ_BYTES)
            except OSError:
                continue
            # Replace rather than refuse: the cut at ROSTER_READ_BYTES can land
            # mid-character, and a mangled tail must not cost us the intact
            # frontmatter at the top of the file.
            text = raw.decode("utf-8", "replace")
            parsed = parse_frontmatter(text)
            if parsed is None:
                continue
            name, description = parsed
            if is_preparer_name(name):
                # A helper Dark Army runs, not a stage of the work. Skip before
                # `seen` so a collision cannot shadow a real specialist.
                continue
            if len(name) > board.MAX_STAGE_CHARS:
                # A name that cannot be stored as a stage must not be offered
                # as one; clamping it would make the prompt's spelling and the
                # stored spelling disagree.
                continue
            key = name.lower()
            if key in seen:
                continue
            seen.add(key)
            description = " ".join(description.split())[:MAX_ROSTER_DESC_CHARS]
            out.append((name, description))
    return out


def _roster_block(roster) -> str:
    """The prompt's menu of real helpers, or the explicit no-helpers line."""
    if not roster:
        return ("There are no helpers available for this project. "
                "Write NONE under SPECIALISTS.\n\n")
    lines = ["SPECIALISTS may name only these helpers, exactly as written, "
             "one per line, or NONE:"]
    for name, description in roster:
        lines.append(f"- {name} — {description}" if description else f"- {name}")
    return "\n".join(lines) + "\n\n"


def _folders_block(roots) -> str:
    """The prompt's closed set of project folders, or nothing at all.

    Empty for fewer than two roots: with one choice the picker is already on
    it, so an opinion about it is noise rather than help. The section is
    named `FOLDER:` and not `PROJECT:` because `PROJECT:` is already an
    *input* line further down the prompt — reusing the label invites the
    helper to echo its own input back as the answer.
    """
    choices = [str(r) for r in (roots or ()) if str(r or "").strip()]
    if len(choices) < 2:
        return ""
    lines = ["After SPECIALISTS, add one final section:",
             "FOLDER:",
             "FOLDER names the project folder this work belongs in. It must "
             "be exactly one of the paths below, copied character for "
             "character on a single line, or NONE."]
    for root in choices[:MAX_PROJECT_CHOICES]:
        lines.append(f"- {root}")
    return "\n".join(lines) + "\n\n"


def candidate_ok(title: str) -> bool:
    """Whether a card title may be offered under DEPENDS ON.

    A title that reads as a section label (`SUMMARY: rewrite`, `FOLDER: x`,
    `AREA: y`, `DEPENDS ON: z`) would, echoed into the answer, cut a section
    in half; such a title is left out, on the daemon and on the phone alike.
    """
    text = str(title or "").strip()
    if not text:
        return False
    return not (_SECTION_IDEA.search(text) or _AREA_LABEL.search(text)
                or _DEPENDS_LABEL.search(text) or "FOLDER:" in text)


def _dependencies_block(candidates) -> str:
    """The prompt's closed list of cards this work may wait on, or nothing.

    `candidates` is `[(card_id, title), ...]`. Empty for none, so the whole
    prompt stays byte-identical to the call this function has always made.
    """
    titles = [str(t) for _i, t in (candidates or ()) if str(t or "").strip()]
    if not titles:
        return ""
    lines = ["After SPECIALISTS and FOLDER, and before AREA, add one section:",
             "DEPENDS ON:",
             "DEPENDS ON lists the cards below that this work must wait for, "
             "one title per line, copied character for character, or NONE."]
    for title in titles[:MAX_DEPENDENCY_CHOICES]:
        lines.append(f"- {title}")
    return "\n".join(lines) + "\n\n"


def prompt_for(*, title: str, summary: str, tool: str, project: str,
               roster=(), attachments=(), idea: str = "",
               roots=(), candidates=()) -> str:
    """The data half of the `-p` argument: mode head, roster, fields.

    The rules live in the named agent, not here. Summary is collapsed and
    cut to `MAX_SUMMARY_INPUT` so a pasted essay cannot become the prompt
    we send. The roster rides between the mode head and the fields so the
    head stays the prompt's prefix. With attachments, an `ATTACHMENTS:`
    section (one absolute path per line) follows the last field; the
    no-tools sentence is swapped in the brief, not in this string.

    A non-empty `idea` switches modes: the four-section head, and the one box
    as the whole input — the title and description are not sent, because in
    that mode they are what the helper is being asked to write. An **empty**
    `idea` is byte-identical to the call this function has always made.

    `roots` is the closed set of project folders the composer could actually
    select; with fewer than two of them the block is empty and the whole
    prompt is byte-identical to the call this function has always made.

    `candidates` (`[(card_id, title)]`) rides only in idea mode, after the
    folder block and before the `IDEA:` line; without any, or in the legacy
    mode, the prompt is byte-identical.
    """
    paths = tuple(p for p in (attachments or ()) if p)
    thought = " ".join((idea or "").split())[:MAX_IDEA_INPUT]
    if thought:
        body = (
            f"{_mode_head(idea=True)}"
            f"{_roster_block(roster)}"
            f"{_folders_block(roots)}"
            f"{_dependencies_block(candidates)}"
            f"IDEA: {thought}\n"
            f"ASSISTANT: {tool or ''}\n"
            f"PROJECT: {project or ''}\n"
        )
    else:
        text = " ".join((summary or "").split())[:MAX_SUMMARY_INPUT]
        body = (
            f"{_mode_head()}"
            f"{_roster_block(roster)}"
            f"{_folders_block(roots)}"
            f"TITLE: {title or ''}\n"
            f"DESCRIPTION: {text}\n"
            f"ASSISTANT: {tool or ''}\n"
            f"PROJECT: {project or ''}\n"
        )
    if paths:
        body += "ATTACHMENTS:\n" + "\n".join(str(p) for p in paths) + "\n"
    return body + _areas_block()


def argv(executable: str, model: str = "", **fields) -> list[str]:
    """The command line, in one place so the test and the daemon cannot drift.

    `model` is the card-preparer slot of Dark Army's Agent models setting,
    resolved by the daemon (`_agent_model_for(root, helper, "card-preparer")`);
    empty keeps today's argv byte-identical (`HELPER_MODELS[tool]` / `MODEL`).

    `executable` is the binary of `helper_tool(fields["tool"])`, resolved by
    the daemon; the shape of the line follows the same choice. For claude,
    each flag is argued for in `session_title.py`'s module docstring; none of
    them is optional politeness, and dropping any one turns a prepare call
    into a visible session on somebody's screen. Attachments, when present,
    add `--allowed-tools` with one scoped `Read(//<absolute path>)` rule per
    file — the `//` prefix is what makes a Read rule absolute rather than
    project-relative.

    Three routes for the brief. Claude is started *as* the agent
    (``--agents`` JSON plus ``--agent``); grok by ``--agent <brief_path>``
    immediately before ``-p`` (an empty path raises — the daemon always
    writes the file); Codex, which has no such flag, prepends the brief to
    the data prompt. ``brief`` / ``brief_name`` / ``brief_description``
    default to the shipped constants; ``brief_path`` defaults to empty.
    """
    attachments = tuple(p for p in (fields.get("attachments") or ()) if p)
    tool = helper_tool(fields.get("tool"))
    prompt = prompt_for(
        title=str(fields.get("title") or ""),
        summary=str(fields.get("summary") or ""),
        tool=str(fields.get("tool") or ""),
        project=str(fields.get("project") or ""),
        # A data field like `title`, never str()'d — a list's repr must
        # not end up in the prompt.
        roster=fields.get("roster") or (),
        # A data field like `roster`, never str()'d.
        roots=fields.get("roots") or (),
        candidates=fields.get("candidates") or (),
        attachments=attachments,
        idea=str(fields.get("idea") or ""),
    )
    brief = _brief(
        str(fields.get("brief") or card_preparer_brief.BRIEF),
        bool(attachments),
    )
    brief_name = str(fields.get("brief_name") or card_preparer_brief.NAME)
    brief_description = str(
        fields.get("brief_description") or card_preparer_brief.DESCRIPTION)
    brief_path = str(fields.get("brief_path") or "")
    model = str(model or "")
    if tool == "codex":
        return [
            executable, "exec",
            "--model", model or HELPER_MODELS["codex"],
            "--ephemeral",
            "--ignore-user-config",
            "--skip-git-repo-check",
            "--sandbox", "read-only",
            "--color", "never",
            "--", brief + "\n\n" + prompt,
        ]
    if tool == "grok":
        if not brief_path:
            raise ValueError("grok Prepare needs brief_path")
        return [
            executable,
            "--model", model or HELPER_MODELS["grok"],
            "--output-format", "plain",
            "--no-subagents",
            "--disable-web-search",
            "--no-plan",
            "--agent", brief_path,
            "-p", prompt,
        ]
    cmd = [
        executable,
        "-p", prompt,
        "--model", model or MODEL,
        "--no-session-persistence",
        "--strict-mcp-config",
        "--setting-sources", "",
        "--agents", agents_json(brief_name, brief_description, brief),
        "--agent", brief_name,
    ]
    if attachments:
        cmd.append("--allowed-tools")
        for path in attachments:
            cmd.append(f"Read(//{path})")
    return cmd


def _strip_fences(text: str) -> str:
    text = (text or "").strip()
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    while lines and not lines[-1].strip():
        lines.pop()
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _clean_instructions(block: str) -> str:
    text = _strip_fences(block)
    if not text:
        return ""
    lines = text.splitlines()
    first = lines[0].lstrip()
    for prefix in _LIST_MARKERS:
        if first.startswith(prefix):
            first = first[len(prefix):]
            break
    lines[0] = first
    return "\n".join(lines).strip()


def _clean_specialists(block: str, roster=()) -> list[str]:
    """The SPECIALISTS block against the closed set of real helpers.

    A genuine NONE parses to `[]` before any matching. Each remaining
    candidate loses one trailing parenthetical, then must match a roster
    name — the whole cleaned item first, its first whitespace-separated
    token second, both case-insensitive and never substring (`bc-plan` must
    not claim `bc-planner`). A match is canonicalised to the roster's own
    spelling; an unmatched candidate is dropped. An empty roster keeps
    nothing: the model was shown no helpers and told to answer NONE, so
    anything else is invention.
    """
    text = _strip_fences(block)
    if not text or text.strip().upper() == "NONE":
        return []
    by_key: dict[str, str] = {}
    for name, _description in roster:
        by_key.setdefault(name.lower(), name)
    names = []
    for line in text.splitlines():
        item = line.strip()
        for prefix in _LIST_MARKERS:
            if item.startswith(prefix):
                item = item[len(prefix):].strip()
                break
        if not item or item.upper() == "NONE":
            continue
        item = _TRAILING_PARENTHETICAL.sub("", item)
        match = by_key.get(item.lower())
        if match is None:
            tokens = item.split()
            if tokens:
                match = by_key.get(tokens[0].lower())
        if match is None:
            continue
        names.append(match)
    return board.parse_stages(names)


def parse(raw: str, roster=()) -> tuple[str, list[str]]:
    """The last `INSTRUCTIONS:` block and the last `SPECIALISTS:` block.

    No `INSTRUCTIONS:` section means `("", [])` — refuse, do not salvage a
    preamble or a specialists-only answer as if it were the prompt. The
    specialists are filtered against `roster` — see `_clean_specialists`.
    """
    text = _strip_fences(raw or "")
    if "INSTRUCTIONS:" not in text:
        return "", []
    found: dict[str, str] = {}
    matches = list(_SECTION.finditer(text))
    for i, match in enumerate(matches):
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        found[match.group(1)] = text[start:end]
    if "INSTRUCTIONS" not in found:
        return "", []
    prompt = _clean_instructions(found["INSTRUCTIONS"])
    stages = _clean_specialists(found.get("SPECIALISTS", ""), roster)
    return prompt, stages


def _clean_line(block: str) -> str:
    """A one-line scalar out of a labelled block: fences off, first non-blank
    line, markdown decoration off (`_undecorate`), list marker off, one
    layer of matching quotes off, decoration off again — a heading marker
    precedes a list marker, and a bold wrapper can sit inside quotes.

    Deliberately keeps only the *first* non-blank line and lets the refusals
    judge what is left, rather than joining a paragraph into one line: a
    helper that wrote three paragraphs where a title was asked for has not
    written a title, and stitching them together would hide that.
    """
    text = _strip_fences(block)
    if not text:
        return ""
    line = ""
    for candidate in text.splitlines():
        if candidate.strip():
            line = candidate.strip()
            break
    line = _undecorate(line)
    for prefix in _LIST_MARKERS:
        if line.startswith(prefix):
            line = line[len(prefix):].strip()
            break
    return _undecorate(_unquote(line).strip())


def _partition_idea(text: str) -> dict[str, str]:
    """The idea-mode answer cut at every `_SECTION_IDEA` label, label -> body.
    Shared by `parse_idea` and `parse_objective` so the two can never
    disagree about where a section ends."""
    found: dict[str, str] = {}
    matches = list(_SECTION_IDEA.finditer(text))
    for i, match in enumerate(matches):
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        found[match.group(1) or match.group(2)] = text[start:end]
    return found


def parse_objective(raw: str) -> dict:
    """The three objective suggestions out of an idea-mode answer, keyed by
    card field. An absent label, an empty section and a `NONE` answer all
    read as `""` — no suggestion, never a refusal — and each value is one
    line (`_clean_line`'s rule: list marker and quotes off, first non-blank
    line only). The bounds are `objective_refusal`'s, judged by the caller.
    """
    found = _partition_idea(_strip_fences(raw or ""))
    out: dict = {}
    for label, key in OBJECTIVE_LABELS:
        line = _clean_line(found.get(label, ""))
        if line.upper() == "NONE":
            line = ""
        out[key] = line
    return out


def parse_idea(raw: str, roster=()) -> tuple[str, str, str, list]:
    """`(title, summary, prompt, stages)` from a four-section answer.

    Its own parser rather than a widened `parse`, for the reason
    `_SECTION_IDEA` states. A missing `INSTRUCTIONS:` section is
    `("", "", "", [])` — refuse the whole answer, never salvage a preamble.
    A missing `TITLE:` or `SUMMARY:` reads as empty and is refused by
    `title_refusal` / `summary_refusal`, so a partial answer can never fill
    two fields of four and look like success.
    """
    text = _strip_fences(raw or "")
    # The partition is the guard: a `## INSTRUCTIONS` heading carries no
    # colon, so the legacy literal test would refuse a whole answer here.
    found = _partition_idea(text)
    if "INSTRUCTIONS" not in found:
        return "", "", "", []
    title = _clean_line(found.get("TITLE", ""))
    summary = _clean_line(found.get("SUMMARY", ""))
    prompt = _clean_instructions(found["INSTRUCTIONS"])
    # DEPENDS ON: is asked for after SPECIALISTS, so its lines land inside
    # this body; cut there, or a title starting with a roster name would be
    # read as a specialist.
    specialists = found.get("SPECIALISTS", "")
    cut = _DEPENDS_LABEL.search(specialists)
    if cut:
        specialists = specialists[:cut.start()]
    stages = _clean_specialists(specialists, roster)
    return title, summary, prompt, stages


def parse_folder(raw: str, roots=()) -> str:
    """The `FOLDER:` answer, canonicalised to a member of `roots`, or `""`.

    A **scanner, not a partitioner**: it finds the *last* `FOLDER:` label and
    reads the first non-blank line after it. `_SECTION` / `_SECTION_IDEA` are
    deliberately not widened to know the label — a legacy answer whose
    instructions chattily write "FOLDER:" must go on parsing exactly as it
    does today. Because the section is asked for *last*, its text lands
    inside the `SPECIALISTS` block, where `_clean_specialists` already drops
    everything that is not a roster name.

    The return value is always one of the paths **Dark Army supplied**, never the
    model's own string: a path in an answer is untrusted text. Matching is
    case-insensitive against the roots, and failing that against a root's
    basename where that basename is unique across the list. `NONE`, an empty
    section, a missing section, an off-list path and an ambiguous basename
    all return `""` — no suggestion, never a refusal of the whole call.
    """
    choices = [str(r) for r in (roots or ()) if str(r or "").strip()]
    if not choices:
        return ""
    text = _strip_fences(raw or "")
    index = text.rfind("FOLDER:")
    if index < 0:
        return ""
    line = ""
    for candidate in text[index + len("FOLDER:"):].splitlines():
        if candidate.strip():
            line = candidate.strip()
            break
    for prefix in _LIST_MARKERS:
        if line.startswith(prefix):
            line = line[len(prefix):].strip()
            break
    line = _unquote(line).strip()
    if not line or line.upper() == "NONE":
        return ""
    by_root: dict[str, str] = {}
    for root in choices:
        by_root.setdefault(root.lower(), root)
    match = by_root.get(line.lower())
    if match is not None:
        return match
    by_base: dict[str, list[str]] = {}
    for root in choices:
        base = os.path.basename(root.rstrip("/")).lower()
        if base:
            by_base.setdefault(base, []).append(root)
    hits = by_base.get(line.lower()) or []
    if len(hits) == 1:
        return hits[0]
    return ""


def title_refusal(title: str) -> Optional[str]:
    """Why this generated title may not be kept, or None.

    `session_title.py`'s contract: reject, never truncate. A paragraph cut to
    80 characters reads as a title somebody chose, and the person would have
    to notice it was cut before they could put it right.
    """
    text = str(title or "").strip()
    if not text:
        return "Dark Army did not write a title — press Prepare again, or type one"
    if "\n" in text:
        return ("Dark Army's title ran to more than one line — press Prepare again, "
                "or type one")
    if len(text) > MAX_TITLE_OUT:
        return (f"Dark Army's title was longer than {MAX_TITLE_OUT} characters — "
                "that is a sentence rather than a name for the work")
    if len(text.split()) > MAX_TITLE_WORDS:
        return (f"Dark Army's title ran to more than {MAX_TITLE_WORDS} words — "
                "that is a sentence rather than a name for the work")
    return None


def summary_refusal(summary: str) -> Optional[str]:
    """Why this generated description may not be kept, or None.

    Same rule and the same reason as `title_refusal`; the bound is the
    store's own, so an answer that passes here is one `create` will accept.
    """
    text = str(summary or "").strip()
    if not text:
        return ("Dark Army did not write a description — press Prepare again, or "
                "type one")
    if "\n" in text:
        return ("Dark Army's description ran to more than one line — press Prepare "
                "again, or type one")
    if len(text) > board.MAX_SUMMARY_CHARS:
        return (f"Dark Army's description was longer than "
                f"{board.MAX_SUMMARY_CHARS} characters — not usable")
    return None


_OBJECTIVE_WORDS = {
    "beneficiary": "who benefits",
    "intended_benefit": "intended benefit",
    "success_criterion": "success criterion",
}


def objective_refusal(fields: dict) -> Optional[str]:
    """Why these drafted objective lines may not be kept, or None.

    `title_refusal`'s rule — reject, never truncate — over
    `board_outcomes.OBJECTIVE_LIMITS`, the store's own bounds, so an answer
    that passes here is one `create` will accept. **Empty is not a
    refusal**: the objective is a suggestion, and a helper that offered none
    has simply left the boxes for the person.
    """
    for key, words in _OBJECTIVE_WORDS.items():
        cap = board_outcomes.OBJECTIVE_LIMITS[key]
        text = str((fields or {}).get(key) or "").strip()
        if len(text) > cap:
            return (f"Dark Army's {words} was longer than {cap} characters "
                    "— not usable")
    return None


def refusal(prompt: str, tool: str) -> Optional[str]:
    """Why this generated prompt may not be kept, or None.

    Empty and over-long are ours. Everything else is `dispatch.prompt_refusal`,
    and with no assistant chosen the leading-`-` rule plus the union of every
    `_SUBCOMMANDS` set, so picking an assistant afterwards cannot make a
    prepared card unstartable.
    """
    text = str(prompt or "").strip()
    if not text:
        return "Dark Army's answer was not usable"
    if len(text) > MAX_PROMPT_OUT:
        return (f"Dark Army's answer was longer than {MAX_PROMPT_OUT} characters "
                "— not usable as instructions")
    chosen = str(tool or "")
    if chosen:
        return dispatch.prompt_refusal(chosen, text)
    if text.startswith("-"):
        return ("a prompt cannot start with '-' — the assistant's own command "
                "line would read it as an option, not as instructions")
    words = text.split()
    union: set[str] = set()
    for names in dispatch._SUBCOMMANDS.values():
        union |= set(names)
    if len(words) == 1 and words[0].lower() in union:
        return (f"'{words[0]}' is a command, so it would run that instead "
                "of being read as instructions — say it in a sentence")
    return None


__all__ = [
    "MODEL", "TIMEOUT_SECONDS", "TIMEOUT_WITH_ATTACHMENTS_SECONDS",
    "MAX_SUMMARY_INPUT", "MAX_PROMPT_OUT", "MAX_IDEA_INPUT",
    "MAX_TITLE_OUT", "MAX_TITLE_WORDS",
    "MAX_ROSTER_AGENTS", "MAX_ROSTER_DESC_CHARS", "ROSTER_READ_BYTES",
    "MAX_PROJECT_CHOICES",
    "MAX_DEPENDENCY_CHOICES", "MAX_DEPENDENCY_TITLE_CHARS",
    "MODE_HEAD", "MODE_HEAD_IDEA",
    "PREPARER_ROLE", "BRIEF_READ_BYTES", "GROK_AGENT_FILENAME",
    "Brief", "is_preparer_name", "brief_ok", "read_brief",
    "agents_json", "grok_agent_file",
    "prompt_for", "argv", "env_for", "helper_tool", "HELPER_MODELS",
    "WORKER_MODELS",
    "parse", "parse_idea", "parse_folder", "parse_area", "parse_dependencies",
    "candidate_ok", "refusal",
    "LOG_EXCERPT_CHARS", "log_excerpt",
    "title_refusal", "summary_refusal",
    "OBJECTIVE_LABELS", "parse_objective", "objective_refusal",
    "parse_frontmatter", "read_roster",
]


def _areas_block() -> str:
    """Closed area choices; AREA is the answer's final section after FOLDER."""
    return ("\nAfter SPECIALISTS and FOLDER, add the final section AREA:\n"
            "Choose exactly one area name below, or NONE.\n"
            + "\n".join(f"- {a.name} — {a.concept}" for a in areas.AREAS) + "\n")


def parse_area(raw: str) -> str:
    """Last AREA label's first nonblank line, closed against the shipped table.

    The label may wear markdown decoration (`**AREA:**`, `## AREA`) and the
    answer may carry its description tacked on ("Pocket — phones & widgets",
    "Pocket (phones)"): the line is cut at the first separator and the left
    piece matched exactly. `NONE`, an absent section, an off-list name and
    two names with no separator all read `""` — *no opinion*. The fallback
    to Universal is the daemon's (`_prepare_card_text_locked`), never this
    function's, so the table above stays an honest closed-list reading.
    """
    text = _strip_fences(raw or "")
    matches = list(_AREA_LABEL.finditer(text))
    if not matches:
        return ""
    line = _clean_line(text[matches[-1].end():])
    line = _AREA_TRAIL.split(line, maxsplit=1)[0]
    # Decoration off again: `**Pocket** — phones` only exposes its pair
    # once the description is cut away.
    line = _undecorate(_TRAILING_PARENTHETICAL.sub("", line).strip())
    return areas.normalise(line)[0]


def parse_dependencies(raw: str, candidates=()) -> list[str]:
    """The `DEPENDS ON:` answer as a list of ids Dark Army itself supplied.

    `candidates` is `[(card_id, title)]`, the closed list the helper was
    shown. A scanner like `parse_folder`: the *last* label wins, and the
    section runs to the first blank line or the next label (`_SECTION_IDEA`,
    `AREA`, `FOLDER:`). Each line loses its list marker, quotes and markdown
    decoration and is matched case-insensitively against an offered title,
    or exactly against an offered id. `NONE`, an absent section and unknown
    lines give nothing; duplicates collapse in answer order; at most
    `board.MAX_BLOCKERS` survive. A title offered twice (after the clip)
    resolves to nothing, so the reader never guesses. Never a refusal.
    """
    by_title: dict[str, list[str]] = {}
    ids: set[str] = set()
    for cid, title in (candidates or ()):
        cid = str(cid or "")
        key = " ".join(str(title or "").split())[:MAX_DEPENDENCY_TITLE_CHARS]
        if not cid or not key:
            continue
        ids.add(cid)
        by_title.setdefault(key.lower(), []).append(cid)
    if not ids:
        return []
    text = _strip_fences(raw or "")
    matches = list(_DEPENDS_LABEL.finditer(text))
    if not matches:
        return []
    found: list[str] = []
    rest = text[matches[-1].end():]
    for position, line in enumerate(rest.splitlines()):
        if not line.strip():
            if position == 0:
                continue  # the label's own line ended with nothing after it
            break
        if (_SECTION_IDEA.search(line) or _AREA_LABEL.search(line)
                or "FOLDER:" in line):
            break
        item = _undecorate(line.strip())
        for prefix in _LIST_MARKERS:
            if item.startswith(prefix):
                item = item[len(prefix):].strip()
                break
        item = _undecorate(_unquote(item).strip())
        if not item or item.upper() == "NONE":
            continue
        if item in ids:
            hit = item
        else:
            hits = by_title.get(" ".join(item.split()).lower()) or []
            if len(hits) != 1:
                continue
            hit = hits[0]
        if hit not in found:
            found.append(hit)
    return found[:board.MAX_BLOCKERS]
