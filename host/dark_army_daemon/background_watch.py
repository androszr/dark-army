"""Whether a session's background shell tasks are still running.

**A Stop hook is not a promise the session is waiting on you.** Claude Code's
`Bash` tool takes `run_in_background`: the command is detached, the tool
returns at once with *"Command running in background with ID: … Output is
being written to: …"*, and the harness **ends the turn** — the Stop hook
fires, the transcript goes quiet — then re-invokes the session by itself when
the command exits, delivering a `<task-notification>` as the next user
record. Nobody is asked anything in between.

`subagent_watch` is the same story for an async `Agent(...)`, and this module
is its sibling for the shell: the parent's own transcript says what it
launched, and the task's output file says whether it has come back.

Measured 20 Sep 2026 over this machine's 759 Claude Code transcripts: of
2,038 Stops, **399 (20%) ended with a background task still pending**, and
the 133 resumptions that could be matched to their notification took a
median of 116s and a p90 of 513s — every one of those a "Waiting for input"
Dark Army raised about a session that was waiting on `gh run watch`. The run that
prompted this sat under *Needs you* for two and a half minutes watching four
CI runs.

The reading, in two halves:

* **What is pending** comes from the transcript's tail: every `tool_result`
  announcing a background launch, minus every later user record carrying that
  task's `<task-id>` — the notification is the resumption, so a task whose
  notification has landed is not pending whatever its file says.
* **Whether it has finished** comes from the output file the launch named.
  The harness appends `[exited with code N]` when the command ends; of the
  Bash outputs still on disk here, every finished one carried it. A file
  with the marker is finished; a file without it, and not too old, is still
  running.

Three bounds, all deliberate:

* **`PARK_MAX_SECONDS` is a ceiling on being wrong.** Over the longest thing a
  person here waits on in the background (a TestFlight archive, up to 45
  minutes), under the overnight outlier in the measurement. A task past it
  no longer holds its session, marker or not.
* **Failure means "not running".** An unreadable, missing, oddly named or
  not-a-regular-file output returns nothing, because a hit here *suppresses*
  a card a person would otherwise be shown. This fails open, towards telling
  them. A launch record whose timestamp cannot be read is treated the same
  way: no age means no proof.
* **Claude Code only, and by shape rather than by name.** Grok and Codex end
  no turn on pending work — their journals carry neither the launch sentence
  nor the notification — so they never park on this rung.

**The output path is a string the transcript chose**, and the transcript is
written by a process outside Dark Army, so the open is `subagent_watch._tail`'s:
`O_NONBLOCK` and a regular-file test on the descriptor, never a `stat` first.
The name is also required to be `<tasks dir>/<task id>.output` with the id
the launch announced — the one shape the harness writes — so a transcript
cannot point this reader at an arbitrary file.

The cost, stated plainly: a session whose background command is genuinely
running has its next Stop held back, and a command that hangs holds it for
`PARK_MAX_SECONDS`. Both are the price of not raising a card every time an
agent waits on its own shell.
"""

from __future__ import annotations

import json
import os
import re
import stat as stat_mod
import time
from datetime import datetime
from typing import Optional

# A background task older than this no longer holds its session. Over a
# TestFlight archive (up to 45 minutes of `gh run watch`), under the
# overnight outlier in the measurement above.
PARK_MAX_SECONDS = 60 * 60

# The two sentences the harness writes. A launch is read only off a
# `tool_result` block whose text *begins* with the sentence — the harness's
# own reply — so a session that quotes another's launch (a transcript grep,
# a pasted log) announces nothing. The id is the harness's short token; the
# path runs to whitespace, minus the sentence's full stop.
LAUNCH = re.compile(
    r"Command running in background with ID: ([A-Za-z0-9_-]+)\. "
    r"Output is being written to: (\S+?)\.?(?:\s|$)")
NOTIFIED = re.compile(rb"<task-id>([A-Za-z0-9_-]+)</task-id>")

# The harness's own closing line on a finished task's output.
EXIT_MARKER = b"[exited with code"

