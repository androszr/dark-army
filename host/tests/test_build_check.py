"""`dark_army_menubar.build_check` and the gates `host/build.sh` builds on it.

Three layers, each on its own seam:

* the pure decisions (`panel_freshness`, `vsix_match`, `package_version`,
  `archive_version`) over `tmp_path` trees with `os.utime` and real
  `zipfile` archives;
* the repository invariant — exactly one committed `.vsix`, the manifest's;
* the real `build.sh`, copied into a fake tree and run with `--check-only`
  against stub `swift` / `node` / `npm` executables on `PATH`, the way
  `test_ship_close_out.py` runs `close-out.sh` against a stub server. No
  compiler, no py2app, no fixed port, no real `.build`.
"""
import json
import os
import shutil
import stat
import subprocess
import sys
import time
import zipfile
from datetime import datetime
from pathlib import Path

import pytest

from dark_army_menubar import build_check
from dark_army_menubar.build_check import (
    Verdict,
    archive_version,
    build_manifest,
    lock_match,
    lockfile_digest,
    main,
    package_version,
    panel_freshness,
    vsix_match,
    write_manifest,
)

ROOT = Path(__file__).resolve().parents[2]
BUILD_SH = ROOT / "host" / "build.sh"
EXT_DIR = ROOT / "vscode-extension"

OLD = 1_700_000_000.0
NEW = OLD + 100.0


_PACKAGE_SWIFT = (
    b'// swift-tools-version:5.9\n'
    b'.target(name: "BobPanel", resources: [.copy("Resources/cast"), '
    b'.copy("Resources/brand")])\n'
)


