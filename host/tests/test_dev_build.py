"""Tests for the build-staleness / rebuild helper."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

from dark_army_menubar import dev_build


def test_is_frozen_detects_a_bundle_that_lost_sys_frozen(monkeypatch, tmp_path):
    """The regression that made "Rebuild & Reload" a no-op: `_on_restart`
    relaunches as `sys.executable -m dark_army_menubar`, which skips the
    bundle's `__boot__.py` — the only thing that sets `sys.frozen`. The app then
    runs from the .app while looking like a dev checkout, so `rebuild()` built
    `simulator/build` (which the bundle never runs) and reported success."""
    monkeypatch.delattr(sys, "frozen", raising=False)
    bundled = (tmp_path / "Dark Army.app" / "Contents" / "Resources" /
               "lib" / "python3.14" / "dark_army_menubar" / "dev_build.py")
    monkeypatch.setattr(dev_build, "__file__", str(bundled))

    assert dev_build.is_frozen() is True


def test_is_frozen_is_false_for_a_dev_checkout(monkeypatch, tmp_path):
    monkeypatch.delattr(sys, "frozen", raising=False)
    checkout = tmp_path / "shop-front" / "host" / "dark_army_menubar" / "dev_build.py"
    monkeypatch.setattr(dev_build, "__file__", str(checkout))

    assert dev_build.is_frozen() is False


def test_clean_env_strips_py2app_vars(monkeypatch):
    """A frozen .app inherits PYTHONHOME et al from py2app's launcher stub;
    build subprocesses must not, or host/.venv/bin/python boots against the
    bundle's frozen stdlib instead of the venv."""
    monkeypatch.setenv("PYTHONHOME", "/Apps/Dark Army.app/Contents/Resources")
    monkeypatch.setenv("RESOURCEPATH", "/Apps/Dark Army.app/Contents/Resources")
    monkeypatch.setenv("ARGVZERO", "/Apps/Dark Army.app/Contents/MacOS/Dark Army")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")

    env = dev_build._clean_env()

    for var in dev_build._PY2APP_ENV_VARS:
        assert var not in env
    # everything else survives — build.sh needs the real PATH
    assert env["PATH"] == "/usr/bin:/bin"


def test_clean_env_lets_a_venv_python_use_its_own_site_packages(tmp_path):
    """The concrete failure: with PYTHONHOME set, a child interpreter can't see
    the packages installed next to it."""
    marker = tmp_path / "sentinel_pkg.py"
    marker.write_text("VALUE = 42\n")
    dud_home = tmp_path / "empty-home"
    dud_home.mkdir()

    env = dict(os.environ, PYTHONHOME=str(dud_home), PYTHONPATH=str(tmp_path))
    code = "import sentinel_pkg"

    dirty = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True)
    assert dirty.returncode != 0

    os.environ.update(env)
    try:
        clean = dict(dev_build._clean_env(), PYTHONPATH=str(tmp_path))
        assert subprocess.run(
            [sys.executable, "-c", code], env=clean, capture_output=True
        ).returncode == 0
    finally:
        for var in ("PYTHONHOME", "PYTHONPATH"):
            os.environ.pop(var, None)


def test_a_checkout_is_only_stale_when_swift_changed(tmp_path, monkeypatch):
    """Icons and Python counted when the artifact was the simulator — it baked
    icons into sprite headers and shipped beside the Python. Neither is true of
    the panel, and leaving them in made the row read "stale" permanently after
    any icon rebake, which is how a warning stops being read."""
    import os
    from dark_army_menubar import dev_build

    (tmp_path / "panel" / "Sources" / "BobPanel").mkdir(parents=True)
    (tmp_path / "host" / "dark_army_menubar" / "icons").mkdir(parents=True)
    swift = tmp_path / "panel" / "Sources" / "BobPanel" / "main.swift"
    swift.write_text("// old")
    binary = tmp_path / "BobPanel"
    binary.write_text("bin")
    os.utime(swift, (1000, 1000))
    os.utime(binary, (2000, 2000))

    monkeypatch.setattr(dev_build, "is_frozen", lambda: False)
    icon = tmp_path / "host" / "dark_army_menubar" / "icons" / "dark-army-work-0.png"
    icon.write_text("png")
    os.utime(icon, (3000, 3000))        # newer than the binary
    assert dev_build.check_staleness(tmp_path, str(binary))["stale"] is False

    os.utime(swift, (4000, 4000))       # the source that actually matters
    assert dev_build.check_staleness(tmp_path, str(binary))["stale"] is True


