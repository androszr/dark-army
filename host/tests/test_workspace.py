import json
import os

import pytest

from dark_army_daemon import enrollment
from dark_army_daemon import paths
from dark_army_daemon import vscode_reveal as vr
from dark_army_daemon import workspace as ws


REPO = "/Users/bob/Code/work/dark-army"
OTHER = "/Users/bob/Code/side/finance-demo"


@pytest.fixture(autouse=True)
def fresh_cache():
    ws.invalidate()
    yield
    ws.invalidate()


def write_lock(tmp_path, port, **extra):
    payload = {"port": port, "authToken": "tok", **extra}
    (tmp_path / f"{port}.lock").write_text(json.dumps(payload))


@pytest.fixture
def ide_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(vr, "_BOB_IDE_DIR", tmp_path)
    return tmp_path


# ---- naming a window -------------------------------------------------------


def test_saved_workspace_file_names_the_window(ide_dir):
    write_lock(
        ide_dir, 19001,
        workspaceFolders=[REPO],
        workspaceFsPath="/Users/bob/Code/dark-army.code-workspace",
        workspaceName="dark-army (Workspace)",
    )
    assert ws.windows() == (ws.Window("dark-army", (REPO,)),)


def test_untitled_workspace_falls_back_to_its_name(ide_dir):
    write_lock(
        ide_dir, 19002,
        workspaceFolders=[REPO, OTHER],
        workspaceFsPath=None,
        workspaceName="Playgrounds",
    )
    assert ws.windows() == (ws.Window("Playgrounds", (REPO, OTHER)),)


def test_old_extension_without_identity_uses_its_single_folder(ide_dir):
    """A 0.1.4 lock has neither key — which is still enough to fix `host/`, so an
    unreloaded window is not stranded on the old behaviour."""
    write_lock(ide_dir, 19003, workspaceFolders=[REPO])
    assert ws.windows() == (ws.Window("dark-army", (REPO,)),)


def test_multi_folder_window_with_no_identity_is_not_named(ide_dir):
    """Naming it after its first folder would file two projects under one heading."""
    write_lock(ide_dir, 19004, workspaceFolders=[REPO, OTHER])
    assert ws.windows() == ()


def test_malformed_lock_is_skipped(ide_dir):
    (ide_dir / "19005.lock").write_text("{not json")
    write_lock(ide_dir, 19006, workspaceFolders=[REPO])
    assert ws.windows() == (ws.Window("dark-army", (REPO,)),)


# ---- the ladder ------------------------------------------------------------


def test_subdirectory_resolves_to_its_workspace(ide_dir):
    write_lock(ide_dir, 19010, workspaceFolders=[REPO])
    assert ws.project_label(REPO + "/host") == "dark-army"
    assert ws.project_label(REPO + "/panel/Sources/BobPanel") == "dark-army"
    assert ws.project_label(REPO) == "dark-army"


def test_sibling_prefix_is_not_containment(ide_dir):
    """`/a/bo` does not contain `/a/bob`, which a bare startswith would claim."""
    write_lock(ide_dir, 19011, workspaceFolders=["/Users/bob/Code/bob"])
    assert ws.project_label("/Users/bob/Code/bobsleigh/src") == "src"


def test_nested_checkouts_resolve_to_the_inner_one(ide_dir):
    write_lock(ide_dir, 19012, workspaceFolders=["/Users/bob/Code"])
    write_lock(ide_dir, 19013, workspaceFolders=[REPO])
    assert ws.project_label(REPO + "/host") == "dark-army"


def test_scratchpad_path_finds_its_project(ide_dir):
    write_lock(ide_dir, 19014, workspaceFolders=[REPO])
    mangled = REPO.replace("/", "-")
    cwd = f"/private/tmp/claude-501/{mangled}/b942c2a8-e7c8/scratchpad"
    assert ws.project_label(cwd) == "dark-army"


def test_scratchpad_of_an_unopened_project_falls_through(ide_dir):
    write_lock(ide_dir, 19015, workspaceFolders=[OTHER])
    mangled = REPO.replace("/", "-")
    cwd = f"/private/tmp/claude-501/{mangled}/b942c2a8-e7c8/scratchpad"
    assert ws.project_label(cwd) == "scratchpad"


def test_statusline_project_dir_is_the_next_rung(ide_dir):
    metrics = {"project_dir": REPO}
    assert ws.project_label("/private/tmp/somewhere/else", metrics) == "dark-army"


def test_git_worktree_backs_up_project_dir(ide_dir):
    metrics = {"project_dir": "", "git_worktree": REPO}
    assert ws.project_label("/private/tmp/somewhere/else", metrics) == "dark-army"


def test_no_windows_and_no_metrics_is_todays_basename(ide_dir):
    assert ws.project_label(REPO + "/host") == "host"


def test_containment_beats_statusline(ide_dir):
    """The window the user has open is a better answer than Claude Code's own
    project root, which is the directory the session was started in."""
    write_lock(ide_dir, 19016, workspaceFolders=[REPO])
    metrics = {"project_dir": REPO + "/host"}
    assert ws.project_label(REPO + "/host", metrics) == "dark-army"


