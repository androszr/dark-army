"""Dark Army's channel: the one sanctioned way to put something in front of a session.

A *channel* is an MCP server that Claude Code spawns over stdio and that may
push `notifications/claude/channel` events into the running session, where they
arrive in the model's context as `<channel source="dark-army" …>` and on screen
as `← dark-army: …` (`bob` for a session born under the legacy name — see
*Two names* below). It is the only documented route in that direction — the session's
inbox socket is not a public API — and it is what turns Dark Army from a monitor into
something that can answer.

**Why this file is Python and not TypeScript.** Every documented channel is Bun
plus `@modelcontextprotocol/sdk`, which would put a JS runtime under a Python
daemon and a Swift panel. The contract is three things — declare
`claude/channel`, emit one notification method, speak newline-delimited JSON-RPC
on stdio — so it is answered here in the standard library, and the spike that
proved it lands in a live session was this file's ancestor.

**Two halves, and the second is the valuable one.** Pushing an event in is the
obvious half. The other is `claude/channel/permission`: when a tool call needs
approval, Claude Code sends us the prompt, we hand it to the daemon, and a
verdict typed anywhere in Dark Army comes back down and answers the dialog. Dark Army has
always known the instant a session started waiting and has only ever been able
to say *go and look*.

**Shape of the link to the daemon.** We listen on an ephemeral loopback port and
*tell* the daemon where we are (`channel_attach`, repeated as a heartbeat so a
daemon restart re-learns us); the daemon opens a short connection per message,
exactly like every other sender in this app. A persistent stream was the other
option and buys nothing: both directions are rare, small and one-way at a time.
That announcement also carries a password minted per process, which every
message back down must repeat — see `ChannelServer.listen` for why an ephemeral
port on its own is not a lock.

Standard library only, and no package-relative imports — this file is *copied*
to `~/.dark-army/` and spawned from a directory that has no package to
import from. Copied rather than embedded as a string: `dark-army-notify`
was embedded, grew a second divergent copy on disk, and CLAUDE.md still carries
the scar.

Codex starts the same file with `--host=codex --name=dark-army`, which is a
deliberately smaller MCP server rather than a Claude channel: the four board
verbs only (add, close, attach a plan, attach a report), with no experimental
push/permission capabilities. Host
selection is fixed when the process starts and enforced at both discovery and
call time.

**Two names, one script — the dual-name window.** The server is registered
twice at user scope, as `dark-army` (argv `--name=dark-army`) and as the
legacy `bob` (`--name=bob`; a registration from the build before carries no
flag and *is* the `bob` one, so an absent or unknown name reads as `bob`).
Registration is user-scope, so the harness spawns **both** copies in every
session. Exactly one of them is *active*: the one the channel flag's own
arguments name (`server:bob` / `server:dark-army`; the rest of the command
line, a prompt included, is never read), or `dark-army` when the flag names
neither (`is_active`). A **flagless** process is the build before's lone `bob`
registration with no sibling — a session opened before the install, or in
the gap between the script write and the `dark-army` add — so it is active
unless the launch line names `dark-army`. The other is *passive*: it answers the handshake
with no channel capability and no instructions, lists no tools, refuses every
call and never talks to the daemon. Two reasons it must stay quiet rather than
merely redundant: two tool lists would double every verb in the model's
context, and the daemon's `_attach_is_displaced` refuses a second port that
claims a session the first already holds, so a second attach would leave one
copy's tools unattributable. A launch flag naming **both** names makes both
active — the one shape this window does not serve; never "fix" it by letting
the daemon accept two ports. The window ends with the follow-up *End the
channel dual-name window*, which removes `LEGACY_NAME` and the passive copy.
"""

from __future__ import annotations

import hmac
import copy
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import unquote

#: The name a new session sees. It becomes `<channel source="dark-army">`, the
#: `← dark-army:` line in the terminal and the `mcp__dark-army__` tool prefix.
CURRENT_NAME = "dark-army"
#: The name every machine enrolled before the rename registered, kept live for
#: the dual-name window so a session born under it keeps its `bob_*` tools. It
#: is on CLAUDE.md's closed list as a window-only alias; the follow-up *End the
#: channel dual-name window* removes it.
LEGACY_NAME = "bob"
NAMES = (CURRENT_NAME, LEGACY_NAME)
#: Each name's tool prefix: `dark_army_add_card` under `dark-army`,
#: `bob_add_card` under `bob`. A process offers only its own prefix.
TOOL_PREFIX = {CURRENT_NAME: "dark_army_", LEGACY_NAME: "bob_"}

#: The ten verbs, in the order `tools/list` offers them to Claude.
VERBS = ("add_card", "close_card", "attach_plan", "attach_report",
         "needs_manual_check", "answer_card", "knowledge_read",
         "knowledge_write", "request_start", "next_card")


def _known_name(name) -> str:
    """`name` if it is one of `NAMES`, else the legacy name."""
    return name if name in NAMES else LEGACY_NAME


def tool_name(verb: str, name: str = CURRENT_NAME) -> str:
    """The tool a session born under `name` calls for `verb`."""
    return TOOL_PREFIX[_known_name(name)] + verb


def name_from_argv(argv=None) -> str:
    """Which registration spawned this process: `--name=`, else `bob`.

    The default is the legacy name, not the current one: the build before this
    registered the script with no flag, and that registration *is* the `bob`
    one. Defaulting to `dark-army` would make a stale registration advertise
    `dark_army_*` tools under `mcp__bob__`, which no skill names.
    """
    args = sys.argv[1:] if argv is None else argv
    for arg in args:
        if arg.startswith("--name="):
            return _known_name(arg.split("=", 1)[1].strip().lower())
    return LEGACY_NAME


def name_flag_present(argv=None) -> bool:
    """Whether this process was started with a `--name=` at all.

    Separate from `name_from_argv` because the two `bob`s behave differently:
    an explicit `--name=bob` is this build's legacy registration, which has a
    `dark-army` sibling and so stays passive unless the launch line names it;
    a **flagless** process is the build before's lone registration — a session
    opened before the install, or in the gap between the script write and the
    `dark-army` add — and has no sibling to speak for it (`is_active`).
    """
    args = sys.argv[1:] if argv is None else argv
    return any(str(arg).startswith("--name=") for arg in args)

HOST_CLAUDE = "claude"
HOST_CODEX = "codex"
HOSTS = frozenset({HOST_CLAUDE, HOST_CODEX})

#: Where the daemon listens for the attach heartbeat and the tool calls — the
#: hook door, chosen by the hook handler's one address rule: a socket path
#: named in the environment is the only address; else a port named there (the
#: bridge for sessions from before the private socket, on its way out, and 0
#: when none is named); else `default_hook_socket()`. Never a second try.
DAEMON_HOST = "127.0.0.1"
DAEMON_SOCKET = os.environ.get("DARK_ARMY_HOOK_SOCKET", "")


def _named_port() -> int:
    raw = os.environ.get("BOB_COMPANION_PORT") or os.environ.get("CLAWD_TANK_PORT", "")
    try:
        return int(raw) if raw else 0
    except ValueError:
        # A port that is not a number names nothing a connect could reach;
        # -1 keeps it "named" so the rule still never falls to the socket.
        return -1


DAEMON_PORT = _named_port()


def default_hook_socket() -> str:
    """The private hook socket in Dark Army's own folder — spelled under
    `.dark-army`, never the old name, which is a link to the same folder."""
    return os.path.join(os.path.expanduser("~"), ".dark-army", "hook.sock")


def daemon_address(sock_path: str = "", port: int = 0):
    """The one address rule: `("unix", path)` or `("tcp", port)`."""
    if sock_path:
        return ("unix", sock_path)
    if port:
        return ("tcp", port)
    return ("unix", default_hook_socket())

#: Re-announce this often. The daemon forgets a channel it cannot reach, and a
#: daemon that restarts has forgotten every channel — this is how a session that
#: has been open all day is reachable again a minute later.
HEARTBEAT_SECONDS = 30.0

#: What the session is told about these events, and it has to distinguish two
#: kinds — the first version of this said "informational and one-way, nothing to
#: reply to", which was true when Dark Army could only announce things and became a
#: bug the moment it could carry the user's own words: the agent would
#: acknowledge a reply politely and carry on waiting.
#:
#: `kind="user"` is a person typing into Dark Army instead of into this terminal. It
#: is an instruction and outranks everything else here. `kind="fleet"` is Dark Army
#: talking about the machine, and stays informational.
_INSTRUCTIONS_TEMPLATE = (
    "Dark Army (the local Claude Code monitor) pushes events into this session. They "
    'arrive as <channel source="{source}" kind="…">.\n'
    '- kind="user": your own user, typing in Dark Army\'s panel rather than in this '
    "terminal. Treat it exactly as if they had typed it here: it is an "
    "instruction, and if you were waiting on an answer, this is the answer. Do "
    "not reply through the channel — there is no tool to do so, and the person "
    "is reading this terminal.\n"
    '- kind="fleet": Dark Army reporting something about the machine or the other '
    "agents. Informational; act only if it concerns your work.\n"
    "A permission prompt answered in Dark Army is applied by the harness itself and "
    "needs nothing from you."
)


def instructions_for(name: str) -> str:
    """The instructions an active copy registered under `name` hands over.

    The `source=` attribute is the name the harness tags our events with, so it
    has to be the name this process was registered under.
    """
    return _INSTRUCTIONS_TEMPLATE.replace("{source}", _known_name(name))


#: The current name's rendering, kept for readers that want the text itself.
INSTRUCTIONS = instructions_for(CURRENT_NAME)


# ── the JSON-RPC pieces, kept pure so they can be tested without a pty ────────

