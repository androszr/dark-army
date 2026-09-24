"""Mission Control — the one standing chief-of-staff session.

It answers questions about the picture **and acts on them like any other
session Dark Army opens**: it files cards on the board through the `bob`
channel tools every session carries, spawns helpers with the `Agent` tool,
edits files and runs commands — each write raising the same permission
prompt a card session's would, answered from the panel or the phone. What
stays special is only the *shape*: one standing terminal, one fixed brief,
a constant opening prompt, and a plain reply that never lands on the Needs
you list (`daemon._mission_reply`). A permission prompt or an
`AskUserQuestion` from it is an ordinary `permission` / waiting event and
does land there, which is what makes acting from the Comm tab possible.

Pure module: stdlib plus `paths`. It knows the constants that name the
session, reads the brief off the checkout, and keeps the daemon's record of
which terminal Mission Control lives in. It spawns nothing, reads no pty
and holds no loop state; `daemon_board.BoardVerbsMixin.open_mission` is the
verb and `ptyhost` the host.

**The identity is the terminal, never a session id.** A quiet Mission
Control is evicted on wall-clock staleness like any other row and comes back
under the same id (or, after `/clear`, a new one in the same process). The
pty broker keeps the process across a Dark Army restart and re-adopts it by
its `handle`, so the handle is the one durable fact; `session_id` on the
record is the *last* id seen bound to that terminal, kept so a quiet row can
still be typed at and attached by the id the phone last drew.

**The record is forward-compatible both ways.** `load_record` keeps an
unknown key and defaults a missing one; an older daemon never reads the file
(it does not know it exists), and a corrupt file reads as `{}` — the cost of
a lost record is one duplicate Mission Control at most, which `open_mission`
also guards against by looking for a live terminal wearing `PTY_NAME`
whose bound session, when it has one, carries the `mission` origin stamp.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Optional

from . import paths

logger = logging.getLogger("dark-army.mission")

#: The person-facing label: the snapshot's `name` and every word a person
#: reads. **Never the pty's name** — see `PTY_NAME`.
NAME = "Mission Control"

#: The pty's `name`, what `spawn_local` hands the host and what the adopt
#: rung and End's identity guard look for. **Longer than 40 characters on
#: purpose**: a card's terminal is named `card["title"][:40]` (and a refine's
#: `"refine: " + title`, an ask's `"ask: " + title`, the same slice), so a
#: name a card could wear — "Mission Control" among them — would let a card
#: titled so be adopted as Mission Control and closed by End. A 44-character
#: name is one no slice of a title can produce. The host keeps 80.
PTY_NAME = "Mission Control — Dark Army's chief of staff"
assert len(PTY_NAME) > 40, "a card's terminal name is title[:40]; this must be longer"

#: The `claude --agent` name. An identifier, so it stays lowercase-hyphenated.
AGENT_NAME = "mission-control"

#: The agent's one-line description, what `--agents` carries beside the brief.
AGENT_DESCRIPTION = (
    "Dark Army's chief of staff: answers questions about what the agents "
    "are doing, where a card is, timelines and pitfalls, and acts when "
    "asked — files cards on the board, spawns helpers, edits files and "
    "runs commands like any other session, asking before each write."
)

#: Where the brief lives, relative to Dark Army's own checkout.
BRIEF_RELPATH = "docs/mission-control-brief.md"

#: A brief past this is a mistake, not a brief. 32 KiB is eight times the
#: shipped one and still well inside a single argv element.
MAX_BRIEF_BYTES = 32768

#: The loopback API door the brief reads the live picture from. This is
#: `api_server.API_PORT`'s default and is spelled as a literal here on
#: purpose: the child's environment carries `DARK_ARMY_HOOK_SOCKET` (the
#: **hook socket**, `paths.HOOK_SOCK_PATH`) or, from an older broker, the
#: bridge port `BOB_COMPANION_PORT` — neither answers HTTP at all — and
#: handing the API port down the same way would mean
#: widening `origin.ENV_KEY_RE` — the one key the pty broker's `start` op
#: accepts, kept to one so the RPC is never a general environment injector.
#: `test_mission_control.py` pins this against `api_server.py`'s default.
API_PORT = 19874
API_URL = f"http://127.0.0.1:{API_PORT}"

#: The one file the session ever writes: `/api/state/pretty` is hundreds
#: of kilobytes, so the brief has it saved here and read in windows. Fixed and
#: named so the read grant below can name it and nothing else outside the
#: checkout. `/tmp` is a symlink to `/private/tmp` on macOS and the CLI
#: matches a `Read` rule against the path the model typed, so both
#: spellings are granted.
STATE_SCRATCH = "/tmp/darkarmy-state.json"
STATE_SCRATCH_RESOLVED = "/private/tmp/darkarmy-state.json"

#: The one Bash rule: `curl -s` against the loopback API and nothing else.
#: The wildcard form is the one `claude --help` (2.1.278) shows
#: (`Bash(git *)`); the older prefix form `Bash(curl -s http://…/:*)` was
#: measured on the same CLI on 20 Sep 2026 and does **not** match a URL
#: (the colons inside it). Measured on that CLI with `--permission-prompts
#: none`: this rule allows `curl -s http://127.0.0.1:19874/api/state/pretty
#: -o /tmp/darkarmy-state.json` and `… /api/board -G --data-urlencode
#: card=<id>`, and denies `curl -s http://example.com/`, a quoted URL
#: (`curl -s 'http://…?card=x'`) and a pipe into `python3` — the CLI splits
#: a compound command and judges every part, so the `*` swallows no tail;
#: a pipe into a command on the CLI's own built-in safe list (`wc`, `head`)
#: runs without a prompt, which is why the brief forbids piping at all
#: rather than trusting the rule to catch it. The brief curls
#: `/api/state/pretty`, never `/api/state`: the plain form is one line of
#: ~100 KB, which the CLI's `Read` refuses whole (a token ceiling, not a
#: permission) and whose `Grep` hit is "[Omitted long matching line]"; the
#: indented form is a path route because this rule can carry no `?`.
CURL_RULE = f"Bash(curl -s {API_URL}/*)"


def allowed_tools(root) -> tuple:
    """What the session may do without asking, as `--allowed-tools` rules —
    one rule per argv element (`dispatch.mission_argv`), built per spawn
    from the resolved checkout. **A narrowing, never a widening**, and
    scoped to what the brief uses: `Read` inside Dark Army's own checkout
    (`card_prepare.argv`'s `Read(//<absolute path>)` idiom, the `//`
    prefix making the rule absolute rather than project-relative;
    `Read(//Users/…/**)` is the spelling the CLI itself writes into
    `settings.json`), `Read` of the one scratch file, and `curl -s` at the
    loopback API. Never a bare `Read` — that would pre-approve
    `~/.dark-army/api-token` and every other user-readable file — and
    never `Bash(curl:*)`, which is egress to any host. `Grep` and `Glob`
    stay bare: the CLI's own rule is that a `Read` path rule governs them
    as well, and neither prompts on its own. Everything else — `Edit`,
    `Write`, `git`, `sqlite3`, `python` — raises the ordinary permission
    dialog — the same one a card session raises — which the brief tells
    the session to reach for whenever a person asks it to act."""
    root = str(root or "").rstrip("/")
    return (
        f"Read(//{root.lstrip('/')}/**)",
        "Grep",
        "Glob",
        f"Read(//{STATE_SCRATCH.lstrip('/')})",
        f"Read(//{STATE_SCRATCH_RESOLVED.lstrip('/')})",
        CURL_RULE,
    )

#: The first user turn. A constant, so `dispatch.prompt_refusal` has nothing
#: to judge: no card, no person-typed text and no store field reaches the
#: command line.
OPENING_PROMPT = (
    "Introduce yourself as Mission Control in one line and wait. Answer "
    "a question by reading; act on an instruction like any other session "
    "would — file the card, spawn the helper, make the change — and let "
    "each write ask its permission."
)

#: `open_mission`'s reply detail while a live one is already there, and
#: what every door but loopback says on a fresh spawn: the handle is
#: loopback reply-only (the panel aims its terminal column by it before the
#: row is on the snapshot) and never crosses a phone door.
ALREADY_RUNNING = "already running"
STARTED = "started"

#: The record's known keys and their defaults. `load_record` fills every one
#: of these in and keeps whatever else the file says.
RECORD_DEFAULTS = {
    "handle": "",
    "root": "",
    "session_id": "",
    "opened_at": 0.0,
    #: Set by a successful End and cleared by the next open: the broker
    #: forgets a closed terminal, so without this the snapshot could only
    #: say `off`, never `ended`. A file written before these keys existed
    #: reads them as False / 0.0.
    "ended": False,
    "ended_at": 0.0,
    #: `brief_digest(brief)` of the brief the live terminal was spawned
    #: with. The brief rides `--agents` at spawn and nothing re-reads it —
    #: not `/clear`, not a daemon restart (the broker keeps the process) —
    #: so a brief edited on disk is invisible to a running Mission Control
    #: until it is spawned again. `open_mission` compares this against the
    #: file and replaces a terminal spawned on an older brief. A record
    #: written before the key existed reads `""`, which is *stale*: that
    #: daemon compared nothing, and this build's brief is by definition
    #: not the one it spawned.
    "brief_digest": "",
}


def brief_digest(text: str) -> str:
    """A short, stable fingerprint of a brief's text — what the record keeps
    beside the handle so `open_mission` can tell a terminal spawned on an
    older brief from one spawned on this one. Not a secret; sixteen hex
    characters of SHA-256 over the stripped text `read_brief` returns."""
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()[:16]


def brief_path(root) -> Path:
    """The brief's path inside `root`."""
    return Path(str(root)) / BRIEF_RELPATH