def _make_bundle(tmp_path, name="Dark Army.app"):
    """A .app skeleton with dev_build.py where py2app puts it."""
    app = tmp_path / name
    module = app / "Contents" / "Resources" / "lib" / "python3.14" / \
        "dark_army_menubar" / "dev_build.py"
    module.parent.mkdir(parents=True)
    module.write_text("# frozen copy")
    return app, module


def _make_checkout(tmp_path, name="checkout"):
    root = tmp_path / name
    (root / "host").mkdir(parents=True)
    (root / "host" / "build.sh").write_text("#!/bin/bash\n")
    return root


def test_installed_app_finds_its_checkout_through_the_stamp(tmp_path, monkeypatch):
    """The /Applications copy has no repo ancestor, so the walk finds nothing and
    the build row + Rebuild verb used to vanish for the one install everyone
    runs. build.sh leaves the path behind; this reads it back."""
    app, module = _make_bundle(tmp_path)
    repo = _make_checkout(tmp_path)
    (app / "Contents" / "Resources" / dev_build.REPO_ROOT_STAMP).write_text(
        f"{repo}\n")
    monkeypatch.setattr(dev_build, "__file__", str(module))
    monkeypatch.delattr(sys, "frozen", raising=False)

    assert dev_build.find_repo_root() == repo


def test_a_stamp_pointing_nowhere_is_not_a_repo(tmp_path, monkeypatch):
    """A released .app carries the path it was *built* on; on someone else's
    machine that directory is absent or something else entirely."""
    app, module = _make_bundle(tmp_path)
    (app / "Contents" / "Resources" / dev_build.REPO_ROOT_STAMP).write_text(
        str(tmp_path / "gone"))
    monkeypatch.setattr(dev_build, "__file__", str(module))
    monkeypatch.delattr(sys, "frozen", raising=False)

    assert dev_build.find_repo_root() is None


def test_a_missing_stamp_is_not_an_error(tmp_path, monkeypatch):
    _app, module = _make_bundle(tmp_path)
    monkeypatch.setattr(dev_build, "__file__", str(module))
    monkeypatch.delattr(sys, "frozen", raising=False)

    assert dev_build.find_repo_root() is None


def test_rebuild_gets_past_the_version_gate_without_reinstating_the_fallbacks(
        tmp_path, monkeypatch):
    """A checkout being worked on is untagged and dirty every ordinary day, and
    build.sh's version gate refuses both — so the Rebuild row would die in a
    second with "Rebuild failed". `--allow-untagged` is its escape, and it is
    deliberately not `--dev`, which would also bring back the fallbacks that
    let a stale panel ship."""
    _app, module = _make_bundle(tmp_path)
    repo = _make_checkout(tmp_path)
    monkeypatch.setattr(dev_build, "__file__", str(module))
    monkeypatch.delattr(sys, "frozen", raising=False)

    seen = {}
    monkeypatch.setattr(subprocess, "run",
                        lambda cmd, **kw: seen.update(cmd=cmd, kw=kw))
    dev_build.rebuild(repo)
    assert "--allow-untagged" in seen["cmd"]
    assert "--dev" not in seen["cmd"]
    # And build.sh really does accept it, rather than printing its usage line.
    assert "--allow-untagged)" in (
        Path(__file__).resolve().parents[1] / "build.sh").read_text()


def test_a_released_artifact_has_no_source_and_so_no_rebuild(tmp_path, monkeypatch):
    """The zipped artifact: no `repo-root` stamp (build.sh writes it only under
    `--install`) and no `host/build.sh` anywhere above it, because it was
    unpacked outside the checkout. `find_repo_root()` answering None is what
    withdraws `can_rebuild`, and with it the Rebuild row."""
    app, module = _make_bundle(tmp_path / "Downloads")
    assert not (app / "Contents" / "Resources" / dev_build.REPO_ROOT_STAMP).exists()
    monkeypatch.setattr(dev_build, "__file__", str(module))
    monkeypatch.delattr(sys, "frozen", raising=False)

    assert dev_build.find_repo_root() is None
    # And the honest label for that state, which is what app.py now sends.
    assert dev_build.artifact_label(None) == "Build: release (no source)"
    assert dev_build.deploys_on_rebuild(None) is False