def initialize_result(protocol_version: str, host: str = HOST_CLAUDE,
                      name: str = CURRENT_NAME, active: bool = True) -> dict:
    """Our half of the handshake.

    `serverInfo.name` is the name this process was registered under. A
    **passive** copy (see *Two names* in the module docstring) declares tools
    only — no `experimental`, no `instructions` — so the harness neither
    registers a channel listener on it nor shows the model a second set of
    instructions.

    `experimental["claude/channel"]` is the entire declaration — its *presence*
    is what makes Claude Code register a listener for our notifications. The
    permission key is the second, optional half.

    **On tools.** This used to say "we advertise no tools: Dark Army's channel is
    one-way by design", and that decision has been narrowed rather than
    reversed — the sentence is kept here because a decision that changed is
    worth more than a decision deleted. The channel is still one-way for
    *conversation*: there is no reply tool, Dark Army is not a chat platform, and the
    person is reading this terminal. What it gains is exactly one **outbound
    verb**, `dark_army_add_card`, which writes a card onto Dark Army's Kanban board for a
    human to act on. That is a different kind of thing from a reply: it does not
    talk to anybody, it cannot start anything (see `daemon.dispatch_card`, whose
    only caller is an armed-then-confirmed button), and its whole effect is a
    line of text somebody has to read and press twice on. "While you are in
    there, this also needs doing" stops living in a chat nobody will scroll back
    to, which is the thing the one-way rule was costing.

    Narrowed once more, the same way: there are now **two** outbound verbs, and
    the second is `dark_army_close_card` — a session declaring that the card it is
    already bound to is finished. It still talks to nobody and still starts
    nothing; what it adds is a statement, recorded on the board as somebody's
    word with their name on it, which a human undoes with the Reopen button
    that was already there. It takes no card id at all: see `CLOSE_TOOL`, where
    that omission is the argument.

    And a **third**, by the same narrowing: `dark_army_attach_plan` — a refinement
    session attaching the plan it wrote onto the card it was asked to refine,
    which moves that card from Prep to Backlog. Still no conversation, still
    nothing started: the whole effect is a path recorded on a card and a
    column move a human can undo by dragging. It, too, takes no card id — see
    `ATTACH_TOOL`.

    And a **fourth**, narrower than any of them: `dark_army_needs_manual_check` —
    the same session saying the opposite of `dark_army_close_card`, that the work is
    done but something is left for a person to try by hand, with the steps. It
    moves nothing, starts nothing and closes nothing; its whole effect is a
    badge and a list of steps on the card that session is already executing.
    No card id — see `MANUAL_TOOL`.

    And a **fifth**, for a question Dark Army delivered about this card:
    `dark_army_answer_card`. One message onto the card's thread; nothing else
    changes. No card id — see `ANSWER_TOOL`.

    And a **sixth and seventh**, narrower again: `dark_army_knowledge_read` and
    `dark_army_knowledge_write`, this project's own question-and-answer notes. They
    talk to nobody, start nothing and touch no card; the whole effect of the
    write is a row in `board.db` keyed on the project this session is already
    working in. No project, no root and no session id — see
    `KNOWLEDGE_READ_TOOL`, where that omission is the argument.
    """
    name = _known_name(name)
    capabilities = {"tools": {"listChanged": False}}
    speaks = host == HOST_CLAUDE and active
    if speaks:
        capabilities["experimental"] = {
            "claude/channel": {},
            "claude/channel/permission": {},
        }
    result = {
        "protocolVersion": protocol_version,
        "capabilities": capabilities,
        "serverInfo": {"name": name, "version": "0.1.0"},
    }
    if speaks:
        result["instructions"] = instructions_for(name)
    return result


#: The one tool. Named for what it does to Dark Army rather than for what the agent
#: wants — `dark_army_add_card`, not `queue_work` — because the model has to be able
#: to tell it apart from anything the *project* it is working in provides.
CARD_TOOL_NAME = tool_name("add_card")

#: The schema, and the omissions in it are the interesting part. There is no
#: `session_id` (the daemon resolves the caller by pid at the moment of use and
#: refuses a request it cannot attribute), no `root` (a caller does not get to
#: name a folder Dark Army will later launch into), and **no `column` at all** — every
#: card an agent files lands in Prep, the column for ideas not yet refined into
#: a plan. It used to admit `backlog` and `ready`; `ready` is now retired, and
#: the remaining choice was one value, which is not a choice. In progress is not
#: and never was on offer: arriving there is what a dispatch *means*.
#:
#: `stages` is pure annotation — it grants nothing, and the worst a forged
#: value achieves is a card promising markers that never appear. Nothing on
#: this socket may **create durable objects** beyond the card itself: an
#: argument that minted anything else against a bounded resource would let
#: a forger push a person's own things off their own board, a denial of the
#: surface rather than a wrong label (the folders, retired at schema 22,
#: were refused here on exactly that argument).
#:
#: And **no `model` argument** (decided 2026-08-30, per-card model choice). The
#: card's *content* is reviewed by the human who drags it and presses Start; a
#: model field would ride silently into that press and choose which
#: differently priced model their money is spent on. Every agent-filed card is
#: Default, and a person who wants another model picks it on the card.
#:
#: `depends_on` is annotation of the same kind as `stages`: it links the new
#: card to cards already on the board, mints nothing, and can only ever hold
#: a card a person later presses Start on. Its bounds are the store's
#: `MAX_BLOCKERS` and `MAX_TITLE_CHARS`, restated because this file is
#: copied out and runs with no package to import them from.
DEPENDS_ON_MAX_ITEMS = 8
DEPENDS_ON_MAX_CHARS = 200
CARD_TOOL = {
    "name": CARD_TOOL_NAME,
    "description": (
        "Add a work item to Dark Army's Kanban board for the human to review. Use "
        "this for work you have noticed but are not doing now — it is a note "
        "for a person, not a way to start anything. It lands in Prep (the "
        "column for ideas not yet refined into a plan) and nothing runs "
        "until they act on it themselves."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "maxLength": 120,
                      "description": "One line saying what needs doing."},
            "summary": {"type": "string", "maxLength": 400,
                        "description": ("One or two sentences a non-developer "
                                        "could read, saying what this is for "
                                        "and why it matters. Not the "
                                        "instructions — this is what a person "
                                        "sees on the card.")},
            "notes": {"type": "string", "maxLength": 4000,
                      "description": ("The instructions to hand to whoever "
                                      "picks this up.")},
            "project": {"type": "string",
                        "description": ("Which project, if not this session's "
                                        "own.")},
            "tool": {"type": "string", "enum": ["claude", "codex"],
                     "description": ("Which assistant should take it, if you "
                                     "know.")},
            # Pure annotation, which is what makes it safe on a surface that
            # authenticates nothing: it decides which markers the card draws as
            # still-to-come and it grants no capability whatsoever. The worst a
            # forged value achieves is a card promising stages that never run —
            # visible to the person who has to press Start anyway.
            "stages": {"type": "array", "maxItems": 12,
                       "items": {"type": "string", "maxLength": 48},
                       "description": ("The specialists you expect this work to "
                                       "go through, in order, if you know them "
                                       "(for example the phases of a skill you "
                                       "would run). Shown on the card as a "
                                       "progress track. Leave it out rather "
                                       "than guessing — Dark Army draws what actually "
                                       "runs either way.")},
            # Pure annotation, like `stages`: the card still lands in Prep
            # and starts nothing. `scout` is an investigation ending in a
            # report, never code; `ship` stores as the empty kind.
            "kind": {"type": "string", "enum": ["ship", "scout"],
                     "description": ("scout: an investigation ending in a "
                                     "report, never code")},
            # `dark_army_attach_plan`'s argument, aimed at the card this very
            # call creates — so no card id crosses the socket and no ladder
            # has to guess. The worst a forged value achieves is what add +
            # attach already allow today: a card in Backlog with a Markdown
            # file from its own project on it, which starts nothing.
            "plan": {"type": "string", "maxLength": 1024,
                     "description": ("A plan you have already written for "
                                     "this card — a Markdown file inside the "
                                     "project, absolute or relative to its "
                                     "root. The card lands in Backlog with "
                                     "the plan attached, ready for Start. "
                                     "Use this whenever you file a card for "
                                     "a finished plan, and never write the "
                                     "plan's path into notes instead.")},
            # The cards this one waits on, for a card filed *without* a plan
            # (a planned card takes them from its `Depends on:` header). No
            # capability: the daemon resolves each entry within the card's
            # own project and the store refuses a cycle, a self-wait or
            # another project; a dependency only ever *holds* a card a person
            # has pressed Start on. The worst a forged value achieves is a
            # Prep card that waits on something — visible on the card.
            "depends_on": {"type": "array",
                           "maxItems": DEPENDS_ON_MAX_ITEMS,
                           "items": {"type": "string",
                                     "maxLength": DEPENDS_ON_MAX_CHARS},
                           "description": ("ids or exact titles of cards in "
                                           "the same project that must finish "
                                           "first")},
        },
        "required": ["title"],
    },
}

#: The second tool: an assistant saying the card it is *already working on* is
#: finished. Named for what it does to Dark Army, like its sibling.
CLOSE_TOOL_NAME = tool_name("close_card")

#: One property, and the omissions are the security property rather than an
#: economy of design.
#:
#: **There is no `card_id`.** The daemon resolves the card from the calling
#: session — by pid at the moment of use, through `_channel_session(port)` —
#: and refuses anything else: no attribution, no card, more than one card, or a
#: card already in Done. That scope is what makes a close verb tolerable on a
#: socket that authenticates nothing. `dark_army_add_card` is tolerable because the
#: worst a forged message achieves is a card a human must read and then drag
#: across the board; a close spends exactly that, and it is bought back by the
#: fact that a forger who wins the attach race for a session can only close the
#: card that session is *already executing* — work it is already the author of,
#: on a board that already shows it as in progress. Admitting a `card_id` here
#: would turn an unauthenticated socket into a way to mark arbitrary work
#: finished, and would delete the whole justification.
#:
#: There is no `session_id` (a caller does not get to say who it is) and no
#: `column` (there is one destination and it is Done; anything else is the
#: human's drag).
#: The third tool: a refinement session attaching the plan it wrote onto the
#: card it was asked to refine. Named for what it does to Dark Army, like its
#: siblings.
ATTACH_TOOL_NAME = tool_name("attach_plan")

