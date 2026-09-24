# host/tests/test_relay_latency_bench.py
"""`tools/relay_latency_bench.py` runs, prints the four trips and touches no
real state directory. A subprocess, because the tool redirects
`paths._home()` before it imports the daemon package and this interpreter
has already imported it."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BENCH = ROOT / "tools" / "relay_latency_bench.py"


def test_the_bench_runs_and_prints_the_four_trips(tmp_path):
    env = dict(os.environ)
    env["HOME"] = str(tmp_path / "home")  # belt: even the fallback is a sandbox
    env["TMPDIR"] = str(tmp_path)
    proc = subprocess.run(
        [sys.executable, str(BENCH), "--sessions", "3", "--rounds", "2", "--json"],
        capture_output=True, text=True, timeout=120, env=env,
        cwd=str(ROOT / "host"))
    assert proc.returncode == 0, proc.stderr[-2000:]
    report = json.loads(proc.stdout)
    assert {"state", "state_unchanged", "usage", "action"} <= set(report["trips"])
    for trip, legs in report["trips"].items():
        assert {"post_to_pickup", "mac_dwell", "mac_hold", "mac_open",
                "mac_run", "total"} <= set(legs), trip
        for leg in legs.values():
            assert {"p50", "p90", "max"} <= set(leg)
            assert leg["p50"] <= leg["p90"] <= leg["max"]
    assert report["sealed_bytes"]["state"] > report["sealed_bytes"]["state_unchanged"]
    assert report["sizes"]["state_unchanged"]["plain"] < 200
    assert report["sizes"]["state"]["plain"] > 10_000
    # Nothing was written under a real home: the tool's own tempdir alone,
    # and that one is removed when the run ends.
    assert not (tmp_path / "home" / ".bob-companion").exists()
    assert not (tmp_path / "home" / ".dark-army").exists()
    assert list(tmp_path.glob("relay-latency-bench-*")) == []
    assert list(tmp_path.glob("dark-army-tests-*")) == []


def test_the_bench_has_no_platform_branch_and_runs_in_the_venv():
    src = BENCH.read_text()
    assert "sys.platform" not in src
    assert "platform.system" not in src
    assert "PYTEST_CURRENT_TEST" in src  # the isolation switch, before the import
    assert src.index("PYTEST_CURRENT_TEST") < src.index("from dark_army_daemon import paths")


def test_the_socket_bench_runs_and_prints_the_push_trip(tmp_path):
    env = dict(os.environ)
    env["HOME"] = str(tmp_path / "home")
    env["TMPDIR"] = str(tmp_path)
    proc = subprocess.run(
        [sys.executable, str(BENCH), "--socket", "--sessions", "3",
         "--rounds", "2", "--json"],
        capture_output=True, text=True, timeout=120, env=env,
        cwd=str(ROOT / "host"))
    assert proc.returncode == 0, proc.stderr[-2000:]
    report = json.loads(proc.stdout)
    assert report["route"] == "socket"
    assert {"state", "state_unchanged", "usage", "action", "push"} \
        <= set(report["trips"])
    assert '"push"' in proc.stdout
    for trip, legs in report["trips"].items():
        assert {"post_to_pickup", "mac_dwell", "mac_hold", "mac_open",
                "mac_run", "total"} <= set(legs), trip
        # No mailbox on this lane: its legs read zero.
        assert legs["mac_dwell"]["max"] == 0.0 and legs["mac_hold"]["max"] == 0.0
    assert report["trips"]["push"]["total"]["max"] > 0
    assert report["sealed_bytes"]["push"] > report["sealed_bytes"]["state_unchanged"]
    assert not (tmp_path / "home" / ".bob-companion").exists()
    assert not (tmp_path / "home" / ".dark-army").exists()
    assert list(tmp_path.glob("relay-latency-bench-*")) == []
