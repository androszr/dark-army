#!/usr/bin/env python3
"""Watch a real permission dialog while Dark Army's hook broker holds it.

Dark Army can carry a `PermissionRequest` from a session's own terminal to the panel
or the phone and take the answer there. The whole safety case for that rests on
one property of the *installed CLI*, not of Dark Army: that the terminal's own
dialog stays drawn and answerable while the hook is holding. Nobody had ever
watched it, so `hooks.py`'s `BROKER_WAIT_SECONDS` and `daemon.py`'s
`HOOK_PROMPT_HOLD_SECONDS` were pinned at 30 s — what is survivable if the
property is false, rather than what the feature needs.

This is the check that settles it, and it is deliberately re-runnable: the
answer belongs to the version of the CLI installed on this machine, so every
CLI bump can change it. Run it, read the `VERDICT:` line, and bank the output
in `docs/`.

    cd host && .venv/bin/python ../tools/permission_hold_livefire.py --self-test
    cd host && .venv/bin/python ../tools/permission_hold_livefire.py --endurance

How it watches. It stages a genuine ask by writing a board card whose prompt
asks the assistant to run one specific shell command, and dispatching it with
`own_terminal` — so the session runs on a pty **Dark Army owns**. That is the seam:
`GET /api/terminal/stream?session=…` on loopback answers 200 and holds a
framed stream open whose first `D` frame is the emulator's own `Screen.paint()`
of that terminal, and an `I` frame types straight into it. One socket both
reads what the terminal is drawing and presses a key into it. A VS Code
terminal cannot be painted at all, which is why an editor-hosted session aborts
the run rather than being quietly skipped.

Three legs plus a static one:

* **A** — at the first frame where the daemon publishes the ask, take a paint
  and classify it (`DIALOG` / `NO_DIALOG` / `UNREADABLE`); then press one
  non-committal key (cursor-down) and check the picture moved (`MOVED` /
  `FROZEN`). That proves the dialog is drawn *and* accepting keys mid-hold.
* **B** — who wins the race. B1 answers from Dark Army and expects the dialog to go
  and the session to resume; B2 answers at the terminal and expects the daemon
  to drop the row and refuse a later verdict for it in its own words.
* **C** — `--endurance`: answer neither side and record the wall-clock second
  at which the row leaves, plus the daemon log line that named the reason.
  That is the only thing that can tell *the hold expired* from *the CLI killed
  the hook at its own `timeout`*, and it is published as `HOLD-CEILING:`.

Stdlib only, its own process, loopback HTTP only, and it writes nothing except
the card it deletes in its `finally`. Reads and writes use `X-Bob-Token` and
send no `Origin` header: `Authorization: Bearer` silently 403s while reads keep
working, which would read as "the ask never arrived".
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import socket
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

# ---------------------------------------------------------------------------
# Pure parts. Everything below this banner and above the next one is testable
# without a daemon, a CLI or a socket, and `host/tests/test_permission_hold_
# livefire.py` covers it. It does not and must not stand in for the live run.
# ---------------------------------------------------------------------------

#: The CLI's own permission-dialog furniture, confirmed by `strings -n 6`
#: against the 2.1.263 binary at `~/.local/share/claude/versions/2.1.263`.
#: A paint counts as a dialog only when it carries the asking tool's name
#: *and* at least one of these, so an ordinary transcript that happens to
#: mention Bash is not mistaken for a dialog that is up.
DIALOG_MARKERS = (
    "Do you want to proceed?",
    "and tell Claude what to do differently",
    "Yes, and don't ask again",
)

#: Paint classes, leg A's first half.
DIALOG = "DIALOG"
NO_DIALOG = "NO_DIALOG"
UNREADABLE = "UNREADABLE"

#: Leg A's second half.
MOVED = "MOVED"
FROZEN = "FROZEN"
UNSEEN = "UNSEEN"

#: A leg B sub-run.
PASS = "PASS"
FAIL = "FAIL"
SKIPPED = "SKIPPED"

#: The three verdicts. Nothing else may be printed on the `VERDICT:` line.
CONCURRENT = "CONCURRENT"
AWAITED = "AWAITED"
INCONCLUSIVE = "INCONCLUSIVE"

#: Printed unconditionally under the verdict, whatever it says. The subagent
#: path is not covered by any result here and must never be read as covered:
#: the CLI awaits the hooks in the async-subagent spawn context, and the
#: daemon refuses to hold such an ask at all.
SCOPE_TEMPLATE = (
    "SCOPE: this verdict covers the ordinary PermissionRequest path on {version} "
    "only. An ask raised inside a subagent is NOT covered - the CLI awaits the "
    "hooks in the async-subagent spawn context, and the daemon refuses to hold "
    'one (daemon.py\'s "a subagent\'s dialog is not answerable from here").'
)

#: The lines the banked record must carry, each on its own line, so a later
#: reader — or a test — can find them without reading prose.
REQUIRED_RECORD_LINES = (
    "VERDICT:",
    "SCOPE:",
    "CLI-VERSION:",
    "HOLD-CEILING:",
    "NOTIFY-SCRIPT:",
)

_CSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_OSC_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_ESC_RE = re.compile(r"\x1b[@-Z\\-_]")


def strip_ansi(text: str) -> str:
    """The paint without its escape sequences, whitespace collapsed.

    A dialog's words are routinely split across a line wrap or a colour
    change, so marker matching happens on this rather than on the raw bytes.
    """
    text = _OSC_RE.sub("", text)
    text = _CSI_RE.sub("", text)
    text = _ESC_RE.sub("", text)
    return " ".join(text.split())


def classify_paint(paint, tool_name: str = "") -> str:
    """What the terminal was drawing when the daemon said the ask was open.

    `DIALOG` — the asking tool's name and at least one marker from
    `DIALOG_MARKERS`; the dialog is on screen while the hook holds.
    `NO_DIALOG` — a readable screen carrying none of it; the CLI awaited the
    hooks and the hold is a freeze with nothing to answer.
    `UNREADABLE` — nothing to judge: an empty paint, or an attach that was
    refused. Deliberately not folded into `NO_DIALOG`: the two failure shapes
    are opposite and conflating them would read a broken socket as proof the
    dialog was never drawn.
    """
    if paint is None:
        return UNREADABLE
    if isinstance(paint, (bytes, bytearray)):
        paint = bytes(paint).decode("utf-8", "replace")
    flat = strip_ansi(paint)
    if not flat.strip():
        return UNREADABLE
    tool = (tool_name or "").strip()
    if tool and tool.lower() not in flat.lower():
        return NO_DIALOG
    for marker in DIALOG_MARKERS:
        if marker.lower() in flat.lower():
            return DIALOG
    return NO_DIALOG


#: What the CLI prints under a tool call it ran because a *hook* answered
#: the dialog. Confirmed on 2.1.263. This is the strongest possible evidence
#: for leg B1: not "the row went away and something happened next", but the
#: CLI itself, on the session's own screen, naming which of the two answers
#: it used.
HOOK_DECISION_MARKERS = (
    "Allowed by PermissionRequest hook",
    "Denied by PermissionRequest hook",
)


def hook_credit_count(paint) -> int:
    """How many tool calls on this screen the CLI credits a hook with.

    A count rather than a flag, because leg B2's question is *whether this
    particular answer came from Dark Army or from the keyboard* — and by the time
    it runs, leg B1's own credit line is already on the screen above. A
    terminal answer that works leaves the count where it was while the
    command still runs.
    """
    if isinstance(paint, (bytes, bytearray)):
        paint = bytes(paint).decode("utf-8", "replace")
    flat = strip_ansi(paint or "").lower()
    return sum(flat.count(m.lower()) for m in HOOK_DECISION_MARKERS)


def bob_answer_landed(paint) -> bool:
    """Did the CLI say, in its own words, that a hook answered this dialog?"""
    return hook_credit_count(paint) > 0


#: Footer text meaning the session is in a permission mode that answers for
#: itself. Measured on this machine: under the settings file's
#: `defaultMode: auto` the CLI raised no `PermissionRequest` at all — not for
#: `rm -f`, not for an outbound `curl` — so what decides whether there is an
#: ask to watch is the *mode*, not the command. The check cycles the mode
#: until none of these is on screen: that is the CLI's default, the mode a
#: person who has changed nothing is in, and the one the broker exists for.
NON_DEFAULT_MODE_MARKERS = (
    "auto mode on",
    "accept edits on",
    "plan mode on",
    "bypass permissions on",
    "bypassing permissions",
)


def permission_mode_marker(paint) -> str:
    """Whichever of `NON_DEFAULT_MODE_MARKERS` the footer is showing, or ""
    for the CLI's default mode.

    Pure, so the cycling below has a stop condition that can be tested — a
    fixed number of Shift-Tab presses would silently stop meaning "default"
    the first time the CLI adds a mode to the cycle.
    """
    if isinstance(paint, (bytes, bytearray)):
        paint = bytes(paint).decode("utf-8", "replace")
    flat = strip_ansi(paint or "").lower()
    for marker in NON_DEFAULT_MODE_MARKERS:
        if marker in flat:
            return marker
    return ""


def verdict_for(paint_class: str, answerable: str, b1: str, b2: str) -> str:
    """The one line this whole check exists to print.

    `CONCURRENT` needs everything: the dialog was drawn while Dark Army held it, it
    still took a keystroke, and both halves of the race behaved. `AWAITED` is
    the one *informative* failure — a readable screen with no dialog on it
    means the CLI ran the hooks first. Everything else is `INCONCLUSIVE`,
    including an unreadable paint, a frozen dialog and a leg that could not
    stage its ask: none of those is evidence either way, and only a positive
    result may move a constant.
    """
    if paint_class == NO_DIALOG:
        return AWAITED
    if (paint_class == DIALOG and answerable == MOVED
            and b1 == PASS and b2 == PASS):
        return CONCURRENT
    return INCONCLUSIVE


def missing_record_lines(text: str) -> list:
    """Which of `REQUIRED_RECORD_LINES` the record does not carry at the
    start of a line of its own."""
    starts = set()
    for line in text.splitlines():
        for want in REQUIRED_RECORD_LINES:
            if line.startswith(want):
                starts.add(want)
    return [w for w in REQUIRED_RECORD_LINES if w not in starts]


# ---------------------------------------------------------------------------
# The live half.
# ---------------------------------------------------------------------------

HOST = "127.0.0.1"
PORT = 19874
STATE_DIR = Path.home() / ".dark-army"
TOKEN_PATH = STATE_DIR / "api-token"
NOTIFY_PATH = STATE_DIR / "dark-army-notify"
LOG_PATH = Path.home() / "Library" / "Logs" / "DarkArmy" / "dark-army.log"

KIND_DATA = b"D"
KIND_EXIT = b"X"
KIND_ERROR = b"E"
KIND_INPUT = b"I"
_HEAD = struct.Struct(">cI")

#: One non-committal key: it moves the dialog's selection and answers nothing,
#: which is exactly what leg A needs — proof the dialog takes keys without
#: spending the ask leg B still needs.
KEY_DOWN = b"\x1b[B"
KEY_UP = b"\x1b[A"
#: What a person presses to accept the highlighted option.
KEY_ACCEPT = b"\r"
#: Shift-Tab (CBT) — the CLI's own mode cycler, offered in its footer as
#: "shift+tab to cycle".
KEY_SHIFT_TAB = b"\x1b[Z"


class Refusal(Exception):
    """The run cannot be staged. Never a verdict — the caller reports
    INCONCLUSIVE and says why."""


class Log:
    """Everything the run saw, in order, printed as it happens and banked so
    the record can hold it verbatim."""

    def __init__(self) -> None:
        self.lines: list = []
        self._began = time.time()

    def __call__(self, text: str = "") -> None:
        stamp = "%7.1fs " % (time.time() - self._began)
        for i, line in enumerate(str(text).split("\n")):
            out = (stamp if i == 0 else " " * 9) + line
            self.lines.append(out.rstrip())
            print(out.rstrip(), flush=True)

    #: Where raw paints are banked, when `--save-paints` asked for it. The
    #: log's own copy is indented and right-stripped for reading; the tests'
    #: fixtures want the emulator's bytes exactly as they arrived.
    save_paints_dir = None

    def save_paint(self, name: str, payload: bytes) -> None:
        if not self.save_paints_dir:
            return
        target = Path(self.save_paints_dir)
        target.mkdir(parents=True, exist_ok=True)
        (target / name).write_bytes(payload or b"")
        self("banked raw paint %s (%d bytes)" % (name, len(payload or b"")))

    def block(self, title: str, body: str) -> None:
        self("--- %s ---" % title)
        for line in body.split("\n"):
            self.lines.append("    " + line.rstrip())
            print("    " + line.rstrip(), flush=True)
        self("--- end %s ---" % title)

    def text(self) -> str:
        return "\n".join(self.lines)


def read_token() -> str:
    try:
        return TOKEN_PATH.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise Refusal("no API token at %s (%s)" % (TOKEN_PATH, exc))


def get_state(token: str, timeout: float = 5.0) -> dict:
    req = urllib.request.Request("http://%s:%d/api/state" % (HOST, PORT))
    req.add_header("X-Bob-Token", token)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def action(token: str, payload: dict, timeout: float = 20.0) -> tuple:
    """`POST /api/action` the way the panel writes: `X-Bob-Token`, no
    `Origin`. Returns `(status, body dict)`; a 4xx/5xx is data, not an
    exception — a refusal in the daemon's own words is often the observation."""
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request("http://%s:%d/api/action" % (HOST, PORT),
                                 data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Bob-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw or "{}")
        except ValueError:
            return exc.code, {"raw": raw}