#: One property, and the omissions are the security property — `CLOSE_TOOL`'s
#: argument, made in this verb's terms.
#:
#: **There is no `card_id`.** The daemon resolves the card from the calling
#: session — by pid at the moment of use, through `_channel_session(port)` —
#: in a two-rung ladder: the Prep card whose `refine_session_id` is the
#: caller, or failing that exactly one Prep card the caller *authored* that
#: has no plan yet (which is what makes a hand-run `/ship` whole: file the
#: card, then attach to it in the next breath). Everything else fails closed.
#: What that scope buys on a socket that authenticates nothing: the worst a
#: forger who wins the attach race achieves is attaching an existing Markdown
#: file *inside that card's own project* to the card its session is already
#: recorded on — a workflow gate unlocked, not a capability gained. Nothing
#: runs, nothing is typed anywhere, and the human still reads the card and
#: drags it. Strictly less than `dark_army_close_card` already tolerates. Admitting
#: a `card_id` here would delete that justification, not widen an API.
#: A session refining several cards at once names its card through the plan
#: file's `- **Card:**` header, never through this call, and the daemon lets
#: that header choose only among the cards it bound to the session itself.
#:
#: There is no `session_id` (a caller does not get to say who it is) and no
#: `column` (there is one destination and it is Backlog — the Prep column's
#: exit condition, written by the store in the same statement as the path).
ATTACH_TOOL = {
    "name": ATTACH_TOOL_NAME,
    "description": (
        "Attach the plan you have just written to the Dark Army board card this "
        "session was started to refine (or the one it filed with "
        "dark_army_add_card a moment ago). Give the plan file's path inside this "
        "project. The card moves from Prep to Backlog with the plan on it — "
        "do not also create a second card for the same plan. When this "
        "session is refining several cards at once, the plan's header line "
        "`- **Card:** <id>` says which card this plan is for."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "maxLength": 1024,
                     "description": ("The plan's path — a Markdown file "
                                     "inside this project, absolute or "
                                     "relative to the project root.")},
        },
        "required": ["path"],
    },
}

REPORT_TOOL_NAME = tool_name("attach_report")

#: One property, and the omissions are `ATTACH_TOOL`'s security property made
#: in this verb's terms.
#:
#: **There is no `card_id`.** The daemon resolves the card from the calling
#: session — by pid at the moment of use, through `_channel_session(port)` —
#: as the one open card `by_session`. What a forger who wins the attach race
#: achieves is pointing the card that session is already bound to at a
#: Markdown file inside that card's own project. Nothing runs, nothing is
#: typed anywhere, and the card stays In progress. Strictly less than
#: `dark_army_close_card` already tolerates. Admitting a `card_id` here would
#: delete that justification, not widen an API.
REPORT_TOOL = {
    "name": REPORT_TOOL_NAME,
    "description": (
        "Attach the report this scout session wrote to the Dark Army board card "
        "it is bound to (dark_army_attach_report). The report lives at "
        "scout/<YYYY-MM-DD>-<slug>/report.md in this project; give its "
        "absolute path. A report under scout/ must pass "
        "python3 .claude/skills/scout/scout_check.py or it is refused. The "
        "card stays In progress; then dark_army_close_card with the "
        "report's absolute path in the note."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "maxLength": 1024,
                     "description": ("The report's path — a Markdown file "
                                     "inside this project, normally "
                                     "scout/<YYYY-MM-DD>-<slug>/report.md; "
                                     "absolute preferred, or relative to "
                                     "the project root.")},
        },
        "required": ["path"],
    },
}

CLOSE_TOOL = {
    "name": CLOSE_TOOL_NAME,
    "description": (
        "Declare that the Dark Army board card this session was started for is "
        "finished. Use it only when the work the card names is done **and "
        "checked** — every verification you can run yourself has passed. If "
        "anything is left for a person to try by hand, say nothing and leave "
        "the card in progress. It closes the one card this session is bound "
        "to and can touch no other; the note is the single sentence somebody "
        "reading the Done column will see under your name. In a batch of "
        "cards, this closes the card Dark Army shows In progress for you; "
        "call dark_army_next_card afterwards."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "note": {"type": "string", "maxLength": 400,
                     "description": ("One sentence saying why this is "
                                     "finished — what was done and what "
                                     "checked it.")},
        },
        "required": ["note"],
    },
}


#: The fourth tool: the same session as `dark_army_close_card`, saying the opposite
#: thing about the same card. Named for what it does to Dark Army, like its siblings.
MANUAL_TOOL_NAME = tool_name("needs_manual_check")

#: Two properties — the steps, and the optional `path` of the check file the
#: session wrote under the project's `manual-check/` folder, which the daemon
#: re-validates (inside the card's root, in that folder, passing
#: `manual_check.check`) — and the omissions are `CLOSE_TOOL`'s security
#: property made in this verb's terms — with strictly less to buy back.
#:
#: **There is no `card_id`.** The daemon resolves the card from the calling
#: session, through `_channel_session(port)`, and refuses everything else: no
#: attribution, no card, more than one open card, or more than one Done card
#: that session closed when it has no open one.
#: What a forger who reaches this port achieves is a badge and a paragraph of
#: steps on a card that session is *already executing* — no column moves, no
#: session starts, nothing is typed anywhere, and a person clears it with one
#: press. That is less than `dark_army_close_card` already tolerates and less than
#: `dark_army_add_card` does. Admitting a `card_id` would delete the justification
#: rather than widen an API: it would turn a socket anything on the machine can
#: write to into a way to hang a chore on arbitrary work.
#:
#: There is no `session_id` (a caller does not get to say who it is) and no way
#: to *clear* the flag — that is a person's press, and a verb an agent could
#: use to unflag its own work would make the badge meaningless.
MANUAL_TOOL = {
    "name": MANUAL_TOOL_NAME,
    "description": (
        "Say that the work on this session's Dark Army board card is finished but "
        "something is left for a person to check by hand, and give them the "
        "steps. First write the check file at "
        "manual-check/<YYYY-MM-DD>-<slug>/check.md and run "
        "python3 .claude/skills/ship/manual_check.py on it; call this with the "
        "steps and that file's path, then call dark_army_close_card: a card "
        "with an open check goes to Done, and the check waits in the Checks "
        "section until a person records Passed or Failed. Write the steps as a "
        "numbered list a non-developer could follow - what to open, what to "
        "press, what they should see - and end with one line saying why a "
        "test could not do it. If you can check it in code instead, do that "
        "and do not call this."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "steps": {"type": "string", "maxLength": 1000,
                      "description": ("The numbered steps, then one line "
                                      "beginning 'Why not automated:'. Plain "
                                      "words, one action per line, naming the "
                                      "surface and what should happen.")},
            "path": {"type": "string", "maxLength": 1024,
                     "description": ("Absolute path of the check file you "
                                     "wrote at manual-check/<YYYY-MM-DD>-"
                                     "<slug>/check.md, checked with python3 "
                                     ".claude/skills/ship/manual_check.py.")},
        },
        "required": ["steps"],
    },
}


#: The fifth tool: write one answer onto the card Dark Army just asked about.
#: Named for what it does to Dark Army, like its siblings.
ANSWER_TOOL_NAME = tool_name("answer_card")

#: One property, and the omissions are `CLOSE_TOOL`'s security property made
#: in this verb's terms.
#:
#: **There is no `card_id`.** The daemon resolves the card from the calling
#: session — a consult ledger entry whose bound session is the caller, or
#: exactly one card `by_session` names — and refuses anything else. Admitting
#: an id would let an unauthenticated socket write "the assistant's answer"
#: onto arbitrary cards. There is no `session_id` (a caller does not get to
#: say who it is). Use only when Dark Army delivered a question about this card;
#: writes one message; changes nothing else.
ANSWER_TOOL = {
    "name": ANSWER_TOOL_NAME,
    "description": (
        "Write one answer onto the Dark Army board card Dark Army just asked you about. "
        "Use this only when Dark Army delivered a question about that card. It "
        "writes one message onto the card's thread and changes nothing "
        "else — it does not start the work, close the card, or edit files."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "answer": {"type": "string", "maxLength": 4000,
                       "description": ("The answer to the question Dark Army "
                                       "asked about this card.")},
        },
        "required": ["answer"],
    },
}


#: The sixth and seventh tools: this project's own notes — the questions
#: somebody has answered about what it is for, who uses it and what it must
#: never do. Named for what they do to Dark Army, like their siblings.
KNOWLEDGE_READ_TOOL_NAME = tool_name("knowledge_read")
KNOWLEDGE_WRITE_TOOL_NAME = tool_name("knowledge_write")

#: **The scoping is the whole security property, and it is an omission.**
#: There is no `project`, no `root`, no `session_id` and no "all projects"
#: mode in either schema. The daemon resolves the project from the calling
#: session by pid at the moment of use (`_session_place`, then membership in
#: `_known_project_roots()`), and refuses a request it cannot place.
#:
#: What a forger who wins the attach race gains is reading and overwriting the
#: notes of the project whose session it displaced — a project it already has
#: a session inside. Nothing runs, nothing is typed, no card is created or
#: moved, no file outside `board.db` is written, and no other project's rows
#: are reachable. That is strictly less than `dark_army_add_card` already tolerates,
#: which puts a row on a human's board. The cost is misleading prose in one
#: project's notes, bounded by the clamps and by
#: `knowledge_store.MAX_ENTRIES_PER_ROOT`, and every row records the author
#: session so a person can see who wrote it.
#:
#: The read takes no arguments at all: an empty properties object, because
#: there is nothing a caller could usefully say that would not also be a way
#: of naming somebody else's project.
KNOWLEDGE_READ_TOOL = {
    "name": KNOWLEDGE_READ_TOOL_NAME,
    "description": (
        "Read this project's stored knowledge notes from Dark Army - the questions "
        "somebody has already answered about what this project is for, who "
        "uses it, what it must never do, and how success is judged. Takes no "
        "arguments: it always returns this project's notes and can never "
        "reach another project's. Read these before asking a person "
        "something the project has already answered."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {},
    },
}

#: Three properties and the same omissions. `key` is the catalogue's question
#: id and is normalised at the store; writing a key that is already there
#: replaces its answer, which is the only way to correct one — there is no
#: delete verb and no way to remove a note from here.
KNOWLEDGE_WRITE_TOOL = {
    "name": KNOWLEDGE_WRITE_TOOL_NAME,
    "description": (
        "File one answer into this project's knowledge notes. Use the "
        "question key from .claude/skills/knowledge/questions.md and the "
        "question's own wording, and keep the answer short - it is stored at "
        "4000 characters and anything past that is dropped without warning. "
        "It always writes into the project this session is working in and can "
        "never reach another project's notes. Writing the same key again "
        "replaces that answer; nothing else changes, nothing runs, and no "
        "card is created or moved."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "key": {"type": "string", "maxLength": 64,
                    "description": ("The catalogue's question id, lower case "
                                    "- letters, digits, dot, dash and "
                                    "underscore.")},
            "question": {"type": "string", "maxLength": 400,
                         "description": ("The question this answers, in the "
                                         "catalogue's own words.")},
            "answer": {"type": "string", "maxLength": 4000,
                       "description": ("The answer, in the person's own "
                                       "words. Kept short.")},
        },
        "required": ["key", "answer"],
    },
}