def test_an_installed_bundle_rebuilds_with_install(tmp_path, monkeypatch):
    """Without --install the build lands in host/dist and the restart relaunches
    the *old* /Applications copy: fifteen minutes, no visible change, and the row
    goes straight back to stale."""
    app, module = _make_bundle(tmp_path)
    repo = _make_checkout(tmp_path)
    monkeypatch.setattr(dev_build, "__file__", str(module))
    monkeypatch.delattr(sys, "frozen", raising=False)

    assert dev_build.deploys_on_rebuild(repo) is True
    assert dev_build.rebuild_title(repo) == "Rebuild & Deploy"

    seen = {}
    monkeypatch.setattr(subprocess, "run",
                        lambda cmd, **kw: seen.update(cmd=cmd, kw=kw))
    dev_build.rebuild(repo)
    assert seen["cmd"] == ["./build.sh", "--allow-untagged", "--install"]


def test_rebuild_decodes_its_output_as_utf8(tmp_path, monkeypatch):
    """`text=True` alone decodes with the *locale*, and an .app launched from
    Finder inherits no LANG — so ASCII, while build.sh prints em dashes. The
    build ran in full and then died reading its own output, which from the menu
    is indistinguishable from the button doing nothing."""
    app, module = _make_bundle(tmp_path)
    repo = _make_checkout(tmp_path)
    monkeypatch.setattr(dev_build, "__file__", str(module))
    monkeypatch.delattr(sys, "frozen", raising=False)

    seen = {}
    monkeypatch.setattr(subprocess, "run",
                        lambda cmd, **kw: seen.update(cmd=cmd, kw=kw))
    dev_build.rebuild(repo)
    assert seen["kw"]["encoding"] == "utf-8"
    assert seen["kw"]["errors"] == "replace"


def test_a_bundle_inside_the_repo_does_not_install(tmp_path, monkeypatch):
    """host/dist/Dark Army.app is the build output itself — installing it
    over /Applications would replace an app the developer did not ask about."""
    repo = _make_checkout(tmp_path)
    app, module = _make_bundle(repo / "host" / "dist")
    monkeypatch.setattr(dev_build, "__file__", str(module))
    monkeypatch.delattr(sys, "frozen", raising=False)

    assert dev_build.deploys_on_rebuild(repo) is False
    assert dev_build.rebuild_title(repo, " Companion") == "Rebuild & Reload Companion"


def test_a_checkout_never_installs(tmp_path, monkeypatch):
    repo = _make_checkout(tmp_path)
    monkeypatch.setattr(
        dev_build, "__file__",
        str(repo / "host" / "dark_army_menubar" / "dev_build.py"))
    monkeypatch.delattr(sys, "frozen", raising=False)

    assert dev_build.deploys_on_rebuild(repo) is False


def _dated_file(path, mtime):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("mtime fixture")
    os.utime(path, (mtime, mtime))
    return str(path)


@pytest.mark.parametrize("artifact,competitor,stale", [(1000, 3000, True), (3000, 1000, False)])
def test_nested_freshness_uses_only_the_selected_copy(tmp_path, monkeypatch, artifact, competitor, stale):
    monkeypatch.setattr(dev_build, "is_frozen", lambda: False)
    _dated_file(tmp_path / "panel/Sources/BobPanel/main.swift", 2000)
    resources = tmp_path / "Dark Army.app/Contents/Resources"
    selected = _dated_file(resources / "BobPanel.app/Contents/MacOS/BobPanel", artifact)
    _dated_file(resources / "BobPanel", competitor)
    _dated_file(tmp_path / "panel/.build/release/BobPanel", competitor)
    _dated_file(tmp_path / "panel/.build/debug/BobPanel", competitor)
    assert dev_build.check_staleness(tmp_path, selected) == {
        "panel_binary": selected, "artifact_mtime": artifact,
        "source_mtime": 2000, "stale": stale,
    }


@pytest.mark.parametrize("frozen", [False, True])
@pytest.mark.parametrize("source", ["host/dark_army_menubar/icons/bob.png",
                                    "host/dark_army_menubar/app.py",
                                    "host/dark_army_daemon/daemon.py"])
def test_python_and_icons_count_only_when_frozen(tmp_path, monkeypatch, frozen, source):
    monkeypatch.setattr(dev_build, "is_frozen", lambda: frozen)
    binary = _dated_file(tmp_path / "BobPanel", 2000)
    _dated_file(tmp_path / "panel/Sources/BobPanel/main.swift", 1000)
    _dated_file(tmp_path / source, 3000)
    info = dev_build.check_staleness(tmp_path, binary)
    assert info["stale"] is frozen
    assert info["source_mtime"] == (3000 if frozen else 1000)