class Pane:
    """One attached terminal stream: the paint down, keystrokes up."""

    def __init__(self, token: str, session_id: str, timeout: float = 8.0):
        self.sock = socket.create_connection((HOST, PORT), timeout=timeout)
        self.sock.settimeout(timeout)
        path = "/api/terminal/stream?session=" + urllib.parse.quote(session_id)
        head = (
            "GET %s HTTP/1.1\r\nHost: %s:%d\r\nX-Bob-Token: %s\r\n"
            "Connection: keep-alive\r\n\r\n" % (path, HOST, PORT, token)
        )
        self.sock.sendall(head.encode("ascii"))
        self._buf = bytearray()
        self.status = self._read_head()

    def _read_head(self) -> int:
        while b"\r\n\r\n" not in self._buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise Refusal("the terminal stream closed before its head")
            self._buf += chunk
        head, rest = bytes(self._buf).split(b"\r\n\r\n", 1)
        self._buf = bytearray(rest)
        first = head.split(b"\r\n", 1)[0].decode("latin-1")
        try:
            return int(first.split()[1])
        except (IndexError, ValueError):
            raise Refusal("the terminal stream answered %r" % first)

    def frame(self, timeout: float = 8.0):
        """The next `(kind, payload)`, or None on timeout/close."""
        self.sock.settimeout(timeout)
        while True:
            if len(self._buf) >= _HEAD.size:
                kind, length = _HEAD.unpack_from(self._buf, 0)
                if len(self._buf) >= _HEAD.size + length:
                    payload = bytes(self._buf[_HEAD.size:_HEAD.size + length])
                    del self._buf[:_HEAD.size + length]
                    return kind, payload
            try:
                chunk = self.sock.recv(65536)
            except (socket.timeout, OSError):
                return None
            if not chunk:
                return None
            self._buf += chunk

    def paint(self, timeout: float = 8.0) -> bytes:
        """The first `D` frame: `Screen.paint()`, the emulator's own drawing
        of that terminal as it stands."""
        while True:
            got = self.frame(timeout)
            if got is None:
                return b""
            kind, payload = got
            if kind == KIND_DATA:
                return payload
            if kind in (KIND_EXIT, KIND_ERROR):
                return b""

    def press(self, data: bytes) -> None:
        self.sock.sendall(_HEAD.pack(KIND_INPUT, len(data)) + data)

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


