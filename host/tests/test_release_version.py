"""The release-version layer of `dark_army_menubar.build_check`.

Kept out of `test_build_check.py` so that file stays about *freshness*. Every
case here runs with no Swift toolchain, no real build and no network:

* the git seam is a repo built in `tmp_path` (`_repo`), so a tag, an untagged
  HEAD, a dirty tree and a directory that is not a checkout are all first-class
  cases rather than manual steps;
* the manifest seam is a `tmp_path` destination read back with `json.loads`;
* the phone seam is a fake `project.pbxproj` written into a `tmp_path` tree.

Two repository invariants live here too, because nothing else holds them: that
`describe_version` and `version._version_from_git` still agree on this
checkout, and that `setup.py` no longer carries a git implementation or a
frozen `"1.0.0"`.
"""
import json
import os
import subprocess
from pathlib import Path

import pytest

from dark_army_menubar import build_check, version

ROOT = Path(__file__).resolve().parents[2]

# A `git init` in a test must not read the developer's own configuration: a
# machine with `commit.gpgsign` on would otherwise hang the suite on a key.
GIT_ENV = {
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "GIT_AUTHOR_NAME": "Dark Army Test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "Dark Army Test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
}


def _git(tree: Path, *args):
    return subprocess.run(
        ["git", "-c", "user.name=Dark Army Test", "-c", "user.email=test@example.invalid",
         "-c", "commit.gpgsign=false", *args],
        cwd=str(tree), env={**os.environ, **GIT_ENV},
        capture_output=True, text=True, timeout=30)


_PBXPROJ = """// !$*UTF8*$!
\t\t\t\tCURRENT_PROJECT_VERSION = {cur};
\t\t\t\tMARKETING_VERSION = {mkt};
\t\t\t\tCURRENT_PROJECT_VERSION = {cur};
\t\t\t\tMARKETING_VERSION = {mkt2};
"""


def _tree(tmp_path: Path, *, ext_version="1.2.3", marketing=("1.0", "1.0"),
          panel=("1.0", "1")) -> Path:
    """A minimal checkout-shaped tree: an extension manifest, a phone project
    and a panel Info.plist. Nothing else in the layer reads anything else."""
    tree = tmp_path / "tree"
    (tree / "vscode-extension").mkdir(parents=True)
    (tree / "vscode-extension" / "package.json").write_text(
        json.dumps({"name": "dark-army-ide", "version": ext_version}))
    (tree / "ios" / "BobPhone.xcodeproj").mkdir(parents=True)
    (tree / "ios" / "BobPhone.xcodeproj" / "project.pbxproj").write_text(
        _PBXPROJ.format(cur="1", mkt=marketing[0], mkt2=marketing[1]))
    (tree / "panel" / "Sources" / "BobPanel").mkdir(parents=True)
    (tree / "panel" / "Sources" / "BobPanel" / "Info.plist").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0"><dict>'
        f'<key>CFBundleShortVersionString</key><string>{panel[0]}</string>'
        f'<key>CFBundleVersion</key><string>{panel[1]}</string>'
        '</dict></plist>\n')
    return tree


def _repo(tmp_path: Path, tag=None, **kwargs) -> Path:
    tree = _tree(tmp_path, **kwargs)
    assert _git(tree, "init", "-q", ".").returncode == 0
    assert _git(tree, "add", "-A").returncode == 0
    assert _git(tree, "commit", "-qm", "one").returncode == 0
    if tag:
        assert _git(tree, "tag", tag).returncode == 0
    return tree


# --- describe_version ------------------------------------------------------


def test_an_exact_clean_tag_is_the_version(tmp_path):
    assert build_check.describe_version(_repo(tmp_path, tag="v1.2.3")) == "v1.2.3"


def test_a_dirty_tagged_tree_says_so(tmp_path):
    tree = _repo(tmp_path, tag="v1.2.3")
    (tree / "untracked.txt").write_text("x")
    assert build_check.describe_version(tree) == "v1.2.3-dirty"