def read_brief(root) -> tuple[str, str]:
    """`(brief, refusal)`. The brief is `""` with the reason in words when
    the file is missing, empty or over `MAX_BRIEF_BYTES` — an oversize file
    is refused rather than truncated, because a truncated brief is a
    different brief."""
    path = brief_path(root)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return "", f"Mission Control's brief is missing ({BRIEF_RELPATH})"
    except OSError as exc:
        return "", f"Mission Control's brief could not be read: {exc}"
    if len(raw) > MAX_BRIEF_BYTES:
        return "", (f"Mission Control's brief is too long "
                    f"({len(raw)} bytes, at most {MAX_BRIEF_BYTES})")
    text = raw.decode("utf-8", "replace").strip()
    if not text:
        return "", f"Mission Control's brief is empty ({BRIEF_RELPATH})"
    return text, ""


def load_record(path) -> dict:
    """The record at `path`, every known key defaulted, unknown keys kept.
    A missing file is `{}`; a corrupt one is logged and `{}` — never a raise,
    because this runs in `BobDaemon.__init__`."""
    try:
        raw = Path(str(path)).read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError:
        logger.warning("could not read %s", path, exc_info=True)
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        logger.warning("ignoring corrupt %s", path)
        return {}
    if not isinstance(data, dict):
        logger.warning("ignoring %s: not a JSON object", path)
        return {}
    out = dict(data)
    for key, default in RECORD_DEFAULTS.items():
        value = out.get(key)
        if isinstance(default, bool):
            out[key] = bool(value) if isinstance(value, bool) else default
        elif isinstance(default, float):
            try:
                out[key] = float(value) if value is not None else default
            except (TypeError, ValueError):
                out[key] = default
        else:
            out[key] = str(value) if isinstance(value, str) else default
    return out


def save_record(path, record: dict) -> None:
    """Write `record` atomically at 0600. `OSError` is logged, never raised:
    a record that could not be written costs one duplicate Mission Control
    at the next restart, not a verb that failed."""
    path = Path(str(path))
    try:
        paths.ensure_state_dir()
        os.makedirs(path.parent, exist_ok=True)
        paths.atomic_write_json(path, dict(record or {}), mode=0o600, indent=2)
    except OSError:
        logger.warning("could not write %s", path, exc_info=True)


def record_alive(record: Optional[dict], term) -> bool:
    """Whether `term` — the terminal at the record's handle, or None — is
    Mission Control alive: a terminal exists, has not exited and still wears
    `PTY_NAME`."""
    if term is None or not record:
        return False
    if not str(record.get("handle") or ""):
        return False
    if getattr(term, "exited", True):
        return False
    return getattr(term, "name", "") == PTY_NAME