def fresh_paint(token: str, session_id: str) -> tuple:
    """A brand-new attach, its paint, and the pane left open for a keypress.
    Returns `(pane, paint)`; `pane` is None where the attach was refused."""
    try:
        pane = Pane(token, session_id)
    except (OSError, Refusal):
        return None, b""
    if pane.status != 200:
        status = pane.status
        pane.close()
        raise Refusal("the terminal stream answered %d - the session is not "
                      "on a terminal Dark Army owns" % status)
    return pane, pane.paint()


def resolve_claude() -> tuple:
    """`(path, version)` of the CLI this machine actually runs, or a refusal.

    Resolved the way a person would — `claude --version` — and then located,
    because the version string is what `SCOPE:` names and the binary is what
    the static half greps.
    """
    exe = None
    for candidate in ("claude",):
        found = _which(candidate)
        if found:
            exe = found
            break
    if not exe:
        raise Refusal("no `claude` on PATH")
    try:
        out = subprocess.run([exe, "--version"], capture_output=True,
                             text=True, timeout=30).stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        raise Refusal("`claude --version` failed: %s" % exc)
    match = re.search(r"(\d+\.\d+\.\d+)", out)
    if not match:
        raise Refusal("`claude --version` said %r" % out)
    version = match.group(1)
    versioned = Path.home() / ".local/share/claude/versions" / version
    binary = str(versioned) if versioned.exists() else exe
    return binary, version


def _which(name: str):
    for part in (os.environ.get("PATH") or "").split(os.pathsep):
        cand = Path(part) / name
        if cand.is_file() and os.access(str(cand), os.X_OK):
            return str(cand)
    return None


def notify_script_stamp() -> str:
    """The installed handler's mtime and digest.

    The daemon may be running from `/Applications/Dark Army.app` while the
    checkout is edited, so a run against yesterday's handler has to be visible
    in the record rather than inferred from the source tree.
    """
    try:
        raw = NOTIFY_PATH.read_bytes()
    except OSError as exc:
        return "%s missing (%s)" % (NOTIFY_PATH, exc)
    mtime = time.strftime("%Y-%m-%dT%H:%M:%S",
                          time.localtime(NOTIFY_PATH.stat().st_mtime))
    return "%s mtime=%s sha256=%s bytes=%d" % (
        NOTIFY_PATH, mtime, hashlib.sha256(raw).hexdigest()[:16], len(raw))


def broker_wait_in_installed_script() -> str:
    """What the *installed* copy's own deadline is — the real patience, since
    the daemon's `hold` reply is a boolean to that script and never a
    duration."""
    try:
        text = NOTIFY_PATH.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "unknown"
    match = re.search(r"BROKER_WAIT_SECONDS\s*=\s*([0-9.]+)", text)
    return match.group(1) if match else "unknown"


def count_await_flag(binary: str) -> str:
    """`strings -n 6` hits for `awaitAutomatedChecksBeforeDialog` — the static
    half of the mode discrimination. A count, never a verdict: the flag being
    present says the mode exists, not that this path took it."""
    try:
        out = subprocess.run(["strings", "-n", "6", binary],
                             capture_output=True, timeout=600)
    except (OSError, subprocess.SubprocessError) as exc:
        return "unavailable (%s)" % exc
    if out.returncode != 0:
        return "unavailable (strings exited %d)" % out.returncode
    return str(out.stdout.count(b"awaitAutomatedChecksBeforeDialog"))


def tail_log(since: float, needle: str, limit: int = 6) -> list:
    """Daemon log lines carrying `needle`, written after `since`. Best
    effort: no log is a blank, never a failure."""
    try:
        raw = LOG_PATH.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    stamp = time.strftime("%Y-%m-%d", time.localtime(since))
    hits = [ln for ln in raw.splitlines()
            if needle in ln and ln.startswith(stamp)]
    return hits[-limit:]