def test_an_untagged_tree_is_branch_count_and_sha(tmp_path):
    tree = _repo(tmp_path)
    branch = _git(tree, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    sha = _git(tree, "rev-parse", "--short", "HEAD").stdout.strip()
    assert build_check.describe_version(tree) == f"{branch}+1@{sha}"


def test_a_branch_name_with_a_slash_never_reaches_the_manifest(tmp_path):
    """`feature/thing` would otherwise put a `/` in a file whose whole rule is
    that no value in it looks like a path."""
    tree = _repo(tmp_path)
    assert _git(tree, "checkout", "-q", "-b", "feature/thing").returncode == 0
    described = build_check.describe_version(tree)
    assert described.startswith("feature-thing+")
    assert "/" not in described


def test_a_directory_that_is_not_a_checkout_is_unknown(tmp_path):
    assert build_check.describe_version(_tree(tmp_path)) == "unknown"


def test_a_directory_that_does_not_exist_is_unknown(tmp_path):
    assert build_check.describe_version(tmp_path / "nope") == "unknown"


def test_is_dirty_reads_the_tree_and_names_paths(tmp_path):
    tree = _repo(tmp_path, tag="v1.2.3")
    assert build_check.is_dirty(tree) is False
    assert build_check.dirty_paths(tree) == []
    (tree / "untracked.txt").write_text("x")
    assert build_check.is_dirty(tree) is True
    assert "untracked.txt" in build_check.dirty_paths(tree)
    # Not a checkout: git could not answer, which is not the same as clean.
    assert build_check.is_dirty(_tree(tmp_path / "other")) is None


# --- plist_version ---------------------------------------------------------


@pytest.mark.parametrize("described,expected", [
    ("v1.2.3", "1.2.3"),
    ("1.2.3", "1.2.3"),
    ("v10.0.11", "10.0.11"),
    ("v1.2.3-dirty", "0.0.0"),
    ("v1.2.3-rc1", "0.0.0"),
    ("main+0@abc1234", "0.0.0"),
    ("main+0@abc1234-dirty", "0.0.0"),
    ("unknown", "0.0.0"),
    ("", "0.0.0"),
    ("v1.2", "0.0.0"),
])
def test_plist_version_never_emits_something_macos_would_reject(described, expected):
    """The one case that stops an invalid value reaching Info.plist: those two
    keys are dot-separated digits, and a development string smuggled in whole
    produces a bundle macOS reads wrongly rather than refuses."""
    assert build_check.plist_version(described) == expected


def test_both_plist_keys_take_the_same_string(tmp_path):
    payload = build_check.write_release_manifest(
        _repo(tmp_path, tag="v1.2.3"), tmp_path / "m.json")
    assert payload["app"] == {"short": "1.2.3", "bundle": "1.2.3"}


# --- component readers -----------------------------------------------------


def test_panel_plist_versions_reads_the_pair(tmp_path):
    assert build_check.panel_plist_versions(_tree(tmp_path)) == {
        "short": "1.0", "bundle": "1"}


def test_panel_plist_versions_is_empty_when_unreadable(tmp_path):
    assert build_check.panel_plist_versions(tmp_path / "nope") == {}


def test_ios_marketing_versions_are_distinct_and_in_file_order(tmp_path):
    tree = _tree(tmp_path, marketing=("2.0", "1.5"))
    assert build_check.ios_marketing_versions(tree) == ["2.0", "1.5"]
    assert build_check.ios_current_project_versions(tree) == ["1"]


def test_the_real_phone_project_declares_one_marketing_version():
    """A future divergence in the repo itself trips here rather than at a
    release."""
    assert build_check.ios_marketing_versions(ROOT) == ["1.0"]


# --- release_gate ----------------------------------------------------------


def test_the_gate_passes_a_clean_tagged_tree(tmp_path):
    verdict = build_check.release_gate(_repo(tmp_path, tag="v1.2.3"))
    assert verdict.ok, verdict.reason


def test_the_gate_refuses_a_tree_that_is_not_a_checkout(tmp_path):
    verdict = build_check.release_gate(_tree(tmp_path))
    assert not verdict.ok and "not a git checkout" in verdict.reason


def test_the_gate_refuses_an_untagged_head(tmp_path):
    verdict = build_check.release_gate(_repo(tmp_path))
    assert not verdict.ok and "tag" in verdict.reason


def test_the_gate_refuses_a_non_semver_tag(tmp_path):
    verdict = build_check.release_gate(_repo(tmp_path, tag="v1.2.3-rc1"))
    assert not verdict.ok and "tag" in verdict.reason


def test_the_gate_refuses_a_dirty_tree_and_names_a_path(tmp_path):
    tree = _repo(tmp_path, tag="v1.2.3")
    (tree / "untracked.txt").write_text("x")
    verdict = build_check.release_gate(tree)
    assert not verdict.ok
    assert "uncommitted changes" in verdict.reason
    assert "untracked.txt" in verdict.reason


def test_the_gate_refuses_an_extension_version_that_is_not_semver(tmp_path):
    verdict = build_check.release_gate(_repo(tmp_path, tag="v1.2.3", ext_version="0.1"))
    assert not verdict.ok and "extension manifest" in verdict.reason


def test_the_gate_refuses_two_marketing_versions(tmp_path):
    verdict = build_check.release_gate(
        _repo(tmp_path, tag="v1.2.3", marketing=("1.0", "2.0")))
    assert not verdict.ok and "MARKETING_VERSION" in verdict.reason


def test_allow_untagged_downgrades_the_first_three_and_keeps_the_last_two(tmp_path):
    """`--dev` builds an unnumbered app, but it must still be internally
    consistent: the extension's semver and the phone's single marketing
    version are not about having a number."""
    untagged = build_check.release_gate(_repo(tmp_path), allow_untagged=True)
    assert untagged.ok and "unnumbered" in untagged.reason

    non_git = build_check.release_gate(_tree(tmp_path / "a"), allow_untagged=True)
    assert non_git.ok and "not a git checkout" in non_git.reason

    tree = _repo(tmp_path / "b", tag="v1.2.3")
    (tree / "untracked.txt").write_text("x")
    dirty = build_check.release_gate(tree, allow_untagged=True)
    assert dirty.ok and "uncommitted changes" in dirty.reason

    bad_ext = build_check.release_gate(
        _repo(tmp_path / "c", ext_version="0.1"), allow_untagged=True)
    assert not bad_ext.ok

    two_phones = build_check.release_gate(
        _repo(tmp_path / "d", marketing=("1.0", "2.0")), allow_untagged=True)
    assert not two_phones.ok


def test_the_real_checkout_passes_the_gate_when_untagged_is_allowed():
    verdict = build_check.release_gate(ROOT, allow_untagged=True)
    assert verdict.ok, verdict.reason


# --- release_versions / write_release_manifest -----------------------------


_MANIFEST_KEYS = ["app", "commit", "extension", "panel", "panel_embedded",
                  "phone", "version"]


def test_the_manifest_key_set_is_exactly_the_documented_one(tmp_path):
    dest = tmp_path / "out" / "release-manifest.json"
    build_check.write_release_manifest(_repo(tmp_path, tag="v1.2.3"), dest)
    data = json.loads(dest.read_text())
    assert sorted(data) == _MANIFEST_KEYS


def test_a_tagged_tree_yields_the_number_everywhere(tmp_path):
    tree = _repo(tmp_path, tag="v1.2.3")
    data = build_check.write_release_manifest(tree, tmp_path / "m.json")
    assert data["version"] == "v1.2.3"
    assert data["app"] == data["panel"] == {"short": "1.2.3", "bundle": "1.2.3"}
    assert data["extension"] == "1.2.3"
    assert data["phone"] == {"marketing": ["1.0"], "current_project_version": ["1"]}
    assert data["commit"] == _git(tree, "rev-parse", "--short", "HEAD").stdout.strip()


def test_the_welded_panel_plist_is_recorded_rather_than_papered_over(tmp_path):
    data = build_check.write_release_manifest(
        _repo(tmp_path, tag="v1.2.3"), tmp_path / "m.json")
    assert data["panel_embedded"] == {"short": "1.0", "bundle": "1"}
    assert data["panel_embedded"] != data["panel"]


def test_an_untagged_tree_keeps_its_descriptive_string_and_borrows_no_number(tmp_path):
    tree = _repo(tmp_path)
    data = build_check.write_release_manifest(tree, tmp_path / "m.json")
    assert data["app"] == {"short": "0.0.0", "bundle": "0.0.0"}
    assert data["version"].endswith(
        _git(tree, "rev-parse", "--short", "HEAD").stdout.strip())


def test_an_explicit_version_overrides_the_reread(tmp_path):
    """`build.sh` writes the manifest after the build has dirtied the tree, so
    it hands back the reading the early gate took."""
    tree = _repo(tmp_path, tag="v1.2.3")
    (tree / "untracked.txt").write_text("x")
    assert build_check.describe_version(tree) == "v1.2.3-dirty"
    data = build_check.write_release_manifest(tree, tmp_path / "m.json",
                                              version="v1.2.3")
    assert data["version"] == "v1.2.3"
    assert data["app"]["short"] == "1.2.3"


def test_the_panel_pair_records_the_stamp_that_happened(tmp_path):
    """`build.sh` writes the nested plist inside its "is there a panel binary"
    branch while the manifest write is unconditional, so a --dev build with no
    panel must not ship a manifest asserting a write that never occurred."""
    tree = _repo(tmp_path, tag="v1.2.3")
    dest = tmp_path / "m.json"

    stamped = build_check.write_release_manifest(tree, dest, panel_version="1.2.3")
    assert stamped["panel"] == {"short": "1.2.3", "bundle": "1.2.3"}

    unstamped = build_check.write_release_manifest(tree, dest, panel_version="")
    assert unstamped["panel"] == {"short": "", "bundle": ""}
    # The key set never changes shape — absent is not how this is said.
    assert sorted(unstamped) == _MANIFEST_KEYS
    assert unstamped["app"] == {"short": "1.2.3", "bundle": "1.2.3"}

    # A direct caller with no opinion keeps the app's own pair.
    assert build_check.write_release_manifest(tree, dest)["panel"] == unstamped["app"]


def test_the_cli_carries_the_panel_stamp_through(tmp_path, capsys):
    dest = tmp_path / "rm.json"
    assert build_check.main([
        "release", "--repo-root", str(_repo(tmp_path, tag="v1.2.3")),
        "--panel-version", "", "--manifest", str(dest)]) == 0
    capsys.readouterr()
    assert json.loads(dest.read_text())["panel"] == {"short": "", "bundle": ""}


def _values(obj):
    if isinstance(obj, dict):
        for value in obj.values():
            yield from _values(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from _values(value)
    else:
        yield obj


def test_no_value_in_the_manifest_is_a_path(tmp_path):
    """It ships in a public zip. Nothing here may carry a folder, a hostname or
    a user name — walked value by value rather than trusted."""
    for tree in (_repo(tmp_path / "a", tag="v1.2.3"),
                 _repo(tmp_path / "b"),
                 _tree(tmp_path / "c")):
        data = build_check.write_release_manifest(tree, tmp_path / "m.json")
        for value in _values(data):
            assert "/" not in str(value), value
        assert str(tmp_path) not in json.dumps(data)


def test_an_unreadable_extension_manifest_leaves_no_path_behind(tmp_path):
    """`package_version`'s refusal carries a filesystem path in its `reason`;
    only a good reading may reach the manifest."""
    tree = _repo(tmp_path, tag="v1.2.3")
    (tree / "vscode-extension" / "package.json").unlink()
    data = build_check.write_release_manifest(tree, tmp_path / "m.json")
    assert data["extension"] == ""


# --- CLI -------------------------------------------------------------------


def test_the_release_subcommand_prints_two_tokens(tmp_path, capsys):
    assert build_check.main(["release", "--repo-root", str(_repo(tmp_path, tag="v1.2.3"))]) == 0
    out, err = capsys.readouterr()
    assert out.split("\n")[0].split() == ["v1.2.3", "1.2.3"]
    assert err == ""


def test_the_release_subcommand_refuses_with_exit_2(tmp_path, capsys):
    assert build_check.main(["release", "--repo-root", str(_repo(tmp_path))]) == 2
    out, err = capsys.readouterr()
    assert out == ""
    assert err.startswith("build-check: ") and "tag" in err


def test_the_release_subcommand_writes_the_manifest_and_prints_nothing(tmp_path, capsys):
    dest = tmp_path / "rm.json"
    assert build_check.main([
        "release", "--repo-root", str(_repo(tmp_path)), "--allow-untagged",
        "--version", "v1.2.3", "--manifest", str(dest)]) == 0
    out, err = capsys.readouterr()
    assert out == "" and err == ""
    assert json.loads(dest.read_text())["version"] == "v1.2.3"


# --- the two implementations of one rule -----------------------------------


def test_describe_version_agrees_with_the_runtimes_own_reading():
    """`version.py` deliberately does not import `build_check` — the
    runtime/build separation is worth more than the deduplication — so this is
    the only thing holding the two implementations together."""
    assert build_check.describe_version(ROOT) == version._version_from_git()


def test_the_two_still_agree_on_a_branch_name_with_a_slash(tmp_path, monkeypatch):
    """The pin above runs on whatever branch this checkout is on, which is
    normally `main` — so it can never see the one place the two readings could
    drift: the `/` fold. `_version_from_git` reads the process cwd, so drive it
    from inside a tmp repo on a slashed branch."""
    tree = _repo(tmp_path)
    assert _git(tree, "checkout", "-q", "-b", "feature/thing").returncode == 0
    monkeypatch.chdir(tree)
    assert version._version_from_git() == build_check.describe_version(tree)
    assert "/" not in version._version_from_git()


def test_version_py_does_not_import_the_build_helper():
    text = (ROOT / "host" / "dark_army_menubar" / "version.py").read_text()
    assert "build_check" not in text


def test_setup_py_defers_to_the_helper_and_carries_no_git_of_its_own():
    text = (ROOT / "host" / "setup.py").read_text()
    assert "build_check.plist_version" in text
    assert "BOB_BUILD_VERSION" in text
    assert '"1.0.0"' not in text
    assert "rev-list" not in text
    assert "describe --tags" not in text


def test_the_build_helper_is_a_build_time_import_only():
    """It may be imported by `setup.py`, which runs in a process that never
    becomes the app, and by nothing in the running menu bar."""
    assert "build_check" in (ROOT / "host" / "setup.py").read_text()
    assert "build_check" not in (
        ROOT / "host" / "dark_army_menubar" / "app.py").read_text()


def test_the_rebuild_row_stays_gated_on_the_flag_the_menu_bar_sends():
    """Pinned by grep because the Swift line already exists and this suite must
    run without a Swift toolchain."""
    text = (ROOT / "panel" / "Sources" / "BobPanel" / "SettingsMenuModel.swift").read_text()
    assert text.count("if context.canRebuild") == 1
