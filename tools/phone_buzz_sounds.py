#!/usr/bin/env python3
"""Bake the phone's three buzz sounds — one per kind a push may carry.

A remote notification's `aps.sound` names a file in the app bundle, so the
three kinds the daemon tells apart (`alerts.KINDS`, mapped by
`relay/api/push.js`'s `SOUNDS`) need three files under
`ios/BobPhone/Resources/sounds/`. They are generated here rather than
hand-made so the same bytes come back on every run: `--check` re-renders and
refuses on any drift, and `host/tests/test_phone_buzz_kinds.py` does the
same comparison in the suite.

Stdlib only, deterministic (no clock, no randomness): pure sine segments,
mono, 16-bit, 22050 Hz, a short linear fade at each edge so nothing clicks,
every cue under a second so the buzz is a cue rather than a jingle.

    python3 tools/phone_buzz_sounds.py            # write the three files
    python3 tools/phone_buzz_sounds.py --check    # exit 1 on any byte drift
    python3 tools/phone_buzz_sounds.py --out DIR  # write somewhere else
"""
from __future__ import annotations

import argparse
import io
import math
import struct
import sys
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "ios" / "BobPhone" / "Resources" / "sounds"

RATE = 22050          # Hz, plenty for a cue and a small file
FADE_SECONDS = 0.012  # each segment's edges, so no click at a boundary
PEAK = 0.7            # of full scale, the two louder cues
SOFT = 0.35           # of full scale, the finished note — quieter on purpose

# name -> (peak amplitude, ((frequency Hz, seconds), ...)). Sums under 1.0s.
#   permission: two rising notes — an ask.
#   question:   three rising notes — a chime that keeps going up.
#   finished:   one low note, soft — nothing is waiting on you.
SPECS: dict[str, tuple[float, tuple[tuple[float, float], ...]]] = {
    "buzz-permission": (PEAK, ((523.25, 0.25), (783.99, 0.30))),
    "buzz-question": (PEAK, ((523.25, 0.22), (659.25, 0.22), (880.00, 0.31))),
    "buzz-finished": (SOFT, ((392.00, 0.60),)),
}


def _segment(freq: float, seconds: float, peak: float) -> list[int]:
    n = int(round(seconds * RATE))
    fade = max(1, int(round(FADE_SECONDS * RATE)))
    out: list[int] = []
    for i in range(n):
        env = 1.0
        if i < fade:
            env = i / fade
        elif i >= n - fade:
            env = (n - 1 - i) / fade
        value = peak * env * math.sin(2.0 * math.pi * freq * i / RATE)
        sample = int(round(value * 32767.0))
        out.append(max(-32768, min(32767, sample)))
    return out


def render(name: str) -> bytes:
    """The whole WAV file for one named cue, byte-stable across runs."""
    peak, notes = SPECS[name]
    samples: list[int] = []
    for freq, seconds in notes:
        samples.extend(_segment(freq, seconds, peak))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(struct.pack(f"<{len(samples)}h", *samples))
    return buf.getvalue()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT,
                    help=f"folder to write into (default: {DEFAULT_OUT})")
    ap.add_argument("--check", action="store_true",
                    help="re-render and exit 1 if any file on disk differs")
    args = ap.parse_args(argv)
    drift = 0
    for name in SPECS:
        path = args.out / f"{name}.wav"
        data = render(name)
        if args.check:
            if not path.is_file():
                print(f"missing: {path}")
                drift += 1
            elif path.read_bytes() != data:
                print(f"drift: {path}")
                drift += 1
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        print(f"wrote {path} ({len(data)} bytes)")
    if args.check:
        print("ok" if not drift else f"{drift} file(s) differ")
        return 1 if drift else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
