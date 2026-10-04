"""Dark Army as launcher — the whole of it, in one file so it can be audited in one read.

**This is a capability Dark Army has never had.** Until now it has observed sessions
and reached *into* ones that already existed: a `<channel>` push, a `/compact`
typed on an input line, a SIGTERM at a pid it re-verified. It has never started
one. Starting a session spends money, opens a window on somebody's screen and
hands an argv to a program that can edit their files, so every guard below is
load-bearing and none of them is a UI nicety.

Six properties, each with the specific thing it stops:

* **Only a deliberate human gesture puts work in front of this** — Start, the
  drop into **In progress**, or an enqueue made by one of those; the drain
  replays an enqueue a person made, re-guarded in full at the instant it
  fires. The only caller of `spawn` is still `BobDaemon.dispatch_card`, and
  the only callers of *that* are the board's Start button, the drop of a card
  into In progress (a drag somebody has to pick up, carry and release, which
  is why it stands in for the old arm-then-confirm), and
  `BobDaemon._flush_queue_dispatches` — the per-project work queue's drain.

  **This is an amendment, made deliberately on 23 Aug 2026, and it is
  narrower than it looks.** The property used to read "there is no timer, no
  queue drain and no scheduler"; the queue is exactly the thing it forbade.
  What survives is the part that was load-bearing: Dark Army never *selects* work.
  Nothing enters the queue without a person having made one of the two
  gestures above on that specific card, the queue remembers only membership
  and order, and the drain re-reads the card out of `board.db` and re-runs
  `guard()` and the plan gate at the moment it fires — so a card that moved,
  was deleted, gained a session or was edited into a refusable shape is
  refused exactly as a fresh press would be. The drain adds no timer of its
  own: it rides the reconcile, which rides the agents snapshot. It has its
  own kill switch (`board_autostart`) beside `board_dispatch`, and with it
  off cards still queue and only a person's press starts them. The reasoning
  is in `plans/2026-08-23-per-project-work-queue.md`; a session that finds
  this surprising should read that before taking it back out.

* **A preference can remove it entirely.** `board_dispatch`, checked in
  `BobDaemon.dispatch_card` before anything else so the refusal is in one place
  and no surface can route around it. Default on, because the arm-then-confirm
  is the real gate and a switch defaulted off would make the board's central
  verb look broken; `dispatch_enabled` rides in the board snapshot so the button
  is *absent* rather than present and inert.

* **State is re-checked at the instant of dispatch.** `guard()` reads the card
  the daemon just fetched out of `board.db`, not the fields the panel sent: the
  card must be in a startable column, name a tool, carry no session and not
  already be dispatching. This is `delete_abandoned_agent`'s category guard in
  another shape — a board window three snapshots behind cannot start a card that
  has since moved.

* **It refuses a directory that is not a known project.** The caller supplies
  `roots` — the folders of every open VS Code window (`workspace.windows()`)
  unioned with the cwd of every live session, each realpath'd — and the card's
  root must be an exact member and an existing directory. `workspace.py` is this
  app's definition of a project and it is reused rather than re-derived. A card
  whose window has since closed is refused in words, recoverable by opening it,
  which is also the only place the session could usefully appear. A card's
  worktree (`<root>/.worktrees/card-<id8>`, `docs/card-worktrees.md`) is
  derived from that root at the moment of spawn, never a root of its own:
  the guard still tests the card's `root`, and only the terminal's `cwd` moves.

* **argv only, and the prompt never touches a shell.** The executable comes from
  `_EXECUTABLES`, a fixed allowlist; the card contributes exactly one element,
  the prompt. Nothing is joined, quoted, interpolated, or handed to `/bin/sh` at
  any point — there is no `subprocess` in this file at all. The extension is
  given `{shellPath, shellArgs, cwd}` and calls `window.createTerminal`, which
  spawns the argv directly. The prompt is **untrusted**: an agent may have
  written it through the channel's card tool.

* **And the prompt is refused if the CLI's own parser would read it as
  something other than a prompt.** "No shell" is true and it is *not* enough —
  it only says nothing between here and `execve` reinterprets the string. The
  program on the far end of that `execve` reinterprets it, and both CLIs here
  do. Measured on the installed versions (claude 2.1.239, codex-cli 0.147.0):
  `codex completion` writes a shell completion script, `codex apply`
  git-applies the last diff into the working tree, `codex logout` drops the
  credentials, `claude mcp` dispatches to its own subcommand — so a one-word
  prompt that happens to be a subcommand name *runs that subcommand* instead of
  starting a session. And `codex "--please-fix-the-tests"` is rejected as an
  unknown flag, which means a prompt opening with `-` is being parsed as one:
  `-c key=value` and `--dangerously-bypass-approvals-and-sandbox` are real
  codex flags, so a leading dash is a sandbox switch away from mattering.
  Both halves are needed and neither is sufficient. `argv_for` puts codex's
  prompt after `--` (verified: `codex -- completion` arrives as a positional);
  `--` does **not** help claude, whose commander-based parser still dispatched
  `mcp` behind it. So `guard()` additionally refuses a prompt that begins with
  `-` or that is exactly a known subcommand of the chosen tool. The lists are
  small, explicit and per-tool, and a conservative list that turns away a
  handful of legitimate one-word prompts is the right side of the trade against
  running `codex apply` on somebody's tree.

* **Bounded.** `MAX_CONCURRENT_DISPATCH` cards may be in flight machine-wide,
  **at most one per project**, and a card that was just tried is refused for
  `DISPATCH_COOLDOWN`. The per-project bound is not politeness: binding works by
  elimination (see `daemon._reconcile_board`), and it is what makes "the first
  new session in this project" a single session.

**What is not claimed.** This stops a runaway board and a mis-aimed launch. It
does not sandbox the agent, and a session started this way is exactly as capable
as one the user started by hand — deliberately, see the flags below.

**The flags deliberately not passed** are listed in the comment block below,
where they are `#`-commented rather than left in this docstring on purpose: a
reviewer greps this file for those flag names to prove none of them is passed,
and prose naming them in order to disclaim them is exactly what would answer
such a grep. `test_dispatch.py` tokenises the file for the same reason. Four
of the bullets there are still disclaimers; the model and effort flags are the
exceptions, passed only when a card names a model out of `MODELS` or a level
out of `EFFORTS`.
"""

from __future__ import annotations

# `session_title.py` is the contrast that makes the point. That module spawns
# `claude -p` with four flags whose entire purpose is that the namer never
# becomes a session anybody can see. Every one of them is wrong here:
#
# * no `--no-session-persistence` — the transcript *is* the row. `session_stats`,
#   `history.py`, the title and every cost figure read it.
# * no `--setting-sources ""` — that removes hooks, and hooks are the only way
#   Dark Army learns the session exists. With it, dispatch would launch something Dark Army
#   could never bind, track or stop.
# * no `--strict-mcp-config` — the channel is user-scope; a dispatched session
#   should have exactly the MCP surface the user's own sessions have.
# * `--model` IS now passed, when — and only when — the card names one:
#   per-card, out of the shipped `MODELS` allowlist. This reverses the
#   decision recorded here until 30 Aug 2026, which held that the model
#   belonged to the user's own settings and never to the board
#   (plans/2026-08-30-per-card-model-choice.md). A Default card still launches
#   with no model flag at all, exactly as before.
# * the effort flag is passed on the same terms: only when a card (or the main
#   slot in Settings) names a level out of `EFFORTS`, before any `--`, and a
#   Default card launches with no effort flag at all
#   (plans/2026-10-03-card-and-role-effort-level.md).
# * and no `--dangerously-load-development-channels` — adding it would silently
#   give a dispatched session a capability a hand-started one does not have, on
#   Dark Army's initiative. A dispatched session is an ordinary session.

import logging
import os
import re
import shutil
import time
from typing import Iterable, Optional

from . import agents_poll, channel_server, grok_leader, origin, ptyhost, vscode_reveal

logger = logging.getLogger("dark-army.dispatch")