def test_no_cwd_keeps_whatever_the_row_had(ide_dir):
    """A background agent the reconciler knows only by name must not come out of
    this less labelled than it went in."""
    assert ws.project_label("", None, "some-project") == "some-project"
    assert ws.project_label(None, None, "some-project") == "some-project"


# ---- caching ---------------------------------------------------------------


def test_windows_are_cached(ide_dir):
    write_lock(ide_dir, 19020, workspaceFolders=[REPO])
    assert ws.windows() == (ws.Window("dark-army", (REPO,)),)
    (ide_dir / "19020.lock").unlink()
    assert ws.windows() == (ws.Window("dark-army", (REPO,)),)  # still cached
    assert ws.windows(force=True) == ()


def test_an_empty_machine_caches_its_empty_answer(ide_dir, monkeypatch):
    calls = []
    real = vr._bob_ext_locks
    monkeypatch.setattr(vr, "_bob_ext_locks", lambda: (calls.append(1), real())[1])
    assert ws.windows() == ()
    assert ws.windows() == ()
    assert len(calls) == 1


# ---- the enrolment rungs ---------------------------------------------------
#
# The ledger, not the window, is the strongest available statement of "this
# folder is a project" — so the project's own folder name wins over everything
# below it. The seam is the tmp `paths.ENROLLMENT_PATH` that conftest's autouse
# door fixture already points at, which is why every test above is unaffected:
# with no ledger file there is nothing enrolled and the old four rungs answer.


def enrol(*roots):
    """Seed the tmp ledger with these roots and drop the memoised parse."""
    paths.ENROLLMENT_PATH.parent.mkdir(parents=True, exist_ok=True)
    paths.ENROLLMENT_PATH.write_text(json.dumps({
        "version": 1,
        "projects": [{"root": r, "digest": "x", "label": "IGNORED"}
                     for r in roots],
    }))
    enrollment.invalidate()


def real_dir(tmp_path, name):
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    return os.path.realpath(str(d))


def test_a_cwd_deep_inside_an_enrolled_root_takes_the_folders_name(tmp_path,
                                                                   ide_dir):
    root = real_dir(tmp_path, "finance-demo")
    os.makedirs(os.path.join(root, "a/b/c"), exist_ok=True)
    enrol(root)
    assert ws.project_label(root) == "finance-demo"
    assert ws.project_label(os.path.join(root, "a/b/c")) == "finance-demo"


def test_the_enrolled_folder_beats_the_windows_chosen_name(tmp_path, ide_dir):
    """The whole point of the rule: a deliberately named `.code-workspace` does
    not keep its chosen name once its folder is enrolled."""
    root = real_dir(tmp_path, "finance-demo")
    write_lock(ide_dir, 19030, workspaceFolders=[root],
               workspaceFsPath="/Users/bob/Code/Ledgerly.code-workspace")
    assert ws.project_label(root) == "Ledgerly"  # before enrolment
    enrol(root)
    ws.invalidate()
    assert ws.project_label(root) == "finance-demo"


def test_the_ledgers_stored_label_is_ignored(tmp_path, ide_dir):
    """A hand-edited ledger label must not become a renaming affordance."""
    root = real_dir(tmp_path, "finance-demo")
    enrol(root)
    assert ws.project_label(root) == "finance-demo"


def test_a_scratchpad_path_matches_an_enrolled_root_with_no_window_open(
        tmp_path, ide_dir):
    root = real_dir(tmp_path, "finance-demo")
    enrol(root)
    scratch = "/private/tmp/claude-501/" + root.replace(os.sep, "-") + "/abc"
    assert ws.windows() == ()
    assert ws.project_label(scratch) == "finance-demo"


def test_nested_enrolled_roots_resolve_to_the_inner_one(tmp_path, ide_dir):
    """Enrolling a subfolder separately is the ledger stating it is its own
    project — the rule behaving as written, mirroring the containment rung."""
    outer = real_dir(tmp_path, "dark-army")
    inner = real_dir(tmp_path, "dark-army/host")
    enrol(outer, inner)
    assert ws.project_label(inner + "/tests") == "host"
    assert ws.project_label(outer + "/panel") == "dark-army"


def test_a_cwd_outside_every_enrolled_root_walks_the_old_ladder(tmp_path,
                                                                ide_dir):
    enrol(real_dir(tmp_path, "finance-demo"))
    write_lock(ide_dir, 19031, workspaceFolders=[REPO])
    ws.invalidate()
    assert ws.project_label(REPO + "/host") == "dark-army"
    assert ws.project_label("/somewhere/else/entirely") == "entirely"


def test_an_empty_cwd_still_returns_the_callers_fallback(tmp_path, ide_dir):
    enrol(real_dir(tmp_path, "finance-demo"))
    assert ws.project_label("", None, "some-project") == "some-project"
