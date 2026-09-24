#!/usr/bin/env python3
"""Measure Dark Army's background service over a one-minute window.

Prints one line: the service's CPU (average, min–max) and context switches
per second as `top` samples them, how many times a minute the board's
database log (`~/.dark-army/board.db-wal`) was rewritten, and how many
sessions were live. Run it before and after installing a build, with the same
sessions open, to show the steady-state gain in numbers.

Read-only and stdlib-only. It finds the service by the process listening on
Dark Army's loopback API port (`lsof`), falling back to a source run's
`dark_army_menubar` command line (`pgrep`); `--pid` names it outright. It
sends no signal to anything and writes nothing under `~/.dark-army`.

    python3 tools/daemon_churn_probe.py              # 60s window
    python3 tools/daemon_churn_probe.py --seconds 30
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

API_PORT = 19874
WAL_PATH = Path.home() / ".dark-army" / "board.db-wal"
LIVE_BUCKETS = ("running", "waiting", "sleeping")
SAMPLE_SECONDS = 10


def _lines(argv: list[str], timeout: float = 10.0) -> list[str]:
    try:
        done = subprocess.run(argv, capture_output=True, text=True,
                              timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        return []
    return [line.strip() for line in done.stdout.splitlines() if line.strip()]


def find_daemon_pid() -> tuple[int | None, str]:
    """The service's pid, or None and the reason in words."""
    listeners = {int(p) for p in _lines(
        ["lsof", "-nP", f"-iTCP:{API_PORT}", "-sTCP:LISTEN", "-t"]) if p.isdigit()}
    if len(listeners) == 1:
        return listeners.pop(), ""
    if len(listeners) > 1:
        return None, f"more than one process listens on port {API_PORT}: {sorted(listeners)}"
    found = []
    for line in _lines(["pgrep", "-fl", "dark_army_menubar"]):
        pid, _, command = line.partition(" ")
        if pid.isdigit() and "pytest" not in command and "build_check" not in command:
            found.append(int(pid))
    if len(found) == 1:
        return found[0], ""
    if not found:
        return None, "Dark Army's background service is not running (nothing listens on its port)"
    return None, f"several candidate processes, name one with --pid: {found}"


def _count(text: str) -> float | None:
    """`top`'s event figure: digits with an optional K/M/G and a +/- mark."""
    match = re.fullmatch(r"([\d.]+)([KMG]?)[+-]?", text)
    if not match:
        return None
    scale = {"": 1, "K": 1e3, "M": 1e6, "G": 1e9}[match.group(2)]
    return float(match.group(1)) * scale


def parse_top(output: str, pid: int) -> list[tuple[float, float]]:
    """`(cpu %, context switches in the interval)` per sample, the first
    (absolute, not a delta) sample dropped."""
    samples = []
    for line in output.splitlines():
        fields = line.split()
        if len(fields) >= 3 and fields[0] == str(pid):
            try:
                cpu = float(fields[1])
            except ValueError:
                continue
            csw = _count(fields[2])
            if csw is not None:
                samples.append((cpu, csw))
    return samples[1:]


def _wal_mtime() -> int | None:
    try:
        return WAL_PATH.stat().st_mtime_ns
    except OSError:
        return None


def live_sessions() -> str:
    lines = _lines(["curl", "-s", "--max-time", "3",
                    f"http://127.0.0.1:{API_PORT}/api/state"])
    try:
        state = json.loads("".join(lines))
    except ValueError:
        return "unknown"
    agents = state.get("agents") if isinstance(state, dict) else None
    if not isinstance(agents, dict):
        return "unknown"
    return str(sum(len(agents.get(b) or []) for b in LIVE_BUCKETS))


def measure(pid: int, seconds: int) -> str:
    count = max(1, seconds // SAMPLE_SECONDS)
    top = subprocess.Popen(
        ["top", "-l", str(count + 1), "-s", str(SAMPLE_SECONDS), "-c", "d",
         "-stats", "pid,cpu,csw", "-pid", str(pid)],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    started = time.monotonic()
    rewrites, last = 0, _wal_mtime()
    while top.poll() is None:
        time.sleep(1.0)
        now = _wal_mtime()
        if now is not None and now != last:
            rewrites += 1
        last = now
    output = top.stdout.read() if top.stdout else ""
    elapsed = max(1.0, time.monotonic() - started)
    samples = parse_top(output, pid)
    if not samples:
        return f"daemon pid {pid}: top returned no samples (is the pid still running?)"
    cpus = [cpu for cpu, _ in samples]
    csw_rate = sum(csw for _, csw in samples) / (len(samples) * SAMPLE_SECONDS)
    per_minute = rewrites * 60.0 / elapsed
    return (f"daemon pid {pid}: cpu avg {sum(cpus) / len(cpus):.1f}% "
            f"({min(cpus):.1f}–{max(cpus):.1f}), csw/s avg {csw_rate:.0f}, "
            f"wal rewrites {per_minute:.0f}/min, sessions live {live_sessions()}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Measure Dark Army's background service: CPU, context "
                    "switches and board database rewrites over a window. "
                    "Read-only; never signals or installs anything.")
    parser.add_argument("--seconds", type=int, default=60,
                        help="length of the window, in whole %ds samples "
                             "(default 60)" % SAMPLE_SECONDS)
    parser.add_argument("--pid", type=int, default=None,
                        help="measure this process instead of looking it up")
    args = parser.parse_args(argv)
    if args.seconds < SAMPLE_SECONDS:
        parser.error(f"--seconds must be at least {SAMPLE_SECONDS}")
    pid, reason = (args.pid, "") if args.pid else find_daemon_pid()
    if pid is None:
        print(f"daemon_churn_probe: {reason}", file=sys.stderr)
        return 2
    if not _lines(["ps", "-p", str(pid), "-o", "pid="]):
        print(f"daemon_churn_probe: no process {pid}", file=sys.stderr)
        return 2
    print(measure(pid, args.seconds))
    return 0


if __name__ == "__main__":
    sys.exit(main())