@pytest.mark.parametrize("missing", ["source", "selection", "file", "stat"])
def test_unavailable_build_input_has_no_freshness(tmp_path, monkeypatch, missing):
    binary = _dated_file(tmp_path / "BobPanel", 1000)
    _dated_file(tmp_path / "panel/Sources/BobPanel/main.swift", 2000)
    root = tmp_path
    if missing == "source":
        root = None
    elif missing == "selection":
        binary = None
    elif missing == "file":
        os.unlink(binary)
    else:
        def cannot_stat(path):
            raise OSError("unreadable selected artifact")
        monkeypatch.setattr(dev_build.os.path, "getmtime", cannot_stat)
    assert dev_build.check_staleness(root, binary) is None


def test_replacing_selected_file_changes_the_on_disk_reading(tmp_path, monkeypatch):
    """No process is inspected: this measures the replacement, not loaded bytes."""
    monkeypatch.setattr(dev_build, "is_frozen", lambda: False)
    selected = tmp_path / "BobPanel"
    binary = _dated_file(selected, 1000)
    _dated_file(tmp_path / "panel/Sources/BobPanel/main.swift", 2000)
    before = dev_build.check_staleness(tmp_path, binary)
    replacement = tmp_path / "new-panel"
    _dated_file(replacement, 3000)
    replacement.replace(selected)
    after = dev_build.check_staleness(tmp_path, binary)
    assert before["artifact_mtime"] == 1000 and before["stale"]
    assert after["artifact_mtime"] == 3000 and not after["stale"]
    assert before["panel_binary"] == after["panel_binary"] == binary


def _git_checkout_with_side_folder(tmp_path):
    """A real main checkout (marked by host/build.sh) with a card's side folder
    under `.worktrees/`, made by git itself so the `.git` file and `commondir`
    are exactly what a board Start leaves behind."""
    main = _make_checkout(tmp_path, "dark-army")
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}

    def git(*args):
        subprocess.run(["git", "-C", str(main), *args], check=True,
                       capture_output=True, env=env)
    git("init", "-q", "-b", "main")
    git("-c", "user.email=t@t", "-c", "user.name=t", "add", "-A")
    git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init")
    side = main / ".worktrees" / "card-0001"
    git("worktree", "add", "-q", "-b", "card/0001", str(side))
    return main.resolve(), side


def test_a_side_folder_belongs_to_its_main_checkout(tmp_path):
    """A card's side folder is temporary, so it is never Dark Army's own
    checkout: the helper answers the main checkout it was made from."""
    main, side = _git_checkout_with_side_folder(tmp_path)

    assert dev_build.main_checkout(side) == main
    assert dev_build.main_checkout(main) == main


def test_a_folder_that_is_no_worktree_is_left_alone(tmp_path):
    plain = _make_checkout(tmp_path, "plain")
    assert dev_build.main_checkout(plain) == plain
    odd = _make_checkout(tmp_path, "odd")
    (odd / ".git").write_text("not a pointer\n")
    assert dev_build.main_checkout(odd) == odd
    dangling = _make_checkout(tmp_path, "dangling")
    (dangling / ".git").write_text(f"gitdir: {tmp_path / 'gone'}\n")
    assert dev_build.main_checkout(dangling) == dangling


def test_an_app_stamped_with_a_side_folder_finds_the_main_checkout(tmp_path, monkeypatch):
    """The install that split a session off under `card-…`: an app built from
    a side folder before build.sh learned to stamp the main checkout."""
    app, module = _make_bundle(tmp_path)
    main, side = _git_checkout_with_side_folder(tmp_path)
    (app / "Contents" / "Resources" / dev_build.REPO_ROOT_STAMP).write_text(f"{side}\n")
    monkeypatch.setattr(dev_build, "__file__", str(module))
    monkeypatch.delattr(sys, "frozen", raising=False)

    assert dev_build.find_repo_root() == main


def test_build_sh_stamps_the_main_checkout_from_a_side_folder(tmp_path):
    """build.sh's stamp lines, run against a real side folder, write the main
    checkout; against the main checkout, the main checkout."""
    main, side = _git_checkout_with_side_folder(tmp_path)
    text = (Path(__file__).resolve().parents[1] / "build.sh").read_text()
    start = text.index('    STAMP_ROOT="$( cd "$SCRIPT_DIR/.." && pwd )"')
    end = text.index('Resources/repo-root"', start) + len('Resources/repo-root"')
    snippet = text[start:end]
    for built_from in (side, main):
        out = tmp_path / "Contents" / "Resources"
        out.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["bash", "-euo", "pipefail", "-c", snippet],
            check=True, env={**os.environ, "SCRIPT_DIR": str(built_from / "host"),
                             "APP": str(tmp_path)}, cwd=tmp_path)
        assert (out / "repo-root").read_text().strip() == str(main)
