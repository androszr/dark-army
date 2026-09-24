"""Whether a session's spawned subagents are still working.

**`SubagentStop` is not a promise the child has finished.** For an *async*
spawn — an `Agent(...)` call that returns at once and reports back minutes
later — the harness fires the stop hook when the spawning **turn** ends,
seconds after the start, while the child runs on. Measured 7 Sep 2026 on
2.1.263: a probe agent told to sleep 90s was struck from the parent's live
`subagents` set 7.6s after it began, and a poll of `/api/state` read
`subagents=1`, then `0`, then `0`, while it was still working.

The consequence was a false *"Waiting for input"*. The parent's own Stop
arrives with an empty live set, `_parked_reason` finds nothing to park on,
and a session that is merely waiting on its own child lands under **Needs
you** — staying there until the child returns and the parent's next tool call
dismisses the card. Half an hour, in the run that prompted this.

So the live set is not the whole answer, and this module supplies the other
half from the child's own transcript. **The test is whether the file moved
after the stop hook**, and that is the whole design:

* A child that stopped *because it finished* leaves a file frozen at that
  instant. A child whose hook fired early because its parent's turn ended
  keeps writing. Measured over this machine's 8 days of daemon logs: of 447
  subagents whose stop event could be matched to a transcript, **399 were
  frozen within ±1s of the hook** and **48 went on writing for a further 7s
  to 82 minutes**. Nothing at all landed between 1s and 7s, so
  `WROTE_AFTER_STOP_SECONDS` sits inside an empty band with room either side
  rather than on a threshold somebody has to tune.
* This replaced a reading built on `"stop_reason":"end_turn"`, which was
  sound but nowhere near sufficient: over the 1,194 finished child
  transcripts on this machine, **506 (42%) carry no terminal marker on their
  last line at all** — and they are Opus, Fable and Sonnet children, not one
  model's quirk. Freshness was then the only thing left to release them, so
  every ordinary synchronous spawn would have suppressed its parent's real
  card for the next fifteen minutes. That is the failure this rung exists to
  prevent, inverted.

The markers are kept as an **early release**, not as the test: `end_turn` and
`[Request interrupted` never appear mid-run, so a child that writes one is
finished whatever its mtime says. They can only ever let a card through
sooner, which is the safe direction.

Three bounds, all deliberate:

* **`STALE_SECONDS` is a ceiling on being wrong.** Long enough to sit through
  the longest single tool call this repo has (a full `pytest` run, ~7 minutes,
  writing nothing while it runs); short enough that a child killed outright
  releases its parent soon after.
* **Failure means "not running".** An unreadable, missing, underivable or
  not-a-regular-file path returns False, because a True here *suppresses* a
  card a person would otherwise be shown. This fails open, towards telling
  them. So does the stop-time test itself: a child that cannot be proved to
  have written after its hook is treated as finished.
* **Claude Code only, and by shape rather than by name.** The path is derived
  and then required to exist; Grok and Codex write nothing there, so they
  simply never park on this rung.

**The path is composed from two strings a hook payload chose**, and the hook
socket authenticates nothing beyond the project's enrolment key. So the open
is defensive in a way an ordinary file read need not be: `O_NONBLOCK` and a
regular-file test on the **opened descriptor**, because a FIFO left at that
name blocks `open()` for ever and would wedge the whole daemon — no SSE, no
API, no hooks — and `sessions.json` would restore the state and re-hang it on
the next start.

The cost, stated plainly: a session whose async child is still writing has its
genuine ask held back. It is released at the **next card decision** at which
the file has not moved since the previous one — which is why the observed
mtime is stamped back over the stop time each time, rather than the parent
latching on "it wrote past its hook once" and only `STALE_SECONDS` or a
terminal marker ever letting go. That is the price of not raising a card
every time an agent delegates.
"""

from __future__ import annotations

import os
import re
import stat as stat_mod
import time
from pathlib import Path
from typing import Iterable, Mapping, Optional, Union

from . import session_stats

# A subagent transcript untouched for this long stops holding its parent.
# Over any single tool call an agent makes (a full `pytest` run in this repo
# is ~7 minutes and writes nothing while it runs), under the 30-minute ceiling
# `SUBAGENT_PARK_MAX_SECONDS` puts on the live set.
STALE_SECONDS = 15 * 60

# How far past its own stop hook a transcript must have been written for its
# child to count as still running. The measured gap is 1s to 7s wide with
# nothing in it (see the module docstring), so this is not a tuned threshold;
# it is the middle of an empty band. Erring low costs a suppressed card, erring
# high costs a delayed one, and low is the direction that tells the person.
WROTE_AFTER_STOP_SECONDS = 2.0

# The one marker that never appears mid-run. Matched on the raw bytes of the
# last record: a JSON parse of a half-written line raises, and the answer to
# "has it ended?" for a line nobody can read is no. Whitespace is permitted
# because JSON permits it — a marker that only matches one encoder is one that
# will quietly stop matching. An **early release**, never the test: 42% of
# finished children write no such line.
END_MARKER = re.compile(rb'"stop_reason"\s*:\s*"end_turn"')

# The other ending, and it is not rare: a child stopped by the person writes
# no `end_turn` at all, finishing on a `user` record carrying this sentence.
INTERRUPT_MARKER = re.compile(rb'"\[Request interrupted')

# How much of the tail to read. Generous, because one assistant record is not
# small — a thinking block carries a signature of several kilobytes. Once per
# Stop, per pending child.
TAIL_BYTES = 256 * 1024


