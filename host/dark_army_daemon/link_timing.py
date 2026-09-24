"""Where the time goes on the phone's two doors, measured on the Mac.

Every request that arrives sealed from the phone — through the relay
mailbox or straight at the LAN door — is timed on the Mac's side as a
handful of **hops**, and those samples are rolled up every
`ROLLUP_SECONDS` into one summary per ``(door, device)``: how many
requests, and the typical (p50), slow (p90) and worst (max) figure for
each hop. The rollup is what lands on the access log as one ``timing``
line (`access_log.py`); the per-request figures never touch a file — a
phone away all day makes ~20,000 requests, and a line each would grow the
journal without bound.

This module is **pure and stdlib-only**: no I/O, no event loop, no clock of
its own. ``now`` is handed in on every call, which is what makes the
window arithmetic testable to the second. `BobDaemon.note_link_timing`
owns the one instance and the one clock.

The hops, by door:

* ``dwell`` — relay only: how long the frame sat in the mailbox before the
  Mac picked it up. Mac wall clock at pickup minus the phone's own ``ts``
  inside the sealed frame, clamped at zero. **Approximate**, because it
  spans two clocks; `relay.RELAY_SKEW_SECONDS` already bounds how far
  apart they may be. Never "corrected".
* ``hold`` — relay only: how long the Mac's held GET waited before the
  mailbox handed the frame over (monotonic, one clock, honest).
* ``open`` — both doors: opening the seal (base64 → AEAD → inflate).
* ``run`` — both doors: running the request (`_remote_run` / `_sealed_run`).
* ``answer`` — relay only: sealing and POSTing the reply to the mailbox.
* ``seal`` — LAN only: sealing the reply for the HTTP response.
* ``size`` — both: the plain reply's byte count, so a slow ``run`` can be
  read against how much it produced.

A hop the door does not measure is simply absent from the sample and reads
``0`` in the rollup — the sentence on the access log names the hops each
door has (`access_log.sentence`).
"""

from __future__ import annotations

import math
from collections import deque
from typing import Optional

#: Every hop a sample may carry. A key outside this set is dropped at `add`.
HOPS = ("dwell", "hold", "open", "run", "answer", "seal", "size")
#: The three figures a rollup states per hop.
STATS = ("p50", "p90", "max")
#: Exactly the keys a rollup's ``hops`` dict carries: ``n`` (how many samples
#: the percentiles were taken over — at most `MAX_SAMPLES`, where ``count``
#: is every request the window saw), then ``<hop>_<stat>`` for each hop.
#: `access_log.HOP_KEYS` restates this tuple literally and a test pins the
#: two equal, so a hop added here without its access-log twin fails.
HOP_KEYS = ("n",) + tuple(f"{hop}_{stat}" for hop in HOPS for stat in STATS)
#: How long a window runs before it closes into one rollup.
ROLLUP_SECONDS = 600.0
#: How many samples a window keeps for its percentiles, per (door, device).
#: The count keeps going past this; the ring keeps the newest.
MAX_SAMPLES = 512


def percentile(values, q) -> float:
    """Nearest-rank percentile: the value at rank ``ceil(q/100 * n)`` of
    the sorted list, so ``percentile(v, 50)`` of one sample is that sample,
    of two is the lower, and ties are themselves. ``0.0`` of nothing."""
    ordered = sorted(float(v) for v in values)
    if not ordered:
        return 0.0
    try:
        share = float(q) / 100.0
    except (TypeError, ValueError):
        share = 0.5
    share = min(1.0, max(0.0, share))
    rank = int(math.ceil(share * len(ordered)))
    return ordered[max(0, min(len(ordered), rank) - 1)]


def _clean_hops(hops, size) -> dict:
    """The sample's hops as finite, non-negative floats; unknown keys and
    unusable values dropped, ``size`` folded in as its own hop."""
    out: dict = {}
    if isinstance(hops, dict):
        for name, value in hops.items():
            if name not in HOPS:
                continue
            try:
                number = float(value)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(number):
                continue
            out[str(name)] = max(0.0, number)
    if size is not None:
        try:
            number = float(size)
        except (TypeError, ValueError):
            number = None
        if number is not None and math.isfinite(number):
            out["size"] = max(0.0, number)
    return out


class _Window:
    __slots__ = ("started", "count", "samples")

    def __init__(self, started: float) -> None:
        self.started = float(started)
        self.count = 0
        self.samples: deque = deque(maxlen=MAX_SAMPLES)


class LinkTimer:
    """The bounded rings and the window arithmetic, per ``(door, device)``.

    `add` appends one sample and answers the closed rollup **only** when the
    window that started at its first sample is `ROLLUP_SECONDS` old, at
    which point a fresh window opens with the incoming sample as its first;
    every other call answers ``None``. `rollup` forces the current window
    closed (a test seam and a shutdown flush) — ``None`` when it is empty.
    """

    def __init__(self) -> None:
        self._windows: dict = {}

    def add(self, door: str, device_id: str, kind: str, hops, status,
            size, now: float) -> Optional[dict]:
        key = (str(door or ""), str(device_id or ""))
        try:
            status = int(status)
        except (TypeError, ValueError):
            status = 0
        sample = {
            "kind": str(kind or ""),
            "status": status,
            "hops": _clean_hops(hops, size),
        }
        window = self._windows.get(key)
        closed = None
        if window is not None and window.count > 0 \
                and float(now) - window.started >= ROLLUP_SECONDS:
            closed = self._close(key, window, float(now))
            window = None
        if window is None:
            window = _Window(float(now))
            self._windows[key] = window
        window.count += 1
        window.samples.append(sample)
        return closed

    def rollup(self, door: str, device_id: str, now: float) -> Optional[dict]:
        """Close the current window whatever its age; ``None`` if empty."""
        key = (str(door or ""), str(device_id or ""))
        window = self._windows.get(key)
        if window is None or window.count == 0:
            return None
        closed = self._close(key, window, float(now))
        self._windows.pop(key, None)
        return closed

    def windows(self) -> int:
        """How many (door, device) windows are open — the test seam for
        per-pair isolation."""
        return len(self._windows)

    def _close(self, key, window: _Window, now: float) -> dict:
        figures: dict = {"n": len(window.samples)}
        for hop in HOPS:
            values = [s["hops"][hop] for s in window.samples if hop in s["hops"]]
            figures[f"{hop}_p50"] = percentile(values, 50)
            figures[f"{hop}_p90"] = percentile(values, 90)
            figures[f"{hop}_max"] = max(values) if values else 0.0
        return {
            "door": key[0],
            "device_id": key[1],
            "count": window.count,
            "window_seconds": max(0, int(round(now - window.started))),
            "hops": figures,
        }
