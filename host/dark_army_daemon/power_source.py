"""The host Mac's power source — what it is drawing from and how full it is.

Read from `/usr/bin/pmset -g batt`, the same reading the person's own menu
bar shows, parsed by a pure function (`parse`) and run by `read` on the
snapshot executor, never on the loop (`BobDaemon._power_source_snapshot`).
Published as the omittable `power` section of `ApiServer.state()` for the
phone's Fleet tab (`docs/phone-contract.md`, *The Fleet tab names the Mac's
battery*).

**Why not `psutil.sensors_battery()`.** psutil is already a dependency and
answers `(percent, secsleft, power_plugged)`, but it cannot tell *charging*
from *plugged in and held at 80% by Optimized Battery Charging* — both read
`power_plugged=True` — so the phone would say "charging" over a battery that
is not. `pmset` says which. IOKit's `IOPSCopyPowerSourcesInfo` is not an
option either: the bundle freezes no `pyobjc-framework-IOKit`. Do not swap
this back for simplicity.

**No clock rides the result.** The time estimate `pmset` prints
(`3:12 remaining`) moves every minute and would make the section news on
its own clock, defeating the phone's per-section digest; it is dropped
here, and the result carries only `available`, `present`, `percent`,
`source` and `charge`.

Stdlib only. A capability probe, not a platform test: any failure —
no `pmset`, a hang, a non-zero exit — is `unavailable()`, which the phone
draws as nothing. `subprocess` is a module global so a test can stand in.
"""

from __future__ import annotations

import re
import subprocess

#: The one command run, by absolute path with a fixed argv; its output is
#: parsed, never echoed.
PMSET_ARGV = ("/usr/bin/pmset", "-g", "batt")

#: How long `pmset` may take before the reading is given up as unavailable.
READ_TIMEOUT_SECONDS = 3.0

#: Every `source` word the parser can answer besides `""`.
SOURCES = ("ac", "battery")

#: Every `charge` word the parser can answer besides `""`.
CHARGES = ("charging", "discharging", "charged", "not_charging", "finishing")

_SOURCE_RE = re.compile(r"Now drawing from '([^']*)'")
_SOURCE_WORDS = {"AC Power": "ac", "Battery Power": "battery"}
_PERCENT_RE = re.compile(r"(\d+)%")

# The charge segment's words, checked in this order: `not charging` before
# `charging` and `discharging` before it, because each later word is a
# substring of an earlier one.
_CHARGE_WORDS = (
    ("not charging", "not_charging"),
    ("finishing charge", "finishing"),
    ("discharging", "discharging"),
    ("charging", "charging"),
    ("charged", "charged"),
)


def unavailable() -> dict:
    """The reading when there is none: the phone draws nothing."""
    return {"available": False}


def _charge(line: str) -> str:
    segments = [s.strip().lower() for s in line.split(";")[1:]]
    for word, name in _CHARGE_WORDS:
        for segment in segments:
            if segment.startswith(word):
                return name
    return ""


def parse(text: str) -> dict:
    """`pmset -g batt`'s answer as the `power` section.

    `present` is true only where an `-InternalBattery` line exists — a
    desktop prints the source line alone. `percent` is the whole number
    before `%` on that line, `None` where there is none. Garbage parses
    to `present: False` with an empty `source`, never an exception."""
    text = text if isinstance(text, str) else ""
    match = _SOURCE_RE.search(text)
    source = _SOURCE_WORDS.get(match.group(1), "") if match else ""
    battery = next((line for line in text.splitlines()
                    if "-InternalBattery" in line), None)
    percent = None
    charge = ""
    if battery is not None:
        found = _PERCENT_RE.search(battery)
        if found:
            percent = int(found.group(1))
        charge = _charge(battery)
    return {
        "available": True,
        "present": battery is not None,
        "percent": percent,
        "source": source,
        "charge": charge,
    }


def read(run=None) -> dict:
    """Run `pmset -g batt` and parse it. Executor only: this spawns a
    process and may wait up to `READ_TIMEOUT_SECONDS`.

    ``run`` stands in for `subprocess.run` where a caller wants it to;
    otherwise the module global is used, which a test may monkeypatch.
    Any failure is `unavailable()`."""
    runner = run if run is not None else subprocess.run
    try:
        done = runner(list(PMSET_ARGV), capture_output=True, text=True,
                      timeout=READ_TIMEOUT_SECONDS)
    except Exception:
        # Every failure — `FileNotFoundError`, `TimeoutExpired`, anything a
        # stand-in raises — is the same answer: no reading, draw nothing.
        return unavailable()
    if getattr(done, "returncode", 1) != 0:
        return unavailable()
    return parse(getattr(done, "stdout", "") or "")