# How much of the transcript to read: the launch sits a handful of records
# before the Stop it parks, and one assistant record with a thinking
# signature is a few kilobytes. Once per Stop.
TAIL_BYTES = 512 * 1024
# How much of an output file to read for its marker.
OUTPUT_TAIL_BYTES = 4 * 1024


def _read_tail(path, limit: int) -> Optional[bytes]:
    """The last `limit` bytes of a regular file, or None.

    Opened `O_RDONLY | O_NONBLOCK` and typed on the descriptor: a FIFO at
    that name would otherwise block the daemon's loop for ever, and a
    character device reports a size the seek would never fire on.
    """
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


def output_path(task_id: str, claimed: str) -> Optional[str]:
    """`claimed` when it is the one shape the harness writes, else None.

    `<something>/tasks/<task_id>.output`, absolute, no NUL, no traversal:
    the transcript is not Dark Army's file, and a reader that opened whatever it
    named would be a reader that could be pointed at anything.
    """
    if not task_id or not claimed or "\x00" in claimed:
        return None
    if not os.path.isabs(claimed):
        return None
    parts = claimed.split("/")
    if ".." in parts or "." in parts[1:]:
        return None
    if len(parts) < 3 or parts[-2] != "tasks":
        return None
    if parts[-1] != f"{task_id}.output":
        return None
    return claimed


def _stamp(record: dict) -> Optional[float]:
    """The record's own `timestamp`, as epoch seconds, or None."""
    stamp = record.get("timestamp")
    if not isinstance(stamp, str) or not stamp:
        return None
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _result_texts(record: dict):
    """The text of every `tool_result` block on a user record."""
    if record.get("type") != "user":
        return
    content = (record.get("message") or {}).get("content")
    if not isinstance(content, list):
        return
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            continue
        inner = block.get("content")
        if isinstance(inner, str):
            yield inner
        elif isinstance(inner, list):
            for part in inner:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    yield part["text"]


def _launches(line: bytes):
    """`(task_id, claimed path, launch time)` for each launch the harness
    itself announced on this record; a line that will not parse is none."""
    try:
        record = json.loads(line)
    except ValueError:
        return
    if not isinstance(record, dict):
        return
    at = _stamp(record)
    for text in _result_texts(record):
        m = LAUNCH.match(text)
        if m:
            yield m.group(1), m.group(2), at


def launched(transcript_path: str) -> "dict[str, tuple[str, Optional[float]]]":
    """Every background launch in the transcript's tail that has not been
    notified back: `{task_id: (claimed output path, launch time)}`.

    Order of launch preserved. A task whose `<task-id>` appears in a later
    record is dropped — its notification is the resumption.
    """
    if not transcript_path or "\x00" in transcript_path:
        return {}
    tail = _read_tail(transcript_path, TAIL_BYTES)
    if not tail:
        return {}
    out: "dict[str, tuple[str, Optional[float]]]" = {}
    for line in tail.split(b"\n"):
        if b"running in background" in line:
            for task_id, path, at in _launches(line):
                out[task_id] = (path, at)
        if b"task-id>" in line:
            for m in NOTIFIED.finditer(line):
                out.pop(m.group(1).decode("ascii", "replace"), None)
    return out


def still_running(task_id: str, claimed: str, launched_at: Optional[float],
                  now: Optional[float] = None) -> bool:
    """Whether the task the transcript announced is still running.

    True only where every proof lines up: a launch time inside
    `PARK_MAX_SECONDS`, an output file at the one name the harness writes,
    and no exit marker on its tail. Everything else is "not running".
    """
    if launched_at is None:
        return False
    if (now or time.time()) - launched_at > PARK_MAX_SECONDS:
        return False
    path = output_path(task_id, claimed)
    if path is None:
        return False
    tail = _read_tail(path, OUTPUT_TAIL_BYTES)
    if tail is None:
        return False
    return EXIT_MARKER not in tail


def running_tasks(transcript_path: str,
                  now: Optional[float] = None) -> "list[str]":
    """The ids of this session's background tasks still running, in launch
    order — empty for anything that is not a Claude Code transcript."""
    return [
        task_id
        for task_id, (claimed, at) in launched(transcript_path).items()
        if still_running(task_id, claimed, at, now)
    ]