#: The columns a card may be started *from*. Three, not one, and neither extra
#: is laxness: a drop into In progress is what dispatches, and a card can also
#: land there with no session at all (Dark Army's launcher switched off, or a run that
#: was sent back), so refusing In progress would leave those permanently
#: unstartable with no button on screen able to say why. `prep` is startable
#: because the plan gate (`daemon._plan_gate_refusal`) is a *confirmation*, not
#: a wall — a person may knowingly start unplanned work, and refusing the
#: column here would turn the confirmed press into a dead end. `done` is
#: excluded because starting work somebody has marked finished is always a
#: mistake.
_STARTABLE_COLUMNS = ("prep", "backlog", "in_progress")

#: What Refine is refused with on a scout. The Prep primary on a scout is
#: START, and this is the daemon agreeing with that: a scout has no plan to
#: refine.
SCOUT_REFINE_REFUSAL = "a scout has no plan to refine — press Start"

#: Cards in flight at once, across the whole machine.
MAX_CONCURRENT_DISPATCH = 2
#: And how long after an attempt the same card may be tried again. Short enough
#: not to be in the way of a genuine retry, long enough that a double press
#: cannot open two terminals.
DISPATCH_COOLDOWN = 10.0
#: How long a card waits for its session to appear before it goes back to Ready
#: with an error on it. Generously over a cold `claude` start; well under the
#: point where somebody has forgotten they pressed the button.
DISPATCH_BIND_WINDOW = 120.0

#: Codex and Grok can stop on a question of their own before any session
#: exists — Codex 0.156 asks "Trust this folder?" the first time it runs in a
#: folder, whatever the approval mode. The give-up line names it, so the person
#: knows what is waiting in the terminal instead of a bare "no session".
_FIRST_RUN_NAMES = {"codex": "Codex", "grok": "Grok"}


def first_run_hint(tool, verb: str = "Start") -> str:
    """The give-up words for a tool that may be waiting on its own first-run
    question, or "" for any other tool."""
    name = _FIRST_RUN_NAMES.get(str(tool or ""))
    if not name:
        return ""
    return (f"no session appeared — {name} may be asking to trust this folder: "
            f"answer it in the terminal that opened, then press {verb} again")

#: The allowlist. A tool not named here cannot be launched, whatever a card says.
#: The value is the executable name looked up on the daemon's PATH — never a
#: path out of a card, and never a string a surface supplied.
_EXECUTABLES = {
    "claude": "claude",
    "codex": "codex",
    "grok": "grok",
}

#: Where codex lives when it is not on the PATH we were handed. `shutil.which`
#: alone is not enough here and this is not hypothetical: the LaunchAgent plist
#: sets no `EnvironmentVariables` and `launchctl getenv PATH` is empty, so a
#: login-started Dark Army searches the bare launchd PATH, where `which` finds
#: neither claude (`~/.local/bin`) nor codex (`/opt/homebrew/bin`). It only
#: worked in a checkout because that process inherited a shell's PATH — i.e.
#: the board's central verb was dead in the installed app and green in
#: development. Claude has had the same fallback since `agents_poll` was
#: written (`CLAUDE_CANDIDATES` / `find_claude_binary`) and this is its sibling,
#: kept beside its own finder in the module that needs it, which is where
#: `GROK_CANDIDATES` and `_CODE_CANDIDATES` live too.
CODEX_CANDIDATES = (
    "/opt/homebrew/bin/codex",
    "/usr/local/bin/codex",
    "~/.local/bin/codex",
    "~/.codex/bin/codex",
)

#: The argv each tool wants, and the two are **not** the same shape. See the
#: module docstring: codex takes `--` and honours it (`codex -- completion`
#: arrives as a positional prompt), claude's commander parser does not (it still
#: dispatched the `mcp` subcommand behind a `--`), so claude is guarded only by
#: the refusals in `guard()` and gets no separator it would merely swallow.
#:
#: Grok is codex's shape and that is measured, not assumed: its own `--help`
#: documents `grok [OPTIONS] [PROMPT]`, a flag-shaped prompt without the
#: separator is rejected outright (`unexpected argument \'--please-fix-the-tests\'
#: found`, with a tip naming `--`), and behind the separator both that and a
#: subcommand name (`grok -- completions`) reach the TUI as the prompt — the
#: only thing that then stops it here is having no terminal to open.
#:
#: The model flag, where a card names one, sits **before** the separator: what
#: follows `--` is the prompt, so a flag placed after it is prompt text and the
#: session silently runs on the wrong model. The effort flag follows the same
#: rule, and is spelled per CLI (claude `--effort`, codex `-c
#: model_reasoning_effort=<level>` as two argv elements, grok
#: `--reasoning-effort`); empty means Default, no flag at all. The long form `--model` for all
#: three (claude has no short form; codex and grok accept `-m` too, and one
#: spelling is easier to audit than three).
_ARGV = {
    "claude": lambda exe, prompt, model, effort="": (
        [exe] + (["--model", model] if model else [])
        + (["--effort", effort] if effort else []) + [prompt]
    ),
    "codex": lambda exe, prompt, model, effort="": (
        [exe] + (["--model", model] if model else [])
        + (["-c", f"model_reasoning_effort={effort}"] if effort else [])
        + ["--", prompt]
    ),
    "grok": lambda exe, prompt, model, effort="": (
        [exe] + (["--model", model] if model else [])
        + (["--reasoning-effort", effort] if effort else []) + ["--", prompt]
    ),
}

#: The models a card may name, per tool. Curated, shipped in code, and
#: **allowed to go stale**: there is no runtime discovery here and no free text
#: — a card's model is one of a handful of fixed strings Dark Army itself ships, so a
#: flag-shaped or subcommand-shaped model is structurally impossible and needs
#: none of `prompt_refusal`'s treatment. `""` is always legal and means
#: *Default* — launch with no model flag at all, exactly as before this list
#: existed — and it is deliberately never listed here.
#:
#: A tool in `_EXECUTABLES` with no entry (or an empty tuple) simply has no
#: model choice: `guard()` refuses any non-empty model for it and every chooser
#: is then absent. A flag is never invented for a CLI that has not been checked.
#:
#: Measured on this machine, 2026-08-30: claude's `--help` documents the alias
#: form (`session_title.py` already passes `--model haiku`). Codex's own
#: `codex debug models`, rechecked 2026-09-23 with codex-cli 0.156.1, lists
#: `gpt-6-astra`, `gpt-6-sol` and `gpt-6-luna` alongside the four existing
#: slugs. `grok models`, rechecked 2026-09-21,
#: prints `grok-4.7 (default)`, `grok-4.7-build-fast`, `grok-4.6`, `grok-4.5`.
#: The menu takes `grok-4.7` and keeps the two earlier pins; a `-build` id
#: stays off it, the same way `grok-4.6-build` never was a card choice.
#: Keeping up with a renamed provider
#: model is a one-line edit here, which is the whole maintenance story of this
#: constant — until then that entry launch-fails and Default still works.
#: `agent_models.py` is the second reader: the settings window's per-role
#: allowlist is this dict (plus the card preparer's own cheap model), so a
#: name added or renamed here reaches both choosers from one edit.
MODELS = {
    # Aliases *and* pins, because they answer different questions. An alias
    # ("opus") means "the latest of that family, whatever it becomes" and is
    # the right choice for a card that will be started next week; a pinned id
    # ("claude-opus-5") means "this exact one", which is what steering by
    # model actually requires once a family has several members — and it is
    # the whole reason the alias-only list was not enough. `[1m]` is the
    # million-token-context variant of the same model, a different pin rather
    # than a different family. Read off this build's own known-model list
    # (`~/.local/share/claude/versions/<v>`), which names far more than these:
    # the two current generations are a menu, the archive is not.
    "claude": ("opus", "claude-opus-5-5", "claude-opus-5-5[1m]",
               "claude-opus-5", "claude-opus-5[1m]",
               "claude-opus-4-8",
               "sonnet", "claude-sonnet-5-5", "claude-sonnet-5-5[1m]",
               "claude-sonnet-5", "claude-sonnet-5[1m]",
               "claude-sonnet-4-6",
               "fable", "claude-fable-5-1", "claude-fable-5",
               "haiku"),
    "codex": ("gpt-6-astra", "gpt-6-sol", "gpt-6-luna", "gpt-5.6-sol",
              "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5"),
    "grok": ("grok-4.7", "grok-4.6", "grok-4.5"),
}

