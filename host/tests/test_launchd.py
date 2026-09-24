# host/tests/test_launchd.py
"""Launch-at-login through a launchd user agent.

The interesting property is not writing a plist — it is that `enable()` has to
*take effect* when the label is already loaded. `launchctl bootstrap` refuses a
loaded label, and the old code discarded that return code, so a stale agent
kept launching the previous executable until the next login. Bootout first,
then bootstrap, and say so in the log when bootstrap still fails.
"""

import logging
import os
import plistlib
import subprocess
import sys
import types

import pytest

from dark_army_menubar import launchd


@pytest.fixture
def agent(tmp_path, monkeypatch):
    """launchd pointed at scratch plists, with launchctl recorded, not run."""
    plist = tmp_path / "com.dark-army.menubar.plist"
    monkeypatch.setattr(launchd, "PLIST_PATH", plist)
    calls = []
    returncodes = {}

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        rc = returncodes.get(argv[1], 0)  # keyed on the launchctl verb
        return types.SimpleNamespace(returncode=rc, stdout=b"", stderr=b"boom")

    monkeypatch.setattr(launchd.subprocess, "run", fake_run)
    return plist, calls, returncodes


def _launchctl(calls):
    return [c for c in calls if c[0] == "launchctl"]


def test_enable_writes_the_plist_for_the_current_executable(agent):
    plist, _, _ = agent
    launchd.enable()
    assert plist.exists()
    data = plistlib.loads(plist.read_bytes())
    assert data["Label"] == launchd.PLIST_LABEL
    assert data["ProgramArguments"][0] == sys.executable
    assert launchd.is_enabled() is True


def test_enable_boots_out_before_bootstrapping(agent):
    """With the label already loaded, `bootstrap` refuses and launchd keeps
    the old program path until next login. The bootout has to come first."""
    plist, calls, _ = agent
    launchd.enable()
    verbs = [c[1] for c in _launchctl(calls) if str(plist) in c]
    assert verbs == ["bootout", "bootstrap"]


def test_a_failed_bootout_is_fine_and_bootstrap_still_runs(agent):
    """Bootout of a label that is not loaded fails; that is the ordinary
    fresh-install case and must not be treated as an error."""
    plist, calls, returncodes = agent
    returncodes["bootout"] = 113
    launchd.enable()
    verbs = [c[1] for c in _launchctl(calls) if str(plist) in c]
    assert verbs == ["bootout", "bootstrap"]


def test_a_failed_bootstrap_is_logged(agent, caplog):
    """The return code was silently discarded — a launch agent that never
    loaded looked exactly like one that did."""
    _, _, returncodes = agent
    returncodes["bootstrap"] = 5
    with caplog.at_level(logging.WARNING, logger="dark-army.launchd"):
        launchd.enable()
    assert any("bootstrap" in r.message and "5" in r.message
               for r in caplog.records)


def test_a_clean_bootstrap_logs_nothing(agent, caplog):
    with caplog.at_level(logging.WARNING, logger="dark-army.launchd"):
        launchd.enable()
    assert not caplog.records


def test_disable_boots_out_and_removes_the_plist(agent):
    plist, calls, _ = agent
    launchd.enable()
    calls.clear()
    launchd.disable()
    assert not plist.exists()
    assert launchd.is_enabled() is False
    verbs = [c[1] for c in _launchctl(calls)]
    assert verbs == ["bootout"]


def test_disable_without_a_plist_touches_nothing(agent):
    _, calls, _ = agent
    launchd.disable()
    assert calls == []


def test_is_stale_tracks_the_executable(agent):
    plist, _, _ = agent
    launchd.enable()
    assert launchd.is_stale() is False
    data = plistlib.loads(plist.read_bytes())
    data["ProgramArguments"][0] = "/somewhere/else/python3"
    plist.write_bytes(plistlib.dumps(data))
    assert launchd.is_stale() is True


def test_is_stale_tracks_the_module(agent):
    """A plist naming another module under this very interpreter is stale,
    so the first launch of this build rewrites it — and the rewrite names
    this build's module."""
    plist, _, _ = agent
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_bytes(plistlib.dumps({
        "Label": launchd.PLIST_LABEL,
        "ProgramArguments": [sys.executable, "-m", "other_package.app"],
        "RunAtLoad": True, "KeepAlive": False}))
    assert launchd.is_stale() is True
    launchd.enable()
    data = plistlib.loads(plist.read_bytes())
    assert data["ProgramArguments"] == [sys.executable, "-m", "dark_army_menubar.app"]
    assert launchd.is_stale() is False
    # A plist with nothing after the interpreter is stale too: it starts no app.
    data["ProgramArguments"] = [sys.executable]
    plist.write_bytes(plistlib.dumps(data))
    assert launchd.is_stale() is True


def test_the_stale_plist_repair_never_bootstraps(agent):
    """The repair runs inside the first launch of an upgraded app. A
    `RunAtLoad` job bootstrapped then would start a second copy mid-launch,
    so the repair writes the plist and boots the old job out — nothing
    loads it — and says it takes effect at the next login."""
    plist, calls, _ = agent
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_bytes(plistlib.dumps({
        "Label": launchd.PLIST_LABEL,
        "ProgramArguments": [sys.executable, "-m", "other_package.app"],
        "RunAtLoad": True, "KeepAlive": False}))
    assert launchd.is_stale() is True
    calls.clear()
    result = launchd.repair_stale_login_item()
    # `print` is the read-only pid lookup; only the verbs that change launchd count.
    verbs = [c[1] for c in _launchctl(calls) if c[1] != "print"]
    assert "bootstrap" not in verbs
    assert verbs == ["bootout"]
    assert (result.written, result.bootstrapped) == (True, False)
    assert "next login" in result.detail
    data = plistlib.loads(plist.read_bytes())
    assert data["ProgramArguments"] == [sys.executable, "-m", "dark_army_menubar.app"]
    assert launchd.is_stale() is False


def test_the_repair_never_boots_out_its_own_job(agent, monkeypatch):
    """Where launchd says this very process runs the job, a bootout would
    end the launch: the plist is still rewritten, and nothing is signalled."""
    plist, calls, _ = agent
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_bytes(plistlib.dumps({
        "Label": launchd.PLIST_LABEL,
        "ProgramArguments": ["/elsewhere/python", "-m", "dark_army_menubar.app"]}))
    monkeypatch.setattr(launchd, "_job_pid",
                        lambda label: os.getpid() if label == launchd.PLIST_LABEL else None)
    calls.clear()
    result = launchd.repair_stale_login_item()
    assert _launchctl(calls) == []
    assert result.written and not result.bootstrapped
    assert launchd.is_stale() is False


def test_is_stale_is_false_with_no_plist(agent):
    assert launchd.is_stale() is False