#: The ninth tool: Mission Control asking the person to start a card.
#: Named for what it does to Dark Army, like its siblings.
START_ASK_TOOL_NAME = tool_name("request_start")

#: **This starts nothing**, and that is the whole of its security property.
#: The card id crosses the wire — the one board verb where it does — because
#: the verb's only effect is an entry on Needs you, on the Mac and the phone,
#: that opens the card; the person's own press on START is what starts it
#: (`BoardVerbsMixin.ask_start`), and Dismiss is their No. What a forger who
#: reaches this port achieves is an entry somebody dismisses. The daemon
#: refuses every caller but the Mission Control session. Claude only: Codex's
#: helper is the narrower board list and Mission Control is a Claude session.
START_ASK_TOOL = {
    "name": START_ASK_TOOL_NAME,
    "description": (
        "Ask the person to start one Dark Army board card. This does not start "
        "it: the card appears on the person's Needs you list, on the Mac and "
        "the phone, as 'start asked', and it starts only when they open it "
        "and press START themselves (Dismiss says no). Use it when the person "
        "asked you to start a card. Only Mission Control may call it. Give "
        "the card's id, as /api/board or /api/state shows it."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "card_id": {"type": "string", "maxLength": 64,
                        "description": "The card's id."},
        },
        "required": ["card_id"],
    },
}


#: The tenth tool: a batch session saying it has finished (or is leaving) the
#: card it is on and is ready for the next. Named for what it does to Dark
#: Army, like its siblings.
NEXT_TOOL_NAME = tool_name("next_card")

#: **No properties at all**, and the omission is `CLOSE_TOOL`'s security
#: property made in this verb's terms. The port is the addressing and the
#: scope is the defence: the daemon resolves the calling session through
#: `_channel_session(port)` and moves only *that* session on to the next card
#: of the batch the person ticked at the press — a card they already chose for
#: it. What a forger who reaches this port achieves is this session skipping
#: ahead in its own batch, with the card it left marked as ended work for a
#: person to look at. It starts nothing and names no card; admitting a
#: `card_id` would turn an unauthenticated socket into a way to bind any
#: session to any card. Codex gets it on the close verb's argument: it writes
#: a board row and types nothing.
NEXT_TOOL = {
    "name": NEXT_TOOL_NAME,
    "description": (
        "In a batch of Dark Army board cards, call this after closing the "
        "card you are on (dark_army_needs_manual_check first if a check is "
        "left, then dark_army_close_card), or after deciding to leave it "
        "unclosed. Dark Army binds you to the next waiting card and answers "
        "with its title and plan, or says the batch is finished. It starts "
        "nothing and takes no card id."
    ),
    "inputSchema": {"type": "object", "properties": {}},
}


#: What one `dark_army_knowledge_read` may put into the calling session's context.
#: The store's own bounds allow 80 entries × 4000 characters — ~320 KB, order
#: 80k tokens — and the skill's method calls this **first**, every run, so an
#: uncapped read would spend most of a session's context before the first
#: question is asked. Capped here rather than at the store because the store's
#: job is to keep what a person said: this is a *rendering* for a model, and
#: the full answer is still on disk.
#:
#: Every shortening is **stated in the returned text**. A model that cannot
#: tell a trimmed answer from a complete one will re-ask the question, which
#: is the whole thing the notes exist to stop.
READ_ANSWER_CHARS = 600
READ_RESULT_CHARS = 24000
_TRIMMED = " …[shortened — the full answer is on file]"


def render_knowledge(entries: list) -> str:
    """The stored rows as one bounded block of text for the calling model.

    Pure, so it can be tested without a socket. Entries arrive in the store's
    own order (by key), and the cap **stops at the first entry that does not
    fit** — every later one is omitted too, whatever its size.

    That is a `break` and not a `continue`, and the difference is the whole
    point. Skipping one oversize entry while going on to append the smaller
    ones after it returns a by-key list *with a hole in the middle*. To a
    model that is indistinguishable from those keys being unanswered — so
    `SKILL.md`'s step 2 picks them as the next questions to ask, and
    `dark_army_knowledge_write` replaces the stored answer with no delete verb and
    no confirmation anywhere on the path. A cap that costs somebody the words
    they filed is worse than a shorter reply.

    Belt and braces on top of the order: the omitted keys are **named**, so a
    model that reads to the end cannot mistake them for unanswered even if it
    ignores the ordering. The list is bounded by the store's own limits
    (`MAX_ENTRIES_PER_ROOT` × `MAX_KEY_CHARS`).

    At least one entry is always returned, however long it is: `and lines`
    exempts the first, because an empty reply reads as "nothing is answered",
    which is the same lie by another route.
    """
    rows = [entry for entry in entries if isinstance(entry, dict)]
    lines = []
    shortened = 0
    total = 0
    omitted = []
    for index, entry in enumerate(rows):
        key = str(entry.get("key") or "")
        question = str(entry.get("question") or "").strip()
        answer = str(entry.get("answer") or "").strip()
        trimmed = len(answer) > READ_ANSWER_CHARS
        if trimmed:
            answer = answer[:READ_ANSWER_CHARS].rstrip() + _TRIMMED
        labels = []
        if str(entry.get("stale") or "") == "1":
            labels.append("STALE")
        try:
            confirmed = float(entry.get("last_confirmed") or 0)
        except (TypeError, ValueError):
            confirmed = 0.0
        if confirmed == 0:
            labels.append("UNCONFIRMED")
        prefix = " ".join(labels)
        head = f"{prefix} [{key}]" if prefix else f"[{key}]"
        block = f"{head} {question}\n{answer}" if question else f"{head}\n{answer}"
        if total + len(block) > READ_RESULT_CHARS and lines:
            omitted = [str(row.get("key") or "") for row in rows[index:]]
            break
        lines.append(block)
        total += len(block) + 2
        # Counted on the appended block, never on the entry: the trailer's one
        # job is to let the model tell a trimmed answer in *this reply* from a
        # complete one, and counting an entry that was then dropped describes
        # something the reader cannot see.
        if trimmed:
            shortened += 1
    text = "\n\n".join(lines)
    notes = []
    if shortened:
        notes.append(
            f"{shortened} answer(s) were shortened for this reply; the full "
            "text is stored and unchanged.")
    if omitted:
        notes.append(
            f"{len(omitted)} further note(s) were left out to keep this reply "
            "small. They are already answered, so do not ask about them: "
            + ", ".join(omitted) + ". Ask again once these are dealt with.")
    if notes:
        text = text + "\n\n(" + " ".join(notes) + ")"
    return text


def _render_tool(tool: dict, name: str) -> dict:
    """A fresh copy of `tool` spelled for a session born under `name`.

    The dicts above are written under the current name; the legacy rendering
    swaps the prefix in the tool's own name and in the sibling names its
    description mentions. A copy every time, so no caller can edit the
    template another caller will read.
    """
    out = copy.deepcopy(tool)
    prefix = TOOL_PREFIX[_known_name(name)]
    current = TOOL_PREFIX[CURRENT_NAME]
    if prefix != current:
        out["name"] = out["name"].replace(current, prefix)
        out["description"] = out["description"].replace(current, prefix)
    return out


def tools_for_host(host: str, name: str = CURRENT_NAME) -> list[dict]:
    """The complete capability boundary for one MCP host and one name.

    Discovery and dispatch both call this function. Omitting a tool from
    ``tools/list`` is not a guard: MCP clients may call a known name directly.
    The names carry the prefix of the registration this process answers to
    (`TOOL_PREFIX`), and only that prefix: a `dark-army` process refuses
    `bob_add_card` as unknown, and the other way round.
    """
    return [_render_tool(tool, name) for tool in _tools_for_host(host)]


def _tools_for_host(host: str) -> list[dict]:
    """The templates `tools_for_host` renders, in `tools/list` order."""
    if host == HOST_CODEX:
        # `dark_army_close_card` joins the two board verbs on the argument
        # `CLAUDE.md` already makes for the Close-terminal door: **this writes
        # a board row and types nothing**. Codex's narrowness is about
        # *control* — typing, replying, disposing a tab, answering a
        # permission prompt — and none of that is here. Leaving it out cost
        # the thing the board is for: Codex could open a card and attach a
        # plan but never say it had finished one, so every card a Codex
        # session worked sat in *In progress* for ever with no report,
        # whatever its own `## Work done` said.
        #
        # The scope is unchanged from Claude's: no card id crosses the wire,
        # the card is resolved from the calling session alone, and for Codex
        # that resolution spends the same fresh exact native-holder proof
        # `dark_army_add_card` and `dark_army_attach_plan` spend. What a forger who
        # reaches the port achieves is moving a card that session is already
        # bound to — strictly less than `close_terminal` already tolerates
        # from the same host.
        #
        # `dark_army_needs_manual_check` joins on the same argument and for
        # the same cost: without it a Codex run whose only open item was a
        # hand-check could neither close its card nor release its place, so
        # the project's queue waited on it. It writes steps on the one card
        # the session is bound to and moves nothing, attributed by the same
        # fresh proof as the close.
        #
        # `dark_army_next_card` joins on the close verb's argument too: it
        # moves the calling session on to the next card of the batch the
        # person ticked, writes board rows and types nothing, attributed by
        # the same fresh proof.
        return [CARD_TOOL, CLOSE_TOOL, ATTACH_TOOL, REPORT_TOOL, MANUAL_TOOL,
                NEXT_TOOL]
    return [CARD_TOOL, CLOSE_TOOL, ATTACH_TOOL, REPORT_TOOL, MANUAL_TOOL,
            ANSWER_TOOL, KNOWLEDGE_READ_TOOL, KNOWLEDGE_WRITE_TOOL,
            START_ASK_TOOL, NEXT_TOOL]


def host_from_argv(argv=None) -> str:
    """Parse Dark Army's private host selector; Claude is the compatibility default."""
    args = sys.argv[1:] if argv is None else argv
    for arg in args:
        if arg.startswith("--host="):
            value = arg.split("=", 1)[1].strip().lower()
            return value if value in HOSTS else HOST_CLAUDE
    return HOST_CLAUDE