# ---------------------------------------------------------------------------
# Staging.
# ---------------------------------------------------------------------------

#: Three asks in one session: leg A observes and prods the first, B1 answers
#: it from Dark Army, B2 answers the second at the terminal, and leg C lets the
#: third expire.
#:
#: The command is `rm -f` on a file this script made under /tmp: destructive
#: enough that the CLI's default permission mode never waves it through, and
#: harmless enough that whichever side of the race wins costs nothing. Three
#: separate files because a plain "yes" is an allow *once*.
#:
#: What decides whether an ask happens at all is the session's **permission
#: mode**, not the command. Measured on this machine: under the settings
#: file's `defaultMode: auto`, both `rm -f /tmp/…` and an outbound `curl` ran
#: with no dialog and no `PermissionRequest` at all. So the check does not
#: guess at a command auto mode escalates — it puts the session into the
#: CLI's default mode first (`set_default_permission_mode`), through the same
#: terminal seam it reads the screen with, and says in the log which mode it
#: landed in.
#: The card's own prompt does nothing on purpose. A dispatched session starts
#: working the instant its terminal opens, which is before anything can read
#: or change its permission mode — so the card asks for a word, and the three
#: commands are typed in afterwards, at the terminal, in the mode the check
#: chose.
WARMUP_PROMPT = (
    "Reply with the single word ready. Do nothing else at all: no commands, "
    "no reads, no searches, no edits."
)

WORK_PROMPT = (
    "Run these three shell commands one at a time in this exact order, "
    "waiting for each to finish before the next, and run nothing else at "
    "all: first rm -f {a} then rm -f {b} then rm -f {c}. When all three "
    "have run reply with the single word done."
)



class Litter:
    """What this run has created so far, told to `main`'s `finally` the
    moment each thing exists rather than when `stage_card` returns.

    Staging is five calls long — create, dispatch, wait for the session, wait
    for it to go quiet, set the permission mode, type the work prompt — and
    four of them can raise. Binding the ids from the *return value* meant that
    a failure anywhere after `board_create` left `clean_up` with two empty
    strings: a live `claude`, a pty Dark Army owns and a board card, all left behind
    in silence. The plan's own risk line asked this check to print what it
    could not clean up, and it cannot print what it was never told.
    """

    def __init__(self) -> None:
        self.card_id = ""
        self.session_id = ""


def stage_card(token: str, root: str, log: Log, litter: "Litter") -> tuple:
    """A card in an enrolled root, dispatched onto a terminal Dark Army owns.

    Returns `(card_id, session_id)` and stamps each onto `litter` as soon as
    it is known, so the caller's `finally` can clear up after a failure part
    way through. `own_terminal` is required, not preferred: a VS Code terminal
    cannot be painted, so an editor-hosted session is a named refusal rather
    than a silent skip.
    """
    tag = uuid.uuid4().hex[:8]
    paths = ["/tmp/bob-livefire-%s-%d" % (tag, n) for n in (1, 2, 3)]
    for path in paths:
        Path(path).write_text("bob permission-hold live-fire probe\n")
    status, body = action(token, {
        "action": "board_create",
        "title": "permission-hold live-fire %s" % tag,
        "summary": "Live-fire check for the PermissionRequest broker's hold.",
        "prompt": WARMUP_PROMPT,
        "tool": "claude",
        "root": root,
    })
    card_id = str((body or {}).get("card_id") or "")
    if status != 200 or not card_id:
        raise Refusal("board_create answered %d: %s" % (status, body))
    litter.card_id = card_id
    log("staged card %s in %s" % (card_id, root))
    status, body = action(token, {
        "action": "board_dispatch",
        "card_id": card_id,
        "skip_plan_gate": "1",
        "own_terminal": "1",
    })
    if status != 200:
        raise Refusal("board_dispatch answered %d: %s" % (status, body))
    log("dispatched: %s" % (body or {}).get("detail", ""))
    session_id = wait_for_session(token, card_id, log)
    litter.session_id = session_id
    wait_for_quiet(token, session_id, log)
    set_default_permission_mode(token, session_id, log)
    type_line(token, session_id,
              WORK_PROMPT.format(a=paths[0], b=paths[1], c=paths[2]), log)
    return card_id, session_id


def wait_for_quiet(token: str, session_id: str, log: Log,
                   deadline: float = 180.0) -> None:
    """Until the warm-up turn is over and the input line is the session's
    own again. Typing into a working session queues the text behind the turn,
    and the mode cycler is a keystroke like any other."""
    end = time.time() + deadline
    while time.time() < end:
        state = get_state(token)
        row = find_row(state, session_id)
        if row is not None and row.get("state") not in ("working", "thinking",
                                                        "registered", None):
            log("session is %s; the input line is free" % row.get("state"))
            return
        time.sleep(1.0)
    raise Refusal("the session never finished its warm-up turn")


def set_default_permission_mode(token: str, session_id: str, log: Log,
                                presses: int = 6) -> None:
    """Cycle the session into the CLI's default permission mode.

    Not a nicety. Under this machine's `defaultMode: auto` the CLI ran both
    `rm -f` and an outbound `curl` with no dialog and raised no
    `PermissionRequest` at all — there was nothing for the broker to hold and
    nothing for this check to watch. The mode is what decides that, so the
    check sets it rather than guessing at a command that would beat it, and
    logs the footer it landed on so the record says which mode the
    observation was made in.
    """
    pane, paint = fresh_paint(token, session_id)
    if pane is None:
        raise Refusal("could not attach to the session's terminal")
    marker = permission_mode_marker(paint)
    log("permission mode at start: %s" % (marker or "default"))
    seen = [marker]
    for _ in range(presses):
        if not marker:
            break
        pane.press(KEY_SHIFT_TAB)
        time.sleep(0.6)
        paint = _repaint(token, session_id)
        marker = permission_mode_marker(paint)
        seen.append(marker)
        log("permission mode after shift+tab: %s" % (marker or "default"))
    pane.close()
    if marker:
        raise Refusal("could not reach the CLI's default permission mode; "
                      "the footer cycled through %s"
                      % ", ".join(m or "default" for m in seen))


def type_line(token: str, session_id: str, text: str, log: Log) -> None:
    """Type one line at the session's own input and press Enter.

    Through the same `I` frame the panel's pane uses: the desk types straight
    through, so this is a person's keystrokes as far as the CLI is concerned.
    """
    pane, _ = fresh_paint(token, session_id)
    if pane is None:
        raise Refusal("could not attach to the session's terminal to type")
    pane.press(text.encode("utf-8"))
    time.sleep(0.6)
    pane.press(KEY_ACCEPT)
    pane.close()
    log("typed the work prompt at the terminal")


