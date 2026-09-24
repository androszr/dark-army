"""The command-token ledger: the one place a replayed press returns its
original answer.

A phone writes a press down before it sends it and carries a one-time mark
(`command_token`) with it, so a reply lost on a flaky connection can be
replayed as *the same press* rather than as a second one. This module is the
Mac's half: a bounded, TTL'd map from `(device, token)` to the answer this
same daemon already produced.

**Where it sits, and it is the whole safety case.** It is consulted inside
`ApiServer._sealed_run`'s `action` branch, *below* the action allow-list and
*below* the away lease check, and it answers only with a status this daemon
itself produced a moment ago. A lookup placed above either check would let a
token minted at home replay from a lapsed away window, which is why the
ordering is stated here as well as there.

**Threading.** Plain in-memory state, touched only from the asyncio loop
inside `_sealed_run`. No lock is needed and none is taken; asserting that here
is what keeps a later caller from reaching it off-thread.

**Memory only, and the cost is stated.** A daemon restart forgets every
receipt. What covers that window is the phone's own effect test — a receipt
reaching `done` because the snapshot shows what the press did — which is
`OutboxStore.alreadyLanded`'s argument one rung on: never resend what you can
check.

Stdlib only, and it imports nothing from the daemon: this is a data structure,
not a capability.
"""

from __future__ import annotations

import re
import time
from collections import OrderedDict

#: What a token may look like. Bounded and boring on purpose — the token is
#: only ever a dictionary key here, never a path, a filename or a log field
#: that is not truncated, and a shape test is cheaper than trusting that.
TOKEN_RE = re.compile(r"\A[A-Za-z0-9._-]{8,100}\Z")

#: How many answers are held at once, oldest evicted first.
MAX_RECEIPTS = 256

#: How long one answer stays replayable. Well over any phone retry window and
#: well under "for ever": a press nobody replayed in fifteen minutes is a
#: press whose effect the phone can see for itself.
RECEIPT_SECONDS = 900


class CommandReceipts:
    """`(device_id, token) -> (status, content_type, body)`, bounded and aged.

    `look_up` returns the recorded answer or `None`; `record` files one.
    Both prune, so an idle ledger still lets go of what has expired the next
    time anybody touches it.
    """

    def __init__(self, *, max_receipts: int = MAX_RECEIPTS,
                 ttl: float = RECEIPT_SECONDS):
        self._max = int(max_receipts)
        self._ttl = float(ttl)
        self._items: "OrderedDict[tuple, tuple]" = OrderedDict()

    @staticmethod
    def usable(token) -> str:
        """The token if it is one, else `""`.

        An absent or malformed token means **no dedupe and today's behaviour
        exactly**, which is what keeps a phone older than this ledger working.
        """
        text = token if isinstance(token, str) else ""
        return text if TOKEN_RE.match(text) else ""

    def _prune(self, now: float) -> None:
        floor = now - self._ttl
        for key in [k for k, (stamp, _) in self._items.items() if stamp < floor]:
            self._items.pop(key, None)
        while len(self._items) > self._max:
            self._items.popitem(last=False)

    def look_up(self, device_id: str, token: str):
        """The answer this press already got, or `None`."""
        mark = self.usable(token)
        if not mark:
            return None
        now = time.time()
        self._prune(now)
        found = self._items.get((str(device_id or ""), mark))
        return found[1] if found else None

    def record(self, device_id: str, token: str, answer) -> None:
        """File one final answer against this press.

        The caller records 200 **and** 409: both are final answers about this
        press, and a person's RETRY mints a fresh token, so recording a
        refusal freezes nobody out.
        """
        mark = self.usable(token)
        if not mark:
            return
        now = time.time()
        key = (str(device_id or ""), mark)
        self._items.pop(key, None)
        self._items[key] = (now, answer)
        self._prune(now)

    def __len__(self) -> int:
        return len(self._items)