def channel_event(content: str, meta: dict | None = None) -> dict:
    """One event, on its way into the session's context.

    `meta` keys become attributes on the `<channel>` tag, and Claude Code drops
    any key that is not a bare identifier — silently, so a hyphen costs you the
    attribute and no error. Filtered here rather than trusted, because the
    daemon composes these from live data.
    """
    clean = {}
    for key, value in (meta or {}).items():
        key = str(key)
        if key.replace("_", "").isalnum() and not key[:1].isdigit():
            clean[key] = str(value)
    return {
        "jsonrpc": "2.0",
        "method": "notifications/claude/channel",
        "params": {"content": str(content), "meta": clean},
    }


def permission_verdict(request_id: str, behavior: str) -> dict:
    """An answer to a tool-approval prompt: `allow` or `deny`, nothing else.

    Anything Claude Code did not issue an id for is dropped on its side without
    a word, which is the right shape — a stale verdict from a panel that was
    looking at a prompt somebody has since answered at the terminal must not
    apply to whatever is on screen now.
    """
    behavior = "allow" if str(behavior).lower().startswith("a") else "deny"
    return {
        "jsonrpc": "2.0",
        "method": "notifications/claude/channel/permission",
        "params": {"request_id": str(request_id), "behavior": behavior},
    }


#: The flag that turns this from an ordinary MCP server into a channel. Read off
#: the *parent's* command line, because there is no other way to know: Claude
#: Code hands a channel-registered server exactly the same environment and the
#: same handshake as a plain one (verified by dumping both), and registration is
#: user-scope, so this process starts in **every** session whether or not that
#: session can receive anything.
CHANNEL_FLAGS = ("--dangerously-load-development-channels", "--channels")


