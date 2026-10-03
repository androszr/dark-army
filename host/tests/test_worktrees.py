"""Card worktrees — the pure half (`worktrees.py`).

The names, the argv and the sentences, then the argv driven against a real
repository in `tmp_path`: add → list → remove, the dirty refusal that is the
whole safety property, and a root whose `.git` is a file.
`docs/card-worktrees.md`.
"""

import os
import subprocess

import pytest

from dark_army_daemon import work_record, worktrees


# --- the names ----------------------------------------------------------------

def test_slug_joins_lowercase_runs_and_never_empties():
    assert worktrees.slug("Fix the Wrap: on narrow cards!") == \
        "fix-the-wrap-on-narrow-cards"
    assert worktrees.slug("") == "work"
    assert worktrees.slug("—  ✨ ") == "work"
    assert worktrees.slug(None) == "work"


def test_slug_is_clamped_without_a_trailing_dash():
    out = worktrees.slug("a" * 39 + " " + "b" * 30)
    assert len(out) <= worktrees.MAX_SLUG_CHARS
    assert not out.endswith("-")
    assert out == "a" * 39


def test_branch_and_folder_are_named_after_the_card():
    card = {"id": "3f2a9c1e-7d4b-4e21-9c0a-5b6d7e8f9a0b",
            "title": "Card isolation"}
    assert worktrees.branch_name(card) == "card/3f2a9c1e-card-isolation"
    assert worktrees.worktree_dir("/p", card["id"]) == \
        "/p/.worktrees/card-3f2a9c1e"
    assert worktrees.setup_log_path("/p", card["id"]) == \
        "/p/.worktrees/card-3f2a9c1e.setup.log"


def test_an_id_can_never_carry_a_separator_into_a_name():
    assert worktrees.short_id("../../etc") == "etc"
    assert worktrees.worktree_dir("/p", "a/b") == "/p/.worktrees/card-ab"
    assert worktrees.short_id("") == "card"


def test_inside_is_component_aware(tmp_path):
    root = tmp_path / "proj"
    (root / ".worktrees" / "card-1").mkdir(parents=True)
    (tmp_path / "proj2" / ".worktrees").mkdir(parents=True)
    assert worktrees.inside(str(root), str(root / ".worktrees" / "card-1"))
    assert not worktrees.inside(str(root), str(root / ".worktrees"))
    assert not worktrees.inside(str(root), str(root))
    assert not worktrees.inside(str(root), str(tmp_path / "proj2" / ".worktrees"))
    assert not worktrees.inside("", str(root))


# --- the argv -------------------------------------------------------------------

def _every_argv(root="/r"):
    return (
        worktrees.argv_remote_url(root),
        worktrees.argv_fetch(root),
        worktrees.argv_base_ref(root),
        worktrees.argv_branch_exists(root, "card/x"),
        worktrees.argv_worktree_add(root, "/r/.worktrees/card-x", "card/x", "main"),
        worktrees.argv_worktree_add(root, "/r/.worktrees/card-x", "card/x",
                                    existing=True),
        worktrees.argv_worktree_list(root),
        worktrees.argv_worktree_remove(root, "/r/.worktrees/card-x"),
        worktrees.argv_exclude_path(root),
        worktrees.argv_is_ancestor(root, "origin/main", "main"),
        worktrees.argv_setup_status(root, worktrees.SETUP_SCRIPT),
        worktrees.argv_index_dotfiles(root),
    )


def test_every_argv_starts_with_work_records_head():
    head = work_record._git("/r")
    for argv in _every_argv():
        assert argv[:len(head)] == head
        assert argv[0] == "git"
        assert "--no-optional-locks" in argv


def test_nothing_ever_forces_a_removal():
    for argv in _every_argv():
        assert "--force" not in argv
        assert "-f" not in argv


def test_the_add_has_two_forms():
    new = worktrees.argv_worktree_add("/r", "/r/.worktrees/card-x", "card/x", "main")
    # `--no-track`: a card branch made from `origin/main` never follows it.
    assert new[-7:] == ["worktree", "add", "--no-track", "-b", "card/x",
                        "/r/.worktrees/card-x", "main"]
    old = worktrees.argv_worktree_add("/r", "/r/.worktrees/card-x", "card/x",
                                      "main", existing=True)
    assert old[-4:] == ["worktree", "add", "/r/.worktrees/card-x", "card/x"]


def test_the_module_runs_nothing():
    text = open(worktrees.__file__, encoding="utf-8").read()
    assert "import subprocess" not in text
    assert "open(" not in text


# --- the parsers and the text -----------------------------------------------------