#: The effort levels a card (or a role in Settings) may name, per tool.
#: Curated and shipped in code like `MODELS`; `""` is always legal and means
#: *Default* — no effort flag at all — and is never listed. Measured on this
#: machine, 2026-10-03: claude 2.1.288 `--effort` (low, medium, high, xhigh,
#: max; a level a model lacks is downgraded silently by Claude Code itself),
#: codex 0.157.0 `model_reasoning_effort` (`ultra` is a different capability
#: and stays off the menu), grok 1.0.44 `--reasoning-effort` (the middle five
#: of its seven; `none` stays off). A one-line edit here is the whole
#: maintenance story, as it is for `MODELS`.
EFFORTS = {
    "claude": ("low", "medium", "high", "xhigh", "max"),
    "codex": ("low", "medium", "high", "xhigh", "max"),
    "grok": ("minimal", "low", "medium", "high", "xhigh"),
}

#: Levels a specific model refuses, from `codex debug models`'
#: `supported_reasoning_levels`: `gpt-5.5` has no `max`. Codex refuses an
#: unsupported level at spawn in its own words, so it is excluded here.
EFFORT_EXCLUSIONS = {("codex", "gpt-5.5"): ("max",)}


def efforts_for(tool: str, model: str = "") -> tuple:
    """The effort levels offered for `tool` on `model` (`""` = the tool's own
    default model). `()` for a tool with no catalogue."""
    levels = EFFORTS.get(tool, ())
    gone = EFFORT_EXCLUSIONS.get((tool, str(model or "")), ())
    return tuple(level for level in levels if level not in gone)


def effort_catalogue() -> dict:
    """`{tool: {"": [levels], "<model>": [levels]}}` — one entry per model in
    `MODELS[tool]` plus `""`. Static, so it rides the board snapshot like
    `MODELS` and never makes a frame news."""
    out = {}
    for tool in EFFORTS:
        entry = {"": list(efforts_for(tool, ""))}
        for model in MODELS.get(tool, ()):
            entry[model] = list(efforts_for(tool, model))
        out[tool] = entry
    return out


EFFORT_REFUSAL = "this card names an effort Dark Army does not offer for {tool}"


#: Prompts that are not prompts. Read out of each CLI's own `--help` on the
#: installed version — the `Commands:` block of `claude --help` (2.1.239) and
#: of `codex --help` (codex-cli 0.147.0), plus the aliases those blocks name
#: (`plugins`, `upgrade`, `e`, `a`). Deliberately just the *first* word of a
#: subcommand: the check is against the whole stripped prompt, so `agents` is
#: refused and "agents keep timing out, please look" is not. A short list that
#: turns away a few legitimate one-word prompts beats running `codex apply`.
_SUBCOMMANDS = {
    "claude": frozenset({
        "agents", "auth", "auto-mode", "doctor", "gateway", "import", "install",
        "mcp", "plugin", "plugins", "project", "setup-token", "ultrareview",
        "update", "upgrade", "help",
    }),
    "codex": frozenset({
        "exec", "e", "review", "login", "logout", "mcp", "plugin", "mcp-server",
        "app-server", "remote-control", "app", "completion", "update", "doctor",
        "sandbox", "debug", "apply", "a", "resume", "archive", "delete",
        "unarchive", "fork", "cloud", "exec-server", "features", "help",
    }),
    "grok": frozenset({
        "agent", "completions", "dashboard", "doctor", "du", "disk-usage",
        "export", "help", "inspect", "leader", "login", "logout", "mcp",
        "memory", "models", "plugin", "sessions", "setup", "trace", "update",
        "version", "v", "worktree", "wrap",
    }),
}

#: Named, and refused, rather than absent. A tool listed here is offered on a
#: card and turned away at the moment somebody starts it, with the reason in
#: words — which beats a chooser that simply has no entry for the assistant the
#: person is looking for. **Grok left this list on 22 Aug 2026**: its argv was
#: the only thing unverified about it, and its `--help` plus three measured runs
#: settled that (see `_ARGV`). Empty today, and kept rather than deleted —
#: the next CLI Dark Army learns about arrives here before it arrives in `_ARGV`.
_UNSUPPORTED: dict = {}


def argv_for(tool: str, executable: str, prompt: str, model: str = "",
             effort: str = "") -> list:
    """The exact argv. The prompt is always the **last** element, verbatim.

    No quoting, no escaping, no substitution: `execve` takes an array and VS
    Code's `createTerminal({shellPath, shellArgs})` passes one, so a shell never
    sees this string and there is nothing for a metacharacter in it to mean.

    What *does* still read it is the CLI's own argument parser, which is why
    codex gets a `--` in front of the prompt and why `guard()` refuses the two
    shapes no separator fixes. Both halves; see the module docstring.

    `model` is the card's own choice, empty meaning Default — and empty is the
    *whole* difference: a Default card's argv is byte for byte what it was
    before per-card models existed. The unknown-tool fallback ignores it,
    `guard()` having already refused a non-empty model for a tool with no
    catalogue.

    `effort` is the same shape: empty is Default and adds nothing, and the flag
    sits before any `--` so it is never prompt text.
    """
    build = _ARGV.get(tool)
    if build is None:
        return [executable, str(prompt)]
    return build(executable, str(prompt), str(model or ""), str(effort or ""))


def resolve_executable(tool: str) -> Optional[str]:
    """Where `tool` lives, or None. PATH first, then the known install sites.

    Blocking (it stats a directory list), so the caller owes the executor hop.
    Off the allowlist, deliberately: the alternative is taking a path out of a
    card, which is the one input here that may have been written by an agent.

    **Not bare `shutil.which`.** Dark Army started from a LaunchAgent has the empty
    launchd PATH and `which` finds none of these, so the fallback is not a
    nicety — without it Start answers "not on Dark Army's PATH" forever in the
    installed app while working perfectly in a checkout. Claude routes through
    `agents_poll.find_claude_binary`, the same resolver `channel_install` uses,
    rather than a second copy of that list.
    """
    if tool == "claude":
        return agents_poll.find_claude_binary()
    name = _EXECUTABLES.get(tool)
    if not name:
        return None
    found = shutil.which(name)
    if found:
        return found
    fallbacks = {"codex": CODEX_CANDIDATES,
                 "grok": grok_leader.GROK_CANDIDATES}.get(tool)
    for candidate in fallbacks or ():
        path = os.path.expanduser(candidate)
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


INSTALLED_CACHE_SECONDS = 30
NOT_INSTALLED_REFUSAL = (
    "{tool} is not installed on this Mac "
    "(Dark Army looked on PATH and at its usual install sites)"
)
_installed_cache: tuple[float, dict[str, bool]] | None = None


def installed_tools() -> dict[str, bool]:
    """Return the assistants Start can find, cached for one board refresh window.

    The board snapshot calls this on its executor; filesystem checks never run
    on the API or UI loop. Return a copy so clients cannot mutate the cache.
    """
    global _installed_cache
    now = time.monotonic()
    if _installed_cache is None or now - _installed_cache[0] >= INSTALLED_CACHE_SECONDS:
        _installed_cache = (now, {tool: resolve_executable(tool) is not None
                                  for tool in _EXECUTABLES})
    return dict(_installed_cache[1])


def prompt_refusal(tool: str, prompt: str) -> Optional[str]:
    """Why this prompt may not be handed to this CLI, or None.

    Two shapes, both measured rather than imagined (module docstring): a prompt
    the parser reads as a **flag**, and a prompt it reads as a **subcommand**.
    Everything else — punctuation, quotes, semicolons, newlines, an entire
    paragraph — passes through untouched, because none of it means anything to
    an `execve` array.
    """
    text = str(prompt or "").strip()
    if not text:
        return None
    if text.startswith("-"):
        return ("a prompt cannot start with '-' — the assistant's own command "
                "line would read it as an option, not as instructions")
    words = text.split()
    if len(words) == 1 and words[0].lower() in _SUBCOMMANDS.get(tool, frozenset()):
        return (f"'{words[0]}' is a {tool} command, so it would run that instead "
                "of being read as instructions — say it in a sentence")
    return None