def parent_command(pid: int) -> str:
    """The parent's command line, or "" if it cannot be read."""
    try:
        out = subprocess.run(["ps", "-o", "command=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=3.0)
    except (OSError, subprocess.SubprocessError):
        return ""
    return (out.stdout or "").strip()


#: One channel entry, matched whole: `server:<name>` or a plugin's
#: `<plugin>:<name>@<market>`.
_LAUNCH_ENTRY = re.compile(
    r"server:([A-Za-z0-9_.-]+)|[A-Za-z0-9_.-]+:([A-Za-z0-9_.-]+)@[A-Za-z0-9_.-]+")


def _command_tokens(command: str) -> list:
    """The parent's command line, split on whitespace only.

    `ps -o command=` has already joined argv with spaces, so any quote left in
    it is a prompt's own text (`what's up`), never shell quoting — a shell
    parse would mistake it for one and lose the flag behind it. A channel entry
    never contains a space, so whitespace is the whole of the split."""
    return command.split()


def _launch_entries(value: str):
    """The names in one flag argument (comma-separated), or None when any
    part is not a whole channel entry — then the token is not an argument of
    the flag at all (a prompt, another option's value)."""
    names = []
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        match = _LAUNCH_ENTRY.fullmatch(part)
        if match is None:
            return None
        names.append(match.group(1) or match.group(2))
    return names or None


def launch_names(command: str) -> frozenset:
    """The server names a channel flag's own arguments name, empty without one.

    Only the arguments of a `CHANNEL_FLAGS` token count — `--flag X [Y …]` and
    `--flag=X`, each comma-separated — and a following token counts only while
    every part of it is a whole `server:<name>` / `<plugin>:<name>@<market>`
    entry. The rest of the command line is not read: a prompt that says
    `server:bob` must not wake the `bob` copy beside the one the flag named.
    """
    tokens = _command_tokens(str(command or ""))
    found: set = set()
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        flag, sep, inline = token.partition("=")
        if flag not in CHANNEL_FLAGS:
            continue
        if sep:
            found.update(_launch_entries(inline) or ())
            continue
        while index < len(tokens):
            names = _launch_entries(tokens[index])
            if names is None:
                break
            found.update(names)
            index += 1
    return frozenset(found)


def is_active(command: str, name: str, flagged: bool = True) -> bool:
    """Whether the copy registered under `name` is the one that speaks.

    The launch line decides: the copy its channel flag names is active. When
    it names neither of ours (a plain `claude`, `--resume`, somebody else's
    channel), the current name is active and the legacy copy stays passive.
    Exactly one copy per session for every launch line but one — a flag naming
    **both** names makes both active, which the dual-name window does not
    serve (see the module docstring).

    `flagged=False` is a process started with no `--name=` at all: the build
    before's lone `bob` registration, with no `dark-army` sibling to speak
    for the session — a session opened before the install whose helper
    restarts, or one opened between the script write and the `dark-army` add.
    Passive there would leave the session with no board tools at all, so a
    flagless process is active unless the launch line names `dark-army`.
    """
    named = launch_names(command)
    if name in named:
        return True
    if not flagged:
        return CURRENT_NAME not in named
    return not (named & set(NAMES)) and name == CURRENT_NAME


def is_channel(command: str, name: str) -> bool:
    """Whether the session that spawned us actually registered us as a channel.

    Both halves must be there: the flag *and* our own name — the name this
    process was registered under, so a `server:bob` launch is a channel for
    the `bob` copy and not for the `dark-army` one. A session loading
    somebody else's development channel is not one Dark Army can reach, and claiming
    otherwise is the failure this function exists for — a reply pushed at a
    session that is not a channel is dropped by the harness in silence, so a
    surface that offered one would be a button that does nothing and cannot say
    so. Observed live before it was coded for: two ordinary sessions reported
    themselves reachable, because a user-scope MCP server starts everywhere.
    """
    return name in launch_names(command)


#: Where a session's own id lives in this process's environment, most specific
#: first. **The order is the whole point.** `CLAUDE_CODE_SESSION_ID` is set by
#: Claude Code per session and is exactly right there — but a Grok channel
#: inherits it from `grok agent leader`, a *shared* process that outlives every
#: session it spawns and carries whatever id happened to be in the environment
#: of the Claude session that first started it. Measured live: three Grok
#: channels for three different projects all announced one dead Claude session,
#: 269c6c67, while their real ids sat unread in `GROK_SESSION_ID`. A stale id
#: shared by every channel is worse than no id at all: `_attach_is_displaced`
#: sees a second port claiming a session the first already holds and refuses it,
#: so every Grok channel after the first was rejected every 30 seconds for the
#: life of the leader, and the one that won resolved to a session the daemon had
#: long since evicted. So the per-agent variable is read first and the inherited
#: one is the fallback, which is the direction that cannot be poisoned by a
#: long-lived parent.
SESSION_ID_ENV = ("GROK_SESSION_ID", "CLAUDE_CODE_SESSION_ID")


def session_env_id(env=None) -> str:
    """This session's own id, out of the environment. "" when nothing says.

    Returning "" is a real outcome and not a failure: the daemon falls back to
    resolving the channel by pid, which is what it did before any of these
    variables were read. Announcing somebody else's id is the failure — that is
    a claim on their session, and the registry has no way to tell it from a
    true one.
    """
    env = os.environ if env is None else env
    for name in SESSION_ID_ENV:
        value = str(env.get(name) or "").strip()
        if value:
            return value
    return ""


def grok_session_cwd(session_id: str, grok_home: Path | None = None) -> str:
    """The folder Grok stored for this session, or "".

    Measured live under `grok agent leader`: every MCP child inherits the
    leader's working directory, so `os.getcwd()` is the project that started
    the leader, not the project this session is in. Grok writes the real one
    as the parent of `~/.grok/sessions/<urlencoded-cwd>/<GROK_SESSION_ID>/`.
    Stdlib only — this file is copied out of the package and has no imports
    from `dark_army_daemon`.
    """
    sid = str(session_id or "").strip()
    if not sid:
        return ""
    root = (grok_home or Path.home() / ".grok") / "sessions"
    try:
        matches = [path for path in root.glob(f"*/{sid}") if path.is_dir()]
    except OSError:
        return ""
    if not matches:
        return ""
    try:
        chosen = max(matches, key=lambda path: path.stat().st_mtime)
    except OSError:
        chosen = matches[0]
    return unquote(chosen.parent.name)


def session_cwd(session_id: str = "", env=None) -> str:
    """Working directory to announce with the channel.

    Grok first, from the on-disk session folder, because getcwd() lies under
    the shared leader. Everyone else (Claude, and Grok when the folder is
    missing) falls back to getcwd(), which is the honest answer there.
    """
    env = os.environ if env is None else env
    sid = session_id or session_env_id(env)
    if sid and env.get("GROK_SESSION_ID"):
        folder = grok_session_cwd(sid)
        if folder:
            return folder
    return os.getcwd()


#: A project's enrolment key folder and file. Dark Army only watches projects the
#: user enrolled; every message up to the daemon carries the key of the project
#: this session is in, and an unkeyed one is turned away. Stdlib only and
#: duplicated from `enrollment.py` on purpose — this file is *copied* to
#: `~/.dark-army/dark-army-channel` and cannot import the package. The new
#: folder name is read first; the old one is read second, for projects enrolled
#: before the move, while the read window is open.
KEY_DIR_NAMES = (".dark-army", ".bob-companion")
KEY_FILE_NAME = "key"
MAX_KEY_BYTES = 512
MAX_WALK_DEPTH = 40


def read_project_key(directory: str) -> str:
    """The first non-empty key in `directory`, new folder name first."""
    for name in KEY_DIR_NAMES:
        try:
            with open(os.path.join(directory, name, KEY_FILE_NAME),
                      "r", encoding="utf-8") as fh:
                found = fh.read(MAX_KEY_BYTES).strip()
        except (OSError, ValueError):
            found = ""
        if found:
            return found
    return ""


def project_key(cwd: str = "") -> str:
    """The enrolment key of the project `cwd` sits in, or "".

    **The walk-up skips the home directory, under both names.**
    `~/.dark-army` is the state folder and, on a machine moved from the old
    install, `~/.bob-companion` is a link to it — both are names a project's
    key folder has, so without that skip a session anywhere under $HOME would
    find a `key` there and enrol the whole home directory by accident. The
    skip is per directory, so it covers every name. That is the single most
    damaging bug available in this feature.
    """
    try:
        start = os.path.realpath(cwd or os.getcwd())
    except OSError:
        return ""
    try:
        home = os.path.realpath(str(Path.home()))
    except OSError:
        home = ""
    here = start
    for _ in range(MAX_WALK_DEPTH):
        if not here:
            break
        if here != home:
            found = read_project_key(here)
            if found:
                return found
        parent = os.path.dirname(here)
        if parent == here:
            break
        here = parent
    return ""


def attach_message(port: int, pid: int, cwd: str, session_id: str = "",
                   channel: bool = False, secret: str = "",
                   host: str = HOST_CLAUDE, key: str = "",
                   name: str = CURRENT_NAME) -> dict:
    """Who we are, for the daemon's channel registry.

    `session_id` comes from the environment via `session_env_id` — the key the
    daemon stores sessions under, so there is nothing to resolve and nothing to
    be wrong about. Strictly better than the pid the registry used first, and
    the pid stays as the fallback for a harness that sets nothing. Which
    variable is read, and in what order, is the load-bearing part: see
    `SESSION_ID_ENV`.

    `secret` is this process's inbound password (see `ChannelServer.listen`).
    It travels only over the loopback hook socket and is never written down.

    `"channel"` is the name this process was registered under, which the
    daemon keeps so a question it pushes names the tool this session has.
    """
    return {"type": "channel_attach", "port": int(port), "pid": int(pid),
            "cwd": cwd, "channel": _known_name(name),
            "session_id": session_id, "is_channel": bool(channel),
            "secret": secret,
            # The enrolment key of the project this session is in. A different
            # secret from `secret` above and pointing the other way: `secret`
            # guards *our* inbound port, this says the daemon may listen to us
            # at all.
            "key": key or project_key(cwd),
            "host": host if host in HOSTS else HOST_CLAUDE}


# ── the two ends ──────────────────────────────────────────────────────────────

class ChannelServer:
    """The stdio conversation, the daemon's inbound port, and the heartbeat."""

    def __init__(self, stdin=None, stdout=None, daemon_port: int = DAEMON_PORT,
                 host: str = HOST_CLAUDE, name: str = CURRENT_NAME,
                 active: bool = True, parent: str | None = None,
                 daemon_sock: str = DAEMON_SOCKET):
        self._in = stdin if stdin is not None else sys.stdin
        self._out = stdout if stdout is not None else sys.stdout
        self._daemon_port = daemon_port
        self._daemon_sock = daemon_sock or ""
        self.host = host if host in HOSTS else HOST_CLAUDE
        #: The registration this process answers to, and whether it is the
        #: copy that speaks for this session (see *Two names* in the module
        #: docstring). A passive copy lists nothing, refuses every call and
        #: never talks to the daemon.
        self.name = _known_name(name)
        self.active = bool(active)
        #: The parent's command line, read once — by `main`, which needs it to
        #: decide `active`, or lazily by `parent_command()`. `ps` on every
        #: heartbeat for the life of a session is exactly the habit this
        #: codebase keeps removing.
        self._parent = parent
        self._write_lock = threading.Lock()
        self._sock: socket.socket | None = None
        self.port = 0
        #: This process's inbound password, minted in `listen`. Empty until then.
        self.secret = ""
        #: This project's enrolment key, resolved lazily by `project_key()`.
        self._project_key = ""
        #: Prompts Claude Code has open and we have relayed. Kept so a verdict
        #: for an id we never saw can be dropped here rather than sent on — the
        #: harness would drop it anyway, but silently, and a log line beats
        #: wondering why a button did nothing.
        self.pending: dict[str, dict] = {}

    # -- writing -------------------------------------------------------------

    def send(self, msg: dict) -> None:
        line = json.dumps(msg) + "\n"
        with self._write_lock:
            self._out.write(line)
            self._out.flush()

    def push(self, content: str, meta: dict | None = None) -> None:
        self.send(channel_event(content, meta))

    # -- the daemon's end ----------------------------------------------------

    def listen(self) -> int:
        """Bind an ephemeral loopback port. Returns the port actually taken.

        And mint the password that guards it. The port is the only thing
        standing between any process on this machine and a `push` into the
        session that spawned us — and the session is told to treat a `kind=user`
        push as if the person had typed it, so reaching this socket is reaching
        the agent's keyboard. An ephemeral port is not a secret: it is in
        `lsof`, and 64k blind connects take about a second.

        So the daemon has to prove it is the daemon. The password is minted
        here, travels once in `channel_attach` over the loopback socket the
        daemon already owns, and is held in memory by both ends — never written
        to a file, which is what keeps it out of reach of anything that can read
        the home directory. A same-uid attacker who can read this process's
        memory has already won; one that can only open sockets no longer has.
        """
        self.secret = secrets.token_urlsafe(32)
        self._sock = socket.socket()
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self.port = self._sock.getsockname()[1]
        return self.port

    def serve_daemon(self) -> None:
        """One connection, one JSON line, like every other sender in this app."""
        assert self._sock is not None
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            with conn:
                try:
                    conn.settimeout(5.0)
                    data = conn.makefile("r", encoding="utf-8").readline()
                except OSError:
                    continue
                if data.strip():
                    try:
                        self.handle_daemon_message(data)
                    except Exception:
                        # One bad line must not end the listener: this thread
                        # is the only route the daemon has into the session,
                        # and it never comes back if the loop exits.
                        continue

    def _authentic(self, msg: dict) -> bool:
        """Whether this message came from the daemon we announced ourselves to.

        Constant-time, because the comparison is the whole gate. A server that
        somehow never minted a secret (`listen` not called — only reachable from
        a test) refuses everything rather than falling open: the failure mode of
        an empty password accepted by default is the one this is here to stop.
        """
        if not self.secret:
            return False
        return hmac.compare_digest(str(msg.get("secret") or ""), self.secret)

    def handle_daemon_message(self, line: str) -> None:
        try:
            msg = json.loads(line)
        except ValueError:
            return
        if not isinstance(msg, dict):
            # `[1]` parses fine and then dies on `.get` — before this guard
            # that AttributeError killed the listener thread for good.
            return
        if not self._authentic(msg):
            return
        kind = msg.get("type")
        if self.host != HOST_CLAUDE:
            return
        if kind == "event":
            self.push(msg.get("content", ""), msg.get("meta") or {})
        elif kind == "permission_verdict":
            rid = str(msg.get("request_id") or "")
            if rid:
                self.pending.pop(rid, None)
                self.send(permission_verdict(rid, msg.get("behavior", "deny")))

    def project_key(self) -> str:
        """This session's project key, resolved once and cached.

        Once, because a channel process's working directory does not move under
        it — but re-read while it is empty, so a project enrolled *while* this
        session is running is admitted at its next message rather than at its
        next launch.
        """
        if not self._project_key:
            self._project_key = project_key(session_cwd(
                session_env_id() if self.host == HOST_CLAUDE else ""))
        return self._project_key

    def _keyed(self, msg: dict) -> dict:
        """`msg` with this project's enrolment key on it. Stamped in the two
        transports rather than at each call site so no message shape can be
        forgotten, and on every message rather than once per session —
        un-enrolment has to bite on the next thing a project says."""
        out = dict(msg)
        if not out.get("key"):
            out["key"] = self.project_key()
        return out

    def _connect_daemon(self, timeout: float) -> socket.socket:
        """A socket connected to the daemon's hook door by the one address
        rule. Raises OSError when nothing answers there."""
        kind, where = daemon_address(self._daemon_sock, self._daemon_port)
        if kind == "tcp":
            if where < 0:
                raise OSError("the hook port named in the environment is not a number")
            return socket.create_connection((DAEMON_HOST, where), timeout=timeout)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(timeout)
            sock.connect(where)
        except OSError:
            sock.close()
            raise
        return sock

    def notify_daemon(self, msg: dict) -> bool:
        """Fire-and-forget up to the daemon. Never raises: with no daemon there
        is nothing to be done about it, and this runs inside somebody's editor."""
        msg = self._keyed(msg)
        try:
            with self._connect_daemon(2.0) as sock:
                sock.sendall(json.dumps(msg).encode("utf-8") + b"\n")
            return True
        except OSError:
            return False

    #: How long a tool call waits for the daemon's answer. Short: a `tools/call`
    #: blocks the session that made it, and a board write is one small SQLite
    #: statement behind an executor hop.
    CALL_TIMEOUT = 5.0

    def call_daemon(self, msg: dict) -> dict | None:
        """Send one line up to the daemon and read one line back. None on any
        failure.

        The request/reply sibling of `notify_daemon`, and it needs no new
        transport: `socket_server` already writes the handler's return value back
        as one JSON line — that is how the statusline collector learns what the
        other agents are doing. Everything that can go wrong (no daemon, a
        handler that answered nothing, a timeout) returns None, and the caller
        turns that into an MCP `isError` result rather than leaving the id
        unanswered. An unanswered `tools/call` is a session waiting forever.
        """
        msg = self._keyed(msg)
        try:
            with self._connect_daemon(self.CALL_TIMEOUT) as sock:
                sock.sendall(json.dumps(msg).encode("utf-8") + b"\n")
                sock.settimeout(self.CALL_TIMEOUT)
                line = sock.makefile("r", encoding="utf-8").readline()
        except OSError:
            return None
        if not line.strip():
            return None
        try:
            reply = json.loads(line)
        except ValueError:
            return None
        return reply if isinstance(reply, dict) else None

    def parent_command(self) -> str:
        """The parent's command line, read at most once for this process."""
        if self._parent is None:
            self._parent = parent_command(os.getppid())
        return self._parent

    def heartbeat(self, once: bool = False) -> None:
        # Resolved once: the parent's command line cannot change under us.
        if not self.active:
            # A passive copy never attaches: its active sibling holds this
            # session, and `_attach_is_displaced` would refuse a second port.
            return
        ppid = os.getppid()
        sid = session_env_id() if self.host == HOST_CLAUDE else ""
        msg = attach_message(self.port, ppid, session_cwd(sid),
                             sid,
                             is_channel(self.parent_command(), self.name)
                             if self.host == HOST_CLAUDE else False,
                             secret=self.secret, host=self.host,
                             name=self.name)
        while True:
            self.notify_daemon(msg)
            if once:
                return
            time.sleep(HEARTBEAT_SECONDS)

    # -- Claude Code's end ---------------------------------------------------

    @staticmethod
    def _tool_result(text: str, is_error: bool = False) -> dict:
        """An MCP tool result. Always in this shape, success or failure — an
        error reported as a JSON-RPC error would abort the call rather than tell
        the model what happened, and the model is the one who has to decide
        whether to try again."""
        return {"content": [{"type": "text", "text": str(text)}],
                "isError": bool(is_error)}

    def call_tool(self, params: dict) -> dict:
        """Run `tools/call`. Always returns a result; never raises, never hangs.

        The name selects a branch and an unknown one is still answered. The
        arguments are re-clamped in each branch even though the daemon clamps
        them again: this process is the thing the model can reach, and a schema
        is a description rather than a gate.
        """
        name = str(params.get("name") or "")
        # Only this process's own prefix is a tool at all: a `dark-army` copy
        # answers `bob_add_card` as unknown, and the other way round.
        known = {tool_name(verb, self.name): verb for verb in VERBS}
        if name not in known:
            return self._tool_result(f"unknown tool: {name}", is_error=True)
        if not self.active:
            return self._tool_result(
                f"tool unavailable (passive copy of {self.name}): {name}",
                is_error=True)
        allowed = {tool["name"] for tool in tools_for_host(self.host, self.name)}
        if name not in allowed:
            return self._tool_result(
                f"tool unavailable for {self.host}: {name}", is_error=True)
        args = params.get("arguments")
        if not isinstance(args, dict):
            args = {}
        verb = known[name]
        if verb == "add_card":
            return self._call_add_card(args)
        if verb == "close_card":
            return self._call_close_card(args)
        if verb == "attach_plan":
            return self._call_attach_plan(args)
        if verb == "attach_report":
            return self._call_attach_report(args)
        if verb == "needs_manual_check":
            return self._call_needs_manual_check(args)
        if verb == "answer_card":
            return self._call_answer_card(args)
        if verb == "knowledge_read":
            return self._call_knowledge_read(args)
        if verb == "knowledge_write":
            return self._call_knowledge_write(args)
        if verb == "request_start":
            return self._call_request_start(args)
        if verb == "next_card":
            return self._call_next_card(args)
        return self._tool_result(f"unknown tool: {name}", is_error=True)

    def _call_next_card(self, args: dict) -> dict:
        """`dark_army_next_card`. See `NEXT_TOOL`: no card id crosses this
        boundary, and `args` is ignored entirely — the schema declares no
        properties, and honouring one that arrived anyway would be the first
        step towards naming somebody else's card."""
        reply = self.call_daemon({
            "type": "board_next_request",
            # The port is the whole of the addressing: the daemon resolves
            # *who is asking* and moves only that session on.
            "port": self.port,
            "pid": os.getppid(),
        })
        if reply is None:
            return self._tool_result(
                "Dark Army did not answer — no card was bound.", is_error=True)
        if not reply.get("ok"):
            return self._tool_result(
                str(reply.get("detail") or "Dark Army refused to move on."),
                is_error=True)
        if not reply.get("card_id"):
            return self._tool_result("No card left; the batch is finished.")
        title = str(reply.get("title") or "").strip() or "untitled"
        plan = str(reply.get("plan_path") or "").strip()
        rank = int(reply.get("rank") or 0)
        size = int(reply.get("size") or 0)
        return self._tool_result(
            f"Now on card {rank} of {size}: {title} — Plan: {plan}")

    def _call_request_start(self, args: dict) -> dict:
        """`dark_army_request_start`. See `START_ASK_TOOL`: it asks, it never
        starts. The empty-id refusal is local as well as at the daemon,
        `_call_close_card`'s reasoning."""
        card_id = str(args.get("card_id") or "").strip()[:64]
        if not card_id:
            return self._tool_result("name the card by its id", is_error=True)
        reply = self.call_daemon({
            "type": "board_start_ask_request",
            # Who is asking is resolved by the daemon from the port, never
            # said here.
            "port": self.port,
            "pid": os.getppid(),
            "card_id": card_id,
        })
        if reply is None:
            return self._tool_result(
                "Dark Army did not answer — nothing was asked.", is_error=True)
        if not reply.get("ok"):
            return self._tool_result(
                str(reply.get("detail") or "Dark Army refused the ask."),
                is_error=True)
        title = str(reply.get("title") or "").strip()
        where = f" ({title})" if title else ""
        return self._tool_result(
            f"Asked{where}: the card is on the person's Needs you list as "
            "'start asked'. It has not started — it starts when they open it "
            "and press START, and Dismiss is their no. Tell them that; do not "
            "ask again unless they say so.")

    def _call_knowledge_read(self, args: dict) -> dict:
        """`dark_army_knowledge_read`. No project crosses this boundary — see
        `KNOWLEDGE_READ_TOOL`. `args` is ignored entirely: the schema declares
        no properties, and honouring one that arrived anyway would be the
        first step towards naming somebody else's project.
        """
        reply = self.call_daemon({
            "type": "knowledge_read_request",
            # The port is the whole of the addressing, `_call_add_card`'s
            # reasoning: the daemon resolves *who is asking* and answers only
            # with the notes of the project that session is working in.
            "port": self.port,
            "pid": os.getppid(),
        })
        if reply is None:
            return self._tool_result(
                "Dark Army did not answer — no notes were read.", is_error=True)
        if not reply.get("ok"):
            return self._tool_result(
                str(reply.get("detail") or "Dark Army refused the read."),
                is_error=True)
        entries = reply.get("entries")
        if not isinstance(entries, list) or not entries:
            return self._tool_result(
                "This project has no knowledge notes yet.")
        return self._tool_result(render_knowledge(entries))

    def _call_knowledge_write(self, args: dict) -> dict:
        """`dark_army_knowledge_write`. No project crosses this boundary — see
        `KNOWLEDGE_WRITE_TOOL`. The empty refusals are local as well as at the
        store, `_call_close_card`'s reasoning: a model with nothing to file is
        told immediately rather than after a round trip that can only end the
        same way.
        """
        key = str(args.get("key") or "").strip()[:64]
        answer = str(args.get("answer") or "").strip()[:4000]
        if not key:
            return self._tool_result("a note needs a question key",
                                     is_error=True)
        if not answer:
            return self._tool_result("a note needs an answer", is_error=True)
        reply = self.call_daemon({
            "type": "knowledge_write_request",
            "port": self.port,
            "pid": os.getppid(),
            # **`note_key`, not `key`.** Every message on this socket carries
            # the project's *enrolment* key as `key`, stamped by `_keyed`,
            # which only fills the field when it is empty — a catalogue key
            # sitting there would be read by `BobDaemon._enrolled_root` as an
            # enrolment key, resolve to nothing, and get the write refused at
            # the door on every call. One reserved name, one rename.
            "note_key": key,
            "question": str(args.get("question") or "").strip()[:400],
            "answer": answer,
        })
        if reply is None:
            return self._tool_result(
                "Dark Army did not answer — the note was not stored.", is_error=True)
        if not reply.get("ok"):
            return self._tool_result(
                str(reply.get("detail") or "Dark Army refused the note."),
                is_error=True)
        return self._tool_result(
            f"Filed under {key} in this project's notes. Nothing else "
            "changed.")

    def _call_answer_card(self, args: dict) -> dict:
        """`dark_army_answer_card`. No card id crosses this boundary — see
        `ANSWER_TOOL`. The empty-answer refusal is local as well as at the
        store, `_call_close_card`'s reasoning: a model with nothing to say is
        told immediately rather than after a round trip that can only end the
        same way.
        """
        answer = str(args.get("answer") or "").strip()[:4000]
        if not answer:
            return self._tool_result("an answer needs some text",
                                     is_error=True)
        reply = self.call_daemon({
            "type": "board_answer_request",
            # The port is the whole of the addressing. There is deliberately
            # no card id: the daemon resolves *who is asking* and writes
            # only onto the card that session is answering.
            "port": self.port,
            "pid": os.getppid(),
            "answer": answer,
        })
        if reply is None:
            return self._tool_result(
                "Dark Army did not answer — the message was not stored.",
                is_error=True)
        if not reply.get("ok"):
            return self._tool_result(
                str(reply.get("detail") or "Dark Army refused to store the answer."),
                is_error=True)
        return self._tool_result(
            "Answered on the card's thread. Change nothing else.")

    def _call_needs_manual_check(self, args: dict) -> dict:
        """`dark_army_needs_manual_check`. No card id crosses this boundary — see
        `MANUAL_TOOL`. The empty-steps refusal is local as well as at the
        store, `_call_close_card`'s reasoning: a model with no steps to give is
        told immediately rather than after a round trip that can only end the
        same way.
        """
        steps = str(args.get("steps") or "").strip()[:1000]
        if not steps:
            return self._tool_result("a manual check needs its steps",
                                     is_error=True)
        message = {
            "type": "board_manual_request",
            # The port is the whole of the addressing. There is deliberately no
            # card id: the daemon resolves *who is asking* and flags only the
            # card that session is already bound to.
            "port": self.port,
            "pid": os.getppid(),
            "steps": steps,
        }
        # The check file rides only when one was named: an empty path sends
        # exactly the message this verb sent before the argument existed.
        path = str(args.get("path") or "").strip()[:1024]
        if path:
            message["path"] = path
        reply = self.call_daemon(message)
        if reply is None:
            return self._tool_result(
                "Dark Army did not answer — the card was not flagged.", is_error=True)
        if not reply.get("ok"):
            return self._tool_result(
                str(reply.get("detail") or "Dark Army refused to flag the card."),
                is_error=True)
        title = str(reply.get("title") or "").strip()
        where = f" ({title})" if title else ""
        return self._tool_result(
            f"Flagged on Dark Army's board{where} — the card wears a "
            "manual-check badge with your steps on it until a person records "
            "the outcome. Now close the card with dark_army_close_card: a card "
            "with an open check goes to Done.")

    def _call_attach_plan(self, args: dict) -> dict:
        """`dark_army_attach_plan`. No card id crosses this boundary — see
        `ATTACH_TOOL`. The empty-path refusal is local as well as at the
        daemon, `_call_close_card`'s reasoning: a model with nothing to attach
        is told immediately rather than after a round trip that can only end
        the same way.
        """
        path = str(args.get("path") or "").strip()[:1024]
        if not path:
            return self._tool_result("a plan needs a path", is_error=True)
        reply = self.call_daemon({
            "type": "board_attach_request",
            # The port is the whole of the addressing. There is deliberately
            # no card id: the daemon resolves *who is asking* and attaches
            # only to the card that session is refining or just authored.
            "port": self.port,
            "pid": os.getppid(),
            "path": path,
        })
        if reply is None:
            return self._tool_result(
                "Dark Army did not answer — the plan was not attached.", is_error=True)
        if not reply.get("ok"):
            return self._tool_result(
                str(reply.get("detail") or "Dark Army refused to attach the plan."),
                is_error=True)
        title = str(reply.get("title") or "").strip()
        where = f" ({title})" if title else ""
        return self._tool_result(
            f"Plan attached{where} — the card moved to Backlog with the plan "
            "on it. Do not create another card for this plan.")

    def _call_attach_report(self, args: dict) -> dict:
        """`dark_army_attach_report`. No card id crosses this boundary — see
        `REPORT_TOOL`. The empty-path refusal is local as well as at the
        daemon, `_call_attach_plan`'s reasoning: a model with nothing to
        attach is told immediately rather than after a round trip that can
        only end the same way.
        """
        path = str(args.get("path") or "").strip()[:1024]
        if not path:
            return self._tool_result("a report needs a path", is_error=True)
        reply = self.call_daemon({
            "type": "board_attach_report_request",
            "port": self.port,
            "pid": os.getppid(),
            "path": path,
        })
        if reply is None:
            return self._tool_result(
                "Dark Army did not answer — the report was not attached.",
                is_error=True)
        if not reply.get("ok"):
            return self._tool_result(
                str(reply.get("detail") or "Dark Army refused to attach the report."),
                is_error=True)
        return self._tool_result(
            "Report attached — the card stays in progress; close it with "
            f"{tool_name('close_card', self.name)} and put the report's path "
            "in the note.")

    def _call_close_card(self, args: dict) -> dict:
        """`dark_army_close_card`. No card id crosses this boundary — see `CLOSE_TOOL`.

        The empty-note refusal is local as well as at the store, so a model that
        calls this with nothing to say is told immediately rather than after a
        round trip that can only end the same way.
        """
        note = str(args.get("note") or "").strip()[:400]
        if not note:
            return self._tool_result("a close needs a reason", is_error=True)
        reply = self.call_daemon({
            "type": "board_close_request",
            # The port is the whole of the addressing. There is deliberately no
            # card id: the daemon resolves *who is asking* and closes only the
            # card that session is already bound to.
            "port": self.port,
            "pid": os.getppid(),
            "note": note,
        })
        if reply is None:
            return self._tool_result(
                "Dark Army did not answer — the card was not closed.", is_error=True)
        if not reply.get("ok"):
            return self._tool_result(
                str(reply.get("detail") or "Dark Army refused to close the card."),
                is_error=True)
        title = str(reply.get("title") or "").strip()
        where = f" ({title})" if title else ""
        return self._tool_result(
            f"Moved to Done on Dark Army's board{where}, with your note. A human can "
            "send it back with Reopen.")

    def _call_add_card(self, args: dict) -> dict:
        """`dark_army_add_card`. Unchanged since it was the only verb here."""
        title = str(args.get("title") or "").strip()[:120]
        if not title:
            return self._tool_result("a card needs a title", is_error=True)
        tool = str(args.get("tool") or "")
        if tool not in ("", "claude", "codex"):
            tool = ""
        reply = self.call_daemon({
            "type": "board_card_request",
            # The port is how the daemon resolves *who is asking*. It is a claim
            # like everything else on that socket, which is why the daemon
            # refuses a request it cannot attribute to a live session rather
            # than filing an anonymous card.
            "port": self.port,
            "pid": os.getppid(),
            "title": title,
            "summary": str(args.get("summary") or "").strip()[:400],
            "notes": str(args.get("notes") or "")[:4000],
            "project": str(args.get("project") or "").strip()[:200],
            "tool": tool,
            "kind": str(args.get("kind") or "").strip(),
            "plan": str(args.get("plan") or "").strip()[:1024],
            # Clamped here as well as at the store. A tool call arrives as
            # whatever JSON the model emitted, so a string where an array was
            # asked for is a normal Tuesday — `board.parse_stages` accepts both
            # and this only has to keep the payload from being unbounded.
            "stages": [str(v).strip()[:48]
                       for v in (args.get("stages") or [])
                       if str(v).strip()][:12]
            if isinstance(args.get("stages"), list)
            else str(args.get("stages") or "")[:600],
            # Ids or exact titles, resolved by the daemon within the card's
            # own project. Clamped here for `stages`' reason: the payload
            # stays bounded whatever JSON the model emitted, and a lone string
            # is one reference rather than a refusal.
            "depends_on": [str(v).strip()[:DEPENDS_ON_MAX_CHARS]
                           for v in (args.get("depends_on") or [])
                           if isinstance(v, (str, int)) and str(v).strip()
                           ][:DEPENDS_ON_MAX_ITEMS]
            if isinstance(args.get("depends_on"), list)
            else ([str(args.get("depends_on")).strip()[:DEPENDS_ON_MAX_CHARS]]
                  if isinstance(args.get("depends_on"), str)
                  and str(args.get("depends_on")).strip() else []),
        })
        if reply is None:
            return self._tool_result(
                "Dark Army did not answer — the card was not added.", is_error=True)
        if not reply.get("ok"):
            return self._tool_result(
                str(reply.get("detail") or "Dark Army refused the card."),
                is_error=True)
        where = reply.get("project") or "this project"
        # The card is filed whatever became of its links; a list the daemon
        # could not resolve is said here in its own words, after the rest.
        links = str(reply.get("dependencies_detail") or "").strip()
        links_note = (f" Its links to other cards were not saved: {links}. "
                      "Tell the person which cards it should wait on."
                      if links else "")
        if reply.get("plan_attached"):
            return self._tool_result(
                f"Added to Dark Army's Backlog under {where} with the plan "
                "attached. Nothing runs until a human presses Start. Do not "
                f"call {tool_name('attach_plan', self.name)} for this plan."
                + links_note)
        if str(args.get("plan") or "").strip():
            return self._tool_result(
                f"Added to Dark Army's Prep column under {where}, but the plan "
                "was not attached: "
                + (str(reply.get("plan_detail") or "").strip()
                   or "Dark Army did not say why")
                + ". Tell the person which card and which plan, and do not "
                "file the card again." + links_note,
                is_error=True)
        return self._tool_result(
            f"Added to Dark Army's Prep column under {where}. Nothing runs until a "
            "human acts on it — it still needs refining into a plan. (A plan "
            "this session has already written goes on the card with this "
            "tool's `plan` argument, not in its notes.)" + links_note)

    def handle_stdio_message(self, msg) -> None:
        if not isinstance(msg, dict):
            # A JSON array parses and then dies on `.get`. It cannot carry an
            # id, but JSON-RPC still owes an answer — and the read loop owes
            # itself a next iteration.
            self.send({"jsonrpc": "2.0", "id": None,
                       "error": {"code": -32600,
                                 "message": "invalid request: not an object"}})
            return
        method, mid = msg.get("method"), msg.get("id")
        params = msg.get("params")
        if params is not None and not isinstance(params, dict):
            # Every branch below assumes an object (`params.get`, `call_tool`).
            # Refuse the one message rather than crash out of the loop.
            if mid is not None:
                self.send({"jsonrpc": "2.0", "id": mid,
                           "error": {"code": -32602,
                                     "message": "params must be an object"}})
            return

        if method == "initialize":
            version = (msg.get("params") or {}).get("protocolVersion", "2025-06-18")
            self.send({"jsonrpc": "2.0", "id": mid,
                       "result": initialize_result(version, self.host,
                                                   self.name, self.active)})
        elif method == "notifications/initialized":
            if self.active:
                self.heartbeat(once=True)
        elif (method == "notifications/claude/channel/permission_request"
              and self.host == HOST_CLAUDE and self.active):
            params = msg.get("params") or {}
            rid = str(params.get("request_id") or "")
            if rid:
                self.pending[rid] = params
                # Relayed whole. `description` and `input_preview` are the
                # model's words about its own tool call and are treated as
                # untrusted all the way to the surface that renders them.
                self.notify_daemon({
                    "type": "channel_permission_request",
                    "port": self.port, "pid": os.getppid(),
                    "request_id": rid,
                    "tool_name": str(params.get("tool_name") or ""),
                    "description": str(params.get("description") or ""),
                    "input_preview": str(params.get("input_preview") or ""),
                })
        elif method == "tools/list":
            tools = tools_for_host(self.host, self.name) if self.active else []
            self.send({"jsonrpc": "2.0", "id": mid,
                       "result": {"tools": tools}})
        elif method == "tools/call":
            self.send({"jsonrpc": "2.0", "id": mid,
                       "result": self.call_tool(msg.get("params") or {})})
        elif method in ("prompts/list", "resources/list"):
            self.send({"jsonrpc": "2.0", "id": mid,
                       "result": {method.split("/")[0]: []}})
        elif mid is not None:
            # A request we do not implement must be answered — an unanswered id
            # is a client waiting forever.
            self.send({"jsonrpc": "2.0", "id": mid,
                       "error": {"code": -32601,
                                 "message": f"method not found: {method}"}})

    def run(self) -> None:
        if self.active:
            self.listen()
            threading.Thread(target=self.serve_daemon, daemon=True).start()
            threading.Thread(target=self.heartbeat, daemon=True).start()
        # A passive copy opens no socket and runs no thread: it answers the
        # handshake and the two tool methods on stdio, and nothing else.
        for line in self._in:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            try:
                self.handle_stdio_message(msg)
            except Exception:
                # A handler bug on one message must not end the process —
                # exiting here silently severs the session's only channel.
                continue


def main(argv=None) -> None:
    host = host_from_argv(argv)
    name = name_from_argv(argv)
    if host == HOST_CLAUDE:
        # One `ps`, shared by the active decision and the heartbeat.
        parent = parent_command(os.getppid())
        active = is_active(parent, name, flagged=name_flag_present(argv))
    else:
        # Codex is registered once, under one name: always the active copy.
        parent, active = "", True
    ChannelServer(host=host, name=name, active=active, parent=parent).run()


if __name__ == "__main__":
    main()