def _touch(path: Path, mtime: float, data: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    os.utime(path, (mtime, mtime))
    return path


# --- panel -----------------------------------------------------------------


def _panel_tree(root: Path, binary_mtime: float = NEW, source_mtime: float = OLD) -> dict:
    """A fake checkout: sources at `source_mtime`, a binary and a bundle copy
    of every resource at `binary_mtime` (bundle copies keep the source
    mtime, as SwiftPM does)."""
    src = root / "panel" / "Sources" / "BobPanel"
    paths = {
        "swift": _touch(src / "PanelView.swift", source_mtime),
        "package": _touch(root / "panel" / "Package.swift", source_mtime, _PACKAGE_SWIFT),
        "plist": _touch(src / "Info.plist", source_mtime),
        "resource": _touch(src / "Resources" / "cast" / "a.png", source_mtime, b"png1"),
        "binary": _touch(root / "panel" / ".build" / "release" / "BobPanel", binary_mtime),
    }
    bundle = root / "panel" / ".build" / "release" / "BobPanel_BobPanel.bundle"
    paths["bundle"] = bundle
    paths["bundle_resource"] = _touch(bundle / "cast" / "a.png", source_mtime, b"png1")
    return paths


def test_fresh_panel_passes(tmp_path):
    _panel_tree(tmp_path)
    verdict = panel_freshness(tmp_path)
    assert verdict == Verdict(True, "panel is fresh",
                              str(tmp_path / "panel" / ".build" / "release" / "BobPanel"))


@pytest.mark.parametrize("key", ["swift", "package", "plist"])
def test_newer_linked_source_refuses_and_names_it(tmp_path, key):
    paths = _panel_tree(tmp_path)
    os.utime(paths[key], (NEW + 1, NEW + 1))
    verdict = panel_freshness(tmp_path)
    assert not verdict.ok
    assert str(paths[key]) in verdict.reason
    assert verdict.path == str(paths[key])
    assert "older than its source" in verdict.reason
    assert "rm -rf panel/.build" in verdict.reason


def test_swift_source_in_a_subfolder_counts(tmp_path):
    _panel_tree(tmp_path)
    nested = _touch(tmp_path / "panel" / "Sources" / "BobPanel" / "Board" / "Lanes.swift", NEW + 5)
    verdict = panel_freshness(tmp_path)
    assert not verdict.ok
    assert str(nested) in verdict.reason


def test_missing_binary_refuses(tmp_path):
    paths = _panel_tree(tmp_path)
    paths["binary"].unlink()
    verdict = panel_freshness(tmp_path)
    assert not verdict.ok
    assert "binary missing" in verdict.reason
    assert str(paths["binary"]) in verdict.reason


def test_missing_bundle_refuses(tmp_path):
    paths = _panel_tree(tmp_path)
    shutil.rmtree(paths["bundle"])
    verdict = panel_freshness(tmp_path)
    assert not verdict.ok
    assert "resource bundle missing" in verdict.reason


def test_resource_absent_from_bundle_refuses(tmp_path):
    paths = _panel_tree(tmp_path)
    paths["bundle_resource"].unlink()
    verdict = panel_freshness(tmp_path)
    assert not verdict.ok
    assert "absent from the bundle" in verdict.reason
    assert "cast/a.png" in verdict.reason


def test_resource_size_mismatch_refuses(tmp_path):
    paths = _panel_tree(tmp_path)
    _touch(paths["bundle_resource"], OLD, b"png1-longer")
    verdict = panel_freshness(tmp_path)
    assert not verdict.ok
    assert "differs in size" in verdict.reason


def test_resource_newer_than_bundle_copy_refuses(tmp_path):
    paths = _panel_tree(tmp_path)
    os.utime(paths["resource"], (OLD + 1, OLD + 1))
    verdict = panel_freshness(tmp_path)
    assert not verdict.ok
    assert "newer than its bundle copy" in verdict.reason


def test_a_stray_file_beside_the_declared_resources_is_ignored(tmp_path):
    """`Package.swift` copies `Resources/cast` and `Resources/brand`, not
    `Resources/` itself, so a file dropped directly in there reaches no bundle.
    Demanding it would refuse every build for ever, and `rm -rf panel/.build`
    would not help — which is the one refusal a person cannot act on."""
    paths = _panel_tree(tmp_path)
    _touch(tmp_path / "panel" / "Sources" / "BobPanel" / "Resources" / ".DS_Store",
           NEW, b"finder litter")
    assert build_check.panel_freshness(tmp_path).ok
    assert paths["bundle"].is_dir()


def test_a_dotfile_inside_a_declared_resource_is_ignored(tmp_path):
    _panel_tree(tmp_path)
    _touch(tmp_path / "panel" / "Sources" / "BobPanel" / "Resources" / "cast" / ".DS_Store",
           NEW, b"finder litter")
    assert build_check.panel_freshness(tmp_path).ok


def test_declared_resource_dirs_come_from_the_manifest(tmp_path):
    _panel_tree(tmp_path)
    brand = tmp_path / "panel" / "Sources" / "BobPanel" / "Resources" / "brand"
    _touch(brand / "logo.png", OLD, b"brand")
    names = [p.name for p in build_check.panel_resource_dirs(tmp_path)]
    assert names == ["cast", "brand"]
    # brand is declared but absent from the bundle -> refused
    verdict = build_check.panel_freshness(tmp_path)
    assert not verdict.ok and "brand" in verdict.reason


def test_an_undeclared_resource_dir_is_not_demanded(tmp_path):
    _panel_tree(tmp_path)
    _touch(tmp_path / "panel" / "Sources" / "BobPanel" / "Resources" / "scratch" / "n.png",
           NEW, b"nope")
    assert build_check.panel_freshness(tmp_path).ok


def test_a_repo_root_with_a_dot_component_still_checks_resources(tmp_path):
    """`build.sh` calls the helper with `$SCRIPT_DIR/..`, so the root it passes
    carries a literal `..`. A per-component dotfile test on the absolute path
    reads that as hidden and skips every resource — the gate then reports
    success having looked at nothing, which is the one failure shape a green
    suite cannot see."""
    paths = _panel_tree(tmp_path)
    via_dots = tmp_path / "panel" / ".."
    assert len(list(build_check._resource_files(via_dots))) == 1
    assert build_check.panel_freshness(via_dots).ok

    paths["bundle_resource"].unlink()
    verdict = build_check.panel_freshness(via_dots)
    assert not verdict.ok and "absent from the bundle" in verdict.reason


def test_equal_mtimes_pass(tmp_path):
    """SwiftPM preserves resource mtimes and a link inside the same second as
    a save is legal: `==` is fresh on both halves."""
    paths = _panel_tree(tmp_path, binary_mtime=OLD, source_mtime=OLD)
    assert panel_freshness(tmp_path).ok
    os.utime(paths["bundle_resource"], (OLD, OLD))
    assert panel_freshness(tmp_path).ok


def test_explicit_binary_and_bundle_paths(tmp_path):
    _panel_tree(tmp_path)
    other_bin = _touch(tmp_path / "elsewhere" / "BobPanel", NEW)
    other_bundle = tmp_path / "elsewhere" / "bundle"
    _touch(other_bundle / "cast" / "a.png", OLD, b"png1")
    assert panel_freshness(tmp_path, binary=other_bin, bundle=other_bundle).ok
    assert not panel_freshness(tmp_path, binary=tmp_path / "nope").ok


# --- extension -------------------------------------------------------------


def _write_vsix(path: Path, version: str, mtime: float = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("extension/package.json", json.dumps({"name": "x", "version": version}))
        archive.writestr("extension.vsixmanifest", "<xml/>")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


_EXT_DEV_DEPS = {"esbuild": "^0.23.0", "@vscode/vsce": "^3.0.0"}


def _ext_tree(root: Path, manifest_version: str, archive_versions=(), mtime: float = None,
              lockfile: bool = True) -> Path:
    ext = root / "vscode-extension"
    ext.mkdir(parents=True, exist_ok=True)
    (ext / "package.json").write_text(json.dumps({"name": "dark-army-ide",
                                                  "version": manifest_version,
                                                  "devDependencies": dict(_EXT_DEV_DEPS)}))
    if lockfile:
        _write_lock(ext, manifest_version)
    for version in archive_versions:
        _write_vsix(ext / f"dark-army-ide-{version}.vsix", version, mtime)
    return ext


def _write_lock(ext: Path, version: str, dev_deps=None, lockfile_version: int = 3) -> Path:
    path = ext / "package-lock.json"
    path.write_text(json.dumps({
        "name": "dark-army-ide",
        "version": version,
        "lockfileVersion": lockfile_version,
        "requires": True,
        "packages": {"": {"name": "dark-army-ide", "version": version,
                          "devDependencies": dict(_EXT_DEV_DEPS if dev_deps is None
                                                  else dev_deps)}},
    }))
    return path


def test_package_version_reads_manifest(tmp_path):
    ext = _ext_tree(tmp_path, "0.1.11")
    assert package_version(ext / "package.json") == Verdict(True, "0.1.11",
                                                            str(ext / "package.json"))


@pytest.mark.parametrize("body", [None, "{not json", '{"name": "x"}', '{"version": ""}', "[]"])
def test_package_version_refuses_missing_or_invalid(tmp_path, body):
    manifest = tmp_path / "package.json"
    if body is not None:
        manifest.write_text(body)
    verdict = package_version(manifest)
    assert not verdict.ok
    assert str(manifest) in verdict.reason


def test_archive_version_reads_inside_the_zip(tmp_path):
    assert archive_version(_write_vsix(tmp_path / "a.vsix", "0.1.11")) == "0.1.11"


def test_archive_version_is_empty_on_any_failure(tmp_path):
    assert archive_version(tmp_path / "missing.vsix") == ""
    not_a_zip = tmp_path / "junk.vsix"
    not_a_zip.write_bytes(b"not a zip")
    assert archive_version(not_a_zip) == ""
    with zipfile.ZipFile(tmp_path / "no-manifest.vsix", "w") as archive:
        archive.writestr("extension/README.md", "hi")
    assert archive_version(tmp_path / "no-manifest.vsix") == ""


def test_vsix_match_returns_the_matching_file(tmp_path):
    ext = _ext_tree(tmp_path, "0.1.11", ["0.1.10", "0.1.11"])
    verdict = vsix_match(ext)
    assert verdict.ok
    assert verdict.path == str((ext / "dark-army-ide-0.1.11.vsix").resolve())


def test_vsix_match_refuses_when_only_other_versions_exist(tmp_path):
    ext = _ext_tree(tmp_path, "0.1.11", ["0.1.9", "0.1.10"])
    verdict = vsix_match(ext)
    assert not verdict.ok
    assert "0.1.11" in verdict.reason
    assert "0.1.9, 0.1.10" in verdict.reason


def test_vsix_match_refuses_when_no_package_at_all(tmp_path):
    verdict = vsix_match(_ext_tree(tmp_path, "0.1.11"))
    assert not verdict.ok
    assert "packages present: none" in verdict.reason


def test_vsix_match_refuses_filename_match_with_other_version_inside(tmp_path):
    ext = _ext_tree(tmp_path, "0.1.11")
    _write_vsix(ext / "dark-army-ide-0.1.11.vsix", "0.1.10")
    verdict = vsix_match(ext)
    assert not verdict.ok
    assert "0.1.10" in verdict.reason and "0.1.11" in verdict.reason


def test_vsix_match_built_since(tmp_path):
    ext = _ext_tree(tmp_path, "0.1.11", ["0.1.11"], mtime=OLD)
    assert vsix_match(ext, built_since=OLD - 1).ok
    assert vsix_match(ext, built_since=OLD).ok, "same second as the build start counts"
    refused = vsix_match(ext, built_since=OLD + 1)
    assert not refused.ok
    assert "not built by this run" in refused.reason


def test_vsix_match_refuses_bad_manifest(tmp_path):
    ext = tmp_path / "vscode-extension"
    ext.mkdir()
    assert not vsix_match(ext).ok
    (ext / "package.json").write_text("{oops")
    assert not vsix_match(ext).ok


# --- lockfile --------------------------------------------------------------


def test_lock_match_accepts_a_lockfile_that_describes_the_manifest(tmp_path):
    ext = _ext_tree(tmp_path, "0.1.11")
    verdict = lock_match(ext)
    assert verdict.ok, verdict.reason
    assert verdict.reason == "lockfile matches manifest 0.1.11"
    assert verdict.path == str((ext / "package-lock.json").resolve())


def test_lock_match_refuses_a_missing_lockfile(tmp_path):
    ext = _ext_tree(tmp_path, "0.1.11", lockfile=False)
    verdict = lock_match(ext)
    assert not verdict.ok
    assert "lockfile missing" in verdict.reason
    assert "npm install" in verdict.reason


def test_lock_match_refuses_an_unreadable_lockfile(tmp_path):
    ext = _ext_tree(tmp_path, "0.1.11")
    (ext / "package-lock.json").write_text("{not json")
    verdict = lock_match(ext)
    assert not verdict.ok
    assert "unreadable" in verdict.reason
    assert "npm install" in verdict.reason


def test_lock_match_refuses_a_lockfile_version_npm_ci_cannot_use(tmp_path):
    """`npm ci` reads the `packages` map, which arrived in lockfileVersion 2.
    A v1 lockfile installs nothing and says so in npm's words, not ours."""
    ext = _ext_tree(tmp_path, "0.1.11")
    _write_lock(ext, "0.1.11", lockfile_version=1)
    verdict = lock_match(ext)
    assert not verdict.ok
    assert "lockfileVersion 2 or newer" in verdict.reason


def test_lock_match_refuses_a_lockfile_with_no_root_entry(tmp_path):
    ext = _ext_tree(tmp_path, "0.1.11")
    (ext / "package-lock.json").write_text(json.dumps(
        {"version": "0.1.11", "lockfileVersion": 3, "packages": {"node_modules/x": {}}}))
    verdict = lock_match(ext)
    assert not verdict.ok
    assert 'packages[""]' in verdict.reason


def test_lock_match_refuses_a_version_the_manifest_does_not_declare(tmp_path):
    ext = _ext_tree(tmp_path, "0.1.11")
    _write_lock(ext, "0.1.10")
    verdict = lock_match(ext)
    assert not verdict.ok
    assert "0.1.10" in verdict.reason and "0.1.11" in verdict.reason


def test_lock_match_refuses_a_dependency_the_lockfile_has_never_seen(tmp_path):
    ext = _ext_tree(tmp_path, "0.1.11")
    manifest = json.loads((ext / "package.json").read_text())
    manifest["devDependencies"]["typescript"] = "^5.4.0"
    (ext / "package.json").write_text(json.dumps(manifest))
    verdict = lock_match(ext)
    assert not verdict.ok
    assert "not in the lockfile" in verdict.reason
    assert "typescript" in verdict.reason


def test_lock_match_refuses_the_same_key_with_a_different_range(tmp_path):
    """The case a key-set comparison alone would miss, and the one that
    actually ships a wrong version: the name is in both files and the range
    moved under it."""
    ext = _ext_tree(tmp_path, "0.1.11")
    _write_lock(ext, "0.1.11", dev_deps={**_EXT_DEV_DEPS, "esbuild": "^0.19.0"})
    verdict = lock_match(ext)
    assert not verdict.ok
    assert "different range" in verdict.reason
    assert "^0.23.0" in verdict.reason and "^0.19.0" in verdict.reason


def test_lockfile_digest_is_hex_and_moves_with_the_bytes(tmp_path):
    ext = _ext_tree(tmp_path, "0.1.11")
    lock = ext / "package-lock.json"
    digest = lockfile_digest(lock)
    assert len(digest) == 64
    assert digest == digest.lower()
    assert all(c in "0123456789abcdef" for c in digest)
    assert lockfile_digest(tmp_path / "nope.json") == ""
    lock.write_text(lock.read_text() + " ")
    assert lockfile_digest(lock) != digest


# --- build manifest --------------------------------------------------------


def test_build_manifest_has_every_key_with_the_documented_shape(tmp_path):
    ext = _ext_tree(tmp_path, "0.1.11")
    manifest = build_manifest(tmp_path, ext, vsix=ext / "dark-army-ide-0.1.11.vsix",
                              mode="dev", extension_source="committed")
    assert set(manifest) == {"schema", "built_at", "mode", "app_version", "extension", "tools"}
    assert manifest["schema"] == 1
    assert manifest["mode"] == "dev"
    assert set(manifest["extension"]) == {"version", "vsix", "source", "lockfile_sha256"}
    assert manifest["extension"]["version"] == "0.1.11"
    assert manifest["extension"]["source"] == "committed"
    # A basename, never a path: the note says which package, not where it was.
    assert manifest["extension"]["vsix"] == "dark-army-ide-0.1.11.vsix"
    assert len(manifest["extension"]["lockfile_sha256"]) == 64
    assert set(manifest["tools"]) == {"node", "npm", "swift", "python"}
    assert all(isinstance(v, str) for v in manifest["tools"].values())
    assert manifest["tools"]["python"] == sys.version.split()[0]
    # A fake tree carries no baked version file: stated as empty, never absent.
    assert manifest["app_version"] == ""
    stamped = datetime.strptime(manifest["built_at"], "%Y-%m-%dT%H:%M:%SZ")
    assert stamped.year >= 2024


def test_build_manifest_states_an_unresolvable_tool_as_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(build_check, "_tool_version", lambda argv: "")
    manifest = build_manifest(tmp_path, _ext_tree(tmp_path, "0.1.11"))
    assert manifest["tools"]["node"] == "" and "node" in manifest["tools"]
    assert manifest["extension"]["vsix"] == ""


def test_build_manifest_reads_the_baked_version_without_importing_it(tmp_path):
    version_file = tmp_path / "host" / "dark_army_menubar" / "_version_info.py"
    version_file.parent.mkdir(parents=True)
    version_file.write_text('raise RuntimeError("never imported")\nVERSION = "v1.2.3"\n')
    manifest = build_manifest(tmp_path, _ext_tree(tmp_path, "0.1.11"))
    assert manifest["app_version"] == "v1.2.3"


def test_write_manifest_round_trips_through_json(tmp_path):
    ext = _ext_tree(tmp_path, "0.1.11")
    manifest = build_manifest(tmp_path, ext)
    out = tmp_path / "nested" / "build-manifest.json"
    write_manifest(out, manifest)
    text = out.read_text()
    assert text.endswith("\n")
    assert json.loads(text) == manifest


def test_main_lock_prints_the_path_or_the_reason(tmp_path, capsys):
    ext = _ext_tree(tmp_path, "0.1.11")
    assert main(["lock", "--ext-dir", str(ext)]) == 0
    out, err = capsys.readouterr()
    assert out.strip() == str((ext / "package-lock.json").resolve())
    assert err == ""
    (ext / "package-lock.json").unlink()
    assert main(["lock", "--ext-dir", str(ext)]) == 2
    out, err = capsys.readouterr()
    assert out == ""
    assert err.startswith("build-check: extension lockfile missing")


def test_main_manifest_writes_the_file_and_prints_it(tmp_path, capsys):
    ext = _ext_tree(tmp_path, "0.1.11")
    out_path = tmp_path / "build-manifest.json"
    assert main(["manifest", "--out", str(out_path), "--repo-root", str(tmp_path),
                 "--ext-dir", str(ext), "--mode", "strict",
                 "--extension-source", "none"]) == 0
    out, err = capsys.readouterr()
    assert out.strip() == str(out_path.resolve())
    assert err == ""
    written = json.loads(out_path.read_text())
    assert written["schema"] == 1 and written["extension"]["source"] == "none"


def test_main_manifest_refuses_a_path_it_cannot_write(tmp_path, capsys):
    ext = _ext_tree(tmp_path, "0.1.11")
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    assert main(["manifest", "--out", str(blocker / "build-manifest.json"),
                 "--repo-root", str(tmp_path), "--ext-dir", str(ext)]) == 2
    out, err = capsys.readouterr()
    assert out == ""
    assert err.startswith("build-check: build manifest not written")

# --- CLI -------------------------------------------------------------------


def test_main_panel_exit_codes(tmp_path, capsys):
    paths = _panel_tree(tmp_path)
    assert main(["panel", "--repo-root", str(tmp_path)]) == 0
    assert capsys.readouterr() == ("", "")
    paths["binary"].unlink()
    assert main(["panel", "--repo-root", str(tmp_path)]) == 2
    out, err = capsys.readouterr()
    assert out == ""
    assert err.startswith("build-check: panel binary missing")


def test_main_vsix_prints_path_and_exit_codes(tmp_path, capsys):
    ext = _ext_tree(tmp_path, "0.1.11", ["0.1.11"], mtime=OLD)
    assert main(["vsix", "--ext-dir", str(ext)]) == 0
    out, err = capsys.readouterr()
    assert out.strip() == str((ext / "dark-army-ide-0.1.11.vsix").resolve())
    assert err == ""
    assert main(["vsix", "--ext-dir", str(ext), "--built-since", str(OLD + 1)]) == 2
    out, err = capsys.readouterr()
    assert out == ""
    assert err.startswith("build-check: ")


def test_helper_is_stdlib_only_and_never_imported_by_the_runtime():
    text = (ROOT / "host" / "dark_army_menubar" / "build_check.py").read_text()
    # `_version_info` is read with a regex, never imported: importing it would
    # tie this helper to a file py2app writes, and the baked module is exactly
    # what a fake tree does not have.
    for name in ("rumps", "objc", "Foundation", "dev_build", "vscode_reveal",
                 "vscode_extension", "_version_info"):
        assert f"import {name}" not in text and f"from {name}" not in text, name
    assert "build_check" not in (ROOT / "host" / "dark_army_menubar" / "app.py").read_text()


# --- repository invariant -------------------------------------------------


def test_the_real_manifests_resource_dirs_are_the_ones_we_expect():
    """The resource half of the panel gate is a regex over `Package.swift`. If
    somebody reformats that file the parse can come back empty and the gate
    goes quiet — this is the tripwire for exactly that."""
    names = sorted(p.name for p in build_check.panel_resource_dirs(ROOT))
    assert names == ["brand", "portraits", "workshop"]
    assert build_check.manifest_declares_resources(ROOT)


def test_an_unparseable_manifest_refuses_rather_than_skipping_the_check(tmp_path):
    _panel_tree(tmp_path)
    # A manifest that declares resources in a form the regex cannot read.
    _touch(tmp_path / "panel" / "Package.swift", OLD,
           b'let art = "Resources/cast"\n.target(name: "BobPanel", resources: [.copy(art)])\n')
    verdict = build_check.panel_freshness(tmp_path)
    assert not verdict.ok
    assert "could not be read" in verdict.reason


def test_a_manifest_with_no_resources_at_all_still_passes(tmp_path):
    _panel_tree(tmp_path)
    _touch(tmp_path / "panel" / "Package.swift", OLD, b'.target(name: "BobPanel")\n')
    assert build_check.panel_freshness(tmp_path).ok


def test_repository_holds_exactly_the_manifests_vsix():
    """One committed package, and it is the manifest's: nothing old to fall
    back on by accident."""
    manifest = package_version(EXT_DIR / "package.json")
    assert manifest.ok, manifest.reason
    names = sorted(p.name for p in EXT_DIR.glob("dark-army-ide-*.vsix"))
    assert names == [f"dark-army-ide-{manifest.reason}.vsix"]
    assert archive_version(EXT_DIR / names[0]) == manifest.reason
    tracked = subprocess.run(["git", "ls-files", "vscode-extension"], cwd=ROOT,
                             capture_output=True, text=True)
    if tracked.returncode == 0:
        vsix = [line for line in tracked.stdout.splitlines() if line.endswith(".vsix")]
        assert vsix == [f"vscode-extension/{names[0]}"]


def test_build_sh_greps():
    text = BUILD_SH.read_text()
    assert "sort -V" not in text
    assert text.count("--dev") >= 4
    assert text.count("--check-only") >= 2
    assert "REFUSED" in text
    assert '"${1:-}" = "--install"' not in text
    assert "dark_army_menubar.build_check" in text
    # The install is the committed lockfile and the packager comes out of it:
    # no `npm install` resolving fresh versions, no `npx` reaching the network.
    assert "npm install" not in text
    assert "npx " not in text
    assert "npm ci" in text
    assert "node_modules/.bin/vsce" in text
    # The version is decided once, above everything that writes in the tree,
    # and handed to setup.py rather than recomputed after the build dirtied it.
    assert "BOB_BUILD_VERSION" in text
    assert text.index("BOB_BUILD_VERSION") < text.index("assets/portraits")
    assert text.count("release-manifest.json") == 1
    # The developer path ships only into the copy installed on this machine.
    stamp = text.index('Resources/repo-root"')
    assert text.index("codesign --force") > stamp, "stamp written after signing"
    assert "INSTALL" in text[text.rindex("if ", 0, stamp):stamp], \
        "the repo-root stamp is not behind --install"
    # The version gate has an escape that is not --dev, and it is in the
    # argument loop and the usage line rather than silently accepted.
    assert "--allow-untagged)" in text
    assert "[--allow-untagged]" in text
    # The parsed version never carries stderr with it.
    assert "2>&1)\"" not in text.split("read -r RELEASE_VERSION")[0]
    # The manifest reports the panel stamp that happened, not the one intended.
    assert "--panel-version" in text
    assert text.index("PANEL_STAMPED=\"$PLIST_VERSION\"") < text.index("--panel-version")
    # The version refusal is the one a developer meets on an ordinary day, so
    # its footer must name the escape that keeps every staleness gate strict
    # rather than the one that reinstates the warn-and-continue fallbacks.
    assert "${DIE_HINT:---dev}" in text
    assert 'DIE_HINT="--allow-untagged" die "version:' in text


# --- build.sh under --check-only -------------------------------------------

_SWIFT_STUB = r'''#!/bin/bash
# Fake `swift build`: behaves like the real one for the freshness gate.
if [ "${FAKE_SWIFT_FAIL:-0}" = 1 ]; then
    echo "error: compile failed (fake)" >&2
    exit 1
fi
if [ "${FAKE_SWIFT_NOOP:-0}" = 1 ]; then
    exit 0
fi
# The first build links nothing (a manifest edit SwiftPM ignores); a build
# with the old binary gone links it, as the real one does.
if [ "${FAKE_SWIFT_NOOP_WHILE_BINARY:-0}" = 1 ] && [ -f .build/release/BobPanel ]; then
    exit 0
fi
mkdir -p .build/release/BobPanel_BobPanel.bundle
printf 'binary' > .build/release/BobPanel
chmod +x .build/release/BobPanel
# SwiftPM's resource copy preserves the source mtime: cp -p.
cp -Rp Sources/BobPanel/Resources/. .build/release/BobPanel_BobPanel.bundle/
exit 0
'''

_NODE_STUB = r'''#!/bin/bash
# `node -e '<script>' <package.json>`: print the manifest's version.
sed -n 's/.*"version": *"\([^"]*\)".*/\1/p' "$3"
'''

# `vsce package -o <name> ...`: write a real zip carrying a version inside.
# Installed into node_modules/.bin by the npm stub's `ci`, exactly as a real
# `npm ci` installs the packager the build then runs.
_VSCE_STUB = r"""#!/bin/bash
out=""
while [ $# -gt 0 ]; do
    if [ "$1" = "-o" ]; then out="$2"; shift; fi
    shift
done
[ -n "$out" ] || exit 1
version="${FAKE_VSCE_VERSION:-$(sed -n 's/.*"version": *"\([^"]*\)".*/\1/p' package.json)}"
"$FAKE_PY" - "$out" "$version" <<'EOF'
import json, sys, zipfile
with zipfile.ZipFile(sys.argv[1], "w") as z:
    z.writestr("extension/package.json", json.dumps({"version": sys.argv[2]}))
EOF
"""

# `npm ci` refuses without a lockfile, exactly as the real one does, and
# otherwise installs the packager into node_modules/.bin; `npm run build`
# writes the bundle. FAKE_NPM_CI_EMPTY is a "successful" ci that installs
# nothing — the case build.sh's vsce guard exists for.
_NPM_STUB = r"""#!/bin/bash
case "${1:-}" in
    ci)
        [ -f package-lock.json ] || {
            echo "npm error code EUSAGE: no package-lock.json" >&2
            exit 1
        }
        if [ "${FAKE_NPM_CI_EMPTY:-0}" != 1 ]; then
            mkdir -p node_modules/.bin
            printf '%s' "$FAKE_VSCE_BODY" > node_modules/.bin/vsce
            chmod +x node_modules/.bin/vsce
        fi
        ;;
    run)
        if [ "${2:-}" = "build" ]; then
            mkdir -p dist
            printf 'bundle' > dist/extension.js
        fi
        ;;
esac
exit 0
"""
# A `git init` in a test must not read the developer's own configuration: a
# machine with `commit.gpgsign` on would hang the suite waiting for a key.
GIT_ENV = {
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "GIT_AUTHOR_NAME": "Dark Army Test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "Dark Army Test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
}


def _git(tree: Path, *args):
    env = {**os.environ, **GIT_ENV}
    return subprocess.run(
        ["git", "-c", "user.name=Dark Army Test", "-c", "user.email=test@example.invalid",
         "-c", "commit.gpgsign=false", *args],
        cwd=str(tree), env=env, capture_output=True, text=True, timeout=30)


def _git_checkout(tree: Path, tag: str = "v9.9.9") -> None:
    """Make the fake tree a real, clean, tagged checkout.

    `build.sh` now runs the release gate before anything else, and a strict run
    refuses a tree that is not a checkout, is untagged, or is dirty. The
    `.gitignore` covers exactly what the run itself writes. `*.vsix` is
    ignored here although the real repo commits it: several cases below plant
    a package on disk to drive the freshness gate, and a tree dirtied by the
    fixture would refuse at the version gate before reaching the one under
    test. The real dirty-tree refusal has its own case."""
    (tree / ".gitignore").write_text(
        ".venv/\n.build/\nnode_modules/\ndist/\nbuild/\n*.vsix\n")
    assert _git(tree, "init", "-q", ".").returncode == 0
    assert _git(tree, "add", "-A").returncode == 0
    assert _git(tree, "commit", "-qm", "fake tree").returncode == 0
    if tag:
        assert _git(tree, "tag", tag).returncode == 0


def _stub(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def fake_repo(tmp_path):
    tree = tmp_path / "tree"
    host = tree / "host"
    host.mkdir(parents=True)
    shutil.copy(BUILD_SH, host / "build.sh")
    (host / "build.sh").chmod(0o755)
    (host / ".venv" / "bin").mkdir(parents=True)
    os.symlink(sys.executable, host / ".venv" / "bin" / "python")
    _touch(tree / "assets" / "cast" / "x.png", OLD, b"cast")
    _touch(tree / "panel" / "Package.swift", OLD, _PACKAGE_SWIFT)
    _touch(tree / "panel" / "Sources" / "BobPanel" / "main.swift", OLD)
    _touch(tree / "panel" / "Sources" / "BobPanel" / "Info.plist", OLD)
    _touch(tree / "panel" / "Sources" / "BobPanel" / "Resources" / "cast" / "x.png", OLD, b"cast")
    _ext_tree(tree, "9.9.9")
    _git_checkout(tree)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _stub(bin_dir / "swift", _SWIFT_STUB)
    _stub(bin_dir / "node", _NODE_STUB)
    _stub(bin_dir / "npm", _NPM_STUB)
    home = tmp_path / "home"
    home.mkdir()
    # macOS ships a `/usr/bin/swift` shim, so `/usr/bin` cannot be on PATH for
    # a "no toolchain" case: link only the tools build.sh itself needs.
    usrbin = tmp_path / "usrbin"
    usrbin.mkdir()
    # `git` because the release gate asks the tree for its version; the rest
    # are what build.sh itself shells out to.
    # mktemp: build.sh's version gate captures stderr to a temp file rather than
    # a $$-predictable name, so the fixture's PATH has to carry it too.
    for tool in ("basename", "dirname", "rsync", "sed", "env", "git", "mktemp"):
        real = Path("/usr/bin") / tool
        if real.exists():
            os.symlink(real, usrbin / tool)
    return {"tree": tree, "host": host, "bin": bin_dir, "usrbin": usrbin, "home": home}


def _run(repo, *args, env=None, without=()):
    bin_dir = repo["bin"]
    if without:
        bin_dir = repo["bin"].parent / ("bin-without-" + "-".join(without))
        bin_dir.mkdir(exist_ok=True)
        for tool in ("swift", "node", "npm"):
            if tool not in without:
                shutil.copy(repo["bin"] / tool, bin_dir / tool)
    full_env = {
        "PATH": f"{bin_dir}:{repo['usrbin']}:/bin",
        # The Homebrew probe would otherwise reach this machine's real npm and
        # defeat every "tool is missing" case. Tests that want the probe set it.
        "BOB_BUILD_PATH_DIRS": str(repo["home"] / "no-such-prefix"),
        "PYTHONPATH": str(ROOT / "host"),
        "FAKE_PY": sys.executable,
        # What the npm stub's `ci` installs as node_modules/.bin/vsce. It is an
        # env var rather than a file so a case can swap the packager without
        # touching the tree the build runs in.
        "FAKE_VSCE_BODY": _VSCE_STUB,
        "HOME": str(repo["home"]),
        "TMPDIR": str(repo["home"]),
        **GIT_ENV,
    }
    full_env.update(env or {})
    return subprocess.run([str(repo["host"] / "build.sh"), *args], cwd=repo["host"],
                          env=full_env, capture_output=True, text=True, timeout=60)


def _stale_binary(repo):
    """An old binary and bundle left in .build from a previous run."""
    build = repo["tree"] / "panel" / ".build" / "release"
    _touch(build / "BobPanel", OLD - 10)
    _touch(build / "BobPanel_BobPanel.bundle" / "cast" / "x.png", OLD, b"cast")


def test_strict_all_good_passes(fake_repo):
    result = _run(fake_repo, "--check-only")
    assert result.returncode == 0, result.stderr
    assert "Checks passed" in result.stdout
    assert "dark-army-ide-9.9.9.vsix" in result.stdout
    assert "REFUSED" not in result.stderr
    assert "WARNING" not in result.stderr
    # The version is decided before anything else and named out loud.
    assert "==> Version: v9.9.9 (bundle 9.9.9)" in result.stdout


def test_strict_refuses_an_untagged_tree(fake_repo):
    """The release gate, from the script. An untagged HEAD is every ordinary
    day of development, so the refusal has to name the escape hatch."""
    assert _git(fake_repo["tree"], "tag", "-d", "v9.9.9").returncode == 0
    result = _run(fake_repo, "--check-only")
    assert result.returncode == 1
    assert "REFUSED" in result.stderr and "tag" in result.stderr
    # The footer names --allow-untagged, not --dev: the version refusal fires on
    # every ordinary day, and --dev would also switch off the staleness gates.
    assert "--allow-untagged" in result.stderr
    assert "Checks passed" not in result.stdout


def test_strict_refuses_a_dirty_tree_and_names_a_path(fake_repo):
    (fake_repo["tree"] / "panel" / "Sources" / "BobPanel" / "extra.swift").write_text("//")
    result = _run(fake_repo, "--check-only")
    assert result.returncode == 1
    assert "REFUSED" in result.stderr and "uncommitted changes" in result.stderr
    assert "extra.swift" in result.stderr


def test_strict_refuses_a_tree_that_is_not_a_checkout(fake_repo):
    shutil.rmtree(fake_repo["tree"] / ".git")
    result = _run(fake_repo, "--check-only")
    assert result.returncode == 1
    assert "REFUSED" in result.stderr and "not a git checkout" in result.stderr


def test_allow_untagged_passes_the_version_gate_and_nothing_else(fake_repo):
    """The in-app Rebuild verb's escape. It gets past an untagged tree — every
    ordinary day of development — while the panel and extension gates stay
    strict, which is the whole reason it is not `--dev`."""
    assert _git(fake_repo["tree"], "tag", "-d", "v9.9.9").returncode == 0
    result = _run(fake_repo, "--allow-untagged", "--check-only")
    assert result.returncode == 0, result.stderr
    assert "REFUSED" not in result.stderr
    assert "(bundle 0.0.0)" in result.stdout
    assert "Checks passed" in result.stdout


def test_allow_untagged_keeps_the_panel_gate_strict(fake_repo):
    """`--dev` would warn and carry on here. This flag must not."""
    assert _git(fake_repo["tree"], "tag", "-d", "v9.9.9").returncode == 0
    _stale_binary(fake_repo)
    os.utime(fake_repo["tree"] / "panel" / "Sources" / "BobPanel" / "main.swift",
             (OLD + 50,) * 2)
    result = _run(fake_repo, "--allow-untagged", "--check-only",
                  env={"FAKE_SWIFT_NOOP": "1"})
    assert result.returncode == 1
    assert "REFUSED" in result.stderr and "stale" in result.stderr


def test_dev_builds_an_unnumbered_tree_and_says_so(fake_repo):
    assert _git(fake_repo["tree"], "tag", "-d", "v9.9.9").returncode == 0
    result = _run(fake_repo, "--dev", "--check-only")
    assert result.returncode == 0, result.stderr
    assert "REFUSED" not in result.stderr
    assert "==> Version: " in result.stdout
    # The descriptive string, and a plist form that borrows no number.
    assert "(bundle 0.0.0)" in result.stdout
    assert "Checks passed" in result.stdout


def test_strict_refuses_a_failed_panel_compile_with_a_stale_binary_present(fake_repo):
    _stale_binary(fake_repo)
    result = _run(fake_repo, "--check-only", env={"FAKE_SWIFT_FAIL": "1"})
    assert result.returncode == 1
    assert "REFUSED" in result.stderr and "panel" in result.stderr
    assert "--dev" in result.stderr
    assert "Checks passed" not in result.stdout


def test_strict_refuses_a_compile_that_left_an_old_binary(fake_repo):
    _stale_binary(fake_repo)
    source = fake_repo["tree"] / "panel" / "Sources" / "BobPanel" / "main.swift"
    os.utime(source, (OLD + 50, OLD + 50))
    result = _run(fake_repo, "--check-only", env={"FAKE_SWIFT_NOOP": "1"})
    assert result.returncode == 1
    assert "REFUSED" in result.stderr and "stale" in result.stderr
    assert "main.swift" in result.stderr


def test_a_manifest_edit_that_links_nothing_is_relinked_once(fake_repo):
    """22 Sep 2026: a rename inside Package.swift left the old binary in
    place and every Rebuild was refused as stale. The build sets the old
    binary aside, links once more, and passes on the fresh one."""
    _stale_binary(fake_repo)
    os.utime(fake_repo["tree"] / "panel" / "Package.swift", (OLD + 50,) * 2)
    result = _run(fake_repo, "--check-only", env={"FAKE_SWIFT_NOOP_WHILE_BINARY": "1"})
    assert result.returncode == 0, result.stderr
    assert "relinking it" in result.stdout
    assert "Checks passed" in result.stdout
    release = fake_repo["tree"] / "panel" / ".build" / "release"
    assert (release / "BobPanel").read_bytes() == b"binary"
    assert not (release / "BobPanel.stale").exists()


def test_strict_refuses_a_package_carrying_another_version(fake_repo):
    result = _run(fake_repo, "--check-only", env={"FAKE_VSCE_VERSION": "1.0.0"})
    assert result.returncode == 1
    assert "REFUSED" in result.stderr
    assert "9.9.9" in result.stderr and "1.0.0" in result.stderr


def test_strict_refuses_a_package_not_built_this_run(fake_repo):
    ext = fake_repo["tree"] / "vscode-extension"
    _write_vsix(ext / "dark-army-ide-9.9.9.vsix", "9.9.9", mtime=OLD)
    # The packager "succeeds" without writing anything: the old file is what
    # is left, and only the built-since gate can tell.
    result = _run(fake_repo, "--check-only",
                  env={"FAKE_VSCE_BODY": "#!/bin/bash\nexit 0\n"})
    assert result.returncode == 1
    assert "not built by this run" in result.stderr


def test_strict_refuses_without_swift(fake_repo):
    result = _run(fake_repo, "--check-only", without=("swift",))
    assert result.returncode == 1
    assert "REFUSED" in result.stderr and "Swift" in result.stderr
    assert "--dev" in result.stderr


def test_strict_refuses_without_npm(fake_repo):
    result = _run(fake_repo, "--check-only", without=("npm",))
    assert result.returncode == 1
    assert "REFUSED" in result.stderr and "npm" in result.stderr


def test_dev_without_toolchains_takes_the_committed_matching_vsix(fake_repo):
    ext = fake_repo["tree"] / "vscode-extension"
    _write_vsix(ext / "dark-army-ide-9.9.9.vsix", "9.9.9", mtime=OLD)
    result = _run(fake_repo, "--dev", "--check-only", without=("swift", "npm"))
    assert result.returncode == 0, result.stderr
    assert "WARNING: no Swift toolchain" in result.stderr
    assert "REFUSED" not in result.stderr
    assert "dark-army-ide-9.9.9.vsix" in result.stdout
    assert "Checks passed" in result.stdout


def test_dev_without_toolchains_ignores_a_mismatching_committed_vsix(fake_repo):
    ext = fake_repo["tree"] / "vscode-extension"
    _write_vsix(ext / "dark-army-ide-9.9.8.vsix", "9.9.8", mtime=OLD)
    result = _run(fake_repo, "--dev", "--check-only", without=("swift", "npm"))
    assert result.returncode == 0, result.stderr
    assert "WARNING: no vscode-extension .vsix" in result.stderr
    assert "extension none" in result.stdout


def test_dev_warns_on_a_stale_panel_and_continues(fake_repo):
    _stale_binary(fake_repo)
    os.utime(fake_repo["tree"] / "panel" / "Sources" / "BobPanel" / "main.swift", (OLD + 50,) * 2)
    result = _run(fake_repo, "--dev", "--check-only",
                  env={"FAKE_SWIFT_NOOP": "1"}, without=("npm",))
    assert result.returncode == 0, result.stderr
    assert "WARNING: panel is stale" in result.stderr
    assert "REFUSED" not in result.stderr


def test_flags_combine_in_any_order(fake_repo):
    for args in (("--check-only", "--install", "--dev"), ("--dev", "--install", "--check-only")):
        result = _run(fake_repo, *args)
        assert result.returncode == 0, result.stderr
        assert "Checks passed" in result.stdout


def test_unknown_flag_is_refused_with_usage(fake_repo):
    result = _run(fake_repo, "--bogus")
    assert result.returncode == 2
    assert result.stderr.strip().startswith("usage: ./build.sh")
    assert "Checks passed" not in result.stdout


def test_check_only_stops_before_py2app(fake_repo):
    """The gates run and the script exits before `import py2app`; the fake
    venv is the test interpreter, which may well lack it."""
    result = _run(fake_repo, "--check-only")
    assert result.returncode == 0, result.stderr
    assert "Building .app bundle" not in result.stdout
    assert not (fake_repo["host"] / "dist").exists()


def test_build_start_is_this_second_or_earlier(fake_repo):
    """A package finishing within the same second as the script start passes:
    the stub writes it immediately, so the comparison is exercised live."""
    before = int(time.time())
    result = _run(fake_repo, "--check-only")
    assert result.returncode == 0, result.stderr
    vsix = fake_repo["tree"] / "vscode-extension" / "dark-army-ide-9.9.9.vsix"
    assert vsix.stat().st_mtime >= before
    assert build_check.vsix_match(vsix.parent, built_since=before).ok


def test_the_path_probe_finds_tools_outside_path(fake_repo):
    """A menu-bar app started at login inherits launchd's PATH — /usr/bin:/bin
    and no Homebrew — so `Rebuild & Deploy` runs this script with node and npm
    installed but invisible. The probe is what keeps that from reading as a
    stale-component refusal."""
    prefix = fake_repo["home"] / "brewbin"
    prefix.mkdir()
    for tool in ("node", "npm"):
        shutil.copy(fake_repo["bin"] / tool, prefix / tool)
    result = _run(fake_repo, "--check-only", env={"BOB_BUILD_PATH_DIRS": str(prefix)},
                  without=("node", "npm"))
    assert result.returncode == 0, result.stderr + result.stdout
    assert "Checks passed" in result.stdout


def test_the_path_probe_skips_a_prefix_that_is_not_there(fake_repo):
    missing = fake_repo["home"] / "nope"
    result = _run(fake_repo, "--check-only", env={"BOB_BUILD_PATH_DIRS": str(missing)})
    assert result.returncode == 0, result.stderr
    assert str(missing) not in result.stdout
def _recommit(tree: Path) -> None:
    """The release gate refuses a dirty tree before the extension gates run, so
    a case that edits the lockfile has to leave the checkout clean again."""
    assert _git(tree, "add", "-A").returncode == 0
    assert _git(tree, "commit", "-qm", "lockfile case").returncode == 0
    # The gate also wants HEAD on an exact tag, and the commit just moved it.
    assert _git(tree, "tag", "-f", "v9.9.9").returncode == 0


def test_strict_refuses_a_missing_lockfile_before_the_install_runs(fake_repo):
    """The lock gate is Dark Army's sentence, not npm's EUSAGE wall — and it has to
    land before the install, or the person reads npm's output instead."""
    (fake_repo["tree"] / "vscode-extension" / "package-lock.json").unlink()
    _recommit(fake_repo["tree"])
    result = _run(fake_repo, "--check-only")
    assert result.returncode == 1
    assert "extension lockfile missing" in result.stderr
    assert "npm install" in result.stderr
    assert "EUSAGE" not in result.stderr
    assert "Building VS Code extension" not in result.stdout


def test_strict_refuses_a_lockfile_that_names_another_version(fake_repo):
    ext = fake_repo["tree"] / "vscode-extension"
    _write_lock(ext, "9.9.8")
    _recommit(fake_repo["tree"])
    result = _run(fake_repo, "--check-only")
    assert result.returncode == 1
    assert "9.9.8" in result.stderr and "9.9.9" in result.stderr
    assert "npm install" in result.stderr


def test_strict_refuses_a_lockfile_that_dropped_a_dependency(fake_repo):
    ext = fake_repo["tree"] / "vscode-extension"
    _write_lock(ext, "9.9.9", dev_deps={"esbuild": "^0.23.0"})
    _recommit(fake_repo["tree"])
    result = _run(fake_repo, "--check-only")
    assert result.returncode == 1
    assert "not in the lockfile" in result.stderr
    assert "@vscode/vsce" in result.stderr


def test_strict_refuses_an_install_that_left_no_packager(fake_repo):
    """`npx` used to fetch a packager off the network. Now it comes out of the
    install, so an install that produced none is a refusal in words rather
    than a build that silently reached for the internet."""
    result = _run(fake_repo, "--check-only", env={"FAKE_NPM_CI_EMPTY": "1"})
    assert result.returncode == 1
    assert "vsce is not in node_modules/.bin" in result.stderr


def test_check_only_writes_no_build_manifest(fake_repo):
    """`--check-only` stops before py2app, so there is no bundle to write the
    provenance note into — and it must not leave one lying anywhere."""
    result = _run(fake_repo, "--check-only")
    assert result.returncode == 0, result.stderr
    assert not list(fake_repo["tree"].rglob("build-manifest.json"))


def test_the_manifest_can_be_written_without_py2app(fake_repo, tmp_path):
    """The seam that keeps the provenance work off the manual list: the note
    is written by the same helper the build calls, against the same tree, with
    no bundle and no packager in sight."""
    out = tmp_path / "out" / "build-manifest.json"
    assert build_check.main([
        "manifest", "--out", str(out),
        "--repo-root", str(fake_repo["tree"]),
        "--ext-dir", str(fake_repo["tree"] / "vscode-extension"),
        "--vsix", str(fake_repo["tree"] / "vscode-extension" / "dark-army-ide-9.9.9.vsix"),
        "--mode", "strict", "--extension-source", "built",
    ]) == 0
    written = json.loads(out.read_text())
    assert written["schema"] == 1
    assert written["mode"] == "strict"
    assert written["extension"] == {
        "version": "9.9.9",
        "vsix": "dark-army-ide-9.9.9.vsix",
        "source": "built",
        "lockfile_sha256": build_check.lockfile_digest(
            fake_repo["tree"] / "vscode-extension" / "package-lock.json"),
    }
    assert set(written["tools"]) == {"node", "npm", "swift", "python"}


def test_the_lockfile_is_tracked_and_not_ignored():
    """The tripwire for the negated gitignore rule: if anybody ever ignores
    `vscode-extension/` wholesale the negation silently stops working and the
    pin leaves the repository again."""
    ignored = subprocess.run(["git", "check-ignore", "vscode-extension/package-lock.json"],
                             cwd=ROOT, capture_output=True, text=True)
    assert ignored.returncode != 0, "the extension lockfile is ignored again"
    tracked = subprocess.run(["git", "ls-files", "vscode-extension/package-lock.json"],
                             cwd=ROOT, capture_output=True, text=True)
    assert tracked.returncode == 0
    assert tracked.stdout.strip() == "vscode-extension/package-lock.json"
    # And the bare rule still applies everywhere else.
    elsewhere = subprocess.run(["git", "check-ignore", "relay/package-lock.json"],
                               cwd=ROOT, capture_output=True, text=True)
    assert elsewhere.returncode == 0


def test_the_lockfile_matches_the_manifest():
    """The loud half of the `npm ci` failure story: CI says *why* the install
    would fail, not only that it did."""
    verdict = build_check.lock_match(EXT_DIR)
    assert verdict.ok, verdict.reason


# --- the release artifact names no builder -----------------------------------


def _bundle(tmp_path, home="/Users/someone"):
    app = tmp_path / "Dark Army.app" / "Contents"
    (app / "Resources" / "lib").mkdir(parents=True)
    (app / "Info.plist").write_text(f"<string>{home}/dev/host/.venv/bin/python</string>")
    (app / "Resources" / "BobPanel").write_bytes(
        b"\x00" + f"{home}/dev/panel/a.swift\x00{home}/dev/panel/b.swift".encode())
    with zipfile.ZipFile(app / "Resources" / "lib" / "python312.zip", "w",
                         compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("site.pyc", f"{home}/dev/site.py" * 3)
        archive.writestr("clean.pyc", "/Users/runner/work/wheel.py")
    (app / "Resources" / "neighbour").write_text(f"{home}by/elsewhere")
    os.symlink("/etc/hosts", app / "Resources" / "link")
    return tmp_path / "Dark Army.app"


def test_home_path_hits_reads_plain_files_and_zip_members(tmp_path):
    hits = build_check.home_path_hits(_bundle(tmp_path), "/Users/someone/")
    assert hits == {
        "Contents/Info.plist": 1,
        "Contents/Resources/BobPanel": 2,
        "Contents/Resources/lib/python312.zip!site.pyc": 3,
    }


def test_home_path_verdict_refuses_names_the_worst_and_the_route_out(tmp_path):
    verdict = build_check.home_path_verdict(_bundle(tmp_path), "/Users/someone")
    assert not verdict.ok
    assert "3 file(s)" in verdict.reason
    assert "python312.zip!site.pyc (3)" in verdict.reason
    assert build_check.NEUTRAL_CHECKOUT in verdict.reason
    # Another person's home, or a longer name sharing the prefix, is not ours.
    assert build_check.home_path_verdict(_bundle(tmp_path / "b"), "/Users/nobody").ok
    assert build_check.home_path_verdict(_bundle(tmp_path / "c", "/Users/someoneelse"),
                                         "/Users/someone").ok


def test_main_home_exit_codes(tmp_path, capsys):
    app = _bundle(tmp_path)
    assert main(["home", "--app", str(app), "--home", "/Users/nobody"]) == 0
    assert main(["home", "--app", str(app), "--home", "/Users/someone/"]) == 2
    assert "carries your home folder /Users/someone" in capsys.readouterr().err
    # A build that died before the bundle existed is not a clean scan.
    assert main(["home", "--app", str(tmp_path / "missing.app"),
                 "--home", "/Users/nobody"]) == 2
    assert "no bundle to scan" in capsys.readouterr().err


def test_build_sh_scans_only_the_release_artifact_and_before_signing():
    text = BUILD_SH.read_text()
    gate = ('if [ "$DEV" = 0 ] && [ "$ALLOW_UNTAGGED" = 0 ] && [ "$INSTALL" = 0 ]; then\n'
            '    RELEASE_ARTIFACT=1')
    assert gate in text
    scan = text.index('"${BUILD_CHECK[@]}" home')
    assert text.index(gate) < scan < text.index('codesign --force --sign "$SIGN_ID" "$APP"')
    assert 'if [ "$RELEASE_ARTIFACT" = 1 ]; then' in text[scan - 200:scan]


def test_strict_refuses_a_checkout_inside_home(fake_repo):
    """The fake tree lives in tmp_path; HOME set to tmp_path puts it inside."""
    home = str(fake_repo["tree"].parent)
    result = _run(fake_repo, "--check-only", env={"HOME": home})
    assert result.returncode == 1
    assert "is inside your home folder" in result.stderr
    assert "/Users/Shared/dark-army-release" in result.stderr
    # This machine's own builds are never zipped, so neither is refused.
    for flag in ("--allow-untagged", "--install"):
        other = _run(fake_repo, "--check-only", flag, env={"HOME": home})
        assert "inside your home folder" not in other.stderr, flag
