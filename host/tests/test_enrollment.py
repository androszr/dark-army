"""The enrolment boundary: the key, the ledger, and what each refuses.

Every test points `paths.ENROLLMENT_PATH` at a tmp file, so nothing here reads
or writes the real ledger.
"""

import json
import os
import stat
from pathlib import Path

import pytest

from dark_army_daemon import enrollment, paths


@pytest.fixture(autouse=True)
def ledger(tmp_path, monkeypatch, enforce_enrolment):
    """`enforce_enrolment` puts the real gate back: conftest opens the door for
    every other test in the suite, and this file is the one that is *about* the
    door."""
    path = tmp_path / "enrollment.json"
    monkeypatch.setattr(paths, "ENROLLMENT_PATH", path)
    monkeypatch.setattr(paths, "STATE_DIR", tmp_path / "state")
    enrollment.invalidate()
    yield path
    enrollment.invalidate()


def _project(tmp_path, name="proj", git=True):
    root = tmp_path / name
    (root / "src").mkdir(parents=True)
    if git:
        (root / ".git").mkdir()
    return root


# ── the secret ────────────────────────────────────────────────────────────────

def test_mint_and_digest_round_trip():
    key = enrollment.mint()
    assert len(key) >= 32
    assert enrollment.digest(key) == enrollment.digest(key)
    assert enrollment.digest(key) != enrollment.digest(enrollment.mint())
    # A digest is hex and nothing but.
    assert len(enrollment.digest(key)) == 64
    int(enrollment.digest(key), 16)


# ── resolve, and its three ways of failing closed ─────────────────────────────

def test_resolve_refuses_an_empty_key(tmp_path):
    root = _project(tmp_path)
    assert enrollment.enroll(str(root))[0]
    assert enrollment.resolve("") == ""
    assert enrollment.resolve("   ") == ""
    assert enrollment.resolve(None) == ""


def test_resolve_against_an_empty_ledger_is_empty():
    assert enrollment.resolve("anything-at-all") == ""


def test_resolve_refuses_a_wrong_key(tmp_path):
    root = _project(tmp_path)
    assert enrollment.enroll(str(root))[0]
    assert enrollment.resolve(enrollment.mint()) == ""


def test_resolve_returns_the_root_for_the_right_key(tmp_path):
    root = _project(tmp_path)
    assert enrollment.enroll(str(root))[0]
    key = (root / enrollment.KEY_RELATIVE).read_text().strip()
    assert enrollment.resolve(key) == os.path.realpath(str(root))


# ── containment ───────────────────────────────────────────────────────────────

def test_root_enrolled_is_component_aware(tmp_path):
    """`/a/proj` must not admit `/a/project2`, which a bare prefix would."""
    root = _project(tmp_path, "proj")
    sibling = _project(tmp_path, "project2")
    assert enrollment.enroll(str(root))[0]
    assert enrollment.root_enrolled(str(root / "src")) == os.path.realpath(str(root))
    assert enrollment.root_enrolled(str(sibling)) == ""
    assert enrollment.root_enrolled(str(sibling / "src")) == ""


def test_root_enrolled_of_an_empty_cwd_is_empty(tmp_path):
    root = _project(tmp_path)
    assert enrollment.enroll(str(root))[0]
    assert enrollment.root_enrolled("") == ""


# ── enrol ─────────────────────────────────────────────────────────────────────

def test_enroll_writes_the_key_0600_in_a_0700_directory(tmp_path):
    root = _project(tmp_path)
    ok, _detail = enrollment.enroll(str(root))
    assert ok
    key_path = root / enrollment.KEY_RELATIVE
    assert key_path.is_file()
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(key_path.parent.stat().st_mode) == 0o700


def test_enroll_twice_adds_one_gitignore_line(tmp_path):
    root = _project(tmp_path)
    assert enrollment.enroll(str(root))[0]
    first = (root / enrollment.KEY_RELATIVE).read_text()
    assert enrollment.enroll(str(root))[0]
    body = (root / ".gitignore").read_text()
    assert body.count(enrollment.GITIGNORE_LINE) == 1
    # And it keeps the key: a fresh one would orphan every running session.
    assert (root / enrollment.KEY_RELATIVE).read_text() == first


def test_enroll_writes_no_gitignore_outside_a_repository(tmp_path):
    root = _project(tmp_path, git=False)
    ok, detail = enrollment.enroll(str(root))
    assert ok
    assert not (root / ".gitignore").exists()
    assert "git repository" in detail