def normalise_root(root: str) -> str:
    """A directory as this module compares them: absolute, symlinks resolved, no
    trailing separator. Both sides of the membership test go through here, or
    `/tmp` and `/private/tmp` would be different projects on macOS."""
    if not root:
        return ""
    return os.path.realpath(os.path.expanduser(str(root))).rstrip(os.sep) or os.sep


#: The three refusals that are about *this instant* rather than about the card.
#: Named rather than written inline because the work queue has to tell a
#: transient refusal from a permanent one — it absorbs the first (the card
#: waits and is retried) and dequeues on the second (the card can never start,
#: and a queue that holds one is a card silently never running). Matching on
#: prose would be a contract nobody declared; these constants are that
#: contract, and `is_transient` is its one reader.
COOLDOWN_REFUSAL = "that card was just started — give it a moment"
MACHINE_BUSY_REFUSAL = (f"{MAX_CONCURRENT_DISPATCH} sessions are already"
                        " starting — wait for them to appear")
PROJECT_BUSY_REFUSAL = "another card in this project is already starting"
#: A second press on a card whose own branch and folder are still being
#: prepared (`docs/card-worktrees.md`). Transient: the preparation finishes
#: and starts the card itself, so a queued replay holds rather than being
#: dequeued, and a person's press is told in words to wait.
WORKTREE_PREPARING_REFUSAL = ("Dark Army is still preparing this card's own "
                              "branch and folder — it starts by itself when "
                              "that is done")
#: The window that owns the project runs a Dark Army extension too old to
#: open a terminal in a subfolder (`vscode_reveal.SUBFOLDER_SPAWN_MIN_VERSION`).
#: A refusal and never a fallback: starting in the main checkout instead would
#: switch isolation off without anybody choosing it. Not transient.
WORKTREE_WINDOW_REFUSAL = ("this project's VS Code window has an older Dark "
                           "Army extension that cannot open a terminal in the "
                           "card's own folder — reload the window, or switch "
                           "card isolation off for this project")

_TRANSIENT_REFUSALS = frozenset(
    {COOLDOWN_REFUSAL, MACHINE_BUSY_REFUSAL, PROJECT_BUSY_REFUSAL,
     WORKTREE_PREPARING_REFUSAL})


def is_transient(detail: str) -> bool:
    """Whether a `guard()` refusal will pass on its own.

    Everything else — no tool, an unknown root, a column that cannot start, a
    prompt the tool would read as a flag — is a property of the card and will
    still refuse in an hour. The queue may hold a card only for the first
    kind: holding one for the second is a card that silently never runs.
    """
    return str(detail or "") in _TRANSIENT_REFUSALS


def guard(card: dict, *, roots: Iterable[str], in_flight: Iterable[dict],
          now: float, last_attempt: Optional[float] = None) -> tuple:
    """Whether this card may be dispatched right now. `(ok, detail)`.

    Pure, and given the card as the store just returned it — the point of the
    check is that it is made against the database at the moment of dispatch
    rather than against whatever a surface last rendered.
    """
    if not card:
        return False, "no such card"
    if card.get("column_name") not in _STARTABLE_COLUMNS:
        return False, "only a card in Prep, Backlog or In progress can be started"
    if card.get("session_id"):
        return False, "this card is already being worked on"
    if card.get("link_state") == "dispatching":
        return False, "this card is already starting"
    if card.get("refine_state") in ("dispatching", "live"):
        # The other direction of `refine_guard`'s link-state refusal, and the
        # same pair of states it refuses. `dispatching`: the shared in-flight
        # list covers *other* cards, but `guard` excludes the card's own id
        # from `pending` — so without this, starting a card whose refinement
        # is still binding would race two new claude sessions in one project
        # for one row, which is the exact ambiguity the per-project bound
        # exists to prevent. `live`: the planner is *interviewing* — starting
        # an implementation session beside it puts a second claude in the
        # same project mid-plan, and the planner's eventual `dark_army_attach_plan`
        # is then refused because its card has left Prep. The Refine button
        # is deliberately absent in exactly these states
        # (`BoardCardView.canRefine`); this is the daemon agreeing with it.
        return False, "this card is being refined — wait for the planner"

    tool = str(card.get("tool") or "")
    if not tool:
        return False, "this card does not say which assistant should take it"
    if tool in _UNSUPPORTED:
        return False, _UNSUPPORTED[tool]
    if tool not in _EXECUTABLES:
        return False, f"cannot start {tool}"

    # The model comes out of the same untrusted store field the prompt does,
    # but unlike the prompt it can only ever be one of the handful of fixed
    # strings Dark Army ships in `MODELS` — so the allowlist *is* the whole check,
    # and no shell is involved anywhere on the path (`argv_for` →
    # `vscode_reveal.spawn_agent` → `createTerminal`, argv only). Empty means
    # Default and is always legal, including on an older row with no `model`
    # key at all.
    model = str(card.get("model") or "")
    if model and model not in MODELS.get(tool, ()):
        return False, f"this card names a model Dark Army does not offer for {tool}"
    effort = str(card.get("effort") or "")
    if effort and effort not in efforts_for(tool, model):
        return False, EFFORT_REFUSAL.format(tool=tool)

    # The prompt is checked here, with the rest of the state, so the refusal
    # reaches the presser as a reason on the card rather than as a session that
    # quietly did something else. It is tool-dependent — `mcp` is a subcommand
    # of both, `apply` only of codex — which is why it lives after the tool.
    # `start_prompt`, not the stored field: a planned card's leftover idea is
    # not what the CLI will see, so refusing it would be a wall on a prompt
    # nobody is sending.
    refusal = prompt_refusal(tool, start_prompt(card))
    if refusal:
        return False, refusal

    if last_attempt is not None and now - last_attempt < DISPATCH_COOLDOWN:
        return False, COOLDOWN_REFUSAL

    pending = [c for c in in_flight if c.get("id") != card.get("id")]
    if len(pending) >= MAX_CONCURRENT_DISPATCH:
        return False, MACHINE_BUSY_REFUSAL
    project = card.get("project") or ""
    if any((c.get("project") or "") == project for c in pending):
        # Not politeness. Binding matches the first new session in the project,
        # so two at once in one project would be two cards racing for one row.
        return False, PROJECT_BUSY_REFUSAL

    root = normalise_root(card.get("root") or "")
    if not root:
        return False, "this card does not say which folder to work in"
    known = {normalise_root(r) for r in roots}
    if root not in known:
        return False, "no open window for that project"
    if not os.path.isdir(root):
        return False, "that project folder is not there any more"
    return True, ""


def adhoc_argv(tool: str, executable: str) -> list:
    """The argv of a terminal nobody wrote a card for: `[executable]`.

    One element, and the property that buys is the whole reason this is a
    function rather than `argv_for(tool, exe, "")`. This module's fifth bound
    says "the card contributes exactly one element (the prompt, always last)";
    an ad-hoc launch contributes **zero**. There is no `--` separator to get
    right, no `--model` off an untrusted store field, and no string at all for
    `prompt_refusal`, `_SUBCOMMANDS` or `MODELS` to judge — the only thing on
    the command line is a path `resolve_executable` chose from the allowlist.

    `tool` is taken so the signature reads like `argv_for`'s and so a future
    CLI needing a bare-launch flag has somewhere to put it; it is deliberately
    unused today, and `adhoc_guard` has already refused a tool that is not in
    `_EXECUTABLES` before this is reached.
    """
    del tool  # named for the signature's sake; see the docstring
    return [str(executable)]