def transcript_for(transcript_path: str, agent_id: str) -> Optional[Path]:
    """Where the harness keeps `agent_id`'s own transcript, or None.

    **Composed from the session's own `transcript_path`, never re-derived
    from its cwd.** The obvious `cwd.replace("/", "-")` is wrong and wrong
    silently: Claude Code mangles `.` to `-` as well, so
    `/Users/me/.dark-army` lives under `-Users-me--dark-army`, and
    every project with a dot in its path would resolve to a directory that
    does not exist — a rung that reads "no children running" for ever and is
    indistinguishable from one that is working. The hook carries the real
    string; `session_stats._subagents_dir` is the seam that already turns it
    into this directory, and it is reused rather than copied.
    """
    if not transcript_path or not agent_id:
        return None
    # Both arrive from a hook payload, which authenticates nothing. A NUL is
    # refused here rather than caught below because it is not a path at all:
    # `stat()` raises `ValueError` on one, which is not an `OSError`.
    if "\x00" in transcript_path or "\x00" in agent_id:
        return None
    if agent_id in (".", "..") or "/" in agent_id or "\\" in agent_id:
        return None
    parent = session_stats._subagents_dir(transcript_path)
    if parent is None:
        return None
    return parent / f"agent-{agent_id}.jsonl"


def _tail(path: Path) -> Optional[tuple]:
    """`(st, last non-blank line)` for a regular file, or None.

    The open is `O_RDONLY | O_NONBLOCK` and the file type is checked on the
    **descriptor**, not with a prior `stat`: the name was chosen by a hook
    payload, a FIFO there blocks `open()` indefinitely on the asyncio loop,
    and a `stat`-then-open would be a check/use gap besides. A character
    device is refused for the same reason — `/dev/zero` reports `st_size` 0,
    so the tail seek would never fire and the read would never end.
    """
    fd = None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        st = os.fstat(fd)
        if not stat_mod.S_ISREG(st.st_mode):
            return None
        if st.st_size > TAIL_BYTES:
            os.lseek(fd, -TAIL_BYTES, os.SEEK_END)
        chunks = []
        remaining = TAIL_BYTES
        while remaining > 0:
            block = os.read(fd, remaining)
            if not block:
                break
            chunks.append(block)
            remaining -= len(block)
        tail = b"".join(chunks)
    except (OSError, ValueError):
        return None
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
    lines = [line for line in tail.split(b"\n") if line.strip()]
    if not lines:
        return None
    return st, lines[-1]


def still_running(path: Optional[Path], since: Optional[float] = None,
                  now: Optional[float] = None) -> Optional[float]:
    """The child's transcript mtime when it is still writing, else None.

    `since` is the last moment we know the file had **not** moved past — its
    `SubagentStop` hook the first time, and the mtime this function itself
    returned on every look after that. That re-stamping is what keeps the
    reading a liveness test rather than a latch: "has it written past its
    stop hook" is true for ever once it is true, so a child that wrote for
    two minutes and then finished would have gone on holding its parent
    until `STALE_SECONDS`, which is the very bug this module exists to fix,
    moved to the other end of the run. Asking "has it written since I last
    looked" releases the parent on the next card decision instead.

    The mtime is returned rather than a bool so the caller can store it
    without a second `stat`, and because an mtime is always truthy there is
    no truth-value trap in reading the result as a yes/no.

    `since` None keeps the freshness-only reading, for a pending id with no
    stop time behind it.

    The cost of the re-stamp, stated: two card decisions less than
    `WROTE_AFTER_STOP_SECONDS` apart release a child that is genuinely
    running. That is the fail-open direction — the person is told about an
    ask that is real — and the decision cadence is thirty times the guard: a
    parked parent is re-asked by the `idle_prompt` Notification that fires
    ~60s into any quiet spell, which is also what bounds the release. A
    child that finishes is let go within about a minute, not at its parent's
    next Stop and not at `STALE_SECONDS`.
    """
    if path is None:
        return None
    got = _tail(path)
    if got is None:
        return None
    st, last = got
    if (now or time.time()) - st.st_mtime > STALE_SECONDS:
        return None
    if since is not None and st.st_mtime <= since + WROTE_AFTER_STOP_SECONDS:
        return None
    if (END_MARKER.search(last) is not None
            or INTERRUPT_MARKER.search(last) is not None):
        return None
    return st.st_mtime


def running_ids(transcript_path: str,
                agent_ids: Union[Mapping[str, float], Iterable[str]],
                now: Optional[float] = None) -> "dict[str, float]":
    """Which of `agent_ids` are still writing, `{id: mtime just observed}`.

    Order preserved, deduped. The values are what the caller stores back as
    the next `since`, so the next look asks for movement since this one.

    Takes the pending mapping `{agent_id: last known quiet-until}` the daemon
    keeps, or a bare iterable of ids for a state with no times behind it.
    """
    stops: Mapping[str, float] = (
        agent_ids if isinstance(agent_ids, Mapping) else {})
    seen: set = set()
    out: "dict[str, float]" = {}
    for agent_id in agent_ids:
        if agent_id in seen:
            continue
        seen.add(agent_id)
        since = stops.get(agent_id)
        if not isinstance(since, (int, float)):
            since = None
        mtime = still_running(transcript_for(transcript_path, agent_id),
                              since, now)
        if mtime is not None:
            out[agent_id] = mtime
    return out