def test_enroll_refuses_home_and_the_filesystem_root(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    ok, detail = enrollment.enroll(str(fake_home))
    assert not ok and "home folder" in detail
    ok, detail = enrollment.enroll("/")
    assert not ok
    assert enrollment.projects() == []


def test_enroll_refuses_something_that_is_not_a_folder(tmp_path):
    f = tmp_path / "a-file"
    f.write_text("x")
    ok, _ = enrollment.enroll(str(f))
    assert not ok


# ── un-enrol ──────────────────────────────────────────────────────────────────

def test_unenroll_refuses_the_key_even_with_the_file_left_behind(tmp_path,
                                                                 monkeypatch):
    root = _project(tmp_path)
    assert enrollment.enroll(str(root))[0]
    key = (root / enrollment.KEY_RELATIVE).read_text().strip()

    real_unlink = Path.unlink

    def refuse(self, *a, **kw):
        if self.name == enrollment.KEY_RELATIVE.name:
            raise OSError("nope")
        return real_unlink(self, *a, **kw)

    monkeypatch.setattr(Path, "unlink", refuse)
    ok, _ = enrollment.unenroll(str(root))
    assert ok
    # The file survives; the ledger is the authority, so the key is dead.
    assert (root / enrollment.KEY_RELATIVE).is_file()
    assert enrollment.resolve(key) == ""
    assert enrollment.root_enrolled(str(root)) == ""


def test_unenroll_of_an_unknown_root_is_refused(tmp_path):
    ok, detail = enrollment.unenroll(str(tmp_path / "nowhere"))
    assert not ok and "not enrolled" in detail


# ── the ledger's forward compatibility ────────────────────────────────────────

def test_ledger_read_merge_write_preserves_unknown_keys(tmp_path, ledger):
    root = _project(tmp_path)
    assert enrollment.enroll(str(root))[0]
    data = json.loads(ledger.read_text())
    data["a_newer_builds_key"] = {"anything": 1}
    data["projects"][0]["a_newer_builds_field"] = "keep me"
    ledger.write_text(json.dumps(data))
    enrollment.invalidate()

    other = _project(tmp_path, "other")
    assert enrollment.enroll(str(other))[0]
    after = json.loads(ledger.read_text())
    assert after["a_newer_builds_key"] == {"anything": 1}
    kept = [p for p in after["projects"] if p["root"].endswith("proj")][0]
    assert kept["a_newer_builds_field"] == "keep me"


def test_an_unreadable_ledger_enrols_nothing(ledger):
    ledger.write_text("{not json at all")
    enrollment.invalidate()
    assert enrollment.projects() == []
    assert enrollment.resolve("anything") == ""


# ── what may be published ─────────────────────────────────────────────────────

def test_the_snapshot_carries_no_key_and_no_digest(tmp_path):
    root = _project(tmp_path)
    assert enrollment.enroll(str(root))[0]
    key = (root / enrollment.KEY_RELATIVE).read_text().strip()
    blob = json.dumps(enrollment.snapshot())
    assert key not in blob
    assert enrollment.digest(key) not in blob
    assert "digest" not in blob
    assert enrollment.snapshot()["available"] is True
    assert enrollment.snapshot()["enrolled"][0]["root"] == os.path.realpath(str(root))


# ── the key folder's new name, and the read window ────────────────────────────

def _as_old_only(root):
    """Turn an enrolment by this build into one made before the move: the key
    under `.bob-companion/`, nothing under `.dark-army/`."""
    import shutil
    old = root / enrollment.LEGACY_KEY_RELATIVE
    old.parent.mkdir(mode=0o700)
    os.replace(root / enrollment.KEY_RELATIVE, old)
    shutil.rmtree(root / enrollment.KEY_DIR_NAME)
    return old.read_text()


def _plant(root, relative, value):
    path = root / relative
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(value)
    return path


def test_enroll_writes_the_key_under_dark_army_with_a_self_ignoring_file(tmp_path):
    root = _project(tmp_path)
    assert enrollment.enroll(str(root))[0]
    key_path = root / ".dark-army" / "key"
    assert key_path.is_file()
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(key_path.parent.stat().st_mode) == 0o700
    assert (root / ".dark-army" / ".gitignore").read_text() == "*\n"
    lines = (root / ".gitignore").read_text().splitlines()
    assert lines.count(".dark-army/") == 1
    assert ".bob-companion/" not in lines
    assert not (root / ".bob-companion").exists()


def test_the_self_ignoring_folder_stays_out_of_git_status(tmp_path):
    """The rule the launch-time move leans on: with no line in the project's
    own `.gitignore`, the folder's `*` alone keeps git from seeing it."""
    import shutil
    import subprocess
    git = shutil.which("git")
    if git is None:
        pytest.skip("git is not installed")
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run([git, "init", "-q", str(root)], check=True)
    assert enrollment.enroll(str(root))[0]
    (root / ".gitignore").unlink()
    out = subprocess.run([git, "-C", str(root), "status", "--porcelain",
                          "--untracked-files=all"],
                         check=True, capture_output=True, text=True).stdout
    assert out.strip() == ""


def test_enroll_of_a_root_holding_only_the_old_key_keeps_that_key(tmp_path):
    root = _project(tmp_path, git=False)
    _plant(root, enrollment.LEGACY_KEY_RELATIVE, "old-project-key\n")
    assert enrollment.enroll(str(root))[0]
    assert (root / enrollment.KEY_RELATIVE).read_text() == "old-project-key"
    assert enrollment.resolve("old-project-key") == os.path.realpath(str(root))


def test_enroll_prefers_the_new_key_when_both_exist(tmp_path):
    root = _project(tmp_path, git=False)
    _plant(root, enrollment.KEY_RELATIVE, "new-key")
    _plant(root, enrollment.LEGACY_KEY_RELATIVE, "old-key")
    assert enrollment.enroll(str(root))[0]
    assert enrollment.projects()[0]["digest"] == enrollment.digest("new-key")
    assert (root / enrollment.KEY_RELATIVE).read_text() == "new-key"


def test_enroll_refuses_a_key_folder_that_is_a_symlink(tmp_path):
    """A repository can ship `.dark-army` as a link to anywhere this user can
    write; enrolling must not chmod or write through it."""
    root = _project(tmp_path, git=False)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o755)
    (root / ".dark-army").symlink_to(elsewhere)
    ok, detail = enrollment.enroll(str(root))
    assert not ok and "key folder" in detail
    assert list(elsewhere.iterdir()) == []
    assert stat.S_IMODE(elsewhere.stat().st_mode) == 0o755
    assert enrollment.projects() == []


