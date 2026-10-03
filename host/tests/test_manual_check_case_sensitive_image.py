# host/tests/test_manual_check_case_sensitive_image.py
"""Opt-in: the case-sensitive swap, reproduced on a real case-sensitive
volume. Skipped unless `DARK_ARMY_CASE_SENSITIVE_IMAGE=1` and `hdiutil` exist:
attaching a volume during every run is not something the suite does silently.
The fixture always detaches (`-force`) in `finally`; a failed create or attach
is a skip with the tool's stderr, never a failure."""
import os
import plistlib
import shutil
import subprocess

import pytest

from dark_army_daemon import board, enrollment
from dark_army_daemon.board import BoardStore
from dark_army_daemon.daemon import BobDaemon

pytestmark = pytest.mark.skipif(
    os.environ.get("DARK_ARMY_CASE_SENSITIVE_IMAGE") != "1"
    or not shutil.which("hdiutil"),
    reason="set DARK_ARMY_CASE_SENSITIVE_IMAGE=1 on a Mac with hdiutil")

_CHECK = """# The strip fits

- **Card:** Fit the strip
- **Project:** proj
- **Check:** the strip fits
- **Created:** 2026-09-25T14:32:00+02:00
- **Status:** open
- **Outcome:** none
- **Checked at:** none

## Steps

1. Open the menu bar.

## Why not automated

A real screen.
"""
_STEPS = ("1. Open the menu bar.\n"
          "Why not automated: a real screen.")


_HDIUTIL_TIMEOUT = 60


def _hdiutil(*args):
    return subprocess.run(["hdiutil", *args], capture_output=True,
                          timeout=_HDIUTIL_TIMEOUT)


def _devices_of(image):
    """The `/dev` nodes `hdiutil info` lists for `image`, whole disk first."""
    try:
        info = plistlib.loads(_hdiutil("info", "-plist").stdout)
    except (subprocess.SubprocessError, ValueError, OSError):
        return []
    found = []
    for entry in info.get("images", []):
        if os.path.realpath(entry.get("image-path", "")) \
                == os.path.realpath(str(image)):
            found += [e["dev-entry"] for e in entry.get("system-entities", [])
                      if e.get("dev-entry")]
    return sorted(found, key=len)


@pytest.fixture(scope="module")
def volume(tmp_path_factory):
    base = tmp_path_factory.mktemp("csimage")
    image = base / "cs.sparseimage"
    try:
        made = subprocess.run(
            ["hdiutil", "create", "-size", "16m", "-fs",
             "Case-sensitive APFS", "-volname", "darkarmycs", "-type",
             "SPARSE", str(image)],
            capture_output=True, text=True, timeout=_HDIUTIL_TIMEOUT)
    except subprocess.TimeoutExpired:
        pytest.skip("hdiutil create timed out")
    if made.returncode != 0:
        pytest.skip("hdiutil create failed: " + made.stderr.strip())
    mount = ""
    devices = []
    try:
        try:
            attached = _hdiutil("attach", "-nobrowse", "-mountrandom",
                                str(base), "-plist", str(image))
        except subprocess.TimeoutExpired:
            pytest.skip("hdiutil attach timed out")
        if attached.returncode != 0:
            pytest.skip("hdiutil attach failed: "
                        + attached.stderr.decode(errors="replace").strip())
        for entity in plistlib.loads(attached.stdout).get(
                "system-entities", []):
            if entity.get("dev-entry"):
                devices.append(entity["dev-entry"])
            if entity.get("mount-point"):
                mount = entity["mount-point"]
        if not mount:
            pytest.skip("hdiutil attached no mount point")
        yield os.path.realpath(mount)
    finally:
        # Always detach: by mount point, else by device node; if the attach
        # plist could not be read, find the device through `hdiutil info`.
        try:
            if mount:
                _hdiutil("detach", "-force", mount)
            else:
                for dev in sorted(devices or _devices_of(image), key=len):
                    _hdiutil("detach", "-force", dev)
        except subprocess.SubprocessError:
            pass


def _daemon(tmp_path):
    daemon = BobDaemon(headless=True)
    store = BoardStore(tmp_path / "board.db")
    store.connect()
    daemon._board = store
    return daemon, store


def _enrol(monkeypatch, *roots):
    members = set(roots)
    monkeypatch.setattr(enrollment, "enrolled_roots", lambda: set(members))


@pytest.mark.asyncio
async def test_a_stale_root_admits_nothing_on_a_real_case_sensitive_volume(
        volume, tmp_path, monkeypatch):
    work = os.path.join(volume, "stale-a", "work")
    target = os.path.join(work, "manual-check", "2026-09-25-strip")
    os.makedirs(target)
    check = os.path.join(target, "check.md")
    with open(check, "w") as handle:
        handle.write(_CHECK)
    before = open(check, "rb").read()
    _enrol(monkeypatch, os.path.join(volume, "stale-a", "Work"))
    daemon, store = _daemon(tmp_path)
    try:
        assert daemon._manual_check_home(check) == ""
        text = await daemon.manual_check_text(check)
        assert text["available"] is False
        assert text["reason"] == board.MANUAL_CHECK_PLACE_REFUSAL
        ok, detail = await daemon.record_manual_outcome(check, "passed", "x")
        assert (ok, detail) == (False, board.MANUAL_CHECK_PLACE_REFUSAL)
        assert open(check, "rb").read() == before
        assert (await daemon.manual_checks_report())["checks"] == []
    finally:
        store.close()


@pytest.mark.asyncio
async def test_a_shouted_folder_over_a_link_is_refused_on_a_real_case_sensitive_volume(  # noqa: E501 - the name states the case
        volume, tmp_path, monkeypatch):
    elsewhere = os.path.join(volume, "stale-b", "elsewhere")
    os.makedirs(elsewhere)
    with open(os.path.join(elsewhere, "check.md"), "w") as handle:
        handle.write(_CHECK)
    root = os.path.join(volume, "stale-b", "proj")
    os.makedirs(os.path.join(root, "manual-check"))
    os.symlink(elsewhere, os.path.join(root, "manual-check", "x"))
    _enrol(monkeypatch, root)
    daemon, store = _daemon(tmp_path)
    try:
        daemon._handle_channel_message(
            {"type": "channel_attach", "port": 51000, "pid": 4242,
             "cwd": "/tmp", "session_id": "s1"})
        daemon._session_states["s1"] = {"pid": 4242, "last_event": 0}
        card, _ = store.create({"title": "mine", "project": "bob",
                                "root": root})
        store.bind_session(card["id"], "s1")
        reply = await daemon._handle_board_manual_request({
            "type": "board_manual_request", "port": 51000, "steps": _STEPS,
            "path": "manual-check/X/check.md"})
        assert reply["ok"] is False, reply
        assert reply["detail"] == "there is no file at that path"
        assert store.get(card["id"])["manual_check_path"] == ""
        # The honest spelling leaves the project: refused, as today.
        reply = await daemon._handle_board_manual_request({
            "type": "board_manual_request", "port": 51000, "steps": _STEPS,
            "path": "manual-check/x/check.md"})
        assert reply["ok"] is False, reply
        assert reply["detail"] == board.MANUAL_CHECK_PLACE_REFUSAL
    finally:
        store.close()