def wait_for_session(token: str, card_id: str, log: Log,
                     deadline: float = 180.0) -> str:
    """Until the card is bound to a session *and* that session is on a
    terminal Dark Army owns."""
    end = time.time() + deadline
    seen = ""
    while time.time() < end:
        state = get_state(token)
        for card in (state.get("board") or {}).get("cards") or []:
            if card.get("id") != card_id:
                continue
            sid = str(card.get("session_id") or "")
            if sid and sid != seen:
                seen = sid
                log("card bound to session %s" % sid[:12])
            if sid:
                row = find_row(state, sid)
                if row is not None and row.get("own_terminal"):
                    log("session %s is on a terminal Dark Army owns" % sid[:12])
                    return sid
            if card.get("dispatch_error"):
                raise Refusal("the card says: %s" % card["dispatch_error"])
        time.sleep(0.5)
    if seen:
        raise Refusal("session %s never appeared on a terminal Dark Army owns - an "
                      "editor-hosted session cannot be painted" % seen[:12])
    raise Refusal("no session bound to the card within %ds" % deadline)


def find_row(state: dict, session_id: str):
    agents = state.get("agents") or {}
    for bucket in ("running", "waiting", "sleeping", "abandoned", "finished"):
        for row in agents.get(bucket) or []:
            if row.get("session_id") == session_id:
                return row
    return None


def wait_for_ask(token: str, session_id: str, log: Log,
                 deadline: float = 300.0, interval: float = 0.25,
                 watch=None, exclude=()):
    """The daemon's own `permissions` list is the single source of truth for
    "is the ask open"; nothing here re-derives it. Returns the row.

    `watch(state)` is called on every pass. It exists so a second observation
    can ride this loop rather than run before it: an ask is open for the hold
    plus the poll lapse and no longer, so anything that polls for tens of
    seconds *between* two asks misses the next one entirely. That is not a
    hypothetical — it is what the first successful run of this check did.
    """
    end = time.time() + deadline
    while time.time() < end:
        state = get_state(token)
        if watch is not None:
            watch(state)
        for row in state.get("permissions") or []:
            if row.get("session_id") != session_id:
                continue
            if row.get("request_id") in set(exclude):
                # An answered row can outlive its answer (see `leg_b2`), so
                # "a row for this session" is not "the next ask". Naming the
                # ones already spent is what stops a later leg re-observing
                # an earlier leg's dialog.
                continue
            return row
        time.sleep(interval)
    raise Refusal("no permission ask arrived for %s within %ds - the CLI may "
                  "have run the command without asking"
                  % (session_id[:12], deadline))


def ask_is_open(token: str, request_id: str) -> bool:
    state = get_state(token)
    return any(r.get("request_id") == request_id
               for r in state.get("permissions") or [])


def wait_for_ask_to_close(token: str, request_id: str, deadline: float,
                          interval: float = 0.25) -> float:
    """Seconds until the row left `permissions`, or -1."""
    began = time.time()
    end = began + deadline
    while time.time() < end:
        if not ask_is_open(token, request_id):
            return time.time() - began
        time.sleep(interval)
    return -1.0


# ---------------------------------------------------------------------------
# The legs.
# ---------------------------------------------------------------------------

def leg_a(token: str, session_id: str, log: Log) -> tuple:
    """`(paint_class, answerable, row)` — the concurrency observation.

    The paint is taken from the same polling pass that first saw the row, and
    an attach that lands after the row closed is `UNREADABLE`, never
    `NO_DIALOG`: the two failure shapes are opposite.
    """
    log("leg A: waiting for the daemon to publish an ask")
    row = wait_for_ask(token, session_id, log)
    request_id = str(row.get("request_id") or "")
    tool_name = str(row.get("tool_name") or "")
    log("leg A: ask %s open for %s (%s)"
        % (request_id, tool_name, row.get("description", "")))
    pane, paint = fresh_paint(token, session_id)
    still_open = ask_is_open(token, request_id)
    log.block("PAINT 1 (while the hook holds)",
              paint.decode("utf-8", "replace") if paint else "<empty>")
    log.save_paint("dialog-paint.txt", paint)
    if not still_open and not paint:
        if pane:
            pane.close()
        return UNREADABLE, UNSEEN, row
    paint_class = classify_paint(paint, tool_name)
    if not still_open and paint_class == NO_DIALOG:
        # The row closed under us; an empty-of-dialog screen taken after the
        # ask ended proves nothing at all.
        paint_class = UNREADABLE
    log("leg A: paint classified %s" % paint_class)
    answerable = UNSEEN
    if pane is not None and paint_class == DIALOG:
        pane.press(KEY_DOWN)
        time.sleep(0.4)
        second = _repaint(token, session_id)
        log.block("PAINT 2 (after one cursor-down, mid-hold)",
                  second.decode("utf-8", "replace") if second else "<empty>")
        log.save_paint("dialog-paint-selection-moved.txt", second)
        answerable = MOVED if _differs(paint, second) else FROZEN
        log("leg A: the dialog is %s" % answerable)
        pane.press(KEY_UP)
        time.sleep(0.3)
    if pane is not None:
        pane.close()
    return paint_class, answerable, row


def _repaint(token: str, session_id: str) -> bytes:
    pane, paint = fresh_paint(token, session_id)
    if pane is not None:
        pane.close()
    return paint


def _differs(first: bytes, second: bytes) -> bool:
    """Did the picture move? Compared on the flattened text, so a repaint
    that only re-emitted the same cells with different escapes does not read
    as a moved selection."""
    if not second:
        return False
    a = strip_ansi(first.decode("utf-8", "replace"))
    b = strip_ansi(second.decode("utf-8", "replace"))
    return a != b