def test_unenroll_removes_the_key_under_both_names(tmp_path):
    root = _project(tmp_path)
    assert enrollment.enroll(str(root))[0]
    _plant(root, enrollment.LEGACY_KEY_RELATIVE, "old-key")
    assert enrollment.unenroll(str(root))[0]
    assert not (root / enrollment.KEY_RELATIVE).exists()
    assert not (root / enrollment.LEGACY_KEY_RELATIVE).exists()


def test_migrate_copies_each_enrolled_key_and_is_idempotent(tmp_path, ledger):
    one = _project(tmp_path, "one")
    two = _project(tmp_path, "two")
    assert enrollment.enroll(str(one))[0]
    assert enrollment.enroll(str(two))[0]
    keys = {one: _as_old_only(one), two: _as_old_only(two)}
    ignores = {r: (r / ".gitignore").read_text() for r in (one, two)}
    before = (ledger.stat().st_mtime_ns, ledger.stat().st_size)

    first = enrollment.migrate_key_folders()
    assert sorted(first) == ["one: copied the key to .dark-army/key",
                             "two: copied the key to .dark-army/key"]
    for root, key in keys.items():
        new = root / enrollment.KEY_RELATIVE
        assert new.read_text() == key
        assert stat.S_IMODE(new.stat().st_mode) == 0o600
        assert stat.S_IMODE(new.parent.stat().st_mode) == 0o700
        assert (new.parent / ".gitignore").read_text() == "*\n"
        # The old file stays for the window, and the project's own
        # `.gitignore` is never opened by the move.
        assert (root / enrollment.LEGACY_KEY_RELATIVE).read_text() == key
        assert (root / ".gitignore").read_text() == ignores[root]
        assert enrollment.resolve(key) == os.path.realpath(str(root))

    assert enrollment.migrate_key_folders() == []
    assert (ledger.stat().st_mtime_ns, ledger.stat().st_size) == before


def test_migrate_never_overwrites_a_differing_new_key(tmp_path):
    root = _project(tmp_path)
    assert enrollment.enroll(str(root))[0]
    new = (root / enrollment.KEY_RELATIVE).read_text()
    _plant(root, enrollment.LEGACY_KEY_RELATIVE, "some-other-machines-key")
    lines = enrollment.migrate_key_folders()
    assert len(lines) == 1 and "differs" in lines[0]
    assert (root / enrollment.KEY_RELATIVE).read_text() == new
    assert ((root / enrollment.LEGACY_KEY_RELATIVE).read_text()
            == "some-other-machines-key")