def mission_argv(executable: str, agents_json: str, allowed_tools,
                 agent_name: str, prompt: str, display_name: str = "") -> list:
    """The argv of Mission Control, the one standing chief-of-staff session:
    `[executable, "--agents", <json>, "--allowed-tools", <rule>, <rule>, …,
    "--agent", <name>, <prompt>]` — the prompt last, as the fifth bound
    requires.

    Three flags, and every one is already measured on this machine by
    `card_prepare.argv`, which starts `claude` *as* a named agent with
    `--agents <json>` plus `--agent <name>` and grants reads with
    `--allowed-tools`. Nothing new is invented here. `--allowed-tools` is a
    **narrowing**, never a widening: it pre-approves the reads the brief
    names so the session does not raise a permission dialog for every look,
    and everything it does not name still asks. None of the four flags
    `session_title.py` passes is here — this is an ordinary session with
    hooks, a transcript and a row, exactly as a card's is.

    The order is load-bearing. On the installed CLI (2.1.278, re-read from
    `claude --help` on 20 Sep 2026) `--allowed-tools <tools...>` is a
    **variadic** option — "comma or space-separated" — and a variadic
    option swallows every trailing positional until the next option. So
    every rule rides as its own element (`card_prepare.argv` passes its
    `Read(//<path>)` rules the same way; a rule holding a space, the `curl`
    one or a checkout path with a space in it, is never comma-joined and
    never split) and `--agent <name>` sits *after* the rules, ending the
    variadic list, which leaves the prompt as the one positional. The rule
    shapes — `Read(//<absolute>/**)` and the wildcard `Bash(curl -s
    http://127.0.0.1:19874/*)` — were measured on that CLI with
    `--permission-prompts none` (`mission.CURL_RULE` records what matched
    and what did not). The prompt is `mission.OPENING_PROMPT`, a constant,
    so `prompt_refusal` has nothing to judge: no card, no person-typed text
    and no store field reaches the command line. `agents_json`,
    `allowed_tools` (`mission.allowed_tools(root)`, a sequence of rules)
    and `agent_name` are the caller's (`card_prepare.agents_json` and the
    `mission` constants); this module imports neither `card_prepare` nor
    `mission`.

    `display_name` (`mission.NAME`) rides first as `--name <name>`: the
    CLI's own display name for the session (the prompt box, `/resume`, the
    terminal title and the Remote Control session on the phone's Claude
    app), so Mission Control is never titled after whatever it was last
    asked. `--name` takes exactly one value, so it cannot swallow the
    prompt. Empty leaves the flag off.
    """
    rules = ([allowed_tools] if isinstance(allowed_tools, str)
             else list(allowed_tools or ()))
    named = ["--name", str(display_name)] if display_name else []
    return [
        str(executable),
        *named,
        "--agents", str(agents_json),
        "--allowed-tools", *[str(r) for r in rules],
        "--agent", str(agent_name),
        str(prompt),
    ]


def adhoc_guard(*, root: str, project: str, roots: Iterable[str],
                in_flight: Iterable[dict], tool: str, now: float,
                last_attempt: Optional[float] = None) -> tuple:
    """Whether an assistant may be started in `root` right now. `(ok, detail)`.

    `guard`'s sibling for a launch with no card behind it: pure, judged at the
    instant of the press rather than against whatever a surface last drew, and
    reusing `guard`'s three transient refusal constants **verbatim** so
    `is_transient` classifies them without knowing this verb exists.

    `roots` is the offer set the caller resolved (the enrolment ledger — an
    ad-hoc terminal is always Dark Army's own pty, which needs no editor window),
    and `project` is the label this launch will occupy for the one-per-project
    bound. The order is deliberate: the cheap shape checks, then the three
    time-and-place bounds, then the folder — so somebody pressing twice hears
    the cooldown rather than a complaint about a folder that is fine.
    """
    tool = str(tool or "")
    if not tool:
        return False, "no assistant was chosen"
    if tool in _UNSUPPORTED:
        return False, _UNSUPPORTED[tool]
    if tool not in _EXECUTABLES:
        return False, f"cannot start {tool}"

    if last_attempt is not None and now - last_attempt < DISPATCH_COOLDOWN:
        return False, COOLDOWN_REFUSAL
    pending = list(in_flight or ())
    if len(pending) >= MAX_CONCURRENT_DISPATCH:
        return False, MACHINE_BUSY_REFUSAL
    if project and any((c.get("project") or "") == project for c in pending):
        # `guard`'s own reason, from the other side: binding matches the first
        # new session in the project, so an ad-hoc launch racing a card's
        # dispatch would be two starts competing for one row.
        #
        # Empty is not a project, and testing it as one is how an *unenrolled*
        # folder — whose label is `""` — collided with any pending entry that
        # also had no label and was told "already starting": a transient
        # sentence, inviting a retry, for a permanent condition. An empty
        # label falls through to the folder test below, which is the true
        # reason and says so.
        return False, PROJECT_BUSY_REFUSAL

    root = normalise_root(root or "")
    if not root:
        return False, "no folder was chosen"
    known = {normalise_root(r) for r in roots}
    if root not in known:
        return False, "that folder is not one of Dark Army's projects"
    if not os.path.isdir(root):
        return False, "that project folder is not there any more"
    return True, ""


#: A scout summary shorter than this many words is not a brief a scout can
#: investigate; `scout_prompt` leads with the title instead.
SCOUT_BRIEF_MIN_WORDS = 3


def start_prompt(card: dict) -> str:
    """The prompt a Start session opens with.

    `refine_prompt`'s sibling, and the load-bearing half of CLAUDE.md's
    "pressing Start dispatches `/ship implement <plan path>`". `attach_plan`
    writes `plan_path` and moves the card to Backlog; it does **not** rewrite
    `prompt`. The leftover idea is then what `dispatch_card` used to hand the
    CLI, so a planned Grok (or Claude) card re-planned instead of implementing.
    A card with `plan_path` is therefore the implement handoff built from that
    field, even when `prompt` still holds the idea. A card without a plan is
    the prompt field, unchanged.

    A scout is the exception: it has no plan to implement, so Start opens
    `/scout <idea>` even when a leftover `plan_path` is sitting on the
    row. The stored prompt is left alone: those are the person's words, and
    the snapshot already truncates them.
    """
    card = card or {}
    if str(card.get("kind") or "") == "scout":
        return scout_prompt(card)
    path = str(card.get("plan_path") or "").strip()
    if not path:
        return str(card.get("prompt") or "")
    return (
        f"Plan: {path}\n\n"
        f"Read that plan first, then run: /ship implement {path}\n"
        "The plan carries its own acceptance criteria — implement it, "
        "do not re-plan it."
    )


def scout_prompt(card: dict) -> str:
    """The prompt a scout Start session opens with.

    `refine_prompt`'s shape with `/scout` as the first line (the scout
    skill; `/ship scout` is its alias). Codex gets an explicit skill
    sentence naming `.agents/skills/scout/SKILL.md`: investigate,
    write a report, attach it, close the card with the path in the note, and
    stop. Title and stored instructions follow once, exactly as Refine does.

    **The brief is the fullest line the card has.** A scout's first line is
    the whole question the investigation answers, and the phone's composer
    sends the summary as typed — a card titled "Before open report" with the
    summary `O` used to open `/scout O`, a brief with nothing to
    investigate, and the run wrote a report about its own empty brief. So a
    summary under `SCOUT_BRIEF_MIN_WORDS` words yields the first line to the
    title (a title is never that short on a card a person filed), and the
    summary rides once on its own line below, as the title otherwise does.
    """
    card = card or {}
    title = str(card.get("title") or "").strip()
    summary = str(card.get("summary") or "").strip()
    notes = str(card.get("prompt") or "").strip()
    idea = summary or title
    # The title minus a leading "Scout:" — the label Promote strips too —
    # so a card titled "Scout: why X" with the summary "why X" still leads
    # with the summary rather than with its own label.
    title_brief = re.sub(r"^scout:\s*", "", title, flags=re.I).strip() or title
    if title and len(summary.split()) < SCOUT_BRIEF_MIN_WORDS \
            and len(title_brief.split()) > len(summary.split()):
        idea = title_brief
    parts = ["/scout " + idea]
    if card.get("tool") == "codex":
        parts.append(
            "Read .agents/skills/scout/SKILL.md in this project and follow it "
            "for this idea. Investigate, write a report, attach "
            "the report to this card, close the card with the report's path "
            "in the note, and stop. If the skill or required custom agent is "
            "missing, report that specific limitation."
        )
    if title and title != idea:
        parts.append("")
        parts.append("Title: " + title)
    if summary and summary != idea:
        parts.append("")
        parts.append("Brief: " + summary)
    if notes and notes != idea:
        parts.append("")
        parts.append("Instructions:\n" + notes)
    return "\n".join(parts)