def leg_b1(token: str, session_id: str, row: dict, log: Log) -> str:
    """Answer from Dark Army. PASS when the dialog goes from the terminal *and* the
    CLI says a hook is what answered it.

    The plan's rule was "the dialog is gone and the session resumes (state
    `working` on a later `last_event`)". The CLI's own acknowledgement
    replaced the second half for two reasons, both found by running this.
    It is **stronger**: `Allowed by PermissionRequest hook`, printed by the
    CLI under the tool call, names *which* of the two concurrent answers was
    used, where a state flip only says something happened next. And polling
    the daemon for that flip took tens of seconds, during which the session's
    *next* ask opened and expired unseen — an ask lives for the hold plus the
    poll lapse and no longer, so the wait for it starts immediately now and
    the resumption is observed from inside that loop.
    """
    request_id = str(row.get("request_id") or "")
    log("leg B1: answering `allow` from Dark Army for %s" % request_id)
    status, body = action(token, {
        "action": "permission_verdict",
        "request_id": request_id,
        "behavior": "allow",
    })
    log("leg B1: /api/action answered %d %s" % (status, body))
    if status != 200 or not (body or {}).get("ok", True):
        return FAIL
    took = wait_for_ask_to_close(token, request_id, 40.0)
    if took < 0:
        log("leg B1: the row never left `permissions`")
        return FAIL
    log("leg B1: the row left `permissions` after %.1fs" % took)
    # The verdict reaches the CLI through the hook's *next* poll, so the
    # screen is a second or so behind the daemon's row. Taking one paint the
    # instant the row closed read the acknowledgement as absent on a run
    # where it appeared 1.3s later; this waits for it, briefly, and logs the
    # first paint either way.
    paint, acked, waited = _await_hook_credit(token, session_id)
    log.block("PAINT 3 (after Dark Army's allow)",
              paint.decode("utf-8", "replace") if paint else "<empty>")
    log.save_paint("plain-paint.txt", paint)
    gone = classify_paint(paint, str(row.get("tool_name") or "")) != DIALOG
    log("leg B1: dialog gone from the screen: %s" % gone)
    log("leg B1: waited %.1fs for the CLI to say who answered" % waited)
    log("leg B1: the CLI credits a hook with the answer: %s" % acked)
    return PASS if (gone and acked) else FAIL


def _await_hook_credit(token: str, session_id: str,
                       deadline: float = 12.0) -> tuple:
    """`(first paint, credited, seconds waited)`.

    The first paint is what goes in the log — it is the screen as it was the
    instant the daemon's row closed, which is what "the dialog is gone" is
    judged on. The credit line arrives a beat later, when the hook's next
    poll carries the verdict back into the CLI, so it is waited for
    separately and briefly.
    """
    began = time.time()
    first = _repaint(token, session_id)
    if bob_answer_landed(first):
        return first, True, 0.0
    while time.time() - began < deadline:
        time.sleep(0.5)
        if bob_answer_landed(_repaint(token, session_id)):
            return first, True, time.time() - began
    return first, False, time.time() - began


def _pane_showing_dialog(token: str, session_id: str, tool_name: str,
                         log: Log, deadline: float = 15.0) -> tuple:
    """An open pane whose own paint shows this ask's dialog, plus that paint.
    `(None, b"")` where it never appeared."""
    began = time.time()
    while time.time() - began < deadline:
        pane, paint = fresh_paint(token, session_id)
        if pane is not None and classify_paint(paint, tool_name) == DIALOG:
            log("leg B2: the dialog is drawn after %.1fs; pressing accept at "
                "the terminal" % (time.time() - began))
            return pane, paint
        if pane is not None:
            pane.close()
        time.sleep(0.5)
    return None, b""


class ResumeWatch:
    """Did the session go back to work on an event later than the ask?

    Rides `wait_for_ask`'s own polling loop rather than running before it,
    because the window between two asks is where the next one lives.
    """

    def __init__(self, session_id: str, asked_at: float) -> None:
        self.session_id = session_id
        self.asked_at = asked_at
        self.resumed = False

    def __call__(self, state: dict) -> None:
        if self.resumed:
            return
        row = find_row(state, self.session_id)
        if row is None:
            return
        if (float(row.get("last_event") or 0) > self.asked_at
                and row.get("state") in ("working", "thinking")):
            self.resumed = True


def leg_b2(token: str, session_id: str, log: Log,
           poll_lapse: float = 15.0, resume=None, seen=()) -> tuple:
    """Answer at the terminal, and see whose answer the CLI used.

    PASS needs three things: the dialog leaves the screen and the command
    runs, the CLI does **not** credit a hook with that call (the count of
    credit lines is where leg B1 left it), and a later verdict from Dark Army for
    that same request id is refused in the daemon's own words.

    The plan expected the daemon's row to go inside
    `HOOK_PROMPT_POLL_LAPSE_SECONDS + 5`, on the reasoning that the CLI
    SIGTERMs the hook when the user answers. **That is not a safe assumption
    on 2.1.263**: an earlier run was seen keeping `last_poll_at` moving after
    a desk answer, which leaves the row standing until something else drops
    it. The banked run in
    `docs/2026-09-07-permission-hold-verification.md` did *not* reproduce it —
    the row left `permissions` 1.3s after the keypress — so treat that as the
    prior and this window as defensive: it is the installed script's whole
    deadline plus the lapse plus a little, and the seconds it actually took go
    in the log, which is the number a later reader should compare against. The
    wide window also means a session can briefly carry **two** open rows: an
    answered one still holding, and the next ask behind it.
    """
    log("leg B2: waiting for the next ask")
    try:
        row = wait_for_ask(token, session_id, log, deadline=300.0,
                           watch=resume, exclude=seen)
    except Refusal as exc:
        log("leg B2: %s" % exc)
        return SKIPPED, ""
    request_id = str(row.get("request_id") or "")
    tool_name = str(row.get("tool_name") or "")
    # The daemon publishes the ask when the *hook* fires; the CLI draws the
    # dialog concurrently, which is the whole point of this check and also
    # means the two are not simultaneous. Pressing Enter before the dialog is
    # drawn sends it to the input line, which submits an empty message and
    # answers nothing - observed once, as a leg B2 that timed out while the
    # dialog sat untouched. So wait for the picture, then press.
    pane, before = _pane_showing_dialog(token, session_id, tool_name, log)
    if pane is None:
        log("leg B2: the dialog was never drawn on the terminal")
        return SKIPPED, request_id
    credits_before = hook_credit_count(before)
    pane.press(KEY_ACCEPT)
    pane.close()
    landed, after, waited = _await_dialog_dismissed(token, session_id,
                                                    tool_name)
    log.block("PAINT 4 (after the terminal answered)",
              after.decode("utf-8", "replace") if after else "<empty>")
    log.save_paint("plain-paint.txt", after)
    credits_after = hook_credit_count(after)
    log("leg B2: the dialog left the screen after %.1fs: %s"
        % (waited, landed))
    log("leg B2: hook credit lines on screen before=%d after=%d (a terminal "
        "answer must not add one)" % (credits_before, credits_after))
    wait_seconds = _installed_wait_seconds()
    took = wait_for_ask_to_close(token, request_id,
                                 wait_seconds + poll_lapse + 10.0)
    if took < 0:
        log("leg B2: the row was still open %.0fs after the terminal answered"
            % (wait_seconds + poll_lapse + 10.0))
    else:
        log("leg B2: the daemon's row left `permissions` %.1fs after the "
            "keypress (the installed script's own deadline is %.0fs and the "
            "poll lapse is %.0fs)" % (took, wait_seconds, poll_lapse))
    status, body = action(token, {
        "action": "permission_verdict",
        "request_id": request_id,
        "behavior": "allow",
    })
    log("leg B2: a late verdict for that id answered %d %s" % (status, body))
    refused = status != 200 or not (body or {}).get("ok", True)
    ok = landed and credits_after == credits_before and took >= 0 and refused
    return (PASS if ok else FAIL), request_id