def test_parse_worktree_list_names_every_tree(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "a" / ".worktrees" / "card-1"
    b.mkdir(parents=True)
    text = (f"worktree {a}\nHEAD 0123\nbranch refs/heads/main\n\n"
            f"worktree {b}\nHEAD 4567\nbranch refs/heads/card/1-x\n")
    assert worktrees.parse_worktree_list(text) == {
        os.path.realpath(a), os.path.realpath(b)}
    assert worktrees.parse_worktree_list("") == set()


def test_exclude_text_appends_once():
    assert worktrees.exclude_text("") == "/.worktrees/\n"
    assert worktrees.exclude_text("# git ls-files\n*.log") == \
        "# git ls-files\n*.log\n/.worktrees/\n"
    assert worktrees.exclude_text("*.log\n/.worktrees/\n") is None


def test_base_from_takes_one_line_or_nothing():
    assert worktrees.base_from("origin/main\n") == "origin/main"
    assert worktrees.base_from("") == ""
    assert worktrees.base_from("a\nb") == ""


def test_setup_env_adds_four_names_and_cannot_prompt():
    env = worktrees.setup_env({"PATH": "/bin", "PYTHONHOME": "/bundle"},
                              "/r", "/r/.worktrees/card-1", "cid", "card/1-x")
    assert env["DARK_ARMY_ROOT"] == "/r"
    assert env["DARK_ARMY_WORKTREE"] == "/r/.worktrees/card-1"
    assert env["DARK_ARMY_CARD_ID"] == "cid"
    assert env["DARK_ARMY_BRANCH"] == "card/1-x"
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert "PYTHONHOME" not in env


def test_the_sentences_format():
    assert "exit 3" in worktrees.SETUP_FAILED_REFUSAL.format("exit 3", "/l")
    assert "/l" in worktrees.SETUP_FAILED_REFUSAL.format("exit 3", "/l")
    assert "/w" in worktrees.KEPT_NOTE.format("/w")


# --- against a real repository ------------------------------------------------------

def _run(argv, cwd=None):
    return subprocess.run(argv, cwd=cwd, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, env=work_record.git_env())


def _git(root, *args):
    done = subprocess.run(["git", *args], cwd=str(root), stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, env=work_record.git_env())
    assert done.returncode == 0, done.stderr
    return done.stdout.decode()


@pytest.fixture()
def repo(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / "a.txt").write_text("one\n")
    _git(root, "add", "a.txt")
    _git(root, "commit", "-q", "-m", "first")
    return os.path.realpath(root)


def test_add_list_remove_round_trip(repo):
    path = worktrees.worktree_dir(repo, "abcd1234")
    assert _run(worktrees.argv_worktree_add(repo, path, "card/abcd1234-x",
                                            "main")).returncode == 0
    listed = worktrees.parse_worktree_list(
        _run(worktrees.argv_worktree_list(repo)).stdout.decode())
    assert os.path.realpath(path) in listed
    assert _run(worktrees.argv_branch_exists(repo, "card/abcd1234-x")).returncode == 0
    assert _run(worktrees.argv_worktree_remove(repo, path)).returncode == 0
    assert not os.path.exists(path)
    # The branch stays: it is there to merge.
    assert _run(worktrees.argv_branch_exists(repo, "card/abcd1234-x")).returncode == 0


def test_the_existing_branch_form_checks_the_old_work_out(repo):
    path = worktrees.worktree_dir(repo, "abcd1234")
    _run(worktrees.argv_worktree_add(repo, path, "card/abcd1234-x", "main"))
    (open(os.path.join(path, "b.txt"), "w")).write("two\n")
    _git(path, "add", "b.txt")
    _git(path, "commit", "-q", "-m", "second")
    assert _run(worktrees.argv_worktree_remove(repo, path)).returncode == 0
    assert _run(worktrees.argv_worktree_add(repo, path, "card/abcd1234-x",
                                            existing=True)).returncode == 0
    assert os.path.exists(os.path.join(path, "b.txt"))


def test_a_dirty_tree_is_refused_and_kept(repo):
    path = worktrees.worktree_dir(repo, "abcd1234")
    _run(worktrees.argv_worktree_add(repo, path, "card/abcd1234-x", "main"))
    with open(os.path.join(path, "a.txt"), "a") as handle:
        handle.write("unsaved\n")
    assert _run(worktrees.argv_worktree_remove(repo, path)).returncode != 0
    assert os.path.exists(os.path.join(path, "a.txt"))


def test_an_untracked_file_is_refused_too(repo):
    path = worktrees.worktree_dir(repo, "abcd1234")
    _run(worktrees.argv_worktree_add(repo, path, "card/abcd1234-x", "main"))
    with open(os.path.join(path, "new.txt"), "w") as handle:
        handle.write("unsaved\n")
    assert _run(worktrees.argv_worktree_remove(repo, path)).returncode != 0
    assert os.path.exists(os.path.join(path, "new.txt"))


def test_a_root_whose_git_is_a_file_still_resolves_its_exclude(repo, tmp_path):
    linked = str(tmp_path / "linked")
    _git(repo, "worktree", "add", "-q", "-b", "side", linked, "main")
    assert os.path.isfile(os.path.join(linked, ".git"))
    out = _run(worktrees.argv_exclude_path(linked)).stdout.decode().strip()
    assert out
    where = out if os.path.isabs(out) else os.path.join(linked, out)
    assert where.endswith(os.path.join("info", "exclude"))
    assert os.path.isdir(os.path.dirname(os.path.dirname(where)))


def test_the_exclude_line_keeps_the_folder_out_of_status(repo):
    path = worktrees.worktree_dir(repo, "abcd1234")
    _run(worktrees.argv_worktree_add(repo, path, "card/abcd1234-x", "main"))
    out = _run(worktrees.argv_exclude_path(repo)).stdout.decode().strip()
    where = out if os.path.isabs(out) else os.path.join(repo, out)
    os.makedirs(os.path.dirname(where), exist_ok=True)
    existing = open(where).read() if os.path.exists(where) else ""
    with open(where, "w") as handle:
        handle.write(worktrees.exclude_text(existing))
    assert worktrees.exclude_text(open(where).read()) is None
    assert _git(repo, "status", "--porcelain") == ""


def test_the_ancestry_status_and_prune_argv():
    assert worktrees.argv_is_ancestor("/r", "origin/main", "main")[-4:] == [
        "merge-base", "--is-ancestor", "origin/main", "main"]
    assert worktrees.argv_setup_status("/r", ".dark-army/worktree-setup.sh")[-7:] == [
        "status", "--porcelain", "-z", "--ignored=matching",
        "--untracked-files=all", "--", ".dark-army/worktree-setup.sh"]
    assert worktrees.argv_worktree_prune("/r")[-2:] == ["worktree", "prune"]


def test_only_a_positive_untracked_or_ignored_answer_allows_the_script():
    rel = ".dark-army/worktree-setup.sh"
    assert worktrees.setup_status_allows(f"?? {rel}\0".encode())
    assert worktrees.setup_status_allows(f"!! {rel}\0".encode())
    # The whole folder ignored (an enrolled project): git collapses it to
    # the folder, and only when nothing under it is tracked.
    assert worktrees.setup_status_allows(b"!! .dark-army/\0")
    for refused in (b"", b"\0", f"?? {rel}".encode(),
                    f"?? {rel}\0?? .dark-army/other\0".encode(),
                    f" M {rel}\0".encode(), b"?? .dark-army/\0",
                    f"!! .DARK-ARMY/Worktree-Setup.sh\0".encode(),
                    b"\xff\xfe"):
        assert not worktrees.setup_status_allows(refused), refused


def test_the_setup_script_must_be_the_persons_own_and_nobody_elses_to_write():
    assert worktrees.setup_script_safe(501, 0o100644, 501)
    assert worktrees.setup_script_safe(501, 0o100700, 501)
    assert not worktrees.setup_script_safe(502, 0o100644, 501)
    assert not worktrees.setup_script_safe(501, 0o100664, 501)
    assert not worktrees.setup_script_safe(501, 0o100646, 501)


def test_the_new_sentences_format():
    assert "/s" in worktrees.SETUP_UNSAFE_REFUSAL.format("/s")
    assert "/w" in worktrees.WORKTREES_SYMLINK_REFUSAL.format("/w")
    assert worktrees.SETUP_UNTRUSTED_REFUSAL


KELVIN = "\u212a"


def test_the_index_argv_lists_only_dot_paths_unquoted():
    assert worktrees.argv_index_dotfiles("/r")[-4:] == [
        "ls-files", "-z", "--", ".*"]
    assert "core.quotePath=false" in worktrees.argv_index_dotfiles("/r")


def test_candidates_are_every_spelling_that_folds_to_the_setup_folder():
    entries = [
        f".dar{KELVIN}-army/worktree-setup.sh",      # KELVIN SIGN
        ".DARK-ARMY/Worktree-Setup.sh",               # ASCII case variant
        ".\uff24ark-army/x.sh",                       # full-width D
        ".dark-army/worktree-setup.sh",
        ".dark-army-not/worktree-setup.sh",
        ".gitignore",
        ".github/workflows/ci.yml",
    ]
    out = ("\0".join(entries) + "\0").encode("utf-8")
    got = worktrees.index_script_candidates(out)
    assert got == entries[:4]
    assert worktrees.index_script_candidates(b"") == []
    assert worktrees.index_script_candidates(b"\xff\xfe/x\0") == []


def test_tracked_as_script_compares_inodes_never_names():
    assert worktrees.tracked_as_script((1, 42), [(1, 7), (1, 42)])
    assert not worktrees.tracked_as_script((1, 42), [(1, 7), (2, 42)])
    assert not worktrees.tracked_as_script((1, 42), [])
    assert not worktrees.tracked_as_script(None, [(1, 42)])


def test_only_an_apfs_disk_may_hold_a_setup_script_that_runs():
    """Mac OS Extended stores a name with an invisible character as the name
    without it, so the index check cannot see a tracked script there; an
    unreadable disk kind (`""`) fails closed."""
    assert worktrees.setup_volume_allows("apfs")
    assert worktrees.setup_volume_allows("APFS")
    for other in ("hfs", "msdos", "exfat", "smbfs", "nfs", "", None):
        assert not worktrees.setup_volume_allows(other)


def test_the_hidden_file_listing_has_its_own_larger_cap():
    assert worktrees.MAX_INDEX_DOTFILES_BYTES > work_record.MAX_GIT_OUTPUT_BYTES
    assert "too many hidden files" in worktrees.SETUP_TOO_MANY_HIDDEN_REFUSAL
    assert "APFS" in worktrees.SETUP_VOLUME_REFUSAL


# --- stale registrations: the parser, the decision, the real thing -------------------

PORCELAIN = (
    "worktree /r\nHEAD 0123\nbranch refs/heads/main\n\n"
    "worktree /gone\nHEAD 4567\ndetached\n"
    "prunable gitdir file points to non-existent location\n\n"
    "worktree /held\nHEAD 89ab\ndetached\nlocked\n"
    "prunable gitdir file points to non-existent location\n")


def test_parse_worktree_entries_reads_the_flags():
    entries = worktrees.parse_worktree_entries(PORCELAIN)
    assert [e["path"] for e in entries] == [
        os.path.realpath(p) for p in ("/r", "/gone", "/held")]
    assert [e["prunable"] for e in entries] == [False, True, True]
    assert [e["locked"] for e in entries] == [False, False, True]
    assert entries[1]["reason"] == "gitdir file points to non-existent location"
    assert entries[0]["reason"] == ""
    assert worktrees.parse_worktree_list(PORCELAIN) == {
        e["path"] for e in entries}
    assert worktrees.parse_worktree_entries("") == []


def test_prune_decision_splits_missing_from_present():
    entries = worktrees.parse_worktree_entries(PORCELAIN)
    gone = os.path.realpath("/gone")
    # Nothing exists: the missing prunable entry is forgettable; a locked
    # one and a plain one are in neither list.
    assert worktrees.prune_decision(entries, lambda p: False) == ([gone], [])
    # The prunable entry's folder is still there: blocked, not missing.
    assert worktrees.prune_decision(entries, lambda p: True) == ([], [gone])
    assert worktrees.prune_decision([], lambda p: True) == ([], [])


def test_the_blocked_line_names_the_root_and_the_folder():
    line = worktrees.PRUNE_BLOCKED_LINE.format("/root", "/folder")
    assert "/root" in line and "/folder" in line


def _detached(repo, tmp_path, name):
    path = str(tmp_path / name)
    assert _run(work_record._git(repo) + [
        "worktree", "add", "--detach", path, "HEAD"]).returncode == 0
    return os.path.realpath(path)


def test_prune_forgets_the_dead_and_keeps_the_living(repo, tmp_path):
    import shutil
    dead = _detached(repo, tmp_path, "dead")
    live = _detached(repo, tmp_path, "live")
    shutil.rmtree(dead)
    entries = worktrees.parse_worktree_entries(
        _run(worktrees.argv_worktree_list(repo)).stdout.decode())
    by_path = {e["path"]: e for e in entries}
    assert by_path[dead]["prunable"] and not by_path[live]["prunable"]
    assert worktrees.prune_decision(entries, os.path.isdir) == ([dead], [])
    assert _run(worktrees.argv_worktree_prune(repo)).returncode == 0
    listed = worktrees.parse_worktree_list(
        _run(worktrees.argv_worktree_list(repo)).stdout.decode())
    assert dead not in listed and live in listed
    assert os.path.isdir(live)


def test_a_present_folder_without_its_git_file_is_blocked(repo, tmp_path):
    hollow = _detached(repo, tmp_path, "hollow")
    os.remove(os.path.join(hollow, ".git"))
    entries = worktrees.parse_worktree_entries(
        _run(worktrees.argv_worktree_list(repo)).stdout.decode())
    assert worktrees.prune_decision(entries, os.path.isdir) == ([], [hollow])
