# host/setup.py — py2app configuration for the Dark Army menubar app.
# The bundle is `Dark Army.app` since 22 Sep 2026; the identifier stays
# `com.bob-companion.menubar` (notification, automation and LaunchServices
# records are keyed by it).
import json
import os
from pathlib import Path

from setuptools import setup

# The build-time half of the version rule, kept out of the runtime.
# `dark_army_menubar/__init__.py` is empty and `build_check` imports only
# stdlib, so this pulls in no AppKit and does not make the helper a runtime
# import — see build_check's own docstring.
from dark_army_menubar import build_check


def _bake_version():
    """Generate _version_info.py with this build's version, and return it.

    The judgement is not made here. `build.sh` decides it once, before the
    build dirties the work tree (the portrait rsync and the extension packager both
    write inside the checkout), and hands it over as BOB_BUILD_VERSION. A bare
    `python setup.py py2app` with no such environment falls back to asking
    `build_check.describe_version()` directly."""
    version = (os.environ.get("BOB_BUILD_VERSION") or "").strip()
    if not version:
        version = build_check.describe_version(Path(__file__).resolve().parent.parent)

    path = Path(__file__).parent / "dark_army_menubar" / "_version_info.py"
    # json.dumps, not hand-quoting: this writes a Python module, and the string
    # now arrives from BOB_BUILD_VERSION as well as from git, so a `"` in it
    # would produce a frozen _version_info.py that will not parse.
    path.write_text(f"VERSION = {json.dumps(version)}\n")
    print(f"Baked version: {version}")
    return version


VERSION = _bake_version()
# macOS reads these two, and they are dot-separated digits or nothing. A
# development string cannot go in verbatim, so an unnumbered build says 0.0.0
# rather than borrowing a number it does not have; the descriptive string above
# is what the app's own menu shows.
PLIST_VERSION = build_check.plist_version(VERSION)

APP = ["launcher.py"]
DATA_FILES = []
# Build and test tools only: nothing the app runs imports any of these
# (`tests/test_frozen_bundle.py` walks every runtime module and opens the
# built bundle to prove it). py2app's import scan freezes whatever the venv
# happens to hold, so without this list a heavier venv — pytest and its
# plugins, numpy, setuptools with everything it vendors — changes what ships
# and what THIRD_PARTY_NOTICES.md must credit. The venv must not decide the
# bundle (the `_cffi_backend` lesson below, from the other direction).
# `typing_extensions` is deliberately absent: filelock and cryptography
# import it at run time. No stdlib name belongs here.
EXCLUDES = [
    # the test runner and what comes with it, and a maths library
    "numpy", "pytest", "_pytest", "pluggy", "iniconfig", "pygments",
    # the packaging toolkit
    "setuptools", "pkg_resources", "_distutils_hack",
    # the top-level names setuptools vendors
    "packaging", "jaraco", "more_itertools", "importlib_metadata", "zipp",
    "platformdirs", "tomli", "wheel", "autocommand", "backports",
]
OPTIONS = {
    "argv_emulation": False,
    "iconfile": "AppIcon.icns",
    "plist": {
        "CFBundleName": "Dark Army",
        "CFBundleDisplayName": "Dark Army",
        "CFBundleIdentifier": "com.bob-companion.menubar",
        "CFBundleVersion": PLIST_VERSION,
        "CFBundleShortVersionString": PLIST_VERSION,
        "LSUIElement": True,  # menu-bar-only app (no Dock icon)
    },
    # `cryptography` is listed whole rather than left to the import walker:
    # its `_rust` extension and lazy submodule imports are exactly what the
    # walker misses, and a module that imports fine from source can still be
    # absent from the frozen bundle (relay.py would then fail on launch).
    "packages": ["dark_army_daemon", "dark_army_menubar",
                 "cryptography"],
    # `_cffi_backend` is what `cryptography`'s Rust extension loads at
    # runtime, so the walker cannot see it either; a venv built from
    # requirements-dev.txt alone froze an app that died on launch with
    # `No module named '_cffi_backend'` (20 Sep 2026). A fatter venv only
    # hid it.
    # `websockets` is imported only inside `relay_ws.py`; without this line
    # the installed app would have no socket lane while the venv does, and
    # `build_check` would not catch it.
    "includes": ["rumps", "objc", "Quartz", "_cffi_backend", "websockets"],
    "excludes": EXCLUDES,
    # icons are read from disk at runtime, so they ship as resources rather than
    # off disk at request time. Without it here the frozen app serves a stub.
    "resources": ["dark_army_menubar/icons",
                  "dark_army_menubar/agent_pack"],
}

setup(
    name="Dark Army",
    app=APP,
    data_files=DATA_FILES,
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