def _installed_wait_seconds() -> float:
    raw = broker_wait_in_installed_script()
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 30.0


def _await_dialog_dismissed(token: str, session_id: str, tool_name: str,
                            deadline: float = 25.0) -> tuple:
    """`(dismissed, last paint, seconds waited)` - the screen after the
    terminal's own answer."""
    began = time.time()
    paint = b""
    while time.time() - began < deadline:
        paint = _repaint(token, session_id)
        if classify_paint(paint, tool_name) != DIALOG:
            return True, paint, time.time() - began
        time.sleep(0.5)
    return False, paint, time.time() - began


def leg_c(token: str, session_id: str, log: Log, max_seconds: float,
          seen=()) -> str:
    """`--endurance`: answer neither side, and record when the row goes.

    This is the only thing that can tell *the hold expired* from *the CLI
    killed the hook at its own `timeout`*: seen from the daemon, the two are
    identical except for the second at which they happen.
    """
    log("leg C: waiting for one more ask to let expire")
    try:
        row = wait_for_ask(token, session_id, log, deadline=300.0,
                           exclude=seen)
    except Refusal as exc:
        log("leg C: %s" % exc)
        return "not measured (%s)" % exc
    request_id = str(row.get("request_id") or "")
    began = time.time()
    log("leg C: ask %s open; sampling every 15s for up to %.0fs"
        % (request_id, max_seconds))
    while time.time() - began < max_seconds:
        if not ask_is_open(token, request_id):
            took = time.time() - began
            log("leg C: the row left `permissions` after %.0fs" % took)
            time.sleep(1.0)
            for line in tail_log(began, "Dropping permission prompt"):
                log("leg C: daemon log: %s" % line.strip())
            wait = broker_wait_in_installed_script()
            return ("%.0fs (installed script BROKER_WAIT_SECONDS=%s)"
                    % (took, wait))
        time.sleep(15.0)
    log("leg C: the row was still open after %.0fs" % max_seconds)
    return "still open after %.0fs" % max_seconds


# ---------------------------------------------------------------------------
# Teardown, self-test, main.
# ---------------------------------------------------------------------------

def clean_up(token: str, card_id: str, session_id: str, log: Log) -> None:
    """Delete the card and close the terminal, and say what it could not
    clean up rather than leaving it to be discovered."""
    if session_id:
        # `close_session_terminal` refuses while a permission prompt is up,
        # and this check's own last leg is a prompt nobody answered. Deny it
        # first: the command never runs, the session is unblocked, and the
        # terminal becomes closable.
        try:
            for row in get_state(token).get("permissions") or []:
                if row.get("session_id") != session_id:
                    continue
                status, body = action(token, {
                    "action": "permission_verdict",
                    "request_id": row.get("request_id", ""),
                    "behavior": "deny"})
                log("cleanup: denied leftover ask %s -> %d %s"
                    % (row.get("request_id", ""), status, body))
                time.sleep(3.0)
        except (OSError, urllib.error.URLError, ValueError) as exc:
            log("cleanup: could not clear a leftover ask (%s)" % exc)
        try:
            # No `by_person`. That flag is not what closes a terminal — it is
            # the card-finish guard, and setting it walked this throwaway card
            # into Done, where `_after_board_write`'s Done leg banks a
            # `submitted` row in the *retained* outcome ledger. The
            # `board_delete` below removes the card; the ledger row survives
            # it, so every run of this check was adding a phantom submission
            # to this project's rework-rate denominator.
            status, body = action(token, {"action": "close_terminal",
                                          "session_id": session_id})
            log("cleanup: close_terminal answered %d %s" % (status, body))
        # ValueError too: `action()` raises it out of `json.loads` on a
        # non-JSON 200 body, and an escape from here skips `board_delete` and
        # kills the run before it prints CLI-VERSION:/HOLD-CEILING:/VERDICT:.
        except (OSError, urllib.error.URLError, ValueError) as exc:
            log("cleanup: COULD NOT close the terminal for %s (%s)"
                % (session_id[:12], exc))
    if card_id:
        try:
            status, body = action(token, {"action": "board_delete",
                                          "card_id": card_id})
            log("cleanup: board_delete answered %d %s" % (status, body))
            if status != 200:
                log("cleanup: COULD NOT delete card %s - delete it by hand"
                    % card_id)
        except (OSError, urllib.error.URLError, ValueError) as exc:
            log("cleanup: COULD NOT delete card %s (%s)" % (card_id, exc))


DIALOG_SAMPLE = (
    "\x1b[38;5;250m\x1b[1mBash command\x1b[0m\n\n"
    "  rm -f /tmp/bob-livefire-probe\n\n"
    "\x1b[1mDo you want to proceed?\x1b[0m\n"
    "\x1b[7m 1. Yes\x1b[0m\n"
    "  2. Yes, and don't ask again for rm commands in this project\n"
    "  3. No, and tell Claude what to do differently \x1b[2m(esc)\x1b[0m\n"
)
PLAIN_SAMPLE = (
    "\x1b[2m> \x1b[0mRun these three shell commands, one at a time.\n\n"
    "\x1b[38;5;250m* Thinking\x1b[0m (esc to interrupt)\n"
)