_NAMED_PLAN = re.compile(r"^Plan:[ \t]*(\S[^\n]*?\.md)[ \t]*$", re.I)


def named_plan(card: dict) -> str:
    """The plan path a card's notes lead with, or `""`.

    Pure, and only a *claim*: `Plan: <path>.md` as the notes' first line is
    the handoff shape `start_prompt` writes and the one every agent-filed
    plan card has used. A card carrying it in Prep has a finished plan that
    never got attached — Refine then attaches that file (after the daemon's
    containment check) instead of paying a planning run that could only
    re-plan it or, following the notes, build it without ever leaving Prep.
    A scout never has one.
    """
    card = card or {}
    if str(card.get("kind") or "") == "scout":
        return ""
    first = str(card.get("prompt") or "").lstrip().split("\n", 1)[0]
    match = _NAMED_PLAN.match(first.strip())
    return match.group(1).strip() if match else ""


def refine_prompt(card: dict) -> str:
    """The prompt a refinement session opens with.

    The slash entry remains for Claude/Grok. Codex also gets an explicit
    project-local skill path and planning-only instruction: a positional slash
    token alone does not establish skill loading. Title and complete stored
    instructions follow once; objective and attachments are appended by the
    existing guarded daemon route. No shell expansion or new CLI flag.
    """
    card = card or {}
    title = str(card.get("title") or "").strip()
    idea = str(card.get("summary") or "").strip() or title
    notes = str(card.get("prompt") or "").strip()
    parts = ["/ship " + idea]
    if card.get("tool") == "codex":
        parts.append(
            "Read .agents/skills/ship/SKILL.md in this project and follow its "
            "planning-only mode for this idea. Assess the interview before "
            "writing the plan; attach the plan to this card and stop. "
            "If the skill or required custom agent is missing, report that "
            "specific limitation."
        )
    if title and title != idea:
        parts.append("")
        parts.append("Title: " + title)
    if notes and notes != idea:
        parts.append("")
        parts.append("Instructions:\n" + notes)
    return "\n".join(parts)


#: The first words of a batch refinement's prompt. The ship skill enters its
#: batch form on a first line beginning `/ship batch:` (`references/plan.md`,
#: *Batch: several cards in one session*), so the phrase is a contract with
#: that file, not decoration.
BATCH_PROMPT_HEAD = "/ship batch: "

#: Codex's planning-only instruction in its batch form: `refine_prompt`'s
#: sentence, reworded for several cards, because a positional slash token
#: alone does not load a Codex skill.
_CODEX_BATCH_SENTENCE = (
    "Read .agents/skills/ship/SKILL.md in this project and follow its "
    "planning-only mode, batch form, for the cards below. Ask every card's "
    "interview questions together first, then write the plans in parallel; "
    "attach each plan to its card as soon as it is written, and stop after "
    "the last. If the skill or required "
    "custom agent is missing, report that specific limitation."
)


def batch_blocks(cards: list) -> str:
    """One `## Card k of n — <title>` block per card, in the order given.

    Pure, and shared by every batch verb (a refinement here; the
    batch-implement sibling puts its own head above the same blocks). Each
    block carries `Card id:` — the id the planner writes into the plan's
    `- **Card:**` header, which is how the attach finds the card — then the
    summary when it says more than the title, the stored instructions, and
    the card's objective as `Objective:` lines. `k` is the card's 1-based
    place, the same number the daemon writes as `batch_rank`.
    """
    cards = [c or {} for c in (cards or [])]
    n = len(cards)
    blocks = []
    for k, card in enumerate(cards, start=1):
        title = " ".join(str(card.get("title") or "").split()) or "card"
        summary = str(card.get("summary") or "").strip()
        notes = str(card.get("prompt") or "").strip()
        lines = [f"## Card {k} of {n} — {title}",
                 f"Card id: {card.get('id') or ''}"]
        # A planned card names its plan — the batch-implement form's whole
        # brief per card. Only where there is one, so a refinement's Prep
        # cards (which have none) produce exactly the blocks they always did.
        plan = str(card.get("plan_path") or "").strip()
        if plan:
            lines.append(f"Plan: {plan}")
        if summary and summary != title:
            lines.append("Summary: " + summary)
        if notes and notes != summary:
            lines.append("Instructions:\n" + notes)
        objective = objective_block(card).strip()
        if objective:
            lines.append(objective)
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def refine_batch_prompt(cards: list) -> str:
    """The prompt one planning session opens with when it refines several
    Prep cards at once. Pure.

    `BATCH_PROMPT_HEAD` and a sentence naming how many cards, Codex's
    planning-only instruction once when the batch is Codex's, then
    `batch_blocks`. The attachments are the daemon's to append, per card
    (`_refine_cards_locked`); `refine_prompt` is untouched, so a single
    Refine's argv is byte-identical to what it was.
    """
    cards = [c or {} for c in (cards or [])]
    n = len(cards)
    parts = [BATCH_PROMPT_HEAD
             + f"refine {n} Prep cards in this one session, one plan each"]
    if cards and cards[0].get("tool") == "codex":
        parts.append(_CODEX_BATCH_SENTENCE)
    parts.append("")
    parts.append(batch_blocks(cards))
    return "\n".join(parts)


def stacked_title(agent: str, count: int) -> str:
    """The name a several-card session wears: `<agent> · Stacked cards <n>`.

    Without one, Claude's Remote Control list on the phone shows the CLI's
    own random slug for a batch (`<host>-cheerful-sutherland`), because a
    batch prompt opens on `/ship batch:` and gives the CLI nothing to title
    it from. The agent is the cast member the session will be bound as
    (`_role_nickname`); an empty one leaves just the count. Clamped to the
    terminal tab's 40 characters.
    """
    base = f"Stacked cards {int(count)}"
    agent = str(agent or "").strip()
    return (f"{agent} · {base}" if agent else base)[:40]


def with_session_name(tool: str, argv: list, name: str) -> list:
    """`argv` with `--name <name>` after the executable, for claude only.

    The CLI's display name (prompt box, `/resume`, terminal title and the
    Remote Control session on the phone). `--name` takes exactly one value,
    so it cannot swallow the prompt, which stays the last element. Codex and
    Grok have no such flag and get `argv` back unchanged, as does an empty
    name.
    """
    if tool != "claude" or not name or not argv:
        return list(argv)
    return [argv[0], "--name", str(name), *argv[1:]]


def implement_batch_prompt(cards: list) -> str:
    """The prompt one implementation session opens with when it builds
    several planned Backlog cards, one after another. Pure.

    `BATCH_PROMPT_HEAD` and a sentence naming how many cards and the verb
    that moves the session on (`dark_army_next_card`), then `batch_blocks`,
    whose blocks each carry `Card id:` and `Plan:`. The first non-empty
    line is the head, **never** `Plan:` — a `Plan:` first line enters the
    skill's single-card implement mode, and the ship skill enters the batch
    form on `/ship batch: implement` (`references/implement.md`, *Batch:
    several cards in one session*). `start_prompt` is untouched, so a
    single Start's argv is byte-identical to what it was.
    """
    cards = [c or {} for c in (cards or [])]
    n = len(cards)
    head = (BATCH_PROMPT_HEAD
            + f"implement {n} Backlog cards in this one session, one plan "
            "each, in this order; call "
            + channel_server.tool_name("next_card")
            + " after each card")
    return "\n".join([head, "", batch_blocks(cards)])


#: The head of the block `objective_block` appends. Two newlines in front
#: because it follows the prompt, or the attachment block, as its own
#: paragraph; a test compares the captured argv against it.
OBJECTIVE_BLOCK_HEAD = "\n\nObjective:\n"

_OBJECTIVE_LINES = (
    ("beneficiary", "Who benefits"),
    ("intended_benefit", "Intended benefit"),
    ("success_criterion", "Success criterion"),
)


