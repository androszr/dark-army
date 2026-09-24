"""When Dark Army compacts a session itself instead of telling you to.

`signals.py` has always ended the context rules with the same sentence: *"context
91% — /compact or wrap up"*. It is advice, and `alerts.py` turns it into a banner
because the module that wrote it cannot type into anybody's terminal.

The first attempt at automating this pushed `/compact` down Dark Army's channel, as if
that were the same path as typing it. It is not. Slash commands are expanded by
the client on the **input line**, before a request exists; the channel lands in
the model's context as a `<channel>` block, and the model has no tool that can
compact. The line was delivered, the session shrugged, and the banner was
withheld because Dark Army believed it was handling the problem. Observed: nine
attempts in half an hour, no `compact_boundary`, no context drop.

So this module still answers one question — **may Dark Army run it, and did it work?**
— but the send is no longer its business. The daemon types the command into the
session's terminal (`session_io.send_text` — the VS Code window, or the pty
Dark Army itself hosts), which is the one writer
that reaches the input line without stealing focus. A session that is not in a
window whose extension is new enough to type is not compacted and is not
suppressed either — it gets the banner it has always got. Reachability is the
daemon's to decide; this module only takes the answer.

* **Only for the rules that mean "full".** `ctx-full` always (it fires at 90%,
  or on `exceeds_200k` at any percentage), and `ctx-runway` only at `crit` —
  a session with twenty minutes of room does not need its history rewritten yet.
* **Claude and Grok.** Both clients expand `/compact` on the input line. The
  channel cannot carry a slash command for either of them; the terminal can.
* **One attempt per episode, then the human.** After `SETTLE_SECONDS` the
  warning either went away — the compact worked, the episode is over — or it did
  not, and the session is handed back to `alerts.py` to interrupt about, exactly
  as if this module had never run. A second push at a session that ignored the
  first is the definition of a robot arguing with a terminal.
* **An episode survives a flicker.** A ctx signal routinely drops out for one
  statusline tick. Closing the episode on that tick, then opening a fresh one
  when it returns, is how the first version retried forever and never reached
  `failed`. The episode is only popped once the signal has been absent for
  `CLEAR_SECONDS`.
* **Never silent.** While Dark Army is handling it the signal stays on the row and
  says so (`mark()`), because a warning that vanishes and a warning that is
  being dealt with look identical, and only one of them is true.

Pure, and for the same reason as its two neighbours: the policy is the part with
opinions in it, and it should be testable with a dict and a clock.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional

#: What gets typed. A real slash command, first thing in the line, because the
#: client expands it on the input line — not in a channel message, not in a
#: leader prompt. The harness is the only thing that can expand it, and only
#: when the bytes land on its prompt. Nothing is appended: `/compact
#: <instructions>` is legal and would let Dark Army steer somebody else's summary,
#: which is a decision no timer should be making on a person's behalf.
COMMAND = "/compact"

#: The rules this handles, and the severity each one needs.
COMPACT_RULES = {"ctx-full": {"warn", "crit"}, "ctx-runway": {"crit"}}

#: How long a compact is given to show up in the numbers. Generous: the session
#: has to finish whatever tool call it is in before it reads the line at all,
#: and the statusline that reports the new percentage lags a turn behind that.
SETTLE_SECONDS = 180.0

#: How long the "full" signal has to stay gone before the episode is over.
#: One quiet tick is a statusline gap, not success; thirty seconds is well over
#: one round trip and still short enough that a later refill is a new episode.
CLEAR_SECONDS = 30.0

#: What the row says while Dark Army is dealing with it — replacing the rule's own
#: advice tail, which is addressed to a person who no longer has to do anything.
HANDLED_TEXT = "Dark Army is compacting it"

SEND, HOLD, PASS = "send", "hold", "pass"


def command_for(provider: str = "claude") -> str:
    """The slash command this provider's client expands on its input line.

    Claude Code and Grok both spell it `/compact`. Kept as a function so a
    future client that names the same act differently does not have to fork
    the policy — only this map.
    """
    return COMMAND


def qualifies(signals: Iterable[dict]) -> bool:
    """Is one of these the "this session is full" signal, at a severity that
    means now rather than soon?"""
    for signal in signals or ():
        allowed = COMPACT_RULES.get(str(signal.get("rule") or ""))
        if allowed and str(signal.get("severity") or "") in allowed:
            return True
    return False


def mark(signals: Iterable[dict]) -> None:
    """Say on the row that Dark Army has this one, and take it out of the alert path.

    Mutates the signal dicts in place — they are built fresh by
    `SignalEngine.evaluate` on every tick, so there is nothing here to leak into
    the next one. `handled` is what `alerts.py` reads; the text is what a person
    reads, and both have to change or the panel would advise you to run a
    command that has already been run for you.
    """
    for signal in signals or ():
        allowed = COMPACT_RULES.get(str(signal.get("rule") or ""))
        if not allowed or str(signal.get("severity") or "") not in allowed:
            continue
        signal["handled"] = "compact"
        text = str(signal.get("text") or "")
        head = text.split(" — ", 1)[0]
        signal["text"] = f"{head} — {HANDLED_TEXT}" if head else HANDLED_TEXT


@dataclass
class AutoCompactPolicy:
    """One episode per session at a time, and it either worked or it is over."""

    enabled: bool = True
    settle_seconds: float = SETTLE_SECONDS
    clear_seconds: float = CLEAR_SECONDS
    #: session_id -> {sent_at, sent_pct, failed, cleared_at}
    _state: dict[str, dict] = field(default_factory=dict)

    def consider(self, session_id: str, signals: Iterable[dict],
                 ctx_pct: Optional[float], reachable: bool,
                 now: float) -> str:
        """`SEND` to push the command now, `HOLD` while one is in flight (the
        row is Dark Army's, the banner is suppressed), `PASS` to leave the session to
        `alerts.py` exactly as before this module existed."""
        state = self._state.get(session_id)

        if not qualifies(signals):
            # The warning is gone. One quiet tick is a flicker, not success —
            # start (or keep) the clear clock, and only pop once it has been
            # gone long enough that a later refill is a new episode. A failed
            # episode uses the same clock: a flap must not win a second push,
            # but a genuine later refill must.
            if state is None:
                return PASS
            if state.get("cleared_at") is None:
                state["cleared_at"] = now
            if now - state["cleared_at"] >= self.clear_seconds:
                self._state.pop(session_id, None)
            return PASS

        if state is not None:
            # Signal is back inside the clear window: same episode. Drop the
            # clock so a later gap has to start over.
            state["cleared_at"] = None

        if not self.enabled:
            # Deliberately after the clean-up above: switching the feature off
            # must not strand a session mid-episode, marked as handled by
            # something that is no longer running.
            return PASS

        if state is None:
            if not reachable:
                return PASS          # nothing to type into: banner, as always
            self._state[session_id] = {"sent_at": now, "sent_pct": ctx_pct,
                                       "failed": False, "cleared_at": None}
            return SEND

        if state["failed"]:
            return PASS
        if now - state["sent_at"] < self.settle_seconds:
            return HOLD

        # Settled, and the session is still saying it is full. Either the line
        # was never read or compacting did not buy enough room; either way the
        # automation is done and a person should hear about it.
        state["failed"] = True
        return PASS

    def note_failed(self, session_id: str) -> None:
        """The type-in itself did not land — the window went away between the
        snapshot and the send. Give the session straight back to the alert path
        instead of holding it for three minutes on a promise nobody kept."""
        state = self._state.get(session_id)
        if state is not None:
            state["failed"] = True

    def forget(self, live: Iterable[str]) -> None:
        """Drop sessions that are no longer anywhere — the same leak every other
        per-session map in this codebase has already had to fix."""
        keep = set(live)
        for sid in [s for s in self._state if s not in keep]:
            del self._state[sid]

    def pending(self, session_id: str) -> bool:
        """Is a compact in flight for this session? Read by the tests and by
        anything that wants to say so without re-deciding."""
        state = self._state.get(session_id)
        return bool(state and not state["failed"])


__all__ = ["AutoCompactPolicy", "COMMAND", "COMPACT_RULES", "HANDLED_TEXT",
           "SETTLE_SECONDS", "CLEAR_SECONDS", "SEND", "HOLD", "PASS",
           "command_for", "qualifies", "mark"]
