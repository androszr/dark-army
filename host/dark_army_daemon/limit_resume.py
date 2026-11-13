"""When Dark Army carries a session on after its usage limit resets.

A Claude Code session that hits the account's five-hour (or weekly) limit
ends its turn with a `StopFailure` whose `error` is `rate_limit` and whose
sentence is the one the terminal printed — "You've hit your limit · resets
1:40pm". Before this module the row sat under *Needs you* until somebody came
back, read the sentence and typed `continue` themselves, often hours after the
window had already reset. This module answers one question — **when may Dark
Army type that word for them, and did it work?** — and, like `autocompact.py`,
leaves the typing to the daemon, which owns the one writer that reaches the
input line (`session_io.send_text`).

* **When.** The reset instant, plus `MARGIN_SECONDS`, from the best source on
  hand: the sentence on the card first (it is the harness's own statement
  about *this* limit — an epoch after a `|`, or a clock time such as `1:40pm`,
  optionally dated and with an IANA zone in brackets), then the account-wide
  windows every Claude statusline reports (`five_hour_resets_at`,
  `seven_day_resets_at`). With neither, `UNKNOWN_RESET_RETRY_SECONDS` from now.
* **What.** `RESUME_TEXT`, typed after the kill-line like every other typed
  command, so a half-written draft is discarded rather than submitted.
* **A bounded number of attempts.** A typed prompt dismisses the card
  (`UserPromptSubmit`), so a resume that worked ends the episode by itself. A
  card still standing `SETTLE_SECONDS` after the word was typed means the
  limit had not lifted (or the keystroke never landed), and the next attempt
  is scheduled from the newest reset. After `MAX_ATTEMPTS` the session is left
  to its person, exactly as if this module had never run.
* **The person outranks the timer.** A card dismissed by hand, a prompt typed
  by hand, or the switch turned off ends the episode on the next tick.

Pure, and for autocompact's reason: the policy is the part with opinions in
it, and it should be testable with a dict and a clock.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable, Optional

#: What gets typed. A plain prompt, not a slash command: the model reads it and
#: picks the interrupted work back up.
RESUME_TEXT = "continue"

#: How long after the reset instant the word is typed. A reset is quoted to the
#: minute ("resets 1:40pm") and clocks drift; a minute and a half is enough to
#: be safely past it and short enough that nobody notices the wait.
MARGIN_SECONDS = 90.0

#: With no reset instant anywhere, how long to wait before trying anyway.
UNKNOWN_RESET_RETRY_SECONDS = 30 * 60.0

#: How long after typing before a standing card counts as "the limit had not
#: lifted". The prompt's own `UserPromptSubmit` normally clears it within a
#: second; the slack covers a session finishing a redraw first.
SETTLE_SECONDS = 180.0

#: Typed attempts per episode before the session is handed back to its person.
MAX_ATTEMPTS = 3

#: A session the daemon could not type into is tried again this much later,
#: at most `MAX_UNREACHABLE` times — a window can come back to the foreground.
UNREACHABLE_RETRY_SECONDS = 10 * 60.0
MAX_UNREACHABLE = 6

#: A reset further away than this is not believed: the longest window an
#: account has is a week.
MAX_WAIT_SECONDS = 8 * 24 * 3600.0

#: A window at or above this percentage is the one that is full.
FULL_PCT = 99.5

SEND, WAIT, PASS = "send", "wait", "pass"

_EPOCH = re.compile(r"\|\s*(\d{10})(?:\.\d+)?\b")
_RESETS = re.compile(
    r"\bresets?\s+(?:at\s+)?"
    r"(?:(?P<mon>[A-Za-z]{3})[a-z]*\.?\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?,?\s+"
    r"(?:at\s+)?)?"
    r"(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ap>[ap]\.?m\.?)?"
    r"(?:\s*\((?P<tz>[A-Za-z_]+(?:/[A-Za-z_+\-0-9]+)*)\))?",
    re.IGNORECASE)
_MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"), start=1)}


def is_limit_card(card: Optional[dict]) -> bool:
    """Is this the card a hit usage limit raises?"""
    return bool(card) and card.get("hook") == "StopFailure" \
        and card.get("error_kind") == "rate_limit"


def _zone(name: Optional[str]):
    if not name:
        return None
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:            # unknown zone, or no tz database
        return None


def reset_from_message(text: str, now: float) -> Optional[float]:
    """The reset instant a limit card's sentence names, as an epoch, or None.

    Reads the two shapes Claude Code has printed: `…limit reached|1731081600`
    and `…resets 1:40pm`, the latter optionally dated (`resets Nov 12, 9am`)
    and zoned (`resets 3pm (Europe/Warsaw)`). An undated clock time is the next
    time the clock reads that, so a quarter-past reading of `1pm` is tomorrow.
    """
    if not isinstance(text, str) or not text:
        return None
    hit = _EPOCH.search(text)
    if hit:
        return float(hit.group(1))
    hit = _RESETS.search(text)
    if not hit:
        return None
    hour = int(hit.group("h"))
    minute = int(hit.group("m") or 0)
    ampm = (hit.group("ap") or "").lower().replace(".", "")
    if ampm:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if ampm == "pm" else 0)
    elif not hit.group("m"):
        # A bare number with neither a colon nor am/pm is not a time.
        return None
    if hour > 23 or minute > 59:
        return None
    tz = _zone(hit.group("tz"))
    here = datetime.fromtimestamp(now, tz) if tz else datetime.fromtimestamp(now)
    month = _MONTHS.get((hit.group("mon") or "").lower()[:3])
    try:
        if month:
            when = here.replace(month=month, day=int(hit.group("day")),
                                hour=hour, minute=minute, second=0,
                                microsecond=0)
            if when.timestamp() < now - 86400:
                when = when.replace(year=when.year + 1)
        else:
            when = here.replace(hour=hour, minute=minute, second=0,
                                microsecond=0)
            if when.timestamp() <= now - 60:
                when = when + timedelta(days=1)
    except ValueError:
        return None
    return when.timestamp()


def reset_from_metrics(metrics: Iterable[dict], now: float) -> Optional[float]:
    """The reset instant of the full account window, from statusline payloads.

    A window at `FULL_PCT` names itself; the latest of those is the one that
    has to lift. With no window reading full (a per-model limit, or figures a
    turn behind), the soonest future reset is the best guess, and a wrong guess
    costs one attempt.
    """
    full: list[float] = []
    future: list[float] = []
    for m in metrics or ():
        if not isinstance(m, dict):
            continue
        for pct_key, reset_key in (("five_hour_pct", "five_hour_resets_at"),
                                   ("seven_day_pct", "seven_day_resets_at")):
            reset = m.get(reset_key)
            if not isinstance(reset, (int, float)) or reset <= now:
                continue
            future.append(float(reset))
            pct = m.get(pct_key)
            if isinstance(pct, (int, float)) and pct >= FULL_PCT:
                full.append(float(reset))
    if full:
        return max(full)
    if future:
        return min(future)
    return None


def resume_at(message: str, metrics: Iterable[dict], now: float) -> tuple[float, bool]:
    """When to type, and whether that came from a known reset instant."""
    for reset in (reset_from_message(message, now),
                  reset_from_metrics(metrics, now)):
        if reset is not None and now < reset <= now + MAX_WAIT_SECONDS:
            return reset + MARGIN_SECONDS, True
    return now + UNKNOWN_RESET_RETRY_SECONDS, False


@dataclass
class _Episode:
    due_at: float
    known: bool
    attempts: int = 0
    unreachable: int = 0
    sent_at: Optional[float] = None
    gave_up: bool = False


class LimitResumePolicy:
    """One episode per rate-limited session; `consider` once per snapshot."""

    def __init__(self) -> None:
        self.enabled = True
        self._episodes: dict[str, _Episode] = {}

    def due_at(self, sid: str) -> Optional[float]:
        """When this session will be resumed, for the row; None if it won't."""
        ep = self._episodes.get(sid)
        if ep is None or ep.gave_up or ep.sent_at is not None:
            return None
        return ep.due_at

    def consider(self, sid: str, card: Optional[dict], metrics: Iterable[dict],
                 now: float) -> str:
        """SEND (type now), WAIT (an episode is open) or PASS (nothing to do)."""
        if not self.enabled:
            self._episodes.pop(sid, None)
            return PASS
        ep = self._episodes.get(sid)
        if not is_limit_card(card):
            # A dismissed card or a typed prompt: the episode is over, whether
            # it was this module's prompt or the person's.
            self._episodes.pop(sid, None)
            return PASS
        message = str((card or {}).get("message") or "")
        if ep is None:
            due, known = resume_at(message, metrics, now)
            self._episodes[sid] = _Episode(due_at=due, known=known)
            return WAIT
        if ep.gave_up:
            return PASS
        if ep.sent_at is not None:
            if now - ep.sent_at < SETTLE_SECONDS:
                return WAIT
            # Still limited after the settle: the window had not lifted.
            if ep.attempts >= MAX_ATTEMPTS:
                ep.gave_up = True
                return PASS
            ep.sent_at = None
            ep.due_at, ep.known = resume_at(message, metrics, now)
            return WAIT
        if not ep.known:
            # A reset instant that turned up after the episode opened is
            # better than the blind retry it was waiting on.
            due, known = resume_at(message, metrics, now)
            if known:
                ep.due_at, ep.known = due, True
        return SEND if now >= ep.due_at else WAIT

    def note_sent(self, sid: str, now: float) -> None:
        ep = self._episodes.get(sid)
        if ep is not None:
            ep.attempts += 1
            ep.sent_at = now

    def note_unreachable(self, sid: str, now: float) -> None:
        """Nothing could type at this session; try later, a bounded number of
        times, then leave it to its person."""
        ep = self._episodes.get(sid)
        if ep is None:
            return
        if ep.sent_at is not None:
            # The send `note_sent` counted never landed: undo it.
            ep.sent_at = None
            ep.attempts = max(0, ep.attempts - 1)
        ep.unreachable += 1
        if ep.unreachable >= MAX_UNREACHABLE:
            ep.gave_up = True
        else:
            ep.due_at = now + UNREACHABLE_RETRY_SECONDS

    def forget(self, live: Iterable[str]) -> None:
        keep = set(live)
        for sid in list(self._episodes):
            if sid not in keep:
                self._episodes.pop(sid, None)