def objective_block(card: dict) -> str:
    """The card's objective as a labelled block for the end of a Start or
    Refine prompt, or `""` when every one of the three text fields is blank.

    Pure: reads the card dict already in hand, touches no store. Only the
    non-blank lines are drawn, each value collapsed to one line, so the
    block never shows an empty label. Appended by the daemon **after** the
    attachment block and after `guard` / `refine_guard` judged the one-line
    prompt — a criterion beginning with `-` cannot change what the guard
    saw, and for codex and grok the whole prompt is already behind `--`.
    `outcome_check_on` is deliberately not here: a date is the person's
    reminder, not the assistant's brief.
    """
    card = card or {}
    lines = []
    for key, label in _OBJECTIVE_LINES:
        value = " ".join(str(card.get(key) or "").split())
        if value:
            lines.append(f"{label}: {value}")
    if not lines:
        return ""
    return OBJECTIVE_BLOCK_HEAD + "\n".join(lines)


def refine_guard(card: dict, *, roots: Iterable[str], in_flight: Iterable[dict],
                 now: float, last_attempt: Optional[float] = None) -> tuple:
    """Whether this card may be *refined* right now. `(ok, detail)`.

    `guard`'s sibling, pure for its reason, and deliberately its own function
    rather than a flag on it: the two verbs admit different columns (only Prep
    refines), refuse a card that already has a plan, read different link
    fields (`refine_state`, not only `link_state`) and skip the model check
    (the card's model applies to Start alone). The named assistant is the
    same field Start already honours.

    The cooldown, the machine-wide and per-project bounds and the
    root-membership tests are `guard`'s, verbatim in meaning: refinements and
    dispatches share one in-flight budget (the caller widens `in_flight` to
    both kinds), which is what keeps binding-by-elimination unambiguous — at
    most one new session per project, whichever kind it is.
    """
    if not card:
        return False, "no such card"
    if str(card.get("kind") or "") == "scout":
        return False, SCOUT_REFINE_REFUSAL
    if card.get("column_name") != "prep":
        return False, "only a card in Prep can be refined"
    if card.get("plan_path"):
        return False, "this card already has a plan"
    if card.get("session_id"):
        return False, "this card is already being worked on"
    if card.get("link_state") == "dispatching":
        return False, "this card is already starting"
    if card.get("refine_state") in ("dispatching", "live"):
        return False, "this card is already being refined"

    tool = str(card.get("tool") or "")
    if not tool:
        return False, "this card does not say which assistant should take it"
    if tool in _UNSUPPORTED:
        return False, _UNSUPPORTED[tool]
    if tool not in _EXECUTABLES:
        return False, f"cannot start {tool}"

    # Belt and braces: the prompt is Dark Army's own construction (it opens with
    # `/ship `, not a dash) — but the check runs anyway, because summary,
    # title and instructions are untrusted card text and the refusal belongs
    # here, with the rest of the state, rather than as a session that quietly
    # did something else. A leading `-` inside the instructions cannot trip
    # it: `prompt_refusal` only looks at the start of the whole string.
    refusal = prompt_refusal(tool, refine_prompt(card))
    if refusal:
        return False, refusal

    if last_attempt is not None and now - last_attempt < DISPATCH_COOLDOWN:
        return False, "that card was just started — give it a moment"

    pending = [c for c in in_flight if c.get("id") != card.get("id")]
    if len(pending) >= MAX_CONCURRENT_DISPATCH:
        return False, (f"{MAX_CONCURRENT_DISPATCH} sessions are already starting"
                       " — wait for them to appear")
    project = card.get("project") or ""
    if any((c.get("project") or "") == project for c in pending):
        return False, "another card in this project is already starting"

    root = normalise_root(card.get("root") or "")
    if not root:
        return False, "this card does not say which folder to work in"
    known = {normalise_root(r) for r in roots}
    if root not in known:
        return False, "no open window for that project"
    if not os.path.isdir(root):
        return False, "that project folder is not there any more"
    return True, ""


#: Fixed lead of every consult brief, so a leading `-` in untrusted card text
#: cannot make `prompt_refusal` fire — the brief as a whole never starts with
#: a dash. Standing orders ride here as well as after the question.
#:
#: A helper is a new session, so it is born under the current channel name
#: and has `dark_army_answer_card`, never the legacy `bob_answer_card`.
CONSULT_ANSWER_TOOL = channel_server.tool_name("answer_card",
                                               channel_server.CURRENT_NAME)
CONSULT_PREAMBLE = (
    "You are answering one question about a Dark Army board card. "
    f"Answer only through {CONSULT_ANSWER_TOOL}. Change no files. "
    "Do not start the work the card names. Do not close the card.\n\n"
)


def consult_brief(card: dict, question: str) -> str:
    """The prompt a helper session opens with.

    Preamble first, so a leading `-` in the card's title, summary, prompt or
    the typed question is structurally impossible for `prompt_refusal`. The
    card's full prompt is the store's, never the snapshot preview. The
    standing orders repeat after the question so a long prompt cannot bury
    them.
    """
    card = card or {}
    title = str(card.get("title") or "").strip()
    summary = str(card.get("summary") or "").strip()
    prompt = str(card.get("prompt") or "").strip()
    asked = str(question or "").strip()
    parts = [CONSULT_PREAMBLE.rstrip(), ""]
    if title:
        parts.append("Card title: " + title)
    if summary:
        parts.append("Summary: " + summary)
    if prompt:
        parts.append("Instructions:\n" + prompt)
    parts.append("")
    parts.append("Question:\n" + asked)
    parts.append("")
    parts.append(
        f"Answer through {CONSULT_ANSWER_TOOL}. Answer only. Change no files. "
        "Do not start the work. Do not close the card."
    )
    return "\n".join(parts)


def helper_guard(card: dict, *, roots: Iterable[str], in_flight: Iterable[dict],
                 now: float, last_attempt: Optional[float] = None,
                 key: str = "consult", tool: str = "claude",
                 prompt: str = "") -> tuple:
    """Whether a helper may be launched for this card right now. `(ok, detail)`.

    `refine_guard`'s sibling: the same root membership, machine-wide bound,
    one-per-project rule and cooldown, but it does **not** require a startable
    column or an empty `session_id` — a Done or in-progress card may be asked
    about. At most one pending helper of this kind per card (`in_flight` id
    `<key>:<card_id>`). Does not exclude this card from `pending`, so a
    dispatching card in the same project (including this one) is seen.

    `consult_guard` is this with `key="consult"` and claude's prompt rules;
    the merge-fix helper (`key="merge-fix"`, the card's own tool) and the
    review helper (`key="consult"`, claude — it answers through the same
    channel tool) use it too (`docs/card-worktrees.md`, *Review and merge*).
    """
    if not card:
        return False, "no such card"
    cid = str(card.get("id") or "")
    if any(c.get("id") == f"{key}:{cid}" for c in in_flight):
        return False, "a helper is already looking at this card"

    refusal = prompt_refusal(tool, prompt)
    if refusal:
        return False, refusal

    if last_attempt is not None and now - last_attempt < DISPATCH_COOLDOWN:
        return False, COOLDOWN_REFUSAL

    pending = list(in_flight)
    if len(pending) >= MAX_CONCURRENT_DISPATCH:
        return False, MACHINE_BUSY_REFUSAL
    project = card.get("project") or ""
    if any((c.get("project") or "") == project for c in pending):
        return False, PROJECT_BUSY_REFUSAL

    root = normalise_root(card.get("root") or "")
    if not root:
        return False, "this card does not say which folder to work in"
    known = {normalise_root(r) for r in roots}
    if root not in known:
        return False, "no open window for that project"
    if not os.path.isdir(root):
        return False, "that project folder is not there any more"
    return True, ""


def consult_guard(card: dict, *, roots: Iterable[str], in_flight: Iterable[dict],
                  now: float, last_attempt: Optional[float] = None,
                  question: str = "") -> tuple:
    """`helper_guard` for an ask: claude, `consult:<card_id>`, and the brief
    built from the question."""
    return helper_guard(
        card, roots=roots, in_flight=in_flight, now=now,
        last_attempt=last_attempt, key="consult", tool="claude",
        prompt=consult_brief(card, question))