def self_test() -> int:
    """The pure legs only: no daemon, no CLI, no socket. This is the check's
    own smoke test, not a stand-in for the live run."""
    failures = []
    ran = [0]

    def check(name, got, want):
        ran[0] += 1
        if got != want:
            failures.append("%s: got %r, wanted %r" % (name, got, want))

    check("dialog paint", classify_paint(DIALOG_SAMPLE, "Bash"), DIALOG)
    check("plain paint", classify_paint(PLAIN_SAMPLE, "Bash"), NO_DIALOG)
    check("empty paint", classify_paint(b"", "Bash"), UNREADABLE)
    check("blank paint", classify_paint("   \n  ", "Bash"), UNREADABLE)
    check("none paint", classify_paint(None, "Bash"), UNREADABLE)
    check("dialog for another tool",
          classify_paint(DIALOG_SAMPLE, "WebFetch"), NO_DIALOG)
    check("mode footer read", permission_mode_marker(
        "Context: 90% remaining\n\x1b[2m⏵⏵ auto mode on (shift+tab "
        "to cycle)\x1b[0m"), "auto mode on")
    check("default mode footer", permission_mode_marker(
        "Context: 90% remaining\n⏸ manual mode on"), "")
    check("hook credited", bob_answer_landed(
        "  ⎿  $ rm -f /tmp/x\n  ⎿  Allowed by PermissionRequest "
        "hook\n"), True)
    check("nobody credited", bob_answer_landed("  ⎿  $ rm -f /tmp/x\n"),
          False)
    check("credit lines counted", hook_credit_count(
        "⎿ Allowed by PermissionRequest hook\n⎿ $ x\n"
        "⎿ Denied by PermissionRequest hook\n"), 2)
    check("no credit lines counted", hook_credit_count("⎿ $ rm -f /tmp/x"), 0)
    check("concurrent", verdict_for(DIALOG, MOVED, PASS, PASS), CONCURRENT)
    check("frozen", verdict_for(DIALOG, FROZEN, PASS, PASS), INCONCLUSIVE)
    check("awaited", verdict_for(NO_DIALOG, UNSEEN, SKIPPED, SKIPPED), AWAITED)
    check("unreadable", verdict_for(UNREADABLE, UNSEEN, PASS, PASS),
          INCONCLUSIVE)
    check("half a race", verdict_for(DIALOG, MOVED, PASS, FAIL), INCONCLUSIVE)
    check("record lines", missing_record_lines(
        "\n".join(w + " x" for w in REQUIRED_RECORD_LINES)), [])
    check("every absent line named", missing_record_lines("nothing here"),
          list(REQUIRED_RECORD_LINES))
    for line in failures:
        print("SELF-TEST FAIL: " + line)
    print("self-test: %d checks, %d failed" % (ran[0], len(failures)))
    return 1 if failures else 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Watch a real permission dialog while Dark Army's hook broker "
                    "holds it.")
    parser.add_argument("--self-test", action="store_true",
                        help="run the pure legs only and exit")
    parser.add_argument("--root", default=str(Path(__file__).resolve().parent.parent),
                        help="an enrolled project root to stage the card in")
    parser.add_argument("--endurance", action="store_true",
                        help="also run leg C, which measures the real ceiling")
    parser.add_argument("--endurance-max", type=float, default=2100.0,
                        help="how long leg C is willing to wait, in seconds")
    parser.add_argument("--save-paints", default="",
                        help="bank each paint raw into this directory, for "
                             "checking in as a test fixture")
    args = parser.parse_args(argv)

    if args.self_test:
        return self_test()

    log = Log()
    log.save_paints_dir = args.save_paints or None
    paint_class, answerable = UNREADABLE, UNSEEN
    b1, b2 = SKIPPED, SKIPPED
    ceiling = "n/a"
    version = "unknown"
    session_id, token = "", ""
    # Not locals: `clean_up` reads these, and it has to know about a card the
    # run created a moment before it fell over. See `Litter`.
    litter = Litter()
    # Request ids this run has already spent. An answered row can outlive its
    # answer, so a later leg must not mistake it for the next ask.
    spent: list = []
    try:
        token = read_token()
        state = get_state(token)
        log("daemon: /api/state answered, %d permission rows open"
            % len(state.get("permissions") or []))
        binary, version = resolve_claude()
        log("CLI: %s (%s)" % (version, binary))
        log("NOTIFY-SCRIPT: %s" % notify_script_stamp())
        log("installed script BROKER_WAIT_SECONDS=%s"
            % broker_wait_in_installed_script())
        board = state.get("board") or {}
        if not board.get("dispatch_enabled"):
            raise Refusal("board dispatch is switched off")
        if not board.get("own_terminal_spawn_supported"):
            raise Refusal("this Mac cannot spawn a terminal Dark Army owns")
        _, session_id = stage_card(token, args.root, log, litter)
        paint_class, answerable, row = leg_a(token, session_id, log)
        spent.append(str(row.get("request_id") or ""))
        if paint_class == DIALOG and answerable == MOVED:
            b1 = leg_b1(token, session_id, row, log)
            resume = ResumeWatch(session_id, float(row.get("asked_at") or 0))
            b2, b2_request = leg_b2(token, session_id, log, resume=resume,
                                    seen=spent)
            spent.append(b2_request)
            log("leg B1: the daemon was seen to put the session back to "
                "`working` inside leg B2's own window: %s (corroboration "
                "only - the CLI's credit line above is the finding)"
                % resume.resumed)
        else:
            log("legs B skipped: leg A did not see an answerable dialog")
    except Refusal as exc:
        log("REFUSED: %s" % exc)
    except (OSError, urllib.error.URLError, ValueError) as exc:
        log("REFUSED: %s" % exc)
    finally:
        verdict = verdict_for(paint_class, answerable, b1, b2)
        if args.endurance and litter.session_id and verdict == CONCURRENT:
            try:
                ceiling = leg_c(token, litter.session_id, log,
                                args.endurance_max, seen=spent)
            # ValueError belongs here beside the other three: the legs decode
            # JSON off the daemon, and an exception escaping this block would
            # skip `clean_up` entirely — leaving exactly the litter the
            # `finally` exists to clear.
            except (Refusal, OSError, urllib.error.URLError, ValueError) as exc:
                ceiling = "not measured (%s)" % exc
        if token:
            clean_up(token, litter.card_id, litter.session_id, log)

    try:
        binary, _ = resolve_claude()
        hits = count_await_flag(binary)
    except Refusal:
        binary, hits = "unknown", "unavailable"
    log("static: `awaitAutomatedChecksBeforeDialog` appears %s time(s) in %s"
        % (hits, binary))
    log("legs: A=%s answerable=%s B1=%s B2=%s" % (paint_class, answerable, b1, b2))
    verdict = verdict_for(paint_class, answerable, b1, b2)
    if verdict != CONCURRENT:
        ceiling = "n/a"
    print()
    print("CLI-VERSION: %s" % version)
    print("NOTIFY-SCRIPT: %s" % notify_script_stamp())
    print("HOLD-CEILING: %s" % ceiling)
    print("VERDICT: %s" % verdict)
    print(SCOPE_TEMPLATE.format(version=version))
    return 0 if verdict == CONCURRENT else 1


if __name__ == "__main__":
    sys.exit(main())
