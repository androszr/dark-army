"""The test run never puts a Python tile in the Dock, not even for a moment.

`conftest.pytest_configure` keeps every pytest process — the run and each
`-n auto` worker — out of the Dock. Asking `NSApplication` for the prohibited
policy is not enough on its own: `sharedApplication()` checks the process in
as a foreground app first, and the Dock animates a tile in and out for each
one. The observation here is LaunchServices' own: a process that was ever
foreground announces its demotion as an `ApplicationTypeChanged` whose
previous value is `Foreground`.
"""

import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

HOST = Path(__file__).resolve().parents[1]

_CHILD = """
import os, subprocess, sys, time
sys.path.insert(0, {tests!r})
import conftest
conftest.pytest_configure(None)
def ask(key):
    return subprocess.run(["lsappinfo", "info", "-only", key, str(os.getpid())],
                          capture_output=True, text=True).stdout.strip()
print(ask("ASN"))
print(ask("ApplicationType"))
sys.stdout.flush()
time.sleep(0.5)
"""


def _appkit_available() -> bool:
    try:
        import AppKit  # noqa: F401
    except Exception:
        return False
    return True


@pytest.mark.skipif(shutil.which("lsappinfo") is None or not _appkit_available(),
                    reason="needs macOS LaunchServices and PyObjC")
def test_pytest_process_is_never_a_foreground_app():
    listener = subprocess.Popen(
        ["lsappinfo", "listen", "+appTypeChanged", "forever"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    try:
        time.sleep(0.5)
        child = subprocess.run(
            [sys.executable, "-c", _CHILD.format(tests=str(HOST / "tests"))],
            cwd=HOST, capture_output=True, text=True, timeout=60)
        assert child.returncode == 0, child.stderr
        time.sleep(0.5)
    finally:
        listener.terminate()
        events, _ = listener.communicate(timeout=10)

    asn_line, type_line = child.stdout.strip().splitlines()[-2:]
    asn = asn_line.split("=", 1)[1].strip().rstrip(":")
    assert '"BackgroundOnly"' in type_line
    demotions = [line for line in events.splitlines()
                 if asn in line and '"LSPreviousValue"="Foreground"' in line]
    assert demotions == [], "the pytest process was a Dock app first"