#: Fixed lead of the merge-fix helper's brief (a leading `-` in the card's
#: text is structurally impossible). The orders ride here and again at the end.
MERGE_FIX_PREAMBLE = (
    "You are fixing the merge of one Dark Army board card's branch into the "
    "project's main line. Work only in the folder named below, on the card's "
    "own branch. Never touch the main line.\n\n"
)


def merge_fix_prompt(card: dict, *, branch: str, worktree: str, trunk: str,
                     state: str, detail: str, log: str = "") -> str:
    """The prompt the Fix helper opens with. Preamble first; the card's words
    after; the standing orders repeat last so a long note cannot bury them."""
    card = card or {}
    title = str(card.get("title") or "").strip()
    parts = [MERGE_FIX_PREAMBLE.rstrip(), ""]
    if title:
        parts.append("Card title: " + title)
    parts.append("Card branch: " + str(branch))
    parts.append("Folder: " + str(worktree))
    parts.append("Main line: " + str(trunk))
    if state == "checks_failed":
        parts.append("What happened: the project's merge checks failed.")
        if log:
            parts.append("The checks' log: " + str(log))
    else:
        parts.append("What happened: merging the main line into this branch "
                     "stopped on conflicts.")
    if detail:
        parts.append("Dark Army's note: " + str(detail))
    parts.append("")
    parts.append(
        f"Orders: in this folder run `git merge {trunk}` into the card "
        "branch. Resolve every conflict, `git add` the files and commit with "
        "a real message. If the checks failed, run the project's check "
        "script (.dark-army/merge-check.sh in the main checkout) if there is "
        "one and repair the branch until it passes. Commit on the card "
        f"branch ({branch}) only. Never switch to {trunk}, never "
        "`git reset --hard`, never touch the main checkout, never merge into "
        f"{trunk}, never push, tag, install, release or delete a branch. "
        "Use no bob-tldr or bob-actions blocks. End your last message with "
        "the heading `## Merge ready` when the branch is ready for the "
        "person to press MERGE again, or `## Merge needs you` with what is "
        "left.")
    return "\n".join(parts)


#: The review helper's lead: it only reads, and answers through the channel.
REVIEW_PREAMBLE = (
    "You are reviewing one Dark Army board card's branch. Change no files. "
    f"Answer only through {CONSULT_ANSWER_TOOL}. File no cards. Never commit "
    "or push.\n\n"
)


def review_prompt(card: dict, *, branch: str, trunk: str, worktree: str) -> str:
    """The prompt the review helper opens with: the project's review routine
    (`/review <branch>`) on the card's branch, orders to change nothing, and
    the answer's first line as the verdict the daemon reads."""
    card = card or {}
    title = str(card.get("title") or "").strip()
    parts = [REVIEW_PREAMBLE.rstrip(), ""]
    if title:
        parts.append("Card title: " + title)
    parts.append("Card branch: " + str(branch))
    parts.append("Folder (checked out on that branch): " + str(worktree))
    parts.append("")
    parts.append(
        f"Review the branch the way the project's review routine does "
        f"(`/review {branch}`, in .claude/skills/review/SKILL.md), comparing "
        f"it against {trunk}. Change no files, file no cards, never commit "
        f"or push. Answer once, through {CONSULT_ANSWER_TOOL}: the first "
        "line of your answer must be `VERDICT: SHIP` or `VERDICT: STOP — "
        "<the reason in a few words>`, then the review itself. Use no "
        "bob-tldr or bob-actions blocks.")
    return "\n".join(parts)


async def spawn(root: str, argv: list, name: str, *, stamp: str = "",
                cwd: str = "") -> tuple:
    """Ask the one VS Code window that owns `root` to open a terminal running
    `argv`. `(ok, detail, shell_pid_or_None)`.

    `stamp` is `origin.stamp(...)`: the note the new session carries so its row
    can say what started it, put in the terminal's environment at the moment it
    opens rather than reverse-engineered from a transcript afterwards. It is
    **attribution, not authorisation** — see `origin.py`'s docstring for the
    forgery limit. An empty stamp sends the environment payload this function
    always sent, byte for byte.

    No subprocess is started here and that is the design, not an implementation
    detail: VS Code is the parent of the agent process, which is what makes the
    result a real terminal somebody can type into — and what makes it a session
    with hooks, a transcript and a tab, rather than a headless child of the
    daemon that Dark Army could see but nobody could use.

    The third element is the terminal's shell pid — the receipt the bind uses
    to prove a session came out of this exact terminal. Extensions from 0.1.10
    report it (`shellPid`); an older window omits the key, and absence is a
    *fallback* for the bind, never a spawn failure — `SPAWN_MIN_VERSION`
    deliberately stays where it is.

    `cwd` is the folder the terminal opens in — a card's worktree under
    `<root>/.worktrees/` (`docs/card-worktrees.md`) — and `root` when empty,
    which sends exactly the request this function always sent. The window is
    still the one that owns `root`: the worktree is derived from the root,
    never a root of its own (property 4).
    """
    if not argv:
        return False, "nothing to start", None
    extra = {"cwd": cwd} if cwd and cwd != root else {}
    reply = await vscode_reveal.spawn_agent(
        root, argv, name,
        env_extra=({origin.ENV_VAR: stamp} if stamp else None), **extra)
    if reply is None:
        return False, ("no VS Code window could start it — open the project, or "
                       "reload the window if its Dark Army extension is older than "
                       f"{'.'.join(str(p) for p in vscode_reveal.SPAWN_MIN_VERSION)}"
                       ), None
    if not reply.get("spawned"):
        detail = str(reply.get("error") or "the window refused to start it")
        return False, detail, None
    try:
        shell_pid = int(reply.get("shellPid") or 0) or None
    except (TypeError, ValueError):
        shell_pid = None
    return True, str(reply.get("terminalName") or "started"), shell_pid


async def spawn_local(root: str, argv: list, name: str, *,
                      stamp: str = "", cwd: str = "") -> tuple:
    """`spawn`'s sibling: run `argv` on a terminal Dark Army itself owns
    (`ptyhost`), with `cwd=root`. The same `(ok, detail, pid)` triple, so
    the caller is shape-compatible — except that the third element is the
    child's **own** pid rather than a terminal shell's, which is what lets
    the bind be an identity rather than a descent proof.

    Chosen by `_dispatch_card_locked` when the `board_own_terminal`
    preference is on, or when a press sent `own_terminal` (a card with no
    connected terminal asking to spawn one here), and by
    `_refine_card_locked` on the preference alone — Refine has no per-press
    HERE. Nothing here starts a process: the host does, and `argv_for` /
    `guard()` / `_ARGV` / `MODELS` have already run, so every refusal above
    still applies on this path.

    `stamp` is `spawn`'s, and lands the same way: `origin.env_with` builds the
    child's environment from this process's own, plus the one variable. It is
    attribution, not authorisation (`origin.py`). An empty stamp passes no
    `env` at all, so an unstamped local spawn is the call this function always
    made — which also keeps it working against a persistent broker too old to
    know the field.

    `cwd` is `spawn`'s: the card's worktree, or `root` when empty — so an
    isolation-off or non-git start is the call this function always made.
    """
    if not argv:
        return False, "nothing to start", None
    host = ptyhost.current()
    if host is None:
        return False, "Dark Army has no terminal host running", None
    where = cwd or root
    if stamp:
        return await host.start(where, argv, name,
                                env=origin.env_with(os.environ, stamp))
    return await host.start(where, argv, name)


AREA_BLOCK_HEAD = "\n\nArea: "


def area_block(card: dict, brief_present: bool) -> str:
    """Area context after the guarded prompt; it grants no dispatch authority."""
    from . import areas, identity
    slug = str(card.get("area") or "")
    if not slug or areas.get(slug) is None:
        return ""
    lead = next((n for n in identity.NAMES if n.lower() == areas.anchor(slug)), "")
    line = f"{AREA_BLOCK_HEAD}{areas.name(slug)} — lead {lead}."
    if brief_present:
        line += f" Read {areas.brief_path(slug)} first."
    return line