def test_migrate_skips_home_and_the_filesystem_root(tmp_path, ledger,
                                                    monkeypatch):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    _plant(fake_home, enrollment.LEGACY_KEY_RELATIVE, "home-key")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    ledger.write_text(json.dumps({"version": 1, "projects": [
        {"root": os.path.realpath(str(fake_home)),
         "digest": enrollment.digest("home-key")},
        {"root": os.sep, "digest": enrollment.digest("root-key")},
    ]}))
    enrollment.invalidate()
    assert enrollment.migrate_key_folders() == []
    assert not (fake_home / ".dark-army").exists()


def test_migrate_with_no_key_on_disk_writes_nothing(tmp_path):
    root = _project(tmp_path)
    assert enrollment.enroll(str(root))[0]
    import shutil
    shutil.rmtree(root / enrollment.KEY_DIR_NAME)
    lines = enrollment.migrate_key_folders()
    assert lines == ["proj: no key file on disk; left as is"]
    assert not (root / enrollment.KEY_DIR_NAME).exists()


def test_migrate_refuses_a_key_folder_that_is_a_symlink(tmp_path):
    root = _project(tmp_path)
    assert enrollment.enroll(str(root))[0]
    _as_old_only(root)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o755)
    (root / ".dark-army").symlink_to(elsewhere)
    lines = enrollment.migrate_key_folders()
    assert len(lines) == 1 and "could not copy" in lines[0]
    assert list(elsewhere.iterdir()) == []
    assert stat.S_IMODE(elsewhere.stat().st_mode) == 0o755


def test_migrate_never_copies_an_old_key_that_is_a_symlink(tmp_path):
    """A repository can ship `.bob-companion/key` — or the whole folder — as a
    link to a private file; the move must not publish that file's bytes."""
    secret = tmp_path / "private"
    secret.mkdir()
    (secret / "key").write_text("someone-elses-secret")

    by_file = _project(tmp_path, "by-file")
    by_folder = _project(tmp_path, "by-folder")
    for root in (by_file, by_folder):
        assert enrollment.enroll(str(root))[0]
        _as_old_only(root)
    (by_file / enrollment.LEGACY_KEY_RELATIVE).unlink()
    (by_file / enrollment.LEGACY_KEY_RELATIVE).symlink_to(secret / "key")
    import shutil
    shutil.rmtree(by_folder / enrollment.LEGACY_KEY_DIR_NAME)
    (by_folder / enrollment.LEGACY_KEY_DIR_NAME).symlink_to(secret)

    lines = enrollment.migrate_key_folders()
    assert sorted(lines) == ["by-file: the old key is a symlink; not copied",
                             "by-folder: the old key is a symlink; not copied"]
    for root in (by_file, by_folder):
        assert not (root / enrollment.KEY_RELATIVE).exists()


def test_enroll_never_adopts_an_old_key_through_a_symlink(tmp_path):
    secret = tmp_path / "private"
    secret.mkdir()
    (secret / "key").write_text("someone-elses-secret")
    root = _project(tmp_path, git=False)
    (root / enrollment.LEGACY_KEY_DIR_NAME).symlink_to(secret)
    assert enrollment.enroll(str(root))[0]
    assert (root / enrollment.KEY_RELATIVE).read_text() != "someone-elses-secret"
    assert enrollment.resolve("someone-elses-secret") == ""


def test_migrate_skips_a_key_folder_whose_ignore_file_ignores_nothing(tmp_path):
    root = _project(tmp_path)
    assert enrollment.enroll(str(root))[0]
    _as_old_only(root)
    folder = root / enrollment.KEY_DIR_NAME
    folder.mkdir()
    (folder / ".gitignore").write_text("# shipped by the repository\n")
    lines = enrollment.migrate_key_folders()
    assert len(lines) == 1 and "does not ignore" in lines[0]
    assert not (root / enrollment.KEY_RELATIVE).exists()
    assert (folder / ".gitignore").read_text() == "# shipped by the repository\n"


def test_unenroll_never_deletes_through_a_symlinked_key_folder(tmp_path):
    root = _project(tmp_path)
    assert enrollment.enroll(str(root))[0]
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "key").write_text("not-ours")
    (root / enrollment.LEGACY_KEY_DIR_NAME).symlink_to(elsewhere)
    assert enrollment.unenroll(str(root))[0]
    assert (elsewhere / "key").read_text() == "not-ours"
    assert not (root / enrollment.KEY_RELATIVE).exists()


def test_migrate_says_a_differing_new_key_is_what_the_project_sends(tmp_path):
    root = _project(tmp_path)
    assert enrollment.enroll(str(root))[0]
    _plant(root, enrollment.LEGACY_KEY_RELATIVE, "some-other-machines-key")
    (line,) = enrollment.migrate_key_folders()
    assert "now sends .dark-army/key" in line
    assert "may be refused" in line
